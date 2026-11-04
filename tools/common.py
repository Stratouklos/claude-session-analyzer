"""Shared helpers for the claude-session-analyzer pipeline."""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import redact  # noqa: E402

HOME = os.path.expanduser("~")
PROJECTS = os.path.join(HOME, ".claude", "projects")


def _flag_arg(*flags):
    """Value of the first of `flags` present in argv, honoured by every tool."""
    for flag in flags:
        if flag in sys.argv:
            i = sys.argv.index(flag)
            if i + 1 < len(sys.argv):
                return sys.argv[i + 1]
    return None


def _output_arg():
    return _flag_arg("--output", "-o")


# Flags that consume the argument after them, so positional_args does not
# mistake a flag's value for a positional.
_VALUE_FLAGS = (
    "--output", "-o", "--config", "--gap", "--min-confidence", "--rules", "--sweep",
)


def positional_args():
    """argv entries that are neither a flag nor a flag's value.

    Tools taking a positional argument have to skip past the shared flags, or
    `tool.py --output DIR` reads "--output" as the positional.
    """
    found, skip = [], False
    for arg in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if arg in _VALUE_FLAGS:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        found.append(arg)
    return found


# Findings are generated output: they land under the working directory unless
# told otherwise, never next to the code. Precedence: --output, then
# CSA_FINDINGS, then ./findings.
FINDINGS = os.path.abspath(
    _output_arg() or os.environ.get("CSA_FINDINGS") or "findings"
)

LOCAL_CONFIG = "csa.config.json"
USER_CONFIG = os.path.join(HOME, ".claude-session-analyzer.json")


def resolve_config_path():
    """--config PATH, then CSA_CONFIG, then ./csa.config.json, then
    ~/.claude-session-analyzer.json. First hit wins. An explicitly named path is
    returned whether or not it exists, so a typo is reported rather than
    silently falling through to a different file."""
    explicit = _flag_arg("--config") or os.environ.get("CSA_CONFIG")
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    for candidate in (LOCAL_CONFIG, USER_CONFIG):
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
    return None


CONFIG_PATH = resolve_config_path()

# Everything here is per-user. No corpus, employer, project or person belongs in
# this file - see csa.config.example.json.
CONFIG_DEFAULTS = {
    # Required by the tool that uses them; absent means "fail loudly".
    "author_emails": None,   # compare_style: which commits are hand-written
    "pr_repos": None,        # collect_prs: repos to sweep, "owner/name"
    "pr_author": None,       # collect_prs: GitHub login to filter on
    "window_start": None,    # collect_git, collect_prs: ISO date lower bound
    "included_project_substrings": None,  # every transcript reader: projects to read
    # Optional; empty means no filtering.
    "extra_roots": [],
    "excluded_sessions": [],
    "excluded_project_substrings": [],
    "artifact_excluded_projects": [],
    "project_label_strip": [],
    # Extra terms for the text-matching patterns further down, keyed by pattern.
    # Nested rather than six sibling keys: these are regex terms, and next to
    # `extra_roots` - a list of corpus directories - a flat `extra_profanity`
    # reads like the same kind of setting when it is nothing like it.
    "extra_patterns": {},
}

# Every pattern that accepts extra terms. Declared here rather than discovered,
# so a typo in the config is caught on load no matter which tools that run
# happen to build which patterns.
PATTERN_NAMES = (
    "profanity",           # swearing and exasperation
    "negation_lead",       # a message that opens with a correction
    "correction_phrase",   # a correction anywhere in the message
    "repeat_marker",       # "I already told you" - this one gates the rule bar
    "commit_trailers",     # tool-appended lines, stripped before comparing
    "comment_skip",        # machine directives in code, which are not prose
)


