# claude-session-analyzer pipeline.
#
# `make` (or `make all`) runs the full pipeline to findings/report.html.
# Stages that need a model read batches from findings/*_in/ and expect verdicts
# back in findings/*_out/; judge_batches.py drives that pass through Ollama.
#
# Variables:
#   MODEL        Ollama model used for the judging passes (default minimax-m3:cloud)
#   CHAT_EXPORT  path to an unzipped claude.ai export, if you have one (optional)
#   PY           python3

MODEL ?= minimax-m3:cloud
PY    ?= python3
F     := findings
T     := tools

.PHONY: all redact extract corroborate judge reduce report status test clean help FORCE

all: $(F)/report.html

help:
	@echo "Targets:"
	@echo "  all          run the full pipeline to findings/report.html"
	@echo "  redact       write redacted copies of the allowlisted transcripts"
	@echo "  extract      pull raw material out of the corpus"
	@echo "  corroborate  check what the model wrote against what survived"
	@echo "  judge        batch out for classification and merge verdicts back"
	@echo "  reduce       collapse duplicates and lift the result into rules"
	@echo "  report       build findings/report.html"
	@echo "  status       report which outputs are stale (exits 1 if any)"
	@echo "  test         run the test suite"
	@echo "  clean        remove findings/"
	@echo ""
	@echo "Variables: MODEL=$(MODEL)  CHAT_EXPORT=$(CHAT_EXPORT)"

# ---------------------------------------------------------------------------
# Redact
# ---------------------------------------------------------------------------

# Always runs; the stamp changes only when a redacted copy did, so extract
# reruns exactly when there is new material.
$(F)/_meta/corpus.stamp: FORCE
	$(PY) $(T)/redact_corpus.py

redact: $(F)/_meta/corpus.stamp

FORCE:

# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------

$(F)/candidates_transcripts.jsonl: $(F)/_meta/corpus.stamp
	$(PY) $(T)/extract_transcripts.py

# Always runs; the stamp changes only when the export did.
$(F)/_meta/export.stamp: FORCE
	@test -n "$(CHAT_EXPORT)" || { echo "set CHAT_EXPORT to your claude.ai export dir"; exit 1; }
	$(PY) $(T)/stamp_export.py $(CHAT_EXPORT)

$(F)/candidates_chat.jsonl: $(F)/_meta/corpus.stamp $(F)/_meta/export.stamp
	@test -n "$(CHAT_EXPORT)" || { echo "set CHAT_EXPORT to your claude.ai export dir"; exit 1; }
	$(PY) $(T)/extract_chat.py $(CHAT_EXPORT)

$(F)/interrupts.jsonl: $(F)/_meta/corpus.stamp
	$(PY) $(T)/extract_interrupts.py

$(F)/artifacts_authored.jsonl: $(F)/_meta/corpus.stamp
	$(PY) $(T)/extract_artifacts.py

$(F)/code_comments.jsonl: $(F)/_meta/corpus.stamp
	$(PY) $(T)/extract_code_comments.py

extract: $(F)/candidates_transcripts.jsonl $(F)/interrupts.jsonl \
	$(F)/artifacts_authored.jsonl $(F)/code_comments.jsonl
ifneq ($(CHAT_EXPORT),)
extract: $(F)/candidates_chat.jsonl
endif

# ---------------------------------------------------------------------------
# Corroborate
# ---------------------------------------------------------------------------

$(F)/git_landed.jsonl: $(F)/artifacts_authored.jsonl
	$(PY) $(T)/collect_git.py

$(F)/prs.jsonl:
	$(PY) $(T)/collect_prs.py

$(F)/style_comparison.json: $(F)/git_landed.jsonl
	$(PY) $(T)/compare_style.py

$(F)/rewrites.jsonl: $(F)/artifacts_authored.jsonl $(F)/git_landed.jsonl $(F)/prs.jsonl
	$(PY) $(T)/match_rewrites.py

$(F)/comment_survival.json: $(F)/code_comments.jsonl
	$(PY) $(T)/detect_comment_rewrites.py

$(F)/comment_outcomes.jsonl: $(F)/comment_survival.json
	@touch $@

