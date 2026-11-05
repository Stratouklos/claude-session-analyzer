#!/usr/bin/env python3
"""Model backends for the judging passes, behind one `generate` dispatch.

A model string is `ollama:<name>`, `claude:<alias>`, or a bare name (treated as
`ollama:<name>`). Only the leading prefix selects the backend: an Ollama name
may itself contain a colon (`minimax-m3:cloud`).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import redact  # noqa: E402

MIN_CLAUDE = (2, 1, 248)
# ask() sends rubric and items as one prompt on stdin; this file only satisfies --system-prompt-file.
CLAUDE_SYSTEM = "You are a strict JSON-only judge. Follow the instructions in the user message.\n"

_claude_checked = False


def split_model(model):
    """(backend, name); a bare string is an Ollama name."""
    for backend in ("ollama", "claude"):
        if model.startswith(backend + ":"):
            return backend, model[len(backend) + 1:]
    return "ollama", model


def guard_cloud(model):
    """Refuse a `:cloud` or `-cloud` model (any case) unless ALLOW_CLOUD=1: cloud models send items off this machine."""
    backend, name = split_model(model)
    if backend == "ollama" and re.search(r"[:-]cloud$", name, re.IGNORECASE) and os.environ.get("ALLOW_CLOUD") != "1":
        raise SystemExit(
            f"refusing cloud model {model!r}: items would leave this machine. "
            "Set ALLOW_CLOUD=1 or pick a local model."
        )


def generate(model, prompt, timeout=900):
    backend, name = split_model(model)
    if backend == "claude":
        return _generate_claude(name, prompt, timeout)
    return _generate_ollama(name, prompt, timeout)


def _generate_ollama(name, prompt, timeout):
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    payload = {"model": name, "prompt": prompt, "stream": False}
    req = urllib.request.Request(
        f"{host}/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read()).get("response", "")


def _check_claude_version():
    global _claude_checked
    if _claude_checked:
        return
    need = ".".join(map(str, MIN_CLAUDE))
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=60).stdout
    except FileNotFoundError:
        raise SystemExit(f"claude CLI not found on PATH; version {need} or newer is required")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"`claude --version` timed out after 60s; version {need} or newer is required")
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    if not m:
        raise SystemExit(f"cannot parse `claude --version` output {out.strip()!r}; version {need} or newer is required")
    found = tuple(int(x) for x in m.groups())
    if found < MIN_CLAUDE:
        raise SystemExit(f"claude {'.'.join(map(str, found))} is too old; version {need} or newer is required")
    _claude_checked = True


def _generate_claude(alias, prompt, timeout):
    _check_claude_version()
    tmp = tempfile.mkdtemp(prefix="csa-claude-")
    try:
        system = os.path.join(tmp, "system.md")
        redact.write_private(system, CLAUDE_SYSTEM)
        proc = subprocess.run(
            ["claude", "-p", "--no-session-persistence", "--system-prompt-file", system,
             "--tools", "", "--setting-sources", "project", "--model", alias],
            input=prompt, capture_output=True, text=True, timeout=timeout,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr.strip()[:300]}")
    return proc.stdout
