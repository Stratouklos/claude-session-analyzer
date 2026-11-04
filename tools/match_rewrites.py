"""Find where the user rewrote what the model authored.

This is the only correction signal that costs the user nothing to produce - they never
had to type a complaint for it to count, so it is free of the recall bias that
affects every signal derived from what they said.

Matches authored commit messages against what landed in git, and authored PR
bodies against what is live on GitHub. A high subject-similarity pair whose
bodies diverge is a rewrite.

Usage: python3 tools/match_rewrites.py
Writes: findings/rewrites.jsonl
"""

import difflib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (  # noqa: E402
    FINDINGS,
    REDACTED_MARK,
    ensure_findings_dir,
    strip_trailers,
    write_jsonl,
)

SUBJECT_MATCH = 0.60  # same change, judged on the first line
BODY_DIVERGED = 0.92  # below this, the prose was materially edited


def load(name):
    path = os.path.join(FINDINGS, name)
    if not os.path.exists(path):
        return []
    return [json.loads(line) for line in open(path)]


def norm(text):
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def ratio(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def split_subject(body):
    lines = (body or "").strip().split("\n")
    return lines[0], "\n".join(lines[1:]).strip()


def match_commits(authored, landed):
    rows = []
    for art in authored:
        if REDACTED_MARK in art["body"]:
            continue
        subject, body = split_subject(art["body"])
        if not subject:
            continue
        best = None
        for commit in landed:
            score = ratio(subject, commit["subject"])
            if best is None or score > best[0]:
                best = (score, commit)
        if not best or best[0] < SUBJECT_MATCH:
            rows.append({**base(art, "commit_message"), "outcome": "not_landed",
                         "best_subject_score": round(best[0], 3) if best else None})
            continue
        score, commit = best
        # Strip trailers on the landed side too. They are added by the harness
        # after authoring, so leaving them in marks every commit as rewritten.
        landed_body = strip_trailers(commit["body"])
        body_score = ratio(body, landed_body)
        subject_identical = norm(subject) == norm(commit["subject"])
        rows.append(
            {
                **base(art, "commit_message"),
                "outcome": "landed",
                "sha": commit["sha"],
                "repo": commit["repo"],
                "landed_subject": commit["subject"],
                "landed_body": landed_body[:4000],
                "subject_score": round(score, 3),
                "subject_identical": subject_identical,
                "body_score": round(body_score, 3),
                "rewritten": body_score < BODY_DIVERGED,
                "authored_chars": len(art["body"]),
                "landed_chars": len(commit["subject"]) + len(landed_body),
            }
        )
    return rows


def match_prs(authored, prs):
    rows = []
    for art in authored:
        if REDACTED_MARK in art["body"]:
            continue
        subject, body = split_subject(art["body"])
        probe = art["body"]
        best = None
        for pr in prs:
            score = max(
                ratio(subject, pr.get("title") or ""),
                ratio(probe[:400], (pr.get("body") or "")[:400]),
            )
            if best is None or score > best[0]:
                best = (score, pr)
        if not best or best[0] < SUBJECT_MATCH:
            rows.append({**base(art, "pr_body"), "outcome": "not_matched",
                         "best_subject_score": round(best[0], 3) if best else None})
            continue
        score, pr = best
        body_score = ratio(strip_trailers(probe), strip_trailers(pr.get("body") or ""))
        rows.append(
            {
                **base(art, "pr_body"),
                "outcome": "matched",
                "repo": pr["repo"],
                "number": pr["number"],
                "live_title": pr.get("title"),
                "live_body": (pr.get("body") or "")[:4000],
                "match_score": round(score, 3),
                "body_score": round(body_score, 3),
                "rewritten": body_score < BODY_DIVERGED,
                "authored_chars": len(probe),
                "live_chars": len(pr.get("body") or ""),
                "review_comment_count": len(pr.get("review_threads") or [])
                + len(pr.get("comments") or []),
            }
        )
    return rows


def base(art, kind):
    return {
        "id": art["id"],
        "kind": kind,
        "session": art.get("session"),
        "ts": art.get("ts"),
        "authored": art["body"][:4000],
    }


def main():
    ensure_findings_dir()
    authored = load("artifacts_authored.jsonl")
    landed = load("git_landed.jsonl")
    prs = load("prs.jsonl")

    commits = [a for a in authored if a["kind"] == "commit_message"]
    pr_bodies = [a for a in authored if a["kind"] == "pr_body"]

    rows = match_commits(commits, landed) + match_prs(pr_bodies, prs)
    out = os.path.join(FINDINGS, "rewrites.jsonl")
    write_jsonl(out, rows)

    landed_rows = [r for r in rows if r.get("outcome") in ("landed", "matched")]
    rewritten = [r for r in landed_rows if r.get("rewritten")]
    print(
        json.dumps(
            {
                "authored_commits": len(commits),
                "authored_pr_bodies": len(pr_bodies),
                "matched": len(landed_rows),
                "rewritten": len(rewritten),
                "rewrite_rate": round(len(rewritten) / max(1, len(landed_rows)), 3),
            }
        )
    )
    print(f"wrote {len(rows)} rows -> {out}")


if __name__ == "__main__":
    main()