corroborate: $(F)/style_comparison.json $(F)/rewrites.jsonl \
	$(F)/comment_survival.json $(F)/comment_outcomes.jsonl

# ---------------------------------------------------------------------------
# Judge
# ---------------------------------------------------------------------------

$(F)/triage_in/.done: $(F)/candidates_transcripts.jsonl
	$(PY) $(T)/make_triage_batches.py
	@touch $@

$(F)/triage_out/.done: $(F)/triage_in/.done
	$(PY) judge_batches.py $(MODEL) $(F)/triage_in $(F)/triage_out $(T)/triage_rubric.md
	@touch $@

$(F)/triaged.jsonl: $(F)/triage_out/.done
	$(PY) $(T)/merge_triage.py

$(F)/judge_in/.done: $(F)/triaged.jsonl
	@touch $@

$(F)/judge_out/.done: $(F)/judge_in/.done
	$(PY) judge_batches.py $(MODEL) $(F)/judge_in $(F)/judge_out $(T)/judge_rubric.md
	@touch $@

$(F)/comment_in/.done: $(F)/code_comments.jsonl
	$(PY) $(T)/make_comment_batches.py
	@touch $@

$(F)/comment_out/.done: $(F)/comment_in/.done
	$(PY) judge_batches.py $(MODEL) $(F)/comment_in $(F)/comment_out $(T)/comment_rubric.md
	@touch $@

$(F)/comment_summary.json: $(F)/comment_out/.done
	$(PY) $(T)/summarize_comments.py

judge: $(F)/triaged.jsonl $(F)/judge_out/.done $(F)/comment_summary.json

# ---------------------------------------------------------------------------
# Reduce
# ---------------------------------------------------------------------------

$(F)/incidents.jsonl: $(F)/judge_out/.done $(F)/triaged.jsonl
	$(PY) $(T)/dedupe_incidents.py

$(F)/preference_rules.jsonl: $(F)/incidents.jsonl
	@touch $@

$(F)/judged.jsonl: $(F)/incidents.jsonl
	@touch $@

$(F)/problem_in/.done: $(F)/incidents.jsonl
	$(PY) $(T)/make_problem_batches.py
	@touch $@

$(F)/problem_out/.done: $(F)/problem_in/.done
	$(PY) judge_batches.py $(MODEL) $(F)/problem_in $(F)/problem_out $(T)/problem_rubric.md
	@touch $@

$(F)/problems.jsonl: $(F)/problem_out/.done $(F)/incidents.jsonl
	$(PY) $(T)/merge_problems.py

$(F)/behavior_in/.done: $(F)/problems.jsonl $(F)/incidents.jsonl
	$(PY) $(T)/make_behavior_batch.py
	@touch $@

$(F)/behavior_out/.done: $(F)/behavior_in/.done
	$(PY) judge_batches.py $(MODEL) $(F)/behavior_in $(F)/behavior_out $(T)/behavior_rubric.md
	@touch $@

$(F)/behaviors.jsonl: $(F)/behavior_out/.done $(F)/behavior_in/.done \
	$(F)/problems.jsonl $(F)/incidents.jsonl
	$(PY) $(T)/merge_behaviors.py

$(F)/findings.jsonl: $(F)/incidents.jsonl $(F)/problems.jsonl $(F)/judge_out/.done
	$(PY) $(T)/rank_findings.py

$(F)/report.md: $(F)/findings.jsonl
	@touch $@

reduce: $(F)/incidents.jsonl $(F)/problems.jsonl $(F)/behaviors.jsonl \
	$(F)/findings.jsonl $(F)/report.md

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

$(F)/report.html: $(F)/findings.jsonl $(F)/problems.jsonl $(F)/behaviors.jsonl \
	$(F)/style_comparison.json $(F)/comment_survival.json $(F)/interrupts.jsonl \
	$(F)/preference_rules.jsonl $(F)/incidents.jsonl $(F)/judged.jsonl
	$(PY) $(T)/build_report.py

report: $(F)/report.html

# ---------------------------------------------------------------------------
# Check / clean
# ---------------------------------------------------------------------------

status:
	$(PY) $(T)/status.py

test:
	$(PY) -m unittest discover -s tests -v

clean:
	rm -rf $(F)
