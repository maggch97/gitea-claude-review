from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers  # Adds src to the import path, as in the other standalone tests.
from gitea_claude_review import claude, progress


class ProgressTest(unittest.TestCase):
    def test_redacts_known_rotated_and_token_shaped_values_before_clipping(self):
        log = progress.ProgressLog("Codex", ["known-secret"], lambda: ["rotated-secret"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            log.write("stderr", "known-secret rotated-secret sk-abc123 Bearer opaque-token")
            log.write("tool", "x" * 5996 + "known-secret")
            log.write("message", "first\n::error::not a workflow command\r\x1b[31mred")
        text = output.getvalue()
        for secret in ("known-secret", "rotated-secret", "sk-abc123", "opaque-token"):
            self.assertNotIn(secret, text)
        self.assertIn("[REDACTED]", text)
        self.assertIn("[truncated]", text)
        self.assertTrue(all(line.startswith("[Codex]") for line in text.splitlines()))

    def test_selected_events_do_not_echo_prompts_or_thinking(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            log = progress.ProgressLog("Claude")
            log.event({"type": "user", "message": {"content": "private prompt"}})
            log.event({"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "private thinking"},
                {"type": "text", "text": "Checking ABI"},
                {"type": "tool_use", "name": "Read", "input": {"file_path": "src/a.cs"}}]}})
            log.event({"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "File contents"}]}})
            codex = progress.ProgressLog("Codex")
            codex.event({"type": "item.completed", "item": {"type": "reasoning", "text": "private reasoning"}})
            codex.event({"type": "item.completed", "item": {"type": "agent_message", "text": "Found a risk"}})
            codex.event({"type": "item.completed", "item": {"type": "command_execution", "command": "git diff", "exit_code": 0}})
        text = output.getvalue()
        for visible in ("Checking ABI", "src/a.cs", "File contents", "Found a risk", "git diff"):
            self.assertIn(visible, text)
        for hidden in ("private prompt", "private thinking", "private reasoning"):
            self.assertNotIn(hidden, text)

    def test_both_pipes_are_drained_and_progress_arrives_before_process_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "seen"
            script = Path(folder) / "cli.py"
            script.write_text(
                "import json, pathlib, sys, time\n"
                "sys.stdin.read()\n"
                "print('private-secret diagnostic', file=sys.stderr, flush=True)\n"
                "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'live progress'}}), flush=True)\n"
                "deadline = time.monotonic() + 3\n"
                "while not pathlib.Path(sys.argv[1]).exists():\n"
                "    if time.monotonic() > deadline: sys.exit(9)\n"
                "    time.sleep(0.01)\n"
                "sys.stderr.write('x' * 100000 + '\\n')\n", encoding="utf-8")
            log = progress.ProgressLog("Codex", ["private-secret"])
            original = log.write
            def write(label, value=""):
                original(label, value)
                if value == "live progress":
                    marker.touch()
            output = io.StringIO()
            with contextlib.redirect_stdout(output), mock.patch.object(log, "write", side_effect=write):
                code = progress.run_stream([sys.executable, str(script), str(marker)], "prompt" * 30000,
                                           folder, dict(os.environ), 5, log, lambda event: None)
            self.assertEqual(code, 0)
            self.assertIn("live progress", output.getvalue())
            self.assertIn("[REDACTED] diagnostic", output.getvalue())
            self.assertNotIn("private-secret", output.getvalue())

    def test_silent_process_times_out(self):
        start = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "exceeded timeout_minutes"):
            progress.run_stream([sys.executable, "-c", "import time; time.sleep(30)"], "",
                                ".", dict(os.environ), 0.15, progress.ProgressLog("Codex"), lambda event: None)
        self.assertLess(time.monotonic() - start, 5)

    def test_invalid_stream_fails_without_echoing_raw_payload(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(ValueError, "invalid JSON"):
            progress.run_stream([sys.executable, "-c", "print('private-invalid-payload')"], "",
                                ".", dict(os.environ), 5, progress.ProgressLog("Claude"), lambda event: None)
        self.assertNotIn("private-invalid-payload", output.getvalue())

    def test_claude_missing_final_event_is_not_a_successful_review(self):
        with mock.patch.object(claude, "run_stream", return_value=0):
            with self.assertRaisesRegex(ValueError, "without a result event"):
                claude.run_claude("review", ["claude"], ".", 5)

    def test_claude_reads_final_result_after_intermediate_events(self):
        payload = claude.BEGIN + json.dumps({"summary": "Final result", "findings": []}) + claude.END
        events = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "Working"}]}},
                  {"type": "result", "result": payload, "is_error": False, "num_turns": 3}]
        code = "import json\n" + "\n".join("print(" + repr(json.dumps(e)) + ", flush=True)" for e in events)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            answer = claude.run_claude("review", [sys.executable, "-c", code], ".", 5)
        self.assertEqual(answer.summary, "Final result")
        self.assertEqual(answer.turns, 3)
        self.assertIn("Working", output.getvalue())


if __name__ == "__main__":
    unittest.main()
