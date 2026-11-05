"""Compare candidate judge models against the hand-labeled gold set.

For each model, runs the triage rubric over the gold windows, then the verify
rubric over the rows the model kept (truthy is_correction with confidence >= the production cutoff), by
running judge_batches.py, so the secret gate and model backends are unchanged.
Reports triage agreement, draft precision, verify agreement and wall time.
Every number is PROVISIONAL until all gold rows are marked labeled. It names no
winner and writes no config: the keep/stop call is yours.

A model that exits non-zero becomes a failed row; the rest still run.
Scratch work lives in a temp dir and is removed; nothing is written under findings/.

Usage: MODELS="ollama:qwen3:14b claude:sonnet" python3 tools/bakeoff.py [model ...] [--json]
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import FINDINGS, JUDGE_BATCH, TRIAGE_BATCH, is_user_turn, kept  # noqa: E402
from make_triage_batches import compact  # noqa: E402
import redact  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(REPO, "tools")


def load_gold():
    path = os.path.join(FINDINGS, "gold", "gold.jsonl")
    if not os.path.exists(path):
        raise SystemExit(f"ERROR: {path} not found; run `make gold` first")
    return [json.loads(line) for line in open(path) if line.strip()]


def triage_item(row):
    """The production triage input: make_triage_batches.compact over the gold row's candidate fields."""
    w = row.get("window") or []
    users = [t for t in w if is_user_turn(t)]
    cand = {"source": "", "flags": [], "prompt": (users[-1].get("text") or "") if users else "",
            "preceding_output_chars": 0, "preceding_tool_calls": 0, "thread_ended_here": False}
    cand.update({k: v for k, v in row.items() if k in cand})
    return compact({**cand, "id": row["id"], "window": w})


def write_batches(dirpath, items, size):
    os.makedirs(dirpath, exist_ok=True)
    for i in range(0, len(items), size):
        redact.write_private(os.path.join(dirpath, f"batch_{i // size:03d}.json"),
                             json.dumps(items[i:i + size], ensure_ascii=False))


def run_pass(model, work, name, items, size, rubric):
    in_dir, out_dir = os.path.join(work, name + "_in"), os.path.join(work, name + "_out")
    write_batches(in_dir, items, size)
    proc = subprocess.run(
        [sys.executable, os.path.join(REPO, "judge_batches.py"), model, in_dir, out_dir,
         os.path.join(TOOLS, rubric)],
        cwd=REPO, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"{name} pass exited {proc.returncode}: {tail[0][:200]}")
    verdicts = {}
    for n in sorted(os.listdir(out_dir)):
        if n.endswith(".json"):
            for v in json.load(open(os.path.join(out_dir, n))):
                if isinstance(v, dict) and v.get("id"):
                    verdicts[v["id"]] = v
    return verdicts


def verdict_kept(v):
    """Production's `kept`, but a malformed model verdict (bad confidence) counts as not kept."""
    try:
        return kept(v)
    except (TypeError, ValueError, AttributeError):
        return False


def ratio(num, den):
    return {"value": round(num / den, 3) if den else None, "n": den}


def score(gold, triage, verify):
    """A row with no model verdict counts against the model; a row with no gold label is left out."""
    lab = [g for g in gold if isinstance(g["label"].get("is_correction"), bool)]
    agree = sum(1 for g in lab if triage.get(g["id"], {}).get("is_correction") is g["label"]["is_correction"])
    drafted = [g for g in lab if verdict_kept(triage.get(g["id"], {}))]
    precise = sum(1 for g in drafted if g["label"]["is_correction"])
    vlab = [g for g in gold if isinstance(g["label"].get("confirmed"), bool) and g["id"] in verify]
    vagree = sum(1 for g in vlab if verify[g["id"]].get("confirmed") is g["label"]["confirmed"])
    return {
        "triage_agreement": ratio(agree, len(lab)),
        "draft_precision": ratio(precise, len(drafted)),
        "verify_agreement": ratio(vagree, len(vlab)),
    }


def run_model(model, gold):
    work = tempfile.mkdtemp(prefix="csa-bakeoff-")
    start = time.monotonic()
    row = {"model": model, "status": "ok", "error": None}
    try:
        triage = run_pass(model, work, "triage", [triage_item(g) for g in gold], TRIAGE_BATCH,
                          "triage_rubric.md")
        keep = [{**{k: v for k, v in g.items() if k not in ("label", "labeled", "triage_prefill")},
                 "triage": triage[g["id"]]}
                for g in gold if verdict_kept(triage.get(g["id"], {}))]
        verify = run_pass(model, work, "verify", keep, JUDGE_BATCH, "judge_rubric.md") if keep else {}
        row.update(score(gold, triage, verify))
    except Exception as e:  # noqa: BLE001
        row.update(status="failed", error=str(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    row["wall_seconds"] = round(time.monotonic() - start, 2)
    return row


def fmt(m):
    return "-" if not m or m["value"] is None else f"{m['value']:.3f} (n={m['n']})"


def render(report):
    out = [report["note"], ""]
    out.append(f"{'model':<28}{'triage agree':<16}{'draft prec':<16}{'verify agree':<16}{'wall s':<8}")
    for r in report["models"]:
        if r["status"] == "failed":
            out.append(f"{r['model']:<28}FAILED  {r['error']}  ({r['wall_seconds']}s)")
        else:
            out.append(f"{r['model']:<28}{fmt(r['triage_agreement']):<16}{fmt(r['draft_precision']):<16}"
                       f"{fmt(r['verify_agreement']):<16}{r['wall_seconds']:<8}")
    return "\n".join(out)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    models = args or os.environ.get("MODELS", "").split()
    if not models:
        raise SystemExit("ERROR: name models as arguments or in MODELS, e.g. MODELS='ollama:qwen3:14b claude:sonnet'")
    gold = load_gold()
    if not gold:
        raise SystemExit("ERROR: gold set is empty")
    redact.assert_clean([g["window"] for g in gold], "gold set windows")
    confirmed = sum(1 for g in gold if g.get("labeled"))
    prov = confirmed < len(gold)
    note = (f"{'PROVISIONAL: ' if prov else ''}{confirmed} of {len(gold)} gold rows user-confirmed"
            f"{'; labels are still triage/judge prefill for the rest' if prov else ''}. No winner is named.")
    report = {"provisional": prov, "gold_rows": len(gold), "user_confirmed": confirmed, "note": note,
              "models": [run_model(m, gold) for m in models]}
    print(json.dumps(report, indent=1) if "--json" in sys.argv else render(report))


if __name__ == "__main__":
    main()