def _check_pattern_config(cfg):
    """A key nobody reads is worse than a key that fails: the run reports
    success and the terms are simply never matched."""
    extra = cfg.get("extra_patterns") or {}
    if not isinstance(extra, dict):
        raise SystemExit(
            "ERROR: 'extra_patterns' must be an object keyed by pattern name.\n"
            f"  Known patterns: {', '.join(PATTERN_NAMES)}"
        )
    for key in extra:
        if key not in PATTERN_NAMES:
            raise SystemExit(
                f"ERROR: extra_patterns: '{key}' is not a pattern.\n"
                f"  Known patterns: {', '.join(PATTERN_NAMES)}"
            )
    # These were six top-level keys briefly. Say so rather than ignoring them.
    stale = [f"extra_{n}" for n in PATTERN_NAMES if f"extra_{n}" in cfg]
    if stale:
        raise SystemExit(
            f"ERROR: {', '.join(stale)} moved under 'extra_patterns'.\n"
            '  Was: "extra_profanity": ["bruh"]\n'
            '  Now: "extra_patterns": {"profanity": ["bruh"]}'
        )


def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    if CONFIG_PATH and os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as fh:
            cfg.update(json.load(fh))
        print(f"config: {CONFIG_PATH}")
    elif CONFIG_PATH:
        raise SystemExit(f"ERROR: no config file at {CONFIG_PATH}")
    _check_pattern_config(cfg)
    return cfg


CONFIG = load_config()


def config_required(key):
    """A missing value is a hard stop, not a silent default. Guessing here would
    sweep the wrong corpus and the run would still report success.

    With no config file at all and a terminal to ask on, offer to build one
    first. Without a terminal - an agent, cron, CI - fail instead of prompting:
    a prompt that nobody can answer blocks forever, which is worse than the
    error it replaced."""
    global CONFIG, CONFIG_PATH
    value = CONFIG.get(key)
    if value not in (None, "", [], {}):
        return value

    if CONFIG_PATH is None and sys.stdin.isatty():
        path = interactive_setup()
        if path:
            CONFIG_PATH = path
            CONFIG = load_config()
            value = CONFIG.get(key)
            if value not in (None, "", [], {}):
                return value

    raise SystemExit(
        f"ERROR: '{key}' is not set.\n"
        f"  Looked for: ./{LOCAL_CONFIG}, then {USER_CONFIG}\n"
        f"  Fix: copy csa.config.example.json to one of those, or pass\n"
        f"       --config PATH, or set CSA_CONFIG."
    )


# --- first-run setup ----------------------------------------------------------
# Every derived default is a guess shown for confirmation, never applied
# silently. The point is to remove typing, not to decide on the user's behalf.


def _run(cmd, cwd=None):
    """Command stdout, or None if the command is missing or fails."""
    import subprocess

    try:
        out = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 and out.stdout.strip() else None


def derive_author_emails():
    found = []
    for scope in (["git", "config", "--get", "user.email"],
                  ["git", "config", "--global", "--get", "user.email"]):
        value = _run(scope)
        if value and value not in found:
            found.append(value)
    return found


def derive_pr_author():
    return _run(["gh", "api", "user", "--jq", ".login"]) or os.environ.get("USER", "")


def derive_project_label_strip():
    """build_report already strips the HOME prefix itself; this is for whatever
    sits between HOME and the repo, which only the user can confirm."""
    return []


def _transcript_cwds():
    cwds = set()
    for _project, path in iter_raw_transcripts(allowlisted=False):
        for entry in read_jsonl(path):
            if entry.get("cwd"):
                cwds.add(entry["cwd"])
                break
    return cwds


def derive_window_start():
    """Earliest timestamp actually present in the corpus. Entries are written in
    order, so the first one carrying a timestamp dates the session."""
    earliest = None
    for _project, path in iter_raw_transcripts(allowlisted=False):
        for entry in read_jsonl(path):
            stamp = entry.get("timestamp")
            if stamp:
                day = stamp[:10]
                if earliest is None or day < earliest:
                    earliest = day
                break
    return earliest


def discover_pr_repos():
    """Walk the cwds the sessions ran in, resolve each to a git toplevel, and
    read its origin, so the repo list does not have to be typed out by hand."""
    repos = set()
    seen_tops = set()
    for cwd in sorted(_transcript_cwds()):
        if not os.path.isdir(cwd):
            continue
        top = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)
        if not top or top in seen_tops:
            continue
        seen_tops.add(top)
        url = _run(["git", "remote", "get-url", "origin"], cwd=top)
        if not url:
            continue
        slug = re.sub(r"^.*[:/]([^/:]+/[^/]+?)(?:\.git)?$", r"\1", url.strip())
        if "/" in slug:
            repos.add(slug)
    return sorted(repos)


