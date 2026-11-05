"""Sample a gold set of candidate windows for hand labeling.

Samples N rows from findings/triaged.jsonl with a fixed seed, stratified so
corrections, non-corrections, the low-confidence band and unlabeled rows all
appear. Each record keeps the triage verdict as `triage_prefill` and carries an
editable `label` prefilled from it (and from the judge verdict when present).
Set `labeled` to true on a row once you have checked its label.

Refuses to overwrite a gold file that differs from a fresh prefill (it holds
your edits) unless --force.

Usage: python3 tools/make_gold_set.py [N] [--seed S] [--force]
Writes: findings/gold/gold.jsonl (mode 0600)
"""

import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import FINDINGS  # noqa: E402
import redact  # noqa: E402

DEFAULT_N = 40
DEFAULT_SEED = 20260101
LOW_BAND = 0.5


def gold_path():
    return os.path.join(FINDINGS, "gold", "gold.jsonl")


def usable(triage):
    return isinstance(triage, dict) and isinstance(triage.get("is_correction"), bool)


def bucket(row):
    """Strata: no usable triage, non-correction, low-confidence correction, other correction."""
    t = row.get("triage")
    if not usable(t):
        return "unlabeled"
    if not t["is_correction"]:
        return "non_correction"
    try:
        conf = float(t.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return "low_correction" if conf < LOW_BAND else "correction"


ORDER = ("non_correction", "low_correction", "correction", "unlabeled")


def sample(rows, n, seed):
    rows = sorted(rows, key=lambda r: r["id"])
    if len(rows) <= n:
        return rows
    rng = random.Random(seed)
    pools = {b: [] for b in ORDER}
    for r in rows:
        pools[bucket(r)].append(r)
    for pool in pools.values():
        rng.shuffle(pool)
    picked = []
    while len(picked) < n:
        took = False
        for b in ORDER:
            if pools[b] and len(picked) < n:
                picked.append(pools[b].pop())
                took = True
        if not took:
            break
    return sorted(picked, key=lambda r: r["id"])


def load_judge():
    out = {}
    d = os.path.join(FINDINGS, "judge_out")
    if not os.path.isdir(d):
        return out
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        try:
            data = json.load(open(os.path.join(d, name)))
        except ValueError:
            continue
        if isinstance(data, list):
            for v in data:
                if isinstance(v, dict) and v.get("id") and isinstance(v.get("confirmed"), bool):
                    out[v["id"]] = v
    return out


def record(row, judge):
    t = row.get("triage")
    verdict = t if usable(t) else None
    j = judge.get(row["id"])
    return {  # keeps the merged row's candidate fields so the bakeoff rebuilds production inputs
        "id": row["id"],
        **{k: v for k, v in row.items() if k not in ("id", "triage", "label", "labeled", "triage_prefill")},
        "window": row.get("window"),
        "triage_prefill": verdict,
        "label": {
            "is_correction": verdict["is_correction"] if verdict else None,
            "confirmed": j["confirmed"] if j else None,
        },
        "labeled": False,
    }


def render(records):
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("n", nargs="?", type=int, default=DEFAULT_N)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--force", action="store_true")
    args, _ = ap.parse_known_args()

    src = os.path.join(FINDINGS, "triaged.jsonl")
    if not os.path.exists(src):
        raise SystemExit(f"ERROR: {src} not found; run the judge pass first")
    rows = [json.loads(line) for line in open(src) if line.strip()]
    judge = load_judge()
    records = [record(r, judge) for r in sample(rows, args.n, args.seed)]
    redact.assert_clean(records, "gold set records")
    text = render(records)

    path = gold_path()
    if os.path.exists(path) and not args.force:
        old = open(path).read()
        if old != text:
            labeled = 0
            for line in old.splitlines():
                try:
                    labeled += bool(json.loads(line).get("labeled"))
                except ValueError:
                    pass
            raise SystemExit(
                f"ERROR: {path} differs from a fresh prefill ({labeled} rows marked labeled); "
                "it holds your edits. Not overwritten. Re-run with --force to replace it."
            )
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    redact.write_private(path, text)
    print(f"wrote {len(records)} gold rows -> {path} (0 of {len(records)} labeled; edit label, set labeled=true)")


if __name__ == "__main__":
    main()
