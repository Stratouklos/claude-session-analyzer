"""Find comments Claude wrote that did not survive.

The plain verdict rate over authored comments understates the problem in two
ways, both of which the user pointed out:

  1. When a verbose comment gets reworded, the transcript holds BOTH the bloated
     original and the fix. Every correction counts as a fresh "good" comment and
     dilutes the rate.
  2. When the user rewords a comment in their editor instead of asking Claude to, the
     edit never touches a tool call and a transcript-only sweep cannot see it.

So measure survival instead of opinion:

  in-session shrink   a later authored block covers the same ground as an earlier
                      one in the same file, materially shorter. Claude rewrote it.
  did-not-survive     the authored text is absent from the file as it stands now.
                      Someone changed or deleted it, and mostly that is the user.

Usage: python3 tools/detect_comment_rewrites.py
Writes: findings/comment_survival.json, findings/comment_outcomes.jsonl
"""

import collections
import difflib
import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import FINDINGS, REDACTED_MARK, ensure_findings_dir, write_jsonl  # noqa: E402

SAME_GROUND = 0.45  # normalized similarity that means "about the same thing"
MATERIALLY_SHORTER = 0.75  # later block is this fraction of the earlier or less
SURVIVED = 0.90  # similarity to the best match in the current file


def norm(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def ratio(a, b):
    return difflib.SequenceMatcher(None, a, b).ratio()


def comment_blocks_in_file(path, token_hint):
    """Every run of consecutive comment lines currently in the file."""
    try:
        with open(path, errors="ignore") as fh:
            lines = fh.read().split("\n")
    except OSError:
        return None
    blocks, cur = [], []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith("//"):
            token = "#" if stripped.startswith("#") else "//"
            cur.append(stripped[len(token) :].strip())
        else:
            if cur:
                blocks.append(norm("\n".join(cur)))
                cur = []
    if cur:
        blocks.append(norm("\n".join(cur)))
    return blocks


def main():
    ensure_findings_dir()
    rows = [
        json.loads(line)
        for line in open(os.path.join(FINDINGS, "code_comments.jsonl"))
    ]
    verdicts = {}
    out_dir = os.path.join(FINDINGS, "comment_out")
    if os.path.isdir(out_dir):
        for name in sorted(os.listdir(out_dir)):
            if name.endswith(".json"):
                for v in json.load(open(os.path.join(out_dir, name))):
                    if isinstance(v, dict) and v.get("id"):
                        verdicts[v["id"]] = v.get("verdict")

    # Per-comment outcome, alongside the aggregates. The aggregates say what the
    # corpus looks like; these say what happened to each individual comment,
    # which is what a backtest needs to label a rule's fire as right or wrong.
    outcome_of = {
        r["id"]: {
            "id": r["id"],
            "session": r.get("session"),
            "ts": r.get("ts"),
            "file": os.path.basename(r.get("file") or ""),
            "project": r.get("project"),
            "words": r.get("words"),
            "chars": len(r.get("comment") or ""),
            "verdict": verdicts.get(r["id"]),
            "shrunk": False,
            "shrink": None,
            "checked": False,
            "survived": None,
        }
        for r in rows
    }

    # ---- 1. in-session shrink rewrites ----------------------------------
    by_file = collections.defaultdict(list)
    for r in rows:
        by_file[r["file"]].append(r)

    shrinks = []
    superseded_ids = set()
    for path, items in by_file.items():
        items.sort(key=lambda r: r.get("ts") or "")
        for i, earlier in enumerate(items):
            e_norm = norm(earlier["comment"])
            for later in items[i + 1 :]:
                l_norm = norm(later["comment"])
                if not e_norm or not l_norm:
                    continue
                if len(l_norm) > len(e_norm) * MATERIALLY_SHORTER:
                    continue
                score = ratio(e_norm, l_norm)
                if score < SAME_GROUND:
                    continue
                superseded_ids.add(earlier["id"])
                outcome_of[earlier["id"]]["shrunk"] = True
                outcome_of[earlier["id"]]["shrink"] = round(1 - len(l_norm) / len(e_norm), 3)
                shrinks.append(
                    {
                        "file": os.path.basename(path),
                        "similarity": round(score, 3),
                        "before_words": earlier["words"],
                        "after_words": later["words"],
                        "shrink": round(1 - len(l_norm) / len(e_norm), 3),
                        "before": earlier["comment"][:400],
                        "after": later["comment"][:400],
                        "verdict_before": verdicts.get(earlier["id"]),
                        "verdict_after": verdicts.get(later["id"]),
                    }
                )
                break

    # ---- 2. survival in the working tree --------------------------------
    checked = 0
    gone = []
    missing_files = 0
    file_cache = {}
    for r in rows:
        if REDACTED_MARK in r["comment"]:
            continue
        path = r["file"]
        if path not in file_cache:
            file_cache[path] = comment_blocks_in_file(path, None)
        blocks = file_cache[path]
        if blocks is None:
            missing_files += 1
            continue
        checked += 1
        target = norm(r["comment"])
        best = max((ratio(target, b) for b in blocks), default=0.0)
        outcome_of[r["id"]]["checked"] = True
        outcome_of[r["id"]]["survived"] = best >= SURVIVED
        outcome_of[r["id"]]["best_match"] = round(best, 3)
        if best < SURVIVED:
            gone.append(
                {
                    "file": os.path.basename(path),
                    "best_match": round(best, 3),
                    "words": r["words"],
                    "verdict": verdicts.get(r["id"]),
                    "comment": r["comment"][:300],
                }
            )

    verdict_of_gone = collections.Counter(g["verdict"] for g in gone)
    verdict_all = collections.Counter(verdicts.values())

    result = {
        "authored_blocks": len(rows),
        "in_session_shrinks": len(shrinks),
        "superseded_blocks": len(superseded_ids),
        "checked_against_worktree": checked,
        "files_unavailable": missing_files,
        "did_not_survive": len(gone),
        "survival_rate": round(1 - len(gone) / max(1, checked), 3),
        "verdicts_all": dict(verdict_all),
        "verdicts_of_non_survivors": dict(verdict_of_gone),
        # Aggregates over EVERY shrink, not over the sample below. A median taken
        # from the ten examples would be a different number wearing the same name.
        "shrink_words_before_median": (
            statistics.median(s["before_words"] for s in shrinks) if shrinks else None
        ),
        "shrink_words_after_median": (
            statistics.median(s["after_words"] for s in shrinks) if shrinks else None
        ),
        "shrink_chars_median": (
            round(statistics.median(s["shrink"] for s in shrinks), 3) if shrinks else None
        ),
        "shrink_verdicts_before": dict(
            collections.Counter(s["verdict_before"] for s in shrinks)
        ),
        "shrink_examples": sorted(shrinks, key=lambda s: -s["shrink"])[:10],
        "non_survivor_examples": gone[:10],
    }

    path = os.path.join(FINDINGS, "comment_survival.json")
    with open(path, "w") as fh:
        json.dump(result, fh, indent=2)
    write_jsonl(
        os.path.join(FINDINGS, "comment_outcomes.jsonl"), list(outcome_of.values())
    )
    print(
        json.dumps(
            {k: v for k, v in result.items() if not k.endswith("examples")}, indent=2
        )
    )


if __name__ == "__main__":
    main()