def _ask(prompt, default):
    shown = ", ".join(default) if isinstance(default, list) else (default or "")
    reply = input(f"  {prompt}\n    [{shown or 'empty'}]: ").strip()
    if not reply:
        return default
    if isinstance(default, list):
        return [p.strip() for p in reply.split(",") if p.strip()]
    return reply


def interactive_setup():
    """Build a config by confirming derived values. Returns the path written, or
    None if the user declined."""
    print("\nNo config file found. Building one now.")
    print("Press Enter to accept each suggestion, or type a replacement.")
    print("Comma-separate lists. Nothing is written until you confirm.\n")

    cfg = dict(CONFIG_DEFAULTS)
    print("Scanning the corpus for repos and dates...")
    repos = discover_pr_repos()
    window = derive_window_start()
    print(f"  found {len(repos)} repo(s) with an origin remote\n")

    cfg["author_emails"] = _ask(
        "author_emails - your commit emails, so hand-written commits can be told"
        " from co-authored ones", derive_author_emails())
    cfg["pr_author"] = _ask(
        "pr_author - the GitHub login whose PRs to collect", derive_pr_author())
    cfg["pr_repos"] = _ask(
        "pr_repos - repos to sweep for PRs, owner/name", repos)
    cfg["window_start"] = _ask(
        "window_start - ignore commits and PRs before this date", window or "")
    cfg["included_project_substrings"] = _ask(
        "included_project_substrings - read only projects whose transcript dir"
        " contains one of these, e.g. -Code-my-repo", [])
    cfg["excluded_sessions"] = _ask(
        "excluded_sessions - session ids to leave out entirely, e.g. the session"
        " that designed the sweep", [])
    cfg["excluded_project_substrings"] = _ask(
        "excluded_project_substrings - skip projects whose path contains these,"
        " e.g. forks you did not author", [])
    cfg["artifact_excluded_projects"] = _ask(
        "artifact_excluded_projects - projects whose commits/PRs/comments are not"
        " evidence, though their transcripts still are", [])
    cfg["extra_roots"] = _ask(
        "extra_roots - transcript directories copied from other machines", [])
    cfg["project_label_strip"] = _ask(
        "project_label_strip - cosmetic prefixes to drop from project names in"
        " the report", derive_project_label_strip())

    print("\nThis is what would be written:\n")
    body = json.dumps(cfg, indent=2)
    print(body + "\n")

    # Saying so now beats a tool failing on it three commands later.
    blank = [k for k, v in CONFIG_DEFAULTS.items()
             if v is None and cfg.get(k) in (None, "", [], {})]
    if blank:
        print("Left empty, so the tools that need them will still stop:")
        for key in blank:
            print(f"  {key}")
        print()

    print(f"  1) {USER_CONFIG}   (applies wherever you run it)")
    print(f"  2) ./{LOCAL_CONFIG}   (this directory only)")
    print("  3) do not save")
    choice = input("Save to [1]: ").strip() or "1"
    if choice == "3":
        print("Not saved.")
        return None
    target = os.path.abspath(LOCAL_CONFIG) if choice == "2" else USER_CONFIG
    with open(target, "w") as fh:
        fh.write(body + "\n")
    print(f"\nWrote {target}")
    return target


EXCLUDED_SESSIONS = set(CONFIG["excluded_sessions"])
EXCLUDED_PROJECT_SUBSTRINGS = tuple(CONFIG["excluded_project_substrings"])
ARTIFACT_EXCLUDED_PROJECTS = tuple(CONFIG["artifact_excluded_projects"])


def artifacts_allowed(project):
    """False for projects whose written artifacts are ruled out as evidence -
    ones the user did not review to the standard they apply elsewhere. Their
    TRANSCRIPTS still count; only the authored artifacts are excluded."""
    if not ARTIFACT_EXCLUDED_PROJECTS:
        return True
    return not any(s in (project or "") for s in ARTIFACT_EXCLUDED_PROJECTS)


