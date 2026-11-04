"""Find and replace secrets with betterleaks before any tool reads the text.

Transcripts hold whatever a tool printed: keys from a `.env`, kubeconfigs,
tokens in a URL. Every later stage clips text, and a clipped secret matches no
rule, so redaction runs on each whole transcript entry, once, before anything
else: every string value and dict key, decoded.

A finding is replaced by `[REDACTED:<rule>]`. A finding with no literal to
replace (betterleaks found it after decoding base64 or unicode escapes, or the
match spans entries) drops the entries that hold its first and last line, and
the scan repeats until it is clean.

betterleaks is required. Without it nothing runs: a pipeline that quietly skips
redaction reports success while shipping secrets to a model.

Usage:
  python3 tools/redact.py --self-test
  python3 tools/redact.py --scan PATH...   # rule ids per file, never the values
"""

import base64
import hashlib
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RULES = os.path.join(HERE, "redact.toml")
BETTERLEAKS = os.environ.get("CSA_BETTERLEAKS", "betterleaks")
CHUNK = 200
ROUNDS = 3  # scan-and-replace passes before entries that still have findings are dropped
SELF_TEST_SEED = 20261104
TIMEOUT = 900  # seconds per betterleaks call; a hang must not stall a scheduled run
# Ends greedy rules at a value boundary: no rule's character class takes '-' and '<' together.
SEPARATOR = "--8<--"
LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _binary():
    path = shutil.which(BETTERLEAKS)
    if not path:
        raise SystemExit(
            f"ERROR: {BETTERLEAKS} not found. Redaction is required before any"
            " transcript is read.\n  Fix: brew install betterleaks, or set"
            " CSA_BETTERLEAKS to its path."
        )
    return path


def scan(paths):
    """betterleaks findings for the files at `paths`. Validation stays off, so
    no secret is ever sent to a provider to check it."""
    findings = []
    for start in range(0, len(paths), CHUNK):
        findings += _scan_chunk(paths[start : start + CHUNK])
    return findings


