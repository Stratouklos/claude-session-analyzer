"""Report which pipeline outputs are stale relative to their inputs.

Stages are run by hand, so an output can sit quietly older than the data it was
built from - a report that no longer reflects the findings looks exactly like one
that does. This makes that visible instead of relying on remembering.

Exits 1 if anything is stale, so it can gate a workflow rather than just inform.

Usage: python3 tools/status.py [--quiet]
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import FINDINGS  # noqa: E402

# output -> (inputs, the command that rebuilds it)
STAGES = [
    ("candidates_transcripts.jsonl", ["_meta/corpus.stamp"], "extract_transcripts.py"),
    ("candidates_chat.jsonl", ["_meta/corpus.stamp", "_meta/export.stamp"], "extract_chat.py <export-dir>"),
    ("interrupts.jsonl", ["_meta/corpus.stamp"], "extract_interrupts.py"),
    ("artifacts_authored.jsonl", ["_meta/corpus.stamp"], "extract_artifacts.py"),
    ("code_comments.jsonl", ["_meta/corpus.stamp"], "extract_code_comments.py"),
    ("git_landed.jsonl", ["artifacts_authored.jsonl"], "collect_git.py"),
    ("style_comparison.json", ["git_landed.jsonl"], "compare_style.py"),
    ("rewrites.jsonl", ["artifacts_authored.jsonl", "git_landed.jsonl", "prs.jsonl"], "match_rewrites.py"),
    ("comment_survival.json", ["code_comments.jsonl"], "detect_comment_rewrites.py"),
    ("comment_outcomes.jsonl", ["code_comments.jsonl"], "detect_comment_rewrites.py"),
    ("comment_summary.json", ["code_comments.jsonl"], "summarize_comments.py"),
    ("triaged.jsonl", ["candidates_transcripts.jsonl", "candidates_chat.jsonl"], "merge_triage.py"),
    ("incidents.jsonl", ["triaged.jsonl"], "dedupe_incidents.py"),
    ("preference_rules.jsonl", ["triaged.jsonl"], "dedupe_incidents.py"),
    ("problems.jsonl", ["incidents.jsonl"], "merge_problems.py"),
    ("findings.jsonl", ["incidents.jsonl", "problems.jsonl"], "rank_findings.py"),
    ("behavior_in/all.json", ["problems.jsonl", "incidents.jsonl"], "make_behavior_batch.py"),
    ("behaviors.jsonl", ["behavior_in/all.json"], "merge_behaviors.py"),
    ("report.html", ["findings.jsonl", "problems.jsonl", "behaviors.jsonl",
                     "style_comparison.json", "comment_survival.json",
                     "interrupts.jsonl",
                     "preference_rules.jsonl"], "build_report.py"),
]

# Directories of model verdicts. Newer verdicts than the merge that reads them is
# the same staleness, and the one most likely to be missed.
DIR_INPUTS = {
    "triaged.jsonl": ["triage_out"],
    "incidents.jsonl": ["judge_out"],
    "problems.jsonl": ["problem_out"],
    "behaviors.jsonl": ["behavior_out"],
}


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def newest_in(directory):
    base = os.path.join(FINDINGS, directory)
    if not os.path.isdir(base):
        return None
    times = [
        mtime(os.path.join(base, n)) for n in os.listdir(base) if n.endswith(".json")
    ]
    times = [t for t in times if t]
    return max(times) if times else None


def main():
    quiet = "--quiet" in sys.argv
    stale, missing = [], []

    for output, inputs, command in STAGES:
        out_t = mtime(os.path.join(FINDINGS, output))
        if out_t is None:
            missing.append((output, command))
            continue

        newer = []
        for name in inputs:
            t = mtime(os.path.join(FINDINGS, name))
            if t and t > out_t:
                newer.append(name)
        for directory in DIR_INPUTS.get(output, []):
            t = newest_in(directory)
            if t and t > out_t:
                newer.append(directory + "/")
        if newer:
            stale.append((output, newer, command))

    if not quiet:
        print(f"findings: {FINDINGS}\n")
        for output, newer, command in stale:
            age = (time.time() - mtime(os.path.join(FINDINGS, output))) / 60
            print(f"STALE  {output}  ({age:.0f} min old)")
            print(f"       older than: {', '.join(newer)}")
            print(f"       rebuild:    python3 tools/{command}\n")
        for output, command in missing:
            print(f"ABSENT {output}\n       build: python3 tools/{command}\n")
        if not stale and not missing:
            print("Everything is current.")

    print(json.dumps({"stale": len(stale), "absent": len(missing)}))
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