def window_start():
    return config_required("window_start")

# --- text-matching patterns --------------------------------------------------
# Most of these match how ONE person writes when they are annoyed, in English.
# They are finders: their hits get reviewed, never treated as verdicts. Everyone
# phrases things differently, so each takes extra terms from the config under
# `extra_patterns`, keyed by the names in PATTERN_NAMES above - see
# csa.config.example.json.
#
# Entries are literal phrases by default and are escaped for you. Prefix an
# entry with `re:` to supply a raw regex fragment instead. Extending only adds
# alternatives; the built-in terms always stay in.


def _term(name, entry):
    """One config entry as a regex fragment."""
    where = f"extra_patterns.{name}"
    if not isinstance(entry, str) or not entry.strip():
        raise SystemExit(f"ERROR: {where}: entries must be non-empty strings")
    if entry.startswith("re:"):
        fragment = entry[3:]
        try:
            re.compile(fragment)
        except re.error as exc:
            raise SystemExit(f"ERROR: {where}: bad regex {entry!r} - {exc}")
        return fragment
    # A literal phrase should not fire inside a longer word, so bound whichever
    # ends are word characters. Doubling a boundary the outer pattern already
    # supplies is harmless.
    fragment = re.escape(entry)
    if entry[0].isalnum():
        fragment = r"\b" + fragment
    if entry[-1].isalnum():
        fragment = fragment + r"\b"
    return fragment


def extendable_pattern(name, prefix, terms, suffix, flags=re.I):
    """Built-in terms plus whatever the config adds for `name`."""
    if name not in PATTERN_NAMES:
        raise SystemExit(
            f"ERROR: '{name}' is not in PATTERN_NAMES, so the config could never"
            " name it. Add it there."
        )
    extra = (CONFIG.get("extra_patterns") or {}).get(name) or []
    if isinstance(extra, str):
        extra = [extra]
    joined = "|".join(list(terms) + [_term(name, e) for e in extra])
    return re.compile(prefix + joined + suffix, flags)


PROFANITY = extendable_pattern(
    "profanity",
    r"\b(",
    [
        r"fuck\w*", r"shit\w*", "wtf", "ffs", "wth", r"goddamn\w*", "dammit",
        "damn it", "bullshit", "ugh", "argh", "christ", "jesus", "seriously",
        "for the love of",
    ],
    r")\b",
)

NEGATION_LEAD = extendable_pattern(
    "negation_lead",
    r"^\W*(",
    [
        r"no\b", "nope", "nah", "wrong", r"stop\b", r"don'?t\b", r"do not\b",
        r"that'?s not", "thats not", "why (did|are|is|would|the)",
        "i (told|asked|said|already)", "you (did|keep|still|again)",
        r"again\b", "not what", "never mind", "nevermind", "revert", "undo",
    ],
    r")",
)

CORRECTION_PHRASE = extendable_pattern(
    "correction_phrase",
    r"(",
    [
        "i (told|asked) you", "i already (said|told)", "you (keep|still|again)",
        "stop doing", r"don'?t do that", r"that'?s not what", "thats not what",
        "why did you", "you were told", "as i said", "like i said", r"i said\b",
        "you ignored", r"you didn'?t", "you did not",
        "not what i (asked|wanted|said)",
        "read (the|my) (instruction|prompt|request)", "scope creep",
        "over ?engineer", "too (verbose|long|much)", r"i didn'?t ask",
    ],
    r")",
)


def corpus_roots():
    """Every directory holding session transcripts, local machine first.

    Extra machines are added by unpacking their tarball and listing the path in
    `extra_roots` in the config file, or in CSA_EXTRA_ROOTS (colon-separated).
    Each root may be either a `projects` directory or its parent.
    """
    roots = [("local", PROJECTS)]
    extra = list(CONFIG["extra_roots"])

    # Also accepted: a corpus_roots.json sitting in the findings directory.
    legacy = os.path.join(FINDINGS, "corpus_roots.json")
    if os.path.exists(legacy):
        with open(legacy) as fh:
            extra += json.load(fh)
    env = os.environ.get("CSA_EXTRA_ROOTS", "")
    extra += [p for p in env.split(":") if p.strip()]

    for entry in extra:
        label, path = (entry["label"], entry["path"]) if isinstance(entry, dict) else (
            os.path.basename(entry.rstrip("/")),
            entry,
        )
        path = os.path.expanduser(path)
        if os.path.isdir(os.path.join(path, "projects")):
            path = os.path.join(path, "projects")
        if os.path.isdir(path):
            roots.append((label, path))
        else:
            print(f"WARNING: corpus root not found, skipped: {path}")
    return roots


