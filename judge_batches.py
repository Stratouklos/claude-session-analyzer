#!/usr/bin/env python3
"""Drive a judging pass through an Ollama model (local or :cloud).

Reads every batch in <input_dir>, sends it to the model with <rubric_file> as
instructions, and writes the parsed verdicts to <output_dir>. Generic across the
triage and judge passes; the rubric defines the output shape.

Verdicts are cached by content (model, rubric, item), so a re-run sends only
items the model has not judged, whatever batch they land in. Every batch is
scanned for secrets before the first call; one finding stops the run with
nothing sent.

Usage:
  python3 judge_batches.py <model> <input_dir> <output_dir> <rubric_file> [--limit N]

Example:
  python3 judge_batches.py minimax-m3:cloud findings/triage_in findings/triage_out tools/triage_rubric.md
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))

import redact  # noqa: E402

OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def generate(model, prompt, timeout=900):
    payload = {"model": model, "prompt": prompt, "stream": False}
    req = urllib.request.Request(
        f"{OLLAMA}/api/generate",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read()).get("response", "")


def extract_json(text):
    """Pull the first JSON array or object out of a model reply, tolerating
    markdown fences and surrounding prose."""
    text = text.strip()
    # Strip ```json ... ``` fences.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    # Prefer a top-level array; fall back to an object.
    for candidate in (text,):
        try:
            return json.loads(candidate)
        except ValueError:
            pass
    # Find the first [ ... ] or { ... } span.
    for opener, closer in (("[", "]"), ("{", "}")):
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        for i in range(start, len(text)):
            if text[i] == opener:
                depth += 1
            elif text[i] == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except ValueError:
                        break
    raise ValueError("no JSON found in model reply")


def normalize(parsed):
    """Accept a bare list, or a dict wrapping the list under results/items."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("results", "items", "verdicts", "output"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        # A bare object is the correct shape for passes that emit one object
        # per batch (the problem pass), not one verdict per input item.
        return parsed
    raise ValueError(f"unexpected model output shape: {type(parsed)}")


class Cache:
    """Verdicts keyed by what produced them: a changed rubric, model or item
    misses, so nothing stale is reused and nothing judged is paid for twice."""

    def __init__(self, out_dir, model, rubric):
        self.dir = os.path.join(os.path.dirname(os.path.abspath(out_dir)), "_cache",
                                os.path.basename(os.path.abspath(out_dir)))
        os.makedirs(self.dir, exist_ok=True)
        self.prefix = hashlib.sha256(f"{model}\0{rubric}\0".encode()).hexdigest()

    def key(self, value):
        body = json.dumps(value, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256((self.prefix + body).encode()).hexdigest()

    def get(self, key):
        try:
            with open(os.path.join(self.dir, f"{key}.json")) as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None
        except ValueError:
            # A write cut short by a kill: judge the item again.
            return None

    def put(self, key, value):
        redact.write_private(os.path.join(self.dir, f"{key}.json"), json.dumps(value))


def pair(items, verdicts):
    """Map item index to verdict. Items with ids pair by id only, so a dropped or
    invented id leaves that item unpaired: never cached, asked again later."""
    if not isinstance(verdicts, list):
        return {}
    good = [v for v in verdicts if isinstance(v, dict) and v]
    ids = [it.get("id") if isinstance(it, dict) else None for it in items]
    if all(ids):
        by_id = {v.get("id"): v for v in good}
        return {i: by_id[x] for i, x in enumerate(ids) if x in by_id}
    if not any(ids) and len(good) == len(verdicts) == len(items):
        return dict(enumerate(good))
    return {}


def ask(model, rubric, items, name):
    prompt = (
        f"{rubric}\n\n"
        f"Here is the input batch (a JSON array of {len(items)} items).\n"
        f"Return a JSON array with exactly one verdict object per input item, "
        f"in the same order, ids copied verbatim. Do not omit or merge any items.\n\n"
        f"{json.dumps(items, ensure_ascii=False)}"
    )
    for attempt in range(3):
        try:
            return normalize(extract_json(generate(model, prompt)))
        except Exception as e:  # noqa: BLE001
            print(f"  attempt {attempt + 1} failed for {name}: {e}")
            time.sleep(5)
    return None


def unjudged(cache, items):
    """What a batch still needs from the model. An object batch (one verdict
    for the whole batch, e.g. the problem pass) is a single unit."""
    if not isinstance(items, list):
        return [] if cache.get(cache.key(items)) is not None else [items]
    return [it for it in items if cache.get(cache.key(it)) is None]


def judge_object(model, rubric, cache, name, batch):
    key = cache.key(batch)
    verdict = cache.get(key)
    if verdict is not None:
        return verdict, 0
    verdict = ask(model, rubric, batch, name)
    if verdict is not None:
        cache.put(key, verdict)
    return verdict, 1


def judge_list(model, rubric, cache, name, items):
    """Verdicts for every item that pairs. The second round asks again for the
    items the model dropped or misnamed; any still unpaired are left out."""
    keys = [cache.key(it) for it in items]
    verdicts = [cache.get(k) for k in keys]
    sent = 0
    for _round in range(2):
        todo = [i for i, v in enumerate(verdicts) if v is None]
        if not todo:
            break
        reply = ask(model, rubric, [items[i] for i in todo], name)
        sent += len(todo)
        if reply is None:
            break
        for j, v in pair([items[i] for i in todo], reply).items():
            verdicts[todo[j]] = v
            cache.put(keys[todo[j]], v)
    missing = sum(v is None for v in verdicts)
    if missing:
        print(f"  {name}: {missing} of {len(items)} items got no paired verdict; they are asked again next run")
    kept = [v for v in verdicts if v is not None]
    return (kept if kept or not items else None), sent


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 4:
        print(__doc__)
        sys.exit(2)
    model, in_dir, out_dir, rubric_path = args[:4]
    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]

    rubric = open(rubric_path).read()
    os.makedirs(out_dir, exist_ok=True)
    cache = Cache(out_dir, model, rubric)

    every = sorted(n for n in os.listdir(in_dir) if n.endswith(".json"))
    # A batch file left over from an earlier run would be merged as if it were current.
    for name in os.listdir(out_dir):
        if name.endswith(".json") and name not in every:
            os.remove(os.path.join(out_dir, name))
    batches = [only] if only else every
    if limit:
        batches = batches[:limit]

    loaded = {name: json.load(open(os.path.join(in_dir, name))) for name in batches}
    # Every batch is checked before the first call, so a dirty batch sends nothing at all.
    for name, items in loaded.items():
        todo = unjudged(cache, items)
        if todo:
            redact.assert_clean(todo, f"batch {name}")

    sent = 0
    failed = 0
    for name, items in loaded.items():
        out_path = os.path.join(out_dir, name)
        if isinstance(items, list):
            verdicts, n = judge_list(model, rubric, cache, name, items)
        else:
            verdicts, n = judge_object(model, rubric, cache, name, items)
        sent += n
        if verdicts is None:
            print(f"FAILED {name}: no verdict")
            failed += 1
            if os.path.exists(out_path):
                os.remove(out_path)
            continue
        with open(out_path, "w") as fh:
            json.dump(verdicts, fh, ensure_ascii=False, indent=1)
        count = len(verdicts) if isinstance(verdicts, list) else "1 object"
        print(f"{name}: {len(items)} in -> {count} verdicts")

    print(f"done -> {out_dir} ({sent} items sent to the model)")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
