# claude-session-analyzer

Sweeps your own Claude Code session transcripts and claude.ai chat exports for
the places you corrected the model, then triages, judges, dedupes, ranks and
reports on them. The output is an evidence base: which mistakes actually recur,
how often, and with what supporting quotes — as opposed to a list of rules
someone assumed would help.

It reads only your local corpus. Nothing is uploaded, and no findings are
committed.

## Setup

Python 3, standard library only, plus
[betterleaks](https://github.com/betterleaks/betterleaks) on `PATH`
(`brew install betterleaks`, or set `CSA_BETTERLEAKS`). `collect_prs.py` needs
the [`gh`](https://cli.github.com) CLI authenticated.

Run any tool with no config on a terminal and it will offer to build one,
deriving what it can and asking you to confirm: your commit emails from `git
config`, your login from `gh`, the date window from the earliest session in your
corpus, and the repo list by walking the directories your sessions ran in and
reading each `origin`. Nothing is written until you approve it.

If you would rather write it yourself, copy the template:

```sh
cp csa.config.example.json csa.config.json   # or ~/.claude-session-analyzer.json
```

### Where config comes from

Checked in order, first hit wins. The resolved path is printed on every run.

1. `--config PATH`
2. `CSA_CONFIG`
3. `./csa.config.json`
4. `~/.claude-session-analyzer.json`

| Key | Used by |
| --- | --- |
| `included_project_substrings` | every transcript reader — only projects whose transcript dir contains one of these are read. Required |
| `author_emails` | `compare_style` — which commits you hand-wrote |
| `pr_repos`, `pr_author` | `collect_prs` |
| `window_start` | `collect_git`, `collect_prs` |
| `extra_roots` | corpora copied from other machines |
| `excluded_sessions` | session ids to leave out |
| `excluded_project_substrings` | projects to skip entirely |
| `artifact_excluded_projects` | projects whose commits/PRs/comments are not evidence, though their transcripts still are |
| `project_label_strip` | cosmetic prefixes to drop from project names in the report |

A tool that needs a value you have not set stops with an error naming the key.
It will not fall back to a default and sweep the wrong corpus.

**In a non-interactive context** — an agent, cron, CI — nothing ever prompts. A
missing value is the same hard error, naming the key and the files that were
checked.

## Running the pipeline

`make` (or `make all`) runs the whole pipeline to `findings/report.html`. Each
stage is a file target carrying its real dependencies, so only stale outputs
rebuild. The model passes read batches from `findings/*_in/` and expect verdicts
back in `findings/*_out/`, driven by `judge_batches.py` through Ollama or `claude -p`.

```sh
make            # full pipeline to findings/report.html
make status     # report which outputs are stale (exits 1 if any)
make clean      # remove findings/
```

The judging passes need a model. `MODEL` overrides it (default
`minimax-m3:cloud`). It is an Ollama name, `ollama:<name>`, or
`claude:<alias>` (runs `claude -p`, needs claude 2.1.248 or newer). A `:cloud`
Ollama model is refused unless `ALLOW_CLOUD=1`:

```sh
make MODEL=some-other-model
make MODEL=claude:haiku
```

If you have a claude.ai export, point `CHAT_EXPORT` at the unzipped directory to
include it; otherwise that stage is skipped.

```sh
make CHAT_EXPORT=/path/to/export
```

To run one stage at a time: `make extract`, `make corroborate`, `make judge`,
`make reduce`, `make report`.

## Backends and privacy

- `ollama:` cloud models (a `:cloud` or `-cloud` tag) are refused unless `ALLOW_CLOUD=1`.
- `claude:` sends the redacted batches to Anthropic. Choosing it is the opt-in to upload.
- `OLLAMA_HOST` can point off-machine; check it before a run.
- `claude -p` runs with `--setting-sources project`. Check how your project hooks behave before a real run.
- `make clean` keeps `findings/gold/` (redacted conversation text at rest). `make clean-all` removes it too.

## How it works

The pipeline runs in stages, each writing to `findings/`. Stages that need a
model read a batch of JSON from `findings/*_in/` and expect the verdicts back in
`findings/*_out/` — the judging itself is done by whatever model you point at
those batches, not by this repo.

**Redact** — before anything reads a transcript or a claude.ai chat export.

| Tool | Writes |
| --- | --- |
| `redact_corpus.py` | `redacted/<project>/<session>.jsonl`, `redacted/manifest.json`, `_meta/corpus.stamp` |
| `redact.py --self-test` | nothing; run by `redact_corpus.py` every time |

Transcripts hold whatever a tool printed, keys and tokens included. Every later
stage clips text, and a clipped secret matches no rule, so `redact_corpus.py`
scans each allowlisted session whole with betterleaks (decoded string values,
each with its key) and writes a copy with every finding replaced by
`[REDACTED:<rule>]`. Dict keys are scanned and replaced too. An entry whose
finding has no literal to replace (found after base64 decoding) is dropped.
Readers stop if the corpus was built under a different allowlist or rules. Every reader lists these copies, never the
raw corpus (first-run setup is the one exception: it reads each raw session's
cwd and first date to suggest config values). A copy is rebuilt only when its
session, the rules or the redaction code changed, and
the stamp moves only when a copy did, so `make` re-extracts only when there is
something new. Rules: `tools/redact.toml` (the betterleaks defaults plus a few
for shapes they miss). Validation is off, so no secret is sent anywhere to be
checked.

A claude.ai chat export is redacted the same way before it is extracted:
`extract_chat.py` runs each message and the conversation title through the
redactor before any clip, so a secret cut at a clip boundary cannot reach the
judge or sit in findings. `candidates_chat.jsonl` is rebuilt when the rules or
the export change.

`judge_batches.py` scans every batch again before the first model call and
stops the run, with nothing sent, on any finding. It caches verdicts by model,
rubric and item content, so a re-run sends only items the model has not seen.
An item whose verdict comes back with a missing or invented id is asked once
more, then left out (and asked again next run), never paired by position.

**Extract** — pull raw material out of the corpus.

| Tool | Writes |
| --- | --- |
| `extract_transcripts.py` | `candidates_transcripts.jsonl` |
| `extract_chat.py <export-dir>` | `candidates_chat.jsonl` |
| `extract_interrupts.py` | `interrupts.jsonl` |
| `extract_artifacts.py` | `artifacts_authored.jsonl` |
| `extract_code_comments.py` | `code_comments.jsonl` |

**Corroborate** — check what the model wrote against what survived.

| Tool | Writes |
| --- | --- |
| `collect_git.py` | `git_landed.jsonl` |
| `collect_prs.py [--refresh]` | `prs.jsonl` |
| `match_rewrites.py` | `rewrites.jsonl` |
| `compare_style.py` | `style_comparison.json` |
| `detect_comment_rewrites.py` | `comment_survival.json`, `comment_outcomes.jsonl` |

**Judge** — batch out for classification, then merge the verdicts back.

| Tool | Writes |
| --- | --- |
| `make_triage_batches.py [size]` | `triage_in/batch_NNN.json` |
| `merge_triage.py [--min-confidence N]` | `triaged.jsonl`, `judge_in/` |
| `make_comment_batches.py [size]` | `comment_in/batch_NNN.json` |
| `summarize_comments.py` | `comment_summary.json` |

**Reduce** — collapse duplicates, then lift the result into rules.

| Tool | Writes |
| --- | --- |
| `dedupe_incidents.py [--gap N]` | `incidents.jsonl` |
| `make_problem_batches.py [--min N]` | `problem_in/<kind>.json` |
| `merge_problems.py` | `problems.jsonl` |
| `make_behavior_batch.py` | `behavior_in/all.json` |
| `merge_behaviors.py` | `behaviors.jsonl`, `behaviors_unassigned.jsonl` |
| `rank_findings.py` | `findings.jsonl`, `report.md` |
| `build_report.py` | `report.html` |

**Check** — `status.py` walks the stage graph and names any output older than its
inputs, with the command that rebuilds it. Exits 1 if anything is stale, so it
can gate a workflow rather than just inform. An output that no longer reflects
the data looks exactly like one that does.

### Why there are three levels above an episode

    kind  ->  problem  ->  behavior

A **kind** is a category. It has no checkable action in it, so no rule can attach
to it — "unverified premise" is a label, not an instruction.

A **problem** is rule-shaped but scoped to the situation it came from. Problems
are grouped within a single kind, which is what keeps them tight, and also what
makes their rules read like the session they came from: *"read the CI workflow
before assuming it applies"* is true and does not survive contact with a
different project.

A **behavior** is the standing instruction, found by grouping every rule
candidate at once with the kinds removed as a boundary. That matters because the
clusters that are actually rule-shaped cut *across* kinds: stating something from
memory instead of reading the source appears under one kind in one session and a
different one in the next, and grouping inside a kind can never see them
together. Behaviors are what a ruleset loads; the problem rules stay underneath
as worked examples and as a regression set to check a candidate rule against.

## Would a rule have caught anything?

`backtest_rules.py` replays a candidate enforcement rule over the corpus and asks
whether it would have fired, and whether it would have been right. It is **not**
part of the report: it answers a question you ask while designing a rule, not one
you ask every time the findings are rebuilt.

```sh
cp hook_rules.example.json hook_rules.json     # then edit
python3 tools/backtest_rules.py
python3 tools/backtest_rules.py --sweep chars  # try every threshold, don't guess one
```

Rules are declared in JSON rather than written as code, so adding one is editing
a file:

```json
{
  "name": "commit body over 400 chars",
  "target": "written_artifacts",
  "label": "rewritten",
  "applies_to": {"kind": {"eq": "commit_message"}, "landed": {"eq": true}},
  "when": {"chars": {"gt": 400}}
}
```

`applies_to` is the population the rule could ever fire within, and sets the base
rate. `when` is the trigger. Keeping them separate matters: scoping the trigger
while measuring the base rate over everything scored that exact rule at 1.42x
when its real value is 1.00x.

**Read the `lift` column**, not precision. Lift is precision over the base rate;
1.00x means the rule fires without predicting anything. `fires_per_week` is the
other one that decides whether a rule is usable — a precise rule that fires
twelve times a week gets switched off.

Labels are proxies for "this was a real problem": `rewritten` (you changed the
body), `shrunk` (a shorter version replaced it), `not_survived` (it is not in the
file now). You do not rewrite everything that is wrong, so they understate
precision — which is another reason to read lift, since that bias lands on the
base rate too.

## Output location

`findings/` is created under the current working directory. Override per-run
with `--output PATH` (or `-o`), or for a whole shell with `CSA_FINDINGS`.

It contains verbatim excerpts of your sessions, your repos and your own rulings
on them. It is gitignored here, but treat it as sensitive wherever it lands.

## Tuning the language patterns

Six patterns match text rather than structure. Four are how one person writes in
English when they are annoyed; two are lines that tooling appends. All are
**finders** — their hits get reviewed, never treated as verdicts — and all take
extra terms from a single `extra_patterns` object:

```json
"extra_patterns": {
  "profanity": ["bruh", "yikes"],
  "repeat_marker": ["for the third time", "re:how many (more )?times"]
}
```

| Key under `extra_patterns` | Matches |
| --- | --- |
| `profanity` | swearing and exasperation |
| `negation_lead` | a message that opens with a correction |
| `correction_phrase` | a correction anywhere in the message |
| `repeat_marker` | "I already told you" — this one gates the rule bar |
| `commit_trailers` | trailers stripped before commits are compared |
| `comment_skip` | machine directives in code, which are not prose |

Each list **adds** alternatives; the built-in terms always stay in. Entries are
literal phrases and are escaped for you, bounded so they will not fire inside a
longer word — `"bruh"` matches `bruh` but not `bruhaha`. Prefix an entry with
`re:` for a raw regex fragment.

Anything wrong here stops the run and names where it came from: an unknown key,
an empty entry, a bad regex fragment. The failure this avoids is a term that is
accepted, never matched, and reported as a clean run.

`repeat_marker` is the one worth spending time on. Whether a failure counts
as noncompliant — an instruction already existed and was broken anyway — is what
separates a pattern that needs a standing rule from one a single correction
fixes. Miss the way you phrase "I already told you" and those failures rank as
first offences.

## Caveats worth knowing

- Redaction is rule-based. A secret in a format no rule knows (an in-house
  token, a bespoke connection string) passes through. Add a rule to
  `tools/redact.toml` and a case to the self-test in `tools/redact.py`.
- The patterns above are finders, not measurements. Precision was hand-checked
  once at 47% on one corpus, which is why every hit goes to review.
- Everything derived from complaints undercounts failures you absorbed silently.
  The interrupt and comment-survival signals exist because they need no
  complaint to fire.

## License

MIT — see [LICENSE](LICENSE).