REDACTED_DIR = os.path.join(FINDINGS, "redacted")
# Text holding this was rewritten by redaction, so it cannot be compared with git or disk.
REDACTED_MARK = "[REDACTED:"


def iter_transcripts():
    """Yield (project_dir, path) for every redacted session transcript.

    Analysis tools never read the raw corpus: tools/redact_corpus.py writes a
    redacted copy of each allowlisted session first, and this lists those
    copies. Only first-run setup reads raw sessions, for their cwd and date.
    """
    for row in redacted_manifest()["files"]:
        yield row["project"], os.path.join(REDACTED_DIR, row["copy"])


def redacted_manifest():
    path = os.path.join(REDACTED_DIR, "manifest.json")
    if not os.path.exists(path):
        raise SystemExit(
            f"ERROR: no redacted corpus at {REDACTED_DIR}.\n"
            "  Fix: python3 tools/redact_corpus.py (make runs it first)."
        )
    with open(path) as fh:
        manifest = json.load(fh)
    if manifest.get("config") != corpus_config() or manifest.get("rules") != redact.rules_digest():
        raise SystemExit(
            "ERROR: the redacted corpus was built under a different config or rules.\n"
            "  Fix: python3 tools/redact_corpus.py (make runs it first)."
        )
    return manifest


def allowlist():
    """The project allowlist. Every transcript dir name starts with '-', so an
    entry of only dashes, or a bare string split into characters, would match
    every project: refuse both."""
    value = config_required("included_project_substrings")
    if not isinstance(value, list) or not all(isinstance(s, str) and s.strip("-") for s in value):
        raise SystemExit(
            "ERROR: 'included_project_substrings' must be a list of transcript-dir"
            f" substrings, each with more than dashes. Got: {value!r}"
        )
    return tuple(value)


def corpus_config():
    """The settings that decide which sessions are in the redacted corpus."""
    return {
        "included_project_substrings": list(allowlist()),
        "excluded_project_substrings": sorted(EXCLUDED_PROJECT_SUBSTRINGS),
        "excluded_sessions": sorted(EXCLUDED_SESSIONS),
        "extra_roots": CONFIG["extra_roots"],
        "extra_roots_env": os.environ.get("CSA_EXTRA_ROOTS", ""),
    }


def iter_raw_transcripts(allowlisted=True):
    """Yield (project_dir, path) for every top-level session transcript.

    Sessions are deduped by filename across machines: transcript names are UUIDs,
    so a repeat means the same session was collected twice, not two sessions.
    Only redact_corpus.py and first-run setup read these.
    """
    allow = allowlist() if allowlisted else ()
    seen = set()
    for label, base in corpus_roots():
        for root, _dirs, files in os.walk(base):
            # Subagent transcripts live in subagents/; they are not the user talking.
            if os.path.basename(root) == "subagents":
                continue
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, base)
                project = rel.split(os.sep)[0]
                if allowlisted and not any(s in project for s in allow):
                    continue
                if any(s in project for s in EXCLUDED_PROJECT_SUBSTRINGS):
                    continue
                if any(s in name for s in EXCLUDED_SESSIONS):
                    continue
                if name in seen:
                    continue
                seen.add(name)
                yield (project if label == "local" else f"{label}/{project}"), path