def _scan_chunk(paths):
    work = tempfile.mkdtemp(prefix="csa-scan-")
    try:
        report = os.path.join(work, "report.json")
        # An empty ignore dir: a .betterleaksignore in the cwd must not hide findings.
        ignore = os.path.join(work, "ignore")
        os.mkdir(ignore)
        cmd = [
            _binary(), "dir", *paths,
            "--no-banner", "--no-color", "--ignore-gitleaks-allow",
            "-i", ignore, "-c", RULES,
            "-f", "json", "-r", report, "-l", "error", "--exit-code", "0",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            raise SystemExit(f"ERROR: betterleaks did not finish in {TIMEOUT}s.") from None
        if proc.returncode != 0:
            raise SystemExit(
                f"ERROR: betterleaks exited {proc.returncode}: "
                f"{proc.stderr.strip()[:400]}"
            )
        with open(report) as fh:
            return json.load(fh) or []
    finally:
        shutil.rmtree(work, ignore_errors=True)


def redact_entries(entries, drop_all=False):
    """Return (entries, stats) with every finding removed. `drop_all` drops each
    entry that has a finding instead of replacing literals.

    Scans the decoded string values with their keys, not the raw JSON: inside a
    JSON string a newline is the two characters `\\n`, which breaks rules that key
    on the start of a line. Replacing inside the parsed values keeps every entry
    valid JSON."""
    lines, spans = [], []
    for entry in entries:
        start = len(lines)
        for text in _scan_strings(entry, []):
            lines.extend(text.split("\n"))
            lines.append(SEPARATOR)
        spans.append((start, len(lines) - 1))
    leaves = [leaf for entry in entries for leaf in _leaves(entry, [])]

    work = tempfile.mkdtemp(prefix="csa-redact-")
    try:
        path = os.path.join(work, "decoded.txt")
        with open(path, "w", errors="surrogatepass") as fh:
            fh.write("\n".join(lines))
        findings = scan([path])
    finally:
        shutil.rmtree(work, ignore_errors=True)

    literals, drop_lines = {}, set()
    for f in findings:
        # A decoded finding's Match is only part of the encoded run; the rest would still decode.
        decoded = any(str(t).startswith("decoded:") for t in f.get("Tags") or [])
        literal = None if decoded or drop_all else next(
            (s for s in (f.get("Secret"), f.get("Match")) if s and any(s in leaf for leaf in leaves)),
            None,
        )
        if literal:
            literals.setdefault(literal, f"[REDACTED:{f.get('RuleID', 'secret')}]")
        else:
            # Only the entries holding its ends: a lazy rule can span many clean entries.
            drop_lines.update((f["StartLine"] - 1, f["EndLine"] - 1))
    ordered = sorted(literals, key=len, reverse=True)
    out, dropped = [], 0
    for entry, (start, end) in zip(entries, spans):
        if any(start <= n <= end for n in drop_lines):
            dropped += 1
            continue
        out.append(_replace(entry, ordered, literals))
    return out, {"findings": len(findings), "replaced": len(literals), "dropped_entries": dropped}


def _replace(value, ordered, literals):
    if isinstance(value, str):
        # A lone surrogate (text cut mid-emoji) cannot be written as UTF-8 downstream.
        value = LONE_SURROGATE.sub("\ufffd", value)
        for literal in ordered:
            if literal in value:
                value = value.replace(literal, literals[literal])
        return value
    if isinstance(value, dict):
        return {_replace(k, ordered, literals): _replace(v, ordered, literals) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace(v, ordered, literals) for v in value]
    return value


def redact_jsonl_file(src, dst):
    """Write a redacted copy of the JSONL transcript `src` to `dst` (mode 0600)."""
    entries, unparseable = [], 0
    with open(src, errors="ignore") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                unparseable += 1
    # A replacement can expose a new match, so repeat until clean; what survives is dropped.
    totals = {"findings": 0, "replaced": 0, "dropped_entries": 0}
    redacted = entries
    for round_ in range(ROUNDS + 1):
        redacted, stats = redact_entries(redacted, drop_all=round_ == ROUNDS)
        for k in totals:
            totals[k] += stats[k]
        if not stats["findings"]:
            break
    stats = totals
    # ensure_ascii keeps a lone surrogate (text cut mid-emoji) writable as an escape.
    write_private(dst, "".join(json.dumps(entry) + "\n" for entry in redacted))
    stats["unparseable_lines"] = unparseable
    return stats


def write_private(path, text):
    """Write `text` to `path` atomically, mode 0600: a reader sees the old file
    or the new one, never half of either."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", errors="surrogatepass") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def rules_digest():
    """Changes when the rules, this redaction code or the scanner change, so
    every redacted copy is rebuilt."""
    with open(RULES, "rb") as fh:
        rules = fh.read()
    with open(os.path.abspath(__file__), "rb") as fh:
        code = fh.read()
    proc = subprocess.run([_binary(), "version"], capture_output=True, text=True, timeout=60)
    version = proc.stdout.strip()
    if proc.returncode != 0 or not version:
        raise SystemExit(f"ERROR: `betterleaks version` failed (exit {proc.returncode}).")
    return hashlib.sha256(rules + code + version.encode()).hexdigest()[:16]


def _leaves(value, out):
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            out.append(k)
            _leaves(v, out)
    elif isinstance(value, list):
        for v in value:
            _leaves(v, out)
    return out


def _scan_strings(value, out, key=None):
    """Every string in `value`, each prefixed by its dict key (a bare value has
    lost the context, "token" or "password", that rules match on), and every
    key on its own, since a key can be the secret."""
    if isinstance(value, str):
        out.append(f"{key}: {value}" if key is not None else value)
    elif isinstance(value, dict):
        for k, v in value.items():
            out.append(k)
            _scan_strings(v, out, k)
    elif isinstance(value, list):
        for v in value:
            _scan_strings(v, out, key)
    return out


def findings_in(value):
    """Rule ids betterleaks reports in every string inside `value`, decoded
    (newlines real, not JSON-escaped), so multi-line rules see a whole PEM."""
    work = tempfile.mkdtemp(prefix="csa-gate-")
    try:
        path = os.path.join(work, "batch.txt")
        with open(path, "w", errors="surrogatepass") as fh:
            fh.write(f"\n{SEPARATOR}\n".join(_scan_strings(value, [])))
        return sorted({f.get("RuleID", "?") for f in scan([path])})
    finally:
        shutil.rmtree(work, ignore_errors=True)


def assert_clean(value, label):
    """Stop before a model call if anything secret-shaped is still in `value`."""
    rules = findings_in(value)
    if rules:
        raise SystemExit(
            f"ERROR: {label} still holds secret-shaped text ({', '.join(rules)})."
            " Nothing was sent. Rebuild the redacted corpus, or add a rule to"
            " tools/redact.toml."
        )


def _rand(n, alphabet=string.ascii_letters + string.digits, rng=None):
    rng = rng or random.SystemRandom()
    return "".join(rng.choice(alphabet) for _ in range(n))


def canaries(rng=None):
    """Fake secrets in the shapes transcripts carry, built at run time so no
    secret-shaped literal sits in the source."""
    body = base64.b64encode((rng or random.SystemRandom()).randbytes(900)).decode()
    pem_lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    kind = "RSA " + "PRIVATE KEY"
    pem = "\n".join([f"-----BEGIN {kind}-----", *pem_lines, f"-----END {kind}-----"])
    b32 = string.ascii_uppercase + "234567"
    return {
        "github": "gh" + "p_" + _rand(36, rng=rng),
        "github_as_key": "gh" + "p_" + _rand(36, rng=rng),
        "anthropic": "sk-" + "ant-api03-" + _rand(93, string.ascii_letters + string.digits + "-_", rng=rng) + "AA",
        "aws_id": "AK" + "IA" + _rand(16, b32, rng=rng),
        "aws_secret": _rand(40, string.ascii_letters + string.digits + "/+", rng=rng),
        "slack": "xo" + "xb-" + _rand(12, string.digits, rng=rng) + "-" + _rand(13, string.digits, rng=rng) + "-" + _rand(24, rng=rng),
        "url_password": _rand(18, rng=rng),
        # No vowels, so no stopword the generic rule discards; the digit it requires comes first.
        "keyed_value": _rand(1, string.digits, rng=rng) + _rand(39, "BCDFGHJKLMNPQRSTVWXZ23456789", rng=rng),
        "pem": pem,
        "pem_truncated": pem[:300],
        "pem_base64": base64.b64encode(pem.encode()).decode(),
    }


def canary_transcript(c):
    """A transcript with every canary where a real one turns up."""
    def user(text):
        return {"type": "user", "uuid": _rand(12), "message": {"role": "user", "content": text}}

    def tool_result(text):
        return {
            "type": "user", "uuid": _rand(12), "toolUseResult": {"stdout": text},
            "message": {"role": "user", "content": [{"type": "tool_result", "content": text}]},
        }

    return [
        user(f"no, use this token instead: {c['github']}"),
        user(f"export ANTHROPIC_API_KEY={c['anthropic']}"),
        tool_result(f"aws_access_key_id = {c['aws_id']}\naws_secret_access_key = {c['aws_secret']}"),
        tool_result(f"xoxb token {c['slack']}"),
        {"type": "user", "uuid": _rand(12), "toolUseResult": {"api_key": c["keyed_value"]},
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
        tool_result(f"DATABASE_URL=postgres://app:{c['url_password']}@db.internal:5432/app"),
        tool_result(c["pem"]),
        tool_result(c["pem_truncated"]),
        tool_result(f"client-key-data: {c['pem_base64']}"),
        {"type": "user", "uuid": _rand(12),
         "toolUseResult": {"headers": {f"Authorization: token {c['github_as_key']}": True}},
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "sent"}]}},
        *(user(text) for text in NEGATIVES[:-1]),
        user("cut \ud83d mid-emoji"),
    ]


# Look-alikes that must come through untouched: over-redaction loses evidence.
NEGATIVES = (
    "thanks, that worked",
    "the password policy says rotate every 90 days",
    "session 3f6c2d1e-8a4b-4c9e-9f1a-2b7d5e8c0a11 ended",
    "commit 9fceb02d0ae598e95dc970b74767f19372d61af8 fixed it",
    "[REDACTED:github-pat] was rotated",
    "cut \ufffd mid-emoji",
)


def self_test():
    """Prove the scanner sees every canary raw, then that none survives. The
    seed is fixed so a run cannot fail on an unlucky draw."""
    c = canaries(random.Random(SELF_TEST_SEED))
    work = tempfile.mkdtemp(prefix="csa-selftest-")
    try:
        src = os.path.join(work, "raw.jsonl")
        with open(src, "w") as fh:
            for entry in canary_transcript(c):
                fh.write(json.dumps(entry) + "\n")
        if not scan([src]):
            raise SystemExit("ERROR: redaction self-test: betterleaks found nothing in the raw canaries.")
        dst = os.path.join(work, "redacted.jsonl")
        stats = redact_jsonl_file(src, dst)
        with open(dst) as fh:
            raw_out = fh.read()
        decoded = [s for line in raw_out.splitlines() for s in _leaves(json.loads(line), [])]
        out = raw_out + "\n" + "\n".join(decoded)
        leaked = sorted(name for name, value in c.items() if value in out)
        leaked += sorted(f"{name} (first 24 chars)" for name, value in c.items() if value[:24] in out and name not in leaked)
        if leaked:
            raise SystemExit(f"ERROR: redaction self-test: canaries survived: {', '.join(leaked)}")
        if scan([dst]):
            raise SystemExit("ERROR: redaction self-test: the redacted copy still has findings.")
        lost = [text for text in NEGATIVES if text not in decoded]
        if lost:
            raise SystemExit(f"ERROR: redaction self-test: clean text was altered or dropped: {lost}")
        return stats
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    if "--self-test" in sys.argv:
        stats = self_test()
        print(f"redaction self-test passed: {json.dumps(stats)}")
        return
    if "--scan" in sys.argv:
        paths = sys.argv[sys.argv.index("--scan") + 1 :]
        by_file = {}
        for f in scan(paths):
            by_file.setdefault(f["File"], []).append(f.get("RuleID", "?"))
        for path in paths:
            print(f"{path}\t{len(by_file.get(path, []))}\t{','.join(sorted(set(by_file.get(path, []))))}")
        sys.exit(1 if by_file else 0)
    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    main()
