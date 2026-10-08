from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from helpers import DIFF

from gitea_claude_review import claude
from gitea_claude_review.claude import Finding, Resolution, parse_answer
from gitea_claude_review.diff import changed_files, parse_unified_diff
from gitea_claude_review.gitea import Gitea, GiteaError
from gitea_claude_review.publish import ANY_SUMMARY_RE, REVIEW_ID_RE, Markers, fingerprint, split_anchorable, supports_resolve_api


class DiffTest(unittest.TestCase):
    def test_commentable_lines_on_both_sides(self):
        files = parse_unified_diff(DIFF)
        self.assertEqual(sorted(files), ["docs/old.md", "src/app.py"])
        app = files["src/app.py"]
        self.assertEqual(sorted(app.new), [1, 2, 3, 4, 5])
        self.assertEqual(sorted(app.old), [1, 2, 3, 4])
        self.assertEqual(sorted(files["docs/old.md"].old), [1, 2])
        self.assertEqual(files["docs/old.md"].new, set())

    def test_changed_files(self):
        self.assertEqual(changed_files(DIFF), ["docs/old.md", "src/app.py"])

    def test_no_newline_marker_does_not_shift_lines(self):
        text = "diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n"
        files = parse_unified_diff(text)
        self.assertEqual(files["a"].new, {1})
        self.assertEqual(files["a"].old, {1})


class AnswerTest(unittest.TestCase):
    def test_parses_json_block(self):
        text = (
            "Thinking out loud…\n"
            f"{claude.BEGIN}\n"
            '{"summary": "Looks good overall.", "findings": [{"path": "b/src/app.py", "line": "3", '
            '"side": "new", "severity": "HIGH", "title": "Off by one", "body": "y should be 2"}]}\n'
            f"{claude.END}\n"
        )
        answer = parse_answer(text)
        self.assertTrue(answer.structured)
        self.assertEqual(answer.summary, "Looks good overall.")
        self.assertEqual(answer.findings, [Finding("src/app.py", 3, "new", "high", "Off by one", "y should be 2")])

    def test_unknown_severity_and_missing_block(self):
        answer = parse_answer(f'{claude.BEGIN}{{"summary": "s", "findings": [{{"path": "a", "line": 1, "severity": "huge"}}]}}{claude.END}')
        self.assertEqual(answer.findings[0].severity, "medium")
        raw = parse_answer("plain text only")
        self.assertFalse(raw.structured)
        self.assertEqual(raw.summary, "plain text only")

    def test_parses_resolved_earlier_findings(self):
        answer = parse_answer(
            f'{claude.BEGIN}{{"summary": "s", "findings": [], "resolved": '
            f'[{{"id": "123", "note": " now checked "}}, {{"id": "abc"}}, {{"note": "no id"}}, 7]}}{claude.END}'
        )
        self.assertEqual(answer.resolved, [Resolution(123, "now checked")])
        self.assertEqual(parse_answer(f'{claude.BEGIN}{{"summary": "s"}}{claude.END}').resolved, [])

    def test_claude_env_drops_secrets_it_does_not_need(self):
        env = claude.claude_env({
            "PATH": "/bin", "HOME": "/home/r", "GCR_GITEA_TOKEN": "x", "GITHUB_TOKEN": "y", "GITEA_TOKEN": "z",
            "INPUT_GITEA_TOKEN": "w", "CLAUDE_CODE_OAUTH_TOKEN": "c", "ANTHROPIC_API_KEY": "", "CLAUDE_CODE_FOO": "1",
        })
        self.assertEqual(env, {"PATH": "/bin", "HOME": "/home/r", "CLAUDE_CODE_OAUTH_TOKEN": "c", "CLAUDE_CODE_FOO": "1"})

    def test_command_is_read_only(self):
        cmd = claude.build_command("claude", "opus", 12, ["git diff:*"])
        allowed = cmd[cmd.index("--allowedTools") + 1]
        denied = cmd[cmd.index("--disallowedTools") + 1]
        self.assertIn("Bash(git diff:*)", allowed)
        for tool in ("Edit", "Write", "WebFetch"):
            self.assertIn(tool, denied)
            self.assertNotIn(tool, allowed.split(","))
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")


class AnchorTest(unittest.TestCase):
    def test_only_lines_in_the_diff_are_anchored(self):
        lines = parse_unified_diff(DIFF)
        inside = Finding("src/app.py", 3, "new", "high", "t", "b")
        removed = Finding("docs/old.md", 2, "old", "low", "t", "b")
        outside = Finding("src/app.py", 99, "new", "low", "t", "b")
        unknown = Finding("other.py", 1, "new", "low", "t", "b")
        anchored, loose = split_anchorable([inside, removed, outside, unknown], lines)
        self.assertEqual(anchored, [inside, removed])
        self.assertEqual(loose, [outside, unknown])

    def test_fingerprint_ignores_case_and_spacing(self):
        a = Finding("src/app.py", 3, "new", "high", "Off  by one", "x")
        b = Finding("src/app.py", 9, "new", "low", "off by ONE", "y")
        self.assertEqual(fingerprint(a), fingerprint(b))

    def test_resolve_api_needs_gitea_1_26(self):
        for version, expected in [("1.25.5", False), ("1.26.0", True), ("1.27.3", True), ("1.26.0+dev-12-gabc", True),
                                  ("28.1.0", True), ("", False), ("dev", False)]:
            self.assertEqual(supports_resolve_api(version), expected, version)


