"""Write a redacted copy of every allowlisted session transcript.

Every transcript reader lists these copies (common.iter_transcripts), never the
raw corpus. A copy is rebuilt only when its source changed or the rules did, so
a weekly run rescans only new and growing sessions.

The stamp findings/_meta/corpus.stamp is rewritten only when the set of copies
changed, so make re-extracts exactly when there is something new to read.

Usage: python3 tools/redact_corpus.py
Writes: findings/redacted/<project>/<session>.jsonl, findings/redacted/manifest.json,
        findings/_meta/corpus.stamp
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import redact  # noqa: E402
from common import REDACTED_DIR, corpus_config, ensure_findings_dir, iter_raw_transcripts  # noqa: E402


def main():
    redact.self_test()
    findings = ensure_findings_dir()
    os.makedirs(REDACTED_DIR, mode=0o700, exist_ok=True)
    os.chmod(REDACTED_DIR, 0o700)
    manifest_path = os.path.join(REDACTED_DIR, "manifest.json")
    old = {"files": []}
    if os.path.exists(manifest_path):
        with open(manifest_path) as fh:
            old = json.load(fh)
    previous = {row["copy"]: row for row in old["files"]}
    digest = redact.rules_digest()

    rows, rebuilt = [], 0
    totals = {"findings": 0, "replaced": 0, "dropped_entries": 0, "unparseable_lines": 0}
    for project, src in iter_raw_transcripts():
        st = os.stat(src)
        copy = os.path.join(project, os.path.basename(src))
        dst = os.path.join(REDACTED_DIR, copy)
        key = {"source": src, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "rules": digest}
        prev = previous.get(copy)
        if prev and all(prev.get(k) == v for k, v in key.items()) and os.path.exists(dst):
            rows.append(prev)
            continue
        os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
        stats = redact.redact_jsonl_file(src, dst)
        rebuilt += 1
        for k in totals:
            totals[k] += stats[k]
        rows.append({"project": project, "copy": copy, **key, **stats})

    kept = {row["copy"] for row in rows}
    removed = 0
    for copy in previous:
        if copy not in kept and os.path.exists(os.path.join(REDACTED_DIR, copy)):
            os.remove(os.path.join(REDACTED_DIR, copy))
            removed += 1

    rows.sort(key=lambda r: r["copy"])
    redact.write_private(manifest_path, json.dumps(
        {"config": corpus_config(), "rules": digest, "files": rows}, indent=1) + "\n")

    meta = os.path.join(findings, "_meta")
    os.makedirs(meta, exist_ok=True)
    stamp_path = os.path.join(meta, "corpus.stamp")
    listing = json.dumps([[r["copy"], r["size"], r["mtime_ns"], r["rules"]] for r in rows])
    stamp = hashlib.sha256(listing.encode()).hexdigest() + "\n"
    current = open(stamp_path).read() if os.path.exists(stamp_path) else None
    if stamp != current:
        redact.write_private(stamp_path, stamp)

    print(json.dumps({
        "sessions": len(rows), "rebuilt": rebuilt, "removed": removed,
        "stamp_changed": stamp != current, **totals,
    }))


if __name__ == "__main__":
    main()