def read_jsonl(path):
    with open(path, errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def text_of(content):
    """Flatten a message content field to plain text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            out.append(block.get("text") or "")
    return "\n".join(out)


def is_real_prompt(entry):
    """True for a message the user actually typed, not a tool result or system inject."""
    if entry.get("type") != "user":
        return False
    if entry.get("isSidechain"):
        return False
    if entry.get("toolUseResult") is not None:
        return False
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        # A tool_result-only turn carries no typed text.
        if not any(
            isinstance(b, dict) and b.get("type") == "text" for b in content
        ):
            return False
    body = text_of(content).strip()
    if not body:
        return False
    # Harness injections and command scaffolding, not typed input.
    if body.startswith("<") or body.startswith("Caveat:"):
        return False
    # The harness writes these when the user hits stop. They are not messages - they
    # carry no words to judge - but 65 of them were being counted as prompts and
    # generating candidates with an empty quote.
    if INTERRUPT_MARKER.match(body):
        return False
    return True


INTERRUPT_MARKER = re.compile(r"^\[Request interrupted by user")


def is_interrupt(entry):
    """The user hit stop. Not a message, but a signal: they cut Claude off mid-action,
    which they would rarely bother to complain about afterwards."""
    if entry.get("type") != "user" or entry.get("isSidechain"):
        return False
    if entry.get("toolUseResult") is not None:
        return False
    body = text_of((entry.get("message") or {}).get("content")).strip()
    return bool(INTERRUPT_MARKER.match(body))


def tool_uses(entry):
    """Yield (name, input_dict) for tool_use blocks in an assistant entry."""
    message = entry.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block.get("name") or "", block.get("input") or {}


# The user saying they already told us. Judges instructed to set
# repeat_after_instruction only when the window proves it read that
# conservatively and miss real repeats, including ones stated outright. Repeats
# gate the rule bar, so missing them decides which patterns become rules.
#
# Every marker must name a prior communication act or an ongoing failure. Bare
# "stop" is excluded: "Actually, stop. Give me a synopsis" is abandonment, not
# repetition.
REPEAT_MARKER = extendable_pattern(
    "repeat_marker",
    r"(",
    [
        "we[' ]?ve (talked|discussed|been over)",
        "we (talked|discussed) about (this|that|it)",
        # "i asked you" is a complaint; "what i asked for" is a noun phrase.
        # Require the addressee for told/asked, and keep the intransitive verbs
        # separate.
        r"\bi (already |just )?(told|asked) you\b",
        r"\bi (already |just )?(said|mentioned|explained|stated)\b",
        "which is why i said",
        "as i (said|asked|mentioned|explained)", "like i said",
        # Must be a complaint, not a reference. "a synopsis of what i asked for"
        # is a request; "that isnt what i asked for" is the user repeating
        # themselves.
        r"(that|this) (is|isn'?t|'?s not) what i asked",
        "not what i (asked|wanted|said)",
        r"you keep\b", r"you (still|continue to)\b",
        r"still (do not|don'?t|doesn'?t|not)\b",
        "stop referencing", "stop (doing|trying|using)",
        r"i'?ve (said|told|asked)",
        "per (my|our) (earlier|previous|last)",
        "(said|told|asked|mentioned) (you |this |that )?(before|earlier|already)",
        "how many times",
    ],
    r")",
)


def says_repeat(text):
    """True when the user's own words say they had already told us."""
    return bool(REPEAT_MARKER.search(text or ""))


# Not user language: lines a tool appends to every commit. Extendable all the
# same, because which tools append what differs per setup (Change-Id, Reviewed-by).
TRAILER = extendable_pattern(
    "commit_trailers",
    r"^\s*(",
    [
        "Co-Authored-By", "Co-authored-by", "Claude-Session", "Generated with",
        "Signed-off-by", r"https://claude\.ai/code", "🤖",
    ],
    r")",
)


def strip_trailers(text):
    """Drop harness-mandated trailers. They are not authored prose, and leaving
    them on one side of a comparison makes every commit look rewritten."""
    kept = [l for l in (text or "").splitlines() if not TRAILER.match(l)]
    return "\n".join(kept).strip()


USER_ROLE = "user"


def is_user_turn(turn):
    """True for a turn the person typed, as opposed to the assistant."""
    return turn.get("role") == USER_ROLE


def ensure_findings_dir():
    os.makedirs(FINDINGS, exist_ok=True)
    return FINDINGS


def write_jsonl(path, rows):
    with open(path, "w") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)
