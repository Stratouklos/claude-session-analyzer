"""Model backends: dispatch, the :cloud guard and the claude -p runner.

No real model and no real claude: Ollama is a stub HTTP server, claude is a fake
executable on PATH.
"""

import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_pipeline import REPO, StubModel  # noqa: E402

sys.path.insert(0, os.path.join(REPO, "tools"))
import redact  # noqa: E402

FAKE_CLAUDE = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    if sys.argv[1:] == ["--version"]:
        print(os.environ["FAKE_CLAUDE_VERSION"] + " (Claude Code)")
        sys.exit(0)
    prompt = sys.stdin.read()
    sysfile = sys.argv[sys.argv.index("--system-prompt-file") + 1]
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "stdin": prompt,
                             "sysfile_existed": os.path.exists(sysfile),
                             "sysfile_mode": os.stat(sysfile).st_mode & 0o777}}) + "\\n")
    items = json.loads(prompt.rsplit("\\n\\n", 1)[1])
    print(json.dumps([{{"id": it["id"], "is_correction": True, "confidence": 0.9}} for it in items]))
''')


class Backends(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), StubModel)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="csa-bakeoff-")
        self.findings = os.path.join(self.tmp, "findings")
        self.bin = os.path.join(self.tmp, "bin")
        self.log = os.path.join(self.tmp, "claude.log")
        os.makedirs(self.bin)
        StubModel.seen, StubModel.payloads, StubModel.mode = [], [], "ok"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake_claude(self):
        path = os.path.join(self.bin, "claude")
        with open(path, "w") as fh:
            fh.write(FAKE_CLAUDE.format(python=sys.executable))
        os.chmod(path, 0o755)

    def judge(self, model, version="2.1.248", allow_cloud=False):
        in_dir = os.path.join(self.findings, "triage_in")
        os.makedirs(in_dir, exist_ok=True)
        with open(os.path.join(in_dir, "batch_000.json"), "w") as fh:
            json.dump([{"id": "s:1", "user_said": "no"}, {"id": "s:2", "user_said": "stop"}], fh)
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"],
                   OLLAMA_HOST=f"http://127.0.0.1:{self.server.server_port}",
                   FAKE_CLAUDE_VERSION=version, FAKE_CLAUDE_LOG=self.log)
        env.pop("ALLOW_CLOUD", None)
        if allow_cloud:
            env["ALLOW_CLOUD"] = "1"
        return subprocess.run(
            [sys.executable, "judge_batches.py", model, in_dir, f"{self.findings}/triage_out",
             "tools/triage_rubric.md"],
            cwd=REPO, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def calls(self):
        with open(self.log) as fh:
            return [json.loads(line) for line in fh]

    def test_ollama_prefix_matches_bare_name(self):
        for model in ("stub", "ollama:stub"):
            with self.subTest(model=model):
                shutil.rmtree(self.findings, ignore_errors=True)
                StubModel.seen = []
                proc = self.judge(model)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(StubModel.seen, ["s:1", "s:2"])

    def test_cloud_model_is_refused_before_anything_is_sent(self):
        for model in ("ollama:foo:cloud", "foo:cloud", "gpt-oss:120b-cloud", "ollama:foo:Cloud"):
            with self.subTest(model=model):
                proc = self.judge(model)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("ALLOW_CLOUD=1", proc.stdout + proc.stderr)
                self.assertIn(model, proc.stdout + proc.stderr)
                self.assertEqual(StubModel.seen, [])

    def test_allow_cloud_lets_a_cloud_model_through(self):
        for model in ("ollama:foo:cloud", "gpt-oss:120b-cloud", "ollama:foo:Cloud"):
            with self.subTest(model=model):
                StubModel.seen = []
                shutil.rmtree(self.findings, ignore_errors=True)
                proc = self.judge(model, allow_cloud=True)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(StubModel.seen, ["s:1", "s:2"])

    def test_claude_backend_runs_claude_p_with_a_private_system_file(self):
        self.fake_claude()
        proc = self.judge("claude:haiku")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(StubModel.seen, [], "claude must not touch the Ollama server")
        call = self.calls()[0]
        argv = call["argv"]
        self.assertIn("-p", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")
        self.assertEqual(argv[argv.index("--model") + 1], "haiku")
        sysfile = argv[argv.index("--system-prompt-file") + 1]
        self.assertTrue(call["sysfile_existed"])
        self.assertEqual(call["sysfile_mode"], 0o600)
        self.assertFalse(os.path.exists(sysfile), "the system prompt file is removed after the call")
        self.assertIn('"id": "s:1"', call["stdin"])
        with open(f"{self.findings}/triage_out/batch_000.json") as fh:
            out = json.load(fh)
        self.assertEqual([v["id"] for v in out], ["s:1", "s:2"])

    def test_old_claude_stops_the_run_with_nothing_sent(self):
        self.fake_claude()
        proc = self.judge("claude:haiku", version="2.1.191")
        self.assertNotEqual(proc.returncode, 0)
        text = proc.stdout + proc.stderr
        self.assertIn("2.1.191", text)
        self.assertIn("2.1.248", text)
        self.assertFalse(os.path.exists(self.log), "no judging call was made")
        self.assertEqual(StubModel.seen, [])


def window(text):
    return [{"role": "claude", "text": "I did the thing."}, {"role": "user", "text": text},
            {"role": "claude", "text": "Okay."}]


def parse_report(out):
    # common prints a `config:` line before the report.
    return json.loads(out[out.index("{"):])


def triaged_row(i, is_corr, conf):
    return {"id": f"s:{i:03d}", "window": window(f"message {i}"), "severity": 3.0,
            "triage": {"id": f"s:{i:03d}", "is_correction": is_corr, "class": "A",
                       "kind": "k", "repeat_signal": False, "confidence": conf,
                       "quote": "q", "one_line": "l"}}


class ToolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="csa-gold-")
        self.findings = os.path.join(self.tmp, "findings")
        self.bin = os.path.join(self.tmp, "bin")
        self.log = os.path.join(self.tmp, "claude.log")
        os.makedirs(self.findings)
        os.makedirs(self.bin)
        StubModel.seen, StubModel.payloads, StubModel.mode = [], [], "ok"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_tool(self, *args, **extra):
        env = dict(os.environ, CSA_FINDINGS=self.findings, **extra)
        env.pop("ALLOW_CLOUD", None)
        return subprocess.run([sys.executable, *args], cwd=REPO, env=env, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL)

    def write(self, rel, rows):
        path = os.path.join(self.findings, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))
        return path

    def read_rows(self, rel):
        with open(os.path.join(self.findings, rel)) as fh:
            return [json.loads(line) for line in fh]


class GoldSet(ToolCase):
    def source(self, n=30):
        rows = [triaged_row(i, i % 3 != 0, 0.35 if i % 4 == 0 else 0.9) for i in range(n)]
        rows.append({"id": "s:untriaged", "window": window("no verdict")})
        self.write("triaged.jsonl", rows)
        return {r["id"]: r for r in rows}

    def test_samples_n_deterministically_with_prefill(self):
        src = self.source()
        a = self.run_tool("tools/make_gold_set.py", "10", "--seed", "7")
        self.assertEqual(a.returncode, 0, a.stdout + a.stderr)
        first = self.read_rows("gold/gold.jsonl")
        self.assertEqual(len(first), 10)
        self.assertEqual(len({r["id"] for r in first}), 10)
        b = self.run_tool("tools/make_gold_set.py", "10", "--seed", "7")
        self.assertEqual(b.returncode, 0, b.stdout + b.stderr)
        self.assertEqual(first, self.read_rows("gold/gold.jsonl"))
        for r in first:
            self.assertFalse(r["labeled"])
            want = src[r["id"]].get("triage")
            self.assertEqual(r["triage_prefill"], want)
            self.assertEqual(r["label"]["is_correction"], want["is_correction"] if want else None)
            self.assertIsNone(r["label"]["confirmed"])
        self.assertEqual({r["triage_prefill"]["is_correction"] for r in first if r["triage_prefill"]}, {True, False})
        self.assertTrue(any(r["triage_prefill"] and 0.3 <= r["triage_prefill"]["confidence"] < 0.5 for r in first))
        redact.assert_clean([r["window"] for r in first], "gold")
        self.assertEqual(os.stat(os.path.join(self.findings, "gold/gold.jsonl")).st_mode & 0o777, 0o600)

    def test_takes_all_rows_when_fewer_than_n(self):
        self.source(5)
        self.assertEqual(self.run_tool("tools/make_gold_set.py").returncode, 0)
        self.assertEqual(len(self.read_rows("gold/gold.jsonl")), 6)

    def test_never_overwrites_edited_gold_without_force(self):
        self.source()
        self.run_tool("tools/make_gold_set.py", "10")
        rows = self.read_rows("gold/gold.jsonl")
        rows[0]["labeled"] = True
        rows[0]["label"]["is_correction"] = not rows[0]["label"]["is_correction"]
        path = self.write("gold/gold.jsonl", rows)
        before = open(path).read()
        proc = self.run_tool("tools/make_gold_set.py", "10")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--force", proc.stdout + proc.stderr)
        self.assertEqual(open(path).read(), before)
        self.assertEqual(self.run_tool("tools/make_gold_set.py", "10", "--force").returncode, 0)
        self.assertFalse(any(r["labeled"] for r in self.read_rows("gold/gold.jsonl")))


class Bakeoff(Backends):
    def gold_file(self, labeled):
        rows = []
        for i, (corr, conf) in enumerate([(True, True), (True, False), (False, None)]):
            rows.append({"id": f"g:{i}", "window": window(f"msg {i}"), "triage_prefill": None,
                         "label": {"is_correction": corr, "confirmed": conf}, "labeled": labeled})
        path = os.path.join(self.findings, "gold", "gold.jsonl")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))

    def bakeoff(self, models):
        self.fake_claude()
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ["PATH"], CSA_FINDINGS=self.findings,
                   OLLAMA_HOST=f"http://127.0.0.1:{self.server.server_port}", MODELS=models,
                   FAKE_CLAUDE_VERSION="2.1.248", FAKE_CLAUDE_LOG=self.log)
        env.pop("ALLOW_CLOUD", None)
        return subprocess.run([sys.executable, "tools/bakeoff.py", "--json"], cwd=REPO, env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def snapshot(self):
        return sorted(os.path.join(d, f) for d, _, fs in os.walk(self.findings) for f in fs)

    def test_reports_one_row_per_model_and_stays_provisional(self):
        self.gold_file(labeled=False)
        before = self.snapshot()
        proc = self.bakeoff("ollama:stub claude:fake")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = parse_report(proc.stdout)
        self.assertTrue(report["provisional"])
        self.assertEqual(report["user_confirmed"], 0)
        self.assertIn("0 of 3", report["note"])
        self.assertEqual([r["model"] for r in report["models"]], ["ollama:stub", "claude:fake"])
        for r in report["models"]:
            self.assertEqual(r["status"], "ok")
            for k in ("triage_agreement", "draft_precision", "verify_agreement"):
                self.assertIn(k, r)
            self.assertGreaterEqual(r["wall_seconds"], 0)
            # stub says every row is a correction: agrees on 2 of 3, keeps all 3, 2 of 3 are true
            self.assertEqual(r["triage_agreement"], {"value": 0.667, "n": 3})
            self.assertEqual(r["draft_precision"], {"value": 0.667, "n": 3})
        text = json.dumps(report).lower()
        self.assertNotIn("winner:", text)
        self.assertNotIn("best", text)
        self.assertEqual(self.snapshot(), before, "nothing is written under findings/")

    def test_fully_labeled_gold_is_not_provisional(self):
        self.gold_file(labeled=True)
        report = parse_report(self.bakeoff("ollama:stub").stdout)
        self.assertFalse(report["provisional"])
        self.assertEqual(report["user_confirmed"], 3)

    def test_failing_model_is_a_failed_row_and_others_still_run(self):
        self.gold_file(labeled=False)
        proc = self.bakeoff("ollama:bad:cloud ollama:stub")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        bad, good = parse_report(proc.stdout)["models"]
        self.assertEqual(bad["status"], "failed")
        self.assertIn("exited", bad["error"])
        self.assertEqual(good["status"], "ok")
        self.assertEqual(good["triage_agreement"]["n"], 3)


if __name__ == "__main__":
    unittest.main()