class MarkersTest(unittest.TestCase):
    def test_empty_id_keeps_the_original_marker_texts(self):
        legacy = Markers()
        self.assertEqual(legacy.summary, "<!-- gitea-claude-review:summary -->")
        self.assertEqual(legacy.finding("abcdef012345"), "<!-- gitea-claude-review:finding:abcdef012345 -->")
        self.assertEqual(legacy.fixed("111111111111"), "<!-- gitea-claude-review:fixed:111111111111 -->")
        self.assertEqual(legacy.finding_re.findall("x <!-- gitea-claude-review:finding:abcdef012345 --> y"), ["abcdef012345"])

    def test_namespaced_markers_do_not_match_each_other(self):
        design, legacy = Markers("design"), Markers()
        self.assertEqual(design.summary, "<!-- gitea-claude-review:design:summary -->")
        body = design.finding("abcdef012345") + "\n" + design.fixed("111111111111")
        self.assertEqual(design.finding_re.findall(body), ["abcdef012345"])
        self.assertTrue(design.fixed_re.search(body))
        self.assertEqual(legacy.finding_re.findall(body), [])
        self.assertIsNone(legacy.fixed_re.search(body))
        self.assertEqual(Markers("sec").finding_re.findall(body), [])
        self.assertNotIn(legacy.summary, design.summary)
        for marker in (design.summary, legacy.summary):
            self.assertTrue(ANY_SUMMARY_RE.search(marker))
        self.assertIsNone(ANY_SUMMARY_RE.search(design.finding("abcdef012345")))

    def test_review_id_charset(self):
        for ok in ("design", "sec-2", "a_b", "x" * 32):
            self.assertTrue(REVIEW_ID_RE.match(ok), ok)
        for bad in ("Design", "has space", "-lead", "x" * 33, "a:b"):
            self.assertIsNone(REVIEW_ID_RE.match(bad), bad)


class RetryTest(unittest.TestCase):
    def client(self, responses):
        calls = []

        def transport(method, url, headers, data):
            calls.append(method)
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        sleeps = []
        gitea = Gitea("https://git.example.com", "t", "team", "app", transport=transport, sleep=sleeps.append)
        return gitea, calls, sleeps

    def test_reads_retry_on_gateway_errors_and_timeouts(self):
        gitea, calls, sleeps = self.client([(502, b"bad gateway"), TimeoutError("read timed out"), (200, b'{"login": "bot"}')])
        self.assertEqual(gitea.current_user(), {"login": "bot"})
        self.assertEqual(calls, ["GET", "GET", "GET"])
        self.assertEqual(sleeps, [3, 10])

    def test_reads_give_up_with_the_path_in_the_error(self):
        gitea, _, _ = self.client([TimeoutError("t1"), TimeoutError("t2"), TimeoutError("t3")])
        with self.assertRaisesRegex(GiteaError, r"GET /user got no response: request failed after 3 attempt"):
            gitea.current_user()

    def test_writes_are_never_retried(self):
        gitea, calls, sleeps = self.client([TimeoutError("read timed out")])
        with self.assertRaisesRegex(GiteaError, r"POST /repos/team/app/issues/7/comments"):
            gitea.create_comment(7, "hi")
        self.assertEqual((calls, sleeps), (["POST"], []))
        gitea, calls, _ = self.client([(502, b"bad gateway")])
        with self.assertRaises(GiteaError):
            gitea.create_comment(7, "hi")
        self.assertEqual(calls, ["POST"])


class SubprocessTest(unittest.TestCase):
    def test_claude_runs_without_the_gitea_token_and_envelope_is_read(self):
        # A stand-in for Claude Code: reports the prompt length and any leaked Gitea variables.
        script = textwrap.dedent("""\
            import json, os, sys
            prompt = sys.stdin.read()
            leaked = [k for k in os.environ if 'GITEA' in k or k.startswith('GCR_')]
            summary = 'ok ' + str(len(prompt)) + ' ' + ','.join(leaked)
            block = 'BEGIN_GITEA_REVIEW_JSON' + json.dumps({'summary': summary, 'findings': []}) + 'END_GITEA_REVIEW_JSON'
            print(json.dumps({'result': block, 'is_error': False, 'total_cost_usd': 0.1, 'num_turns': 2}))
        """)
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "fake_claude.py")
            with open(fake, "w", encoding="utf-8") as f:
                f.write(script)
            env = {"PATH": os.environ.get("PATH", ""), "GCR_GITEA_TOKEN": "secret", "GITEA_TOKEN": "secret",
                   "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
            with mock.patch.dict(os.environ, env, clear=True):
                answer = claude.run_claude("hello", [sys.executable, fake], tmp, 30)
        self.assertEqual(answer.summary.strip(), "ok 5")
        self.assertEqual((answer.cost_usd, answer.turns), (0.1, 2))


if __name__ == "__main__":
    unittest.main()
