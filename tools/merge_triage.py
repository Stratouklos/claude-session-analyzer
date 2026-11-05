"""Merge triage output, attach deterministic severity, emit judging batches.

Severity is computed here, not by a model, and deliberately uses only signals
whose meaning is stable across the whole corpus:

  turns_before_next_prompt   how long the user sat through before speaking again
  thread_ended_here          they stopped talking - possible abandonment
  preceding_tool_calls       how much work rested on the mistake
  repeat_signal              they say they had already told us

Profanity is excluded on purpose: its base rate quadrupled mid-corpus, so
weighting on it would bury every finding before 2026-07-20 and most of chat.

Usage: python3 tools/merge_triage.py [--min-confidence 0.3]
Writes: findings/triaged.jsonl, findings/judge_in/batch_NNN.json
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import FINDINGS, JUDGE_BATCH, MIN_CONF, ensure_findings_dir, kept, write_jsonl  # noqa: E402


def load_candidates():
    by_id = {}
    for name in ("candidates_transcripts.jsonl", "candidates_chat.jsonl"):
        path = os.path.join(FINDINGS, name)
        if not os.path.exists(path):
            continue
        for line in open(path):
            row = json.loads(line)
            by_id[row["id"]] = row
    return by_id


def load_triage():
    out_dir = os.path.join(FINDINGS, "triage_out")
    rows = []
    missing = []
    if not os.path.isdir(out_dir):
        return rows, missing
    for name in sorted(os.listdir(out_dir)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(out_dir, name)
        try:
            data = json.load(open(path))
        except ValueError:
            missing.append(name)
            continue
        if isinstance(data, dict):
            data = data.get("results") or data.get("items") or []
        rows += [r for r in data if isinstance(r, dict) and r.get("id")]
    return rows, missing


def severity(cand, verdict):
    """0-10. Higher means the mistake cost the user more."""
    score = 2.0
    turns = cand.get("turns_before_next_prompt")
    if turns is not None:
        # Sitting through a lot of output before objecting means the work was
        # already built on top of the mistake.
        score += min(2.5, turns * 0.4)
    calls = cand.get("preceding_tool_calls") or 0
    score += min(2.0, calls * 0.12)
    if cand.get("thread_ended_here"):
        score += 1.5  # they stopped engaging
    if verdict.get("repeat_signal"):
        score += 2.5  # told us already; the rule did not hold
    return round(min(10.0, score), 2)


def main():
    ensure_findings_dir()
    min_conf = MIN_CONF
    if "--min-confidence" in sys.argv:
        min_conf = float(sys.argv[sys.argv.index("--min-confidence") + 1])

    cands = load_candidates()
    verdicts, bad = load_triage()
    if bad:
        print(f"unparseable triage files: {bad}")

    seen_ids = set()
    merged = []
    orphans = 0
    ghost_ids = []
    for verdict in verdicts:
        vid = verdict["id"]
        if vid in seen_ids:
            continue
        seen_ids.add(vid)
        cand = cands.get(vid)
        if not cand:
            # A verdict for an id that is not a candidate. A model can emit an
            # id that was never in its input, so this join is load-bearing
            # rather than a formality. Never silent.
            orphans += 1
            ghost_ids.append(vid)
            continue
        merged.append(
            {
                **{k: v for k, v in cand.items() if k != "window"},
                "triage": verdict,
                "severity": severity(cand, verdict),
                "window": cand["window"],
            }
        )

    # The triage model silently drops items from long batches even while
    # reporting a full count, so trust the join, not the agent's self-report.
    missing = [cid for cid in cands if cid not in seen_ids]
    if missing:
        compact_path = os.path.join(FINDINGS, "triage_in")
        lookup = {}
        for name in sorted(os.listdir(compact_path)):
            if not name.endswith(".json"):
                continue
            for item in json.load(open(os.path.join(compact_path, name))):
                lookup[item["id"]] = item
        with open(os.path.join(FINDINGS, "triage_missing.json"), "w") as fh:
            json.dump([lookup[m] for m in missing if m in lookup], fh, indent=1)

    if ghost_ids:
        print(f"WARNING: {len(ghost_ids)} fabricated ids dropped: {ghost_ids[:6]}")

    coverage = len(seen_ids) / max(1, len(cands))
    keep = [m for m in merged if kept(m["triage"], min_conf)]
    keep.sort(key=lambda m: -m["severity"])

    write_jsonl(os.path.join(FINDINGS, "triaged.jsonl"), merged)

    judge_dir = os.path.join(FINDINGS, "judge_in")
    os.makedirs(judge_dir, exist_ok=True)
    for stale in os.listdir(judge_dir):
        os.remove(os.path.join(judge_dir, stale))
    batches = [keep[i : i + JUDGE_BATCH] for i in range(0, len(keep), JUDGE_BATCH)]
    for i, batch in enumerate(batches):
        with open(os.path.join(judge_dir, f"batch_{i:03d}.json"), "w") as fh:
            json.dump(batch, fh, ensure_ascii=False, indent=1)

    print(
        json.dumps(
            {
                "candidates": len(cands),
                "triaged": len(seen_ids),
                "coverage": round(coverage, 3),
                "orphan_ids": orphans,
                "corrections": len(keep),
                "judge_batches": len(batches),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
