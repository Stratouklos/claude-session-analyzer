"""End-to-end checks for redaction, the project allowlist and incremental judging.

Each test builds a corpus under a temporary HOME and runs the real tools as
subprocesses. The model is a local stub that records every item it is sent.
"""

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import redact  # noqa: E402

WORK = "-Users-x-Code-work"
PERSONAL = "-Users-x-Personal"


class StubModel(http.server.BaseHTTPRequestHandler):
    """A model that records what it is sent. `mode` makes it misbehave the way
    real models do: drop the last item, or invent an id for it."""

    seen = []
    payloads = []
    mode = "ok"

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        payload = json.loads(body["prompt"].rsplit("\n\n", 1)[1])
        StubModel.payloads.append(payload)
        if isinstance(payload, dict):
            verdicts = {"groups": [{"name": "stub", "size": len(payload)}]}
        else:
            StubModel.seen.extend(it["id"] for it in payload)
            verdicts = [{"id": it["id"], "is_correction": True, "confidence": 0.9} for it in payload]
            if StubModel.mode == "drop_last":
                verdicts = verdicts[:-1]
            elif StubModel.mode == "fake_id":
                verdicts[-1]["id"] = "ghost:000000000000"
        reply = json.dumps({"response": json.dumps(verdicts)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass


def read(path):
    with open(path) as fh:
        return fh.read()


def session(secret, correction, tool_input=None):
    """A session where the model works, then the user corrects it."""
    sid = str(uuid.uuid4())
    base = {"sessionId": sid, "cwd": "/Users/x/Code/work", "gitBranch": "main"}

    def entry(kind, content, ts, **extra):
        return {**base, "type": kind, "uuid": uuid.uuid4().hex, "timestamp": ts,
                "message": {"role": "assistant" if kind == "assistant" else "user",
                            "content": content}, **extra}

    lines = [
        entry("user", "deploy the api to staging", "2026-11-01T10:00:00Z"),
        entry("assistant", [
            {"type": "text", "text": "Deploying now. " + "Checking the rollout. " * 120},
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": tool_input or {"command": f"curl -H 'Authorization: token {secret}' https://api.example.com"}},
        ], "2026-11-01T10:00:05Z"),
        entry("user", correction, "2026-11-01T10:01:00Z"),
        entry("assistant", [{"type": "text", "text": "Understood, switching to staging."}],
              "2026-11-01T10:01:05Z"),
    ]
    return sid, "\n".join(json.dumps(line) for line in lines) + "\n"


class Pipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), StubModel)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="csa-test-")
        self.home = os.path.join(self.tmp, "home")
        self.projects = os.path.join(self.home, ".claude", "projects")
        self.findings = os.path.join(self.tmp, "findings")
        self.config = os.path.join(self.tmp, "config.json")
        self.write_config([WORK])
        self.secret = redact.canaries()["github"]
        StubModel.seen = []
        StubModel.payloads = []
        StubModel.mode = "ok"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, allow):
        with open(self.config, "w") as fh:
            json.dump({"included_project_substrings": allow}, fh)

    def add_session(self, project, correction="no, that's wrong, use the staging cluster instead",
                    tool_input=None):
        sid, body = session(self.secret, correction, tool_input)
        path = os.path.join(self.projects, project, f"{sid}.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(body)
        return sid, path

    def run_tool(self, *args, check=True):
        env = dict(os.environ, HOME=self.home, CSA_CONFIG=self.config,
                   CSA_FINDINGS=self.findings,
                   OLLAMA_HOST=f"http://127.0.0.1:{self.server.server_port}")
        proc = subprocess.run([sys.executable, *args], cwd=REPO, env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if check and proc.returncode != 0:
            self.fail(f"{args} exited {proc.returncode}:\n{proc.stdout}\n{proc.stderr}")
        return proc

    def judge(self, batches, check=True):
        """Write `batches` ({name: content}) to triage_in and judge them."""
        in_dir = os.path.join(self.findings, "triage_in")
        os.makedirs(in_dir, exist_ok=True)
        for name, content in batches.items():
            with open(os.path.join(in_dir, name), "w") as fh:
                json.dump(content, fh)
        return self.run_tool("judge_batches.py", "stub", in_dir, f"{self.findings}/triage_out",
                             "tools/triage_rubric.md", check=check)

    def judge_triage(self):
        self.run_tool("tools/make_triage_batches.py")
        return self.run_tool("judge_batches.py", "stub", f"{self.findings}/triage_in",
                             f"{self.findings}/triage_out", "tools/triage_rubric.md")

    def candidates(self):
        path = os.path.join(self.findings, "candidates_transcripts.jsonl")
        with open(path) as fh:
            return [json.loads(line) for line in fh]

    def all_findings_text(self):
        chunks = []
        for root, _dirs, files in os.walk(self.findings):
            for name in files:
                with open(os.path.join(root, name), errors="ignore") as fh:
                    chunks.append(fh.read())
        return "\n".join(chunks)

    def add_chat_export(self, text):
        """An unzipped claude.ai export with one conversation: a long reply, then `text`."""
        export = os.path.join(self.tmp, "export")
        os.makedirs(export)
        msgs = [
            {"uuid": uuid.uuid4().hex, "sender": "human", "created_at": "2026-11-01T10:00:00Z",
             "text": "deploy the api to staging"},
            {"uuid": uuid.uuid4().hex, "sender": "assistant", "created_at": "2026-11-01T10:00:05Z",
             "text": "Deploying now. " + "Checking the rollout. " * 120},
            {"uuid": uuid.uuid4().hex, "sender": "human", "created_at": "2026-11-01T10:01:00Z",
             "text": text},
        ]
        conv = {"uuid": uuid.uuid4().hex, "name": "deploy chat", "chat_messages": msgs}
        with open(os.path.join(export, "conversations.json"), "w") as fh:
            json.dump([conv], fh)
        return export

    def test_chat_export_is_redacted_before_it_is_extracted(self):
        export = self.add_chat_export(f"no, that's wrong, use token {self.secret} instead")
        self.run_tool("tools/extract_chat.py", export)
        with open(os.path.join(self.findings, "candidates_chat.jsonl")) as fh:
            self.assertTrue(fh.read().strip(), "the correction must be extracted")
        text = self.all_findings_text()
        self.assertNotIn(self.secret, text)
        self.assertNotIn(self.secret[:20], text)
        self.assertIn("[REDACTED:", text)

    def test_chat_token_at_the_clip_boundary_never_reaches_the_model(self):
        # The judge clips the prompt at 1400, ahead of the 1800 extract clip: straddle the one that bites.
        pad = "no, that's wrong, " + "x" * (1400 - 10 - len("no, that's wrong, "))
        export = self.add_chat_export(f"{pad}{self.secret} and more")
        self.run_tool("tools/extract_chat.py", export)
        self.judge_triage()
        self.assertTrue(StubModel.seen, "the candidate must be judged")
        sent = json.dumps(StubModel.payloads)
        for n in (8, 20, len(self.secret)):
            self.assertNotIn(self.secret[:n], sent)
        self.assertNotIn(self.secret, self.all_findings_text())

    def write_export(self, msgs, name="deploy chat"):
        """An unzipped export with one conversation of (sender, text) messages."""
        export = os.path.join(self.tmp, "export")
        os.makedirs(export, exist_ok=True)
        chat = [{"uuid": uuid.uuid4().hex, "sender": who, "created_at": f"2026-11-01T10:0{i}:00Z", "text": text}
                for i, (who, text) in enumerate(msgs)]
        with open(os.path.join(export, "conversations.json"), "w") as fh:
            json.dump([{"uuid": uuid.uuid4().hex, "name": name, "chat_messages": chat}], fh)
        return export

    def chat_candidates(self):
        with open(os.path.join(self.findings, "candidates_chat.jsonl")) as fh:
            return [json.loads(line) for line in fh]

    def test_chat_secret_in_assistant_reply_and_title_is_redacted(self):
        export = self.write_export(
            [("human", "deploy the api"),
             ("assistant", f"Use token {self.secret} to deploy."),
             ("human", "no, that's wrong, use the staging cluster instead")],
            name=f"deploy with {self.secret}")
        self.run_tool("tools/extract_chat.py", export)
        found = self.chat_candidates()
        self.assertEqual(len(found), 1)
        self.assertNotIn(self.secret[:20], self.all_findings_text())
        self.assertIn("[REDACTED:", found[0]["window"][1]["text"])
        self.assertIn("[REDACTED:", found[0]["conversation"])

    def test_chat_message_dropped_by_redaction_keeps_turns_coherent(self):
        pem = redact.canaries()["pem_base64"]
        export = self.write_export(
            [("human", "deploy the api"),
             ("assistant", f"here is the key {pem}"),
             ("human", "no, that's wrong, use the staging cluster instead"),
             ("assistant", "Understood.")])
        self.run_tool("tools/extract_chat.py", export)
        self.assertNotIn(pem[:40], self.all_findings_text())
        found = self.chat_candidates()
        self.assertEqual(len(found), 1)
        cand = found[0]
        self.assertEqual(cand["prompt"], "no, that's wrong, use the staging cluster instead")
        self.assertEqual(len(cand["window"]), 3, "the dropped reply leaves three turns")
        self.assertEqual(cand["window"][cand["turn"]]["text"], cand["prompt"])
        self.assertEqual(cand["preceding_output_chars"], 0)
        self.assertEqual(cand["turns_before_next_prompt"], None)

    def test_clean_chat_is_left_unchanged(self):
        reply = "Deploying now. " + "Checking the rollout. " * 20
        correction = "no, that's wrong, use the staging cluster instead"
        export = self.write_export([("human", "deploy the api"), ("assistant", reply), ("human", correction)],
                                   name="deploy chat")
        self.run_tool("tools/extract_chat.py", export)
        cand = self.chat_candidates()[0]
        self.assertEqual(cand["prompt"], correction)
        self.assertEqual(cand["conversation"], "deploy chat")
        self.assertEqual(cand["window"][1]["text"], reply)
        self.assertEqual(cand["preceding_output_chars"], len(reply))
        self.assertNotIn("[REDACTED", self.all_findings_text())

    def test_export_stamp_changes_only_when_the_export_does(self):
        export = self.write_export([("human", "deploy the api")])
        stamp = os.path.join(self.findings, "_meta", "export.stamp")
        self.run_tool("tools/stamp_export.py", export)
        self.assertEqual(os.stat(stamp).st_mode & 0o777, 0o600)
        os.utime(stamp, ns=(1, 1))
        self.run_tool("tools/stamp_export.py", export)
        self.assertEqual(os.stat(stamp).st_mtime_ns, 1, "unchanged export leaves the stamp alone")
        self.write_export([("human", "deploy the api to production")])
        self.run_tool("tools/stamp_export.py", export)
        self.assertNotEqual(os.stat(stamp).st_mtime_ns, 1, "changed export rewrites the stamp")

    def test_export_stamp_needs_conversations_json(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        proc = self.run_tool("tools/stamp_export.py", empty, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(os.path.join(empty, "conversations.json"), proc.stderr)

    def test_make_reextracts_chat_only_when_the_export_changed(self):
        self.add_session(WORK)
        export = self.write_export([("human", "deploy the api"), ("assistant", "ok"),
                                    ("human", "no, that's wrong, use the staging cluster instead")])
        out = os.path.join(self.findings, "candidates_chat.jsonl")
        env = dict(os.environ, HOME=self.home, CSA_CONFIG=self.config, CSA_FINDINGS=self.findings)

        def make():
            proc = subprocess.run(
                ["make", f"F={self.findings}", f"PY={sys.executable}", f"CHAT_EXPORT={export}", out],
                cwd=REPO, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return os.stat(out).st_mtime_ns

        built = make()
        self.assertEqual(make(), built, "an unchanged export is not re-extracted")
        self.write_export([("human", "deploy the api"), ("assistant", "ok"),
                           ("human", "no, that's wrong, use the production cluster instead")])
        self.assertGreater(make(), built, "a changed export is re-extracted")

    def make_chat(self, export, *extra):
        env = dict(os.environ, HOME=self.home, CSA_CONFIG=self.config, CSA_FINDINGS=self.findings)
        arg = [f"CHAT_EXPORT={export}"] if export else []
        return subprocess.run(
            ["make", f"F={self.findings}", f"PY={sys.executable}", *arg, *extra,
             os.path.join(self.findings, "candidates_chat.jsonl")],
            cwd=REPO, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_make_chat_rebuild_triggers_beyond_content_edits(self):
        self.add_session(WORK)
        msgs = [("human", "deploy the api"), ("assistant", "ok"),
                ("human", "no, that's wrong, use the staging cluster instead")]
        export = self.write_export(msgs)
        out = os.path.join(self.findings, "candidates_chat.jsonl")
        stamp = os.path.join(self.findings, "_meta", "export.stamp")
        proc = self.make_chat(export)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        built, stamped = os.stat(out).st_mtime_ns, os.stat(stamp).st_mtime_ns

        # The recipe echo, not an output mtime bump, proves a rebuild: it is free of filesystem mtime granularity.
        proc = self.make_chat(export)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(os.stat(stamp).st_mtime_ns, stamped, "unchanged export leaves export.stamp alone")
        self.assertEqual(os.stat(out).st_mtime_ns, built)
        self.assertNotIn("extract_chat.py", proc.stdout + proc.stderr, "unchanged export does not re-extract")

        conv = os.path.join(export, "conversations.json")
        size = os.stat(conv).st_size
        os.utime(conv, ns=(1_000_000_000, 1_000_000_000))
        self.assertEqual(os.stat(conv).st_size, size)
        # GNU make 3.81 compares mtimes at 1s granularity; cross a second so the stamp rewrite outranks the last build.
        time.sleep(1.1)
        proc = self.make_chat(export)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("extract_chat.py", proc.stdout + proc.stderr, "mtime-only change re-extracts")

        copy = os.path.join(self.tmp, "export-copy")
        shutil.copytree(export, copy)
        os.utime(os.path.join(copy, "conversations.json"), ns=(1_000_000_000, 1_000_000_000))
        time.sleep(1.1)
        proc = self.make_chat(copy)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("extract_chat.py", proc.stdout + proc.stderr, "same bytes, size and mtime at a new path re-extracts")

    def test_make_chat_without_export_dir_fails(self):
        proc = self.make_chat(None)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("CHAT_EXPORT", proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.findings, "_meta", "export.stamp")))

    def test_make_clean_keeps_gold_but_clean_all_removes_it(self):
        gold = os.path.join(self.findings, "gold")
        os.makedirs(gold, exist_ok=True)
        label = os.path.join(gold, "gold.jsonl")
        with open(label, "w") as fh:
            fh.write('{"id": "s:1", "labeled": true}\n')
        other = os.path.join(self.findings, "candidates_chat.jsonl")
        with open(other, "w") as fh:
            fh.write("{}\n")
        env = dict(os.environ, HOME=self.home, CSA_CONFIG=self.config, CSA_FINDINGS=self.findings)
        run = lambda target: subprocess.run(
            ["make", f"F={self.findings}", target], cwd=REPO, env=env,
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        r = run("clean")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(label), "clean keeps the hand-labeled gold set")
        self.assertFalse(os.path.exists(other), "clean removes rebuildable outputs")
        r = run("clean-all")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(label), "clean-all removes the gold set too")

    def test_raw_fixture_holds_a_detectable_secret(self):
        _sid, raw = self.add_session(WORK)
        with open(raw) as fh:
            self.assertIn(self.secret, fh.read())
        self.assertTrue(redact.scan([raw]), "the scanner must see the planted token")

    def test_no_secret_reaches_findings_or_the_model(self):
        self.add_session(WORK)
        self.run_tool("tools/redact_corpus.py")
        self.run_tool("tools/extract_transcripts.py")
        self.assertTrue(self.candidates(), "the correction must be extracted")
        self.judge_triage()
        self.assertTrue(StubModel.seen)
        text = self.all_findings_text()
        self.assertNotIn(self.secret, text)
        self.assertNotIn(self.secret[:20], text)
        self.assertIn("[REDACTED:", text)

    def test_gate_stops_an_unredacted_batch(self):
        batch_dir = os.path.join(self.findings, "triage_in")
        os.makedirs(batch_dir)
        with open(os.path.join(batch_dir, "batch_000.json"), "w") as fh:
            json.dump([{"id": "s:1", "user_said": f"use {self.secret}"}], fh)
        proc = self.run_tool("judge_batches.py", "stub", batch_dir,
                             f"{self.findings}/triage_out", "tools/triage_rubric.md", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("secret-shaped", proc.stdout + proc.stderr)
        self.assertEqual(StubModel.seen, [])

    def test_allowlist_excludes_other_projects(self):
        self.add_session(WORK)
        self.add_session(PERSONAL)
        self.run_tool("tools/redact_corpus.py")
        self.run_tool("tools/extract_transcripts.py")
        projects = {c["project"] for c in self.candidates()}
        self.assertEqual(projects, {WORK})

    def test_empty_allowlist_stops(self):
        self.write_config([])
        self.add_session(WORK)
        proc = self.run_tool("tools/redact_corpus.py", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("included_project_substrings", proc.stdout + proc.stderr)

    def merge_kept(self, verdicts, *flags):
        """Run merge_triage over hand-written verdicts; return (kept ids, judge_in batch sizes)."""
        os.makedirs(self.findings, exist_ok=True)
        with open(os.path.join(self.findings, "candidates_transcripts.jsonl"), "w") as fh:
            fh.write("".join(json.dumps({"id": v["id"], "window": []}) + "\n" for v in verdicts))
        os.makedirs(os.path.join(self.findings, "triage_out"))
        with open(os.path.join(self.findings, "triage_out", "batch_000.json"), "w") as fh:
            json.dump(verdicts, fh)
        self.run_tool("tools/merge_triage.py", *flags)
        judge_in = os.path.join(self.findings, "judge_in")
        batches = [json.loads(read(os.path.join(judge_in, n))) for n in sorted(os.listdir(judge_in))]
        return {m["id"] for b in batches for m in b}, [len(b) for b in batches]

    def test_merge_triage_keep_rule_and_batching_are_unchanged(self):
        # is_correction is truthiness, the cutoff is inclusive, and judge_in is sliced at 25.
        verdicts = [
            {"id": "t:truthy_int", "is_correction": 1, "confidence": 0.9},
            {"id": "t:truthy_str", "is_correction": "yes", "confidence": 0.9},
            {"id": "t:at_cutoff", "is_correction": True, "confidence": 0.3},
            {"id": "t:below", "is_correction": True, "confidence": 0.29},
            {"id": "t:mid", "is_correction": True, "confidence": 0.4},
            {"id": "t:no_conf", "is_correction": True},
            {"id": "t:false", "is_correction": False, "confidence": 0.99},
        ]
        ids, _ = self.merge_kept(verdicts)
        self.assertEqual(ids, {"t:truthy_int", "t:truthy_str", "t:at_cutoff", "t:mid"})
        shutil.rmtree(os.path.join(self.findings, "triage_out"))
        ids, _ = self.merge_kept(verdicts, "--min-confidence", "0.5")
        self.assertEqual(ids, {"t:truthy_int", "t:truthy_str"})

    def test_merge_triage_slices_judge_in_at_25(self):
        verdicts = [{"id": f"t:{i:03d}", "is_correction": True, "confidence": 0.9} for i in range(51)]
        _, sizes = self.merge_kept(verdicts)
        self.assertEqual(sizes, [25, 25, 1])

    def test_second_run_judges_only_new_sessions(self):
        self.add_session(WORK)
        self.run_tool("tools/redact_corpus.py")
        self.run_tool("tools/extract_transcripts.py")
        self.judge_triage()
        first = list(StubModel.seen)
        self.assertEqual(len(first), len(self.candidates()))

        StubModel.seen = []
        stamp = os.path.join(self.findings, "_meta", "corpus.stamp")
        before = read(stamp)
        self.run_tool("tools/redact_corpus.py")
        self.assertEqual(read(stamp), before, "an unchanged corpus keeps its stamp")
        self.run_tool("tools/extract_transcripts.py")
        self.judge_triage()
        self.assertEqual(StubModel.seen, [], "nothing new, nothing sent")

        new_sid, _ = self.add_session(WORK, correction="stop, don't touch prod, that is wrong")
        self.run_tool("tools/redact_corpus.py")
        self.assertNotEqual(read(stamp), before)
        self.run_tool("tools/extract_transcripts.py")
        self.judge_triage()
        self.assertTrue(StubModel.seen)
        self.assertTrue(all(i.startswith(new_sid) for i in StubModel.seen), StubModel.seen)
        triaged = [v for name in os.listdir(f"{self.findings}/triage_out") if name.endswith(".json")
                   for v in json.loads(read(f"{self.findings}/triage_out/{name}"))]
        self.assertEqual(len(triaged), len(self.candidates()), "old verdicts stay in the output")

    def test_object_batch_is_sent_whole(self):
        batch = {"kind": "bad_assumption", "episodes": [{"n": 1, "quote": "read the CI file first"}]}
        self.judge({"bad_assumption.json": batch})
        self.assertEqual(StubModel.payloads, [batch])
        out = json.loads(read(f"{self.findings}/triage_out/bad_assumption.json"))
        self.assertEqual(out, {"groups": [{"name": "stub", "size": 2}]})

    def test_misbehaving_model_keeps_matched_verdicts_and_resends_the_rest(self):
        items = [{"id": "s:1", "user_said": "no"}, {"id": "s:2", "user_said": "stop"}]
        for mode in ("drop_last", "fake_id"):
            with self.subTest(mode=mode):
                shutil.rmtree(self.findings, ignore_errors=True)
                StubModel.mode, StubModel.seen = mode, []
                self.judge({"batch_000.json": items})
                out = json.loads(read(f"{self.findings}/triage_out/batch_000.json"))
                self.assertEqual([v["id"] for v in out], ["s:1"], "only the paired verdict is kept")
                StubModel.mode, StubModel.seen = "ok", []
                self.judge({"batch_000.json": items})
                self.assertEqual(StubModel.seen, ["s:2"], "the unpaired item is asked again, nothing else")

    def test_gate_runs_before_any_model_call(self):
        proc = self.judge({"batch_000.json": [{"id": "s:1", "user_said": "fine"}],
                           "batch_001.json": [{"id": "s:2", "user_said": f"use {self.secret}"}]},
                          check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(StubModel.seen, [], "a dirty later batch stops the clean earlier one too")

    def test_truncated_cache_entry_is_a_miss(self):
        items = [{"id": "s:1", "user_said": "no"}]
        self.judge({"batch_000.json": items})
        cache_dir = os.path.join(self.findings, "_cache", "triage_out")
        for name in os.listdir(cache_dir):
            with open(os.path.join(cache_dir, name), "w") as fh:
                fh.write('{"id": "s:')
        StubModel.seen = []
        self.judge({"batch_000.json": items})
        self.assertEqual(StubModel.seen, ["s:1"])

    def test_allowlist_that_matches_everything_stops(self):
        self.add_session(WORK)
        for allow in (["-"], [""], "-Code-work"):
            with self.subTest(allow=allow):
                self.write_config(allow)
                proc = self.run_tool("tools/redact_corpus.py", check=False)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("included_project_substrings", proc.stdout + proc.stderr)

    def test_secret_used_as_a_dict_key_is_redacted(self):
        self.add_session(WORK, tool_input={"headers": {f"Authorization: token {self.secret}": True}})
        self.run_tool("tools/redact_corpus.py")
        self.assertNotIn(self.secret, self.all_findings_text())
        self.assertNotIn(self.secret[:20], self.all_findings_text())

    def test_lone_surrogate_does_not_stop_the_build(self):
        _sid, path = self.add_session(WORK)
        with open(path, "a") as fh:
            fh.write('{"type": "user", "uuid": "u1", "message": {"role": "user", "content": "cut \\ud83d here"}}\n')
        self.run_tool("tools/redact_corpus.py")
        self.run_tool("tools/extract_transcripts.py")

    def test_readers_refuse_a_manifest_from_another_config(self):
        self.add_session(WORK)
        self.run_tool("tools/redact_corpus.py")
        self.write_config([PERSONAL])
        proc = self.run_tool("tools/extract_transcripts.py", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("redact_corpus.py", proc.stdout + proc.stderr)

    def test_redacted_text_is_not_scored_as_a_rewrite(self):
        code = (
            "import sys, json; sys.path.insert(0, 'tools'); import match_rewrites as m;"
            "authored = [{'body': 'Rotate key\\n\\nNew key [REDACTED:github-pat] set', 'id': 'a1',"
            " 'project': 'p', 'session': 's', 'ts': 't'}];"
            "landed = [{'subject': 'Rotate key', 'body': 'New key ghp_x set', 'sha': 'abc'}];"
            "print(json.dumps(m.match_commits(authored, landed)))"
        )
        proc = self.run_tool("-c", code)
        self.assertEqual(json.loads(proc.stdout.strip().splitlines()[-1]), [])

    def test_key_header_in_prose_drops_only_its_own_entries(self):
        sid, path = self.add_session(WORK)
        header = "-----BEGIN OPENSSH " + "PRIVATE KEY-----"
        lines = [
            {"type": "user", "uuid": "a1", "message": {"role": "user",
             "content": f"the file starts with {header} and then base64 follows"}},
            {"type": "user", "uuid": "a2", "message": {"role": "user",
             "content": "no, that's wrong, use the staging cluster instead"}},
            {"type": "user", "uuid": "a3", "message": {"role": "user",
             "content": "and it ends with the END OPENSSH PRIVATE KEY----- line"}},
        ]
        with open(path, "a") as fh:
            fh.write("".join(json.dumps(line) + "\n" for line in lines))
        self.run_tool("tools/redact_corpus.py")
        copy = os.path.join(self.findings, "redacted", WORK, f"{sid}.jsonl")
        entries = [json.loads(line) for line in read(copy).splitlines()]
        self.assertIn("a2", [e.get("uuid") for e in entries], "a clean turn between two matches is kept")
        self.assertEqual(redact.findings_in(entries), [])


if __name__ == "__main__":
    unittest.main()
