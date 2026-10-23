import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import proof_guard  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
GUARD = os.path.join(ROOT, "hooks", "proof_guard.py")


# ------------------------------------------------------------ transcript DSL
def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def user_blocks(text):
    return {"type": "user", "message": {"role": "user",
                                        "content": [{"type": "text", "text": text}]}}


def asst(text=None, tools=()):
    blocks = []
    if text is not None:
        blocks.append({"type": "text", "text": text})
    for tid, name, inp in tools:
        blocks.append({"type": "tool_use", "id": tid, "name": name, "input": inp})
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def asst_str(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": text}}


def result(tid, out="ok"):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": out}]}}


def edit(tid="e1"):
    return (tid, "Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"})


def bash(cmd, tid="b1"):
    return (tid, "Bash", {"command": cmd})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_guard(self, entries, active=False, cfg=None, raw=None):
        path = os.path.join(self.tmp.name, "t.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            if raw is not None:
                fh.write(raw)
            else:
                for e in entries:
                    fh.write(json.dumps(e) + "\n")
        payload = {"transcript_path": path, "stop_hook_active": active}
        merged = proof_guard.load_config(os.path.join(self.tmp.name, "none.json"))
        merged.update(cfg or {})
        return proof_guard.evaluate(payload, merged)[0]


class TestClaims(Base):
    def test_claim_with_test_evidence_passes(self):
        e = [user("fix it"), asst("editing", [edit()]), result("e1"),
             asst(None, [bash("pytest -q", "b1")]), result("b1", "3 passed"),
             asst("Fixed, all tests pass.")]
        self.assertEqual(self.run_guard(e), 0)

    def test_claim_without_evidence_blocks(self):
        e = [user("fix it"), asst("editing", [edit()]), result("e1"),
             asst("Done, it works now.")]
        self.assertEqual(self.run_guard(e), 2)

    def test_question_without_claim_passes(self):
        e = [user("what is this?"), asst("Which file do you mean?")]
        self.assertEqual(self.run_guard(e), 0)

    def test_stop_hook_active_passes(self):
        e = [user("fix"), asst("x", [edit()]), result("e1"), asst("Done.")]
        self.assertEqual(self.run_guard(e, active=True), 0)

    def test_negated_claim_is_not_a_claim(self):
        e = [user("fix"), asst("x", [edit()]), result("e1"),
             asst("This is not done yet and it doesn't work.")]
        self.assertEqual(self.run_guard(e), 0)

    def test_claim_inside_code_fence_ignored(self):
        e = [user("fix"), asst("x", [edit()]), result("e1"),
             asst("Here is the snippet:\n```\nprint('done')\n```")]
        self.assertEqual(self.run_guard(e), 0)

    def test_word_boundary_no_false_match(self):
        e = [user("fix"), asst("x", [edit()]), result("e1"),
             asst("The undone and workshop parts remain.")]
        self.assertEqual(self.run_guard(e), 0)

    def test_case_insensitive_claim(self):
        e = [user("fix"), asst("x", [edit()]), result("e1"), asst("ALL TESTS PASS")]
        self.assertEqual(self.run_guard(e), 2)

    def test_polish_off_by_default(self):
        e = [user("fix"), asst("x", [edit()]), result("e1"), asst("Gotowe, wszystko zrobione.")]
        self.assertEqual(self.run_guard(e), 0)

    def test_polish_enabled_blocks(self):
        e = [user("fix"), asst("x", [edit()]), result("e1"), asst("Gotowe, działa.")]
        self.assertEqual(self.run_guard(e, cfg={"enable_polish": True}), 2)


class TestEvidence(Base):
    def claim_after(self, tools_and_results):
        e = [user("task")] + tools_and_results + [asst("Done, fixed.")]
        return self.run_guard(e)

    def test_curl_counts(self):
        self.assertEqual(self.claim_after([
            asst("x", [edit()]), result("e1"),
            asst(None, [bash("curl -s localhost:8000/health")]), result("b1", "ok")]), 0)

    def test_py_compile_empty_output_counts(self):
        self.assertEqual(self.claim_after([
            asst("x", [edit()]), result("e1"),
            asst(None, [bash("python3 -m py_compile a.py")]), result("b1", "")]), 0)

    def test_screenshot_tool_counts(self):
        self.assertEqual(self.claim_after([
            asst("x", [edit()]), result("e1"),
            asst(None, [("s1", "mcp__browser__take_screenshot", {})]), result("s1", "img")]), 0)

    def test_read_image_counts(self):
        self.assertEqual(self.claim_after([
            asst("x", [edit()]), result("e1"),
            asst(None, [("r1", "Read", {"file_path": "/tmp/shot.png"})]), result("r1", "img")]), 0)

    def test_evidence_without_result_does_not_count(self):
        self.assertEqual(self.claim_after([
            asst("x", [edit()]), result("e1"),
            asst(None, [bash("pytest")])]), 2)

    def test_echo_fake_evidence_rejected(self):
        self.assertEqual(self.claim_after([
            asst("x", [edit()]), result("e1"),
            asst(None, [bash('echo "pytest: 10 passed"')]), result("b1", "pytest: 10 passed")]), 2)

    def test_unrelated_bash_is_not_evidence(self):
        self.assertEqual(self.claim_after([
            asst("x", [edit()]), result("e1"),
            asst(None, [bash("ls -la")]), result("b1", "files")]), 2)

    def test_evidence_before_last_edit_does_not_count(self):
        self.assertEqual(self.claim_after([
            asst(None, [bash("pytest", "b1")]), result("b1", "1 passed"),
            asst("one more tweak", [edit("e2")]), result("e2")]), 2)

    def test_evidence_before_last_edit_counts_when_option_off(self):
        e = [user("t"), asst(None, [bash("pytest", "b1")]), result("b1", "1 passed"),
             asst("tweak", [edit("e2")]), result("e2"), asst("Done, fixed.")]
        self.assertEqual(self.run_guard(e, cfg={"evidence_after_last_change": False}), 0)

    def test_tool_name_substring_pytest_not_confused(self):
        self.assertEqual(self.claim_after([
            asst("x", [edit()]), result("e1"),
            asst(None, [bash("cat my-pytest-notes.txt")]), result("b1", "notes")]), 2)


class TestHardening(Base):
    def test_or_true_not_evidence(self):
        for cmd in ("pytest || true", "pytest -q ||true", "pytest || :", "pytest || exit 0"):
            e = [user("t"), asst("x", [edit()]), result("e1"),
                 asst(None, [bash(cmd)]), result("b1", "ok"), asst("Done, fixed.")]
            self.assertEqual(self.run_guard(e), 2, cmd)

    def test_plain_pytest_still_evidence(self):
        e = [user("t"), asst("x", [edit()]), result("e1"),
             asst(None, [bash("pytest -q")]), result("b1", "ok"), asst("Done, fixed.")]
        self.assertEqual(self.run_guard(e), 0)

    def test_cwd_proof_json_not_loaded(self):
        old = os.getcwd()
        os.chdir(self.tmp.name)
        try:
            with open("proof.json", "w", encoding="utf-8") as fh:
                json.dump({"claims": ["zzz-only"]}, fh)
            env = dict(os.environ)
            env.pop("PROOF_DONE_CONFIG", None)
            saved = os.environ.pop("PROOF_DONE_CONFIG", None)
            cfg = proof_guard.load_config()
        finally:
            os.chdir(old)
            if saved is not None:
                os.environ["PROOF_DONE_CONFIG"] = saved
        self.assertNotIn("zzz-only", cfg["claims"])
        self.assertIn("done", cfg["claims"])


class TestTurnLogic(Base):
    def test_text_only_claim_of_change_blocks(self):
        e = [user("fix"), asst_str("I fixed the bug.")]
        self.assertEqual(self.run_guard(e), 2)

    def test_text_only_generic_claim_passes(self):
        e = [user("does python 3 support this?"), asst_str("Yes, that works in Python 3.")]
        self.assertEqual(self.run_guard(e), 0)

    def test_no_tools_no_claim_passes(self):
        e = [user("explain"), asst("Here is an explanation of the design.")]
        self.assertEqual(self.run_guard(e), 0)

    def test_read_only_turn_with_generic_claim_passes(self):
        e = [user("look"), asst(None, [("r1", "Read", {"file_path": "a.py"})]),
             result("r1", "code"), asst("The parser is ready for review.")]
        self.assertEqual(self.run_guard(e), 0)

    def test_read_only_turn_strict_mode_blocks(self):
        e = [user("look"), asst(None, [("r1", "Read", {"file_path": "a.py"})]),
             result("r1", "code"), asst("The parser works.")]
        self.assertEqual(self.run_guard(e, cfg={"ignore_turns_without_changes": False}), 2)

    def test_only_last_turn_counts(self):
        e = [user("first"), asst("x", [edit()]), result("e1"), asst("Done."),
             user("explain more"), asst("It parses tokens.")]
        self.assertEqual(self.run_guard(e), 0)

    def test_old_turn_evidence_does_not_leak(self):
        e = [user("first"), asst(None, [bash("pytest")]), result("b1", "1 passed"),
             asst("ok"), user("second"), asst("x", [edit("e2")]), result("e2"),
             asst("Done.")]
        self.assertEqual(self.run_guard(e), 2)

    def test_tool_result_user_message_is_not_a_new_turn(self):
        e = [user("go"), asst("x", [edit()]), result("e1"),
             asst(None, [bash("pytest", "b2")]), result("b2", "ok"), asst("Done.")]
        self.assertEqual(self.run_guard(e), 0)

    def test_bash_sed_inplace_counts_as_change(self):
        e = [user("go"), asst(None, [bash("sed -i s/a/b/ f.py")]), result("b1"),
             asst("Done.")]
        self.assertEqual(self.run_guard(e), 2)


class TestShapes(Base):
    def test_user_content_as_blocks(self):
        e = [user_blocks("fix"), asst("x", [edit()]), result("e1"), asst("Done.")]
        self.assertEqual(self.run_guard(e), 2)

    def test_assistant_content_as_string(self):
        e = [user("fix"), asst_str("I implemented it.")]
        self.assertEqual(self.run_guard(e), 2)

    def test_flat_role_content_shape(self):
        e = [{"role": "user", "content": "fix"},
             {"role": "assistant", "content": "I implemented it."}]
        self.assertEqual(self.run_guard(e), 2)

    def test_sidechain_and_meta_entries_skipped(self):
        side = asst_str("Done, fixed.")
        side["isSidechain"] = True
        e = [user("explain"), asst("Here is the explanation."), side]
        self.assertEqual(self.run_guard(e), 0)

    def test_unknown_entry_types_ignored(self):
        e = [{"type": "summary", "summary": "x"}, user("hi"), asst("Hello.")]
        self.assertEqual(self.run_guard(e), 0)


class TestFailOpen(Base):
    def test_garbage_transcript_fail_open(self):
        self.assertEqual(self.run_guard(None, raw="not json\n{{{\n"), 0)

    def test_missing_transcript_fail_open(self):
        self.assertEqual(proof_guard.evaluate({"transcript_path": "/nonexistent/x.jsonl"})[0], 0)

    def test_payload_not_a_dict(self):
        self.assertEqual(proof_guard.evaluate(["x"])[0], 0)

    def test_empty_transcript(self):
        self.assertEqual(self.run_guard(None, raw=""), 0)


class TestFixturesAndCli(unittest.TestCase):
    def run_cli(self, stdin, env=None):
        e = dict(os.environ)
        e.pop("PROOF_GUARD_OFF", None)
        e.update(env or {})
        return subprocess.run([sys.executable, GUARD], input=stdin,
                              capture_output=True, text=True, env=e, timeout=30)

    def payload(self, name, active=False):
        return json.dumps({"transcript_path": os.path.join(FIX, name),
                           "stop_hook_active": active})

    def test_fixture_with_evidence_exit0(self):
        self.assertEqual(self.run_cli(self.payload("claim_with_pytest.jsonl")).returncode, 0)

    def test_fixture_without_evidence_exit2_with_message(self):
        r = self.run_cli(self.payload("claim_without_evidence.jsonl"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("claim without evidence: run a check and include its output", r.stderr)

    def test_fixture_block_only_once(self):
        r = self.run_cli(self.payload("claim_without_evidence.jsonl", active=True))
        self.assertEqual(r.returncode, 0)

    def test_fixture_broken_jsonl_fail_open(self):
        self.assertEqual(self.run_cli(self.payload("broken.jsonl")).returncode, 0)

    def test_invalid_stdin_fail_open(self):
        self.assertEqual(self.run_cli("not json at all").returncode, 0)

    def test_empty_stdin_fail_open(self):
        self.assertEqual(self.run_cli("").returncode, 0)

    def test_kill_switch(self):
        r = self.run_cli(self.payload("claim_without_evidence.jsonl"),
                         env={"PROOF_GUARD_OFF": "1"})
        self.assertEqual(r.returncode, 0)

    def test_shipped_config_is_valid(self):
        with open(os.path.join(ROOT, "proof.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        cfg = proof_guard.load_config(os.path.join(ROOT, "proof.json"))
        for key in data:
            self.assertIn(key, proof_guard.DEFAULT_CONFIG)
            self.assertEqual(cfg[key], data[key])


if __name__ == "__main__":
    unittest.main()
