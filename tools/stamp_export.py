"""Stamp a claude.ai export so make re-extracts chat only when it changed.

The stamp findings/_meta/export.stamp is rewritten only when the export path,
size or mtime of conversations.json differs from the last run.

Usage: python3 tools/stamp_export.py <path-to-unzipped-export-dir>
Writes: findings/_meta/export.stamp
"""

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import redact  # noqa: E402
from common import ensure_findings_dir, positional_args  # noqa: E402


def main():
    args = positional_args()
    if not args:
        sys.exit("usage: stamp_export.py <unzipped-export-dir>")
    path = os.path.abspath(os.path.join(args[0], "conversations.json"))
    if not os.path.isfile(path):
        sys.exit(f"missing {path}")
    st = os.stat(path)
    findings = ensure_findings_dir()
    meta = os.path.join(findings, "_meta")
    os.makedirs(meta, exist_ok=True)
    stamp_path = os.path.join(meta, "export.stamp")
    stamp = hashlib.sha256(json.dumps([path, st.st_size, st.st_mtime_ns]).encode()).hexdigest() + "\n"
    current = open(stamp_path).read() if os.path.exists(stamp_path) else None
    if stamp != current:
        redact.write_private(stamp_path, stamp)


if __name__ == "__main__":
    main()
