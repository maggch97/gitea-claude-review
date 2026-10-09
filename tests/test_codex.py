from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import FakeGitea
from gitea_claude_review import codex, main, prompts
from gitea_claude_review.claude import Finding, Resolution
from gitea_claude_review.gitea import Gitea
import test_flows
from test_flows import config


RESULT = {
    "summary": "未发现需要阻塞合并的问题",
    "findings": [{"path": "src/app.py", "line": 3, "side": "new", "severity": "high",
                  "title": "Wrong value", "body": "Use the correct value."}],
    "resolved": [{"id": 123, "note": "The value is corrected."}],
}
AUTH = {"auth_mode": "chatgpt", "tokens": {"access_token": "fixture-access-secret",
        "refresh_token": "fixture-refresh-secret", "id_token": "fixture-id-secret"}, "last_refresh": "old"}


class CodexContractTest(unittest.TestCase):
    def test_strict_result_and_paths(self):
        answer = codex.parse_answer(json.dumps(RESULT))
        self.assertEqual(answer.findings, [Finding(**RESULT["findings"][0])])
        self.assertEqual(answer.resolved, [Resolution(123, "The value is corrected.")])
        invalid = ["not json", "[]", '{}']
        for path in ("../credentials", "/etc/passwd", "C:/auth.json", "a\\b", "."):
            value = copy.deepcopy(RESULT)
            value["findings"][0]["path"] = path
            invalid.append(json.dumps(value))
        for field, bad in (("line", True), ("line", 0), ("severity", "urgent"), ("body", " ")):
            value = copy.deepcopy(RESULT)
            value["findings"][0][field] = bad
            invalid.append(json.dumps(value))
        value = copy.deepcopy(RESULT)
        value["summary"] = ""
        invalid.append(json.dumps(value))
        value = copy.deepcopy(RESULT)
        del value["resolved"]
        invalid.append(json.dumps(value))
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                codex.parse_answer(value)

    def test_auth_never_fabricates_missing_tokens(self):
        for value in ({}, {"auth_mode": "api-key"}, {"auth_mode": "chatgpt", "tokens": {}}, []):
            with self.assertRaises(ValueError):
                codex.read_auth(json.dumps(value))
        self.assertEqual(codex.read_auth(json.dumps(AUTH)), AUTH)

    def test_command_is_read_only_and_has_no_approval_or_user_config(self):
        cmd = codex.build_command("codex", "chosen-model", "high", Path("schema"), Path("out"))
        self.assertEqual(cmd[cmd.index("--sandbox") + 1], "read-only")
        self.assertEqual(cmd[cmd.index("--ask-for-approval") + 1], "never")
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--output-schema"):
            self.assertIn(flag, cmd)
        self.assertIn('web_search="disabled"', cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "chosen-model")
        self.assertNotIn("--max-turns", cmd)
        self.assertEqual(cmd[-1], "-")

    def test_env_only_exposes_the_selected_credential(self):
        source = {"PATH": "/bin", "HOME": "/home/runner", "GCR_GITEA_TOKEN": "bot-secret",
                  "GITHUB_TOKEN": "job-secret", "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret",
                  "CODEX_API_KEY": "ambient-api-key", "CODEX_ACCESS_TOKEN": "ambient-token",
                  "OPENAI_API_KEY": "ambient-openai", "CODEX_HOME": "/personal"}
        for mode, key in (("chatgpt", None), ("api-key", "CODEX_API_KEY"), ("access-token", "CODEX_ACCESS_TOKEN")):
            env = codex.codex_env(Path("/isolated"), mode, "selected-credential", source)
            expected = {"PATH": "/bin", "HOME": str(Path("/isolated")), "CODEX_HOME": str(Path("/isolated"))}
            if key:
                expected[key] = "selected-credential"
            self.assertEqual(env, expected)

    def test_config_and_prompt_route_to_codex(self):
        cfg = main.load_config({"GCR_PROVIDER": "codex", "GCR_CODEX_AUTH_MODE": "api-key",
                                "GCR_OPENAI_API_KEY": "chosen-key", "GCR_CODEX_EFFORT": "high"})
        self.assertEqual((cfg.provider, cfg.codex_auth_mode, cfg.openai_api_key), ("codex", "api-key", "chosen-key"))
        with mock.patch.object(codex, "run_codex", return_value=codex.parse_answer(json.dumps(RESULT))) as run:
            main.run_model(cfg, "rules\n" + prompts.OUTPUT_CONTRACT)
        prompt = run.call_args.args[0]
        self.assertNotIn("BEGIN_GITEA_REVIEW_JSON", prompt)
        self.assertIn(prompts.CODEX_OUTPUT_CONTRACT, prompt)
        self.assertEqual(run.call_args.kwargs["api_key"], "chosen-key")

    def test_secret_preflight_write_and_callback_use_a_separate_client(self):
        cfg = config("checkout", "pull_request", {}, provider="codex",
                     codex_auth_json=json.dumps(AUTH), codex_secrets_token="secret-manager-token")
        with mock.patch.object(main, "Gitea") as client, mock.patch.object(codex, "run_codex") as run:
            run.return_value = codex.parse_answer(json.dumps(RESULT))
            main.run_model(cfg, prompts.OUTPUT_CONTRACT)
            client.assert_called_once_with(cfg.server_url, "secret-manager-token", "team", "app")
            client.return_value.check_secret_access.assert_called_once_with("CODEX_AUTH_JSON")
            client.return_value.update_secret.assert_called_once_with("CODEX_AUTH_JSON", json.dumps(AUTH))
            run.call_args.kwargs["persist_auth"]("new-cache")
            self.assertEqual(client.return_value.update_secret.call_args.args, ("CODEX_AUTH_JSON", "new-cache"))

    def test_failed_secret_preflight_never_starts_codex(self):
        cfg = config("checkout", "pull_request", {}, provider="codex",
                     codex_auth_json=json.dumps(AUTH), codex_secrets_token="manager")
        with mock.patch.object(main, "Gitea") as client, mock.patch.object(codex, "run_codex") as run:
            client.return_value.update_secret.side_effect = RuntimeError("write permission denied")
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                main.run_model(cfg, "review")
            run.assert_not_called()

    def test_secret_rest_contract_and_sanitized_write_failure(self):
        calls = []
        def transport(method, url, headers, data):
            calls.append((method, url, headers, json.loads(data) if data else None))
            if method == "GET":
                return 200, b'[{"name":"CODEX_AUTH_JSON"}]'
            return 204, b""
        client = Gitea("https://gitea.example", "manager", "team", "app", transport=transport)
        client.check_secret_access("CODEX_AUTH_JSON")
        client.update_secret("CODEX_AUTH_JSON", json.dumps(AUTH))
        self.assertEqual(calls[-1][0], "PUT")
        self.assertTrue(calls[-1][1].endswith("/repos/team/app/actions/secrets/CODEX_AUTH_JSON"))
        self.assertEqual(calls[-1][2]["Authorization"], "token manager")
        self.assertEqual(calls[-1][3], {"data": json.dumps(AUTH)})
        with self.assertRaisesRegex(ValueError, "missing"):
            client.check_secret_access("MISSING")
        client = Gitea("https://gitea.example", "manager", "team", "app",
                       transport=lambda *args: (403, json.dumps(AUTH).encode()))
        with self.assertRaisesRegex(RuntimeError, "HTTP 403") as error:
            client.update_secret("CODEX_AUTH_JSON", json.dumps(AUTH))
        self.assertNotIn("fixture-refresh-secret", str(error.exception))


class CodexProcessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "checkout"
        self.workspace.mkdir()
        self.home = self.root / "persistent-auth"
        self.script = self.root / "fixture_cli.py"
        self.script.write_text(textwrap.dedent('''\
            import json, os, pathlib, sys, time
            mode = sys.argv[1]
            prompt = sys.stdin.read()
            output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])
            home = pathlib.Path(os.environ['CODEX_HOME'])
            result = json.loads(sys.argv[2])
            if mode == 'refresh' or mode == 'refresh-fail':
                auth = json.loads((home / 'auth.json').read_text())
                auth['tokens']['refresh_token'] = 'fixture-rotated-secret'
                auth['last_refresh'] = 'new'
                (home / 'auth.json').write_text(json.dumps(auth))
            if mode == 'env':
                result['summary'] = ','.join(sorted(k for k in os.environ if k.startswith('GCR_')
                    or 'GITEA' in k or k in ('GITHUB_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN', 'OPENAI_API_KEY')))
                result['summary'] = result['summary'] or 'No leaked secrets'
            if mode == 'timeout':
                time.sleep(30)
            if mode != 'missing':
                output.write_text('bad json' if mode == 'bad-json' else json.dumps(result), encoding='utf-8')
            if mode == 'fail' or mode == 'refresh-fail':
                print('fixture-refresh-secret', file=sys.stderr)
                sys.exit(42)
        '''), encoding="utf-8")

    def run_cli(self, mode="ok", result=None, **kwargs):
        original = codex.build_command
        def command(binary, model, effort, schema, output):
            return [sys.executable, str(self.script), mode, json.dumps(RESULT if result is None else result)] + original(
                binary, model, effort, schema, output)[1:]
        with mock.patch.object(codex, "build_command", side_effect=command):
            return codex.run_codex("review", sys.executable, "", "", str(self.workspace), kwargs.pop("timeout_s", 10), **kwargs)

    def account(self, **kwargs):
        return self.run_cli(auth_mode="chatgpt", auth_storage="directory", account_home=str(self.home), **kwargs)

    def test_bootstrap_refresh_and_reuse_never_restore_old_seed(self):
        self.account(mode="refresh", auth_json=json.dumps(AUTH))
        current = json.loads((self.home / "auth.json").read_text())
        self.assertEqual(current["tokens"]["refresh_token"], "fixture-rotated-secret")
        self.account(auth_json="old invalid seed ignored after bootstrap")
        self.assertEqual(json.loads((self.home / "auth.json").read_text()), current)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), [".review-auth.lock", "auth.json"])
        if os.name != "nt":
            self.assertEqual((self.home / "auth.json").stat().st_mode & 0o777, 0o600)

    def test_refresh_is_preserved_even_when_cli_fails(self):
        with self.assertRaisesRegex(RuntimeError, "exited with 42") as failure:
            self.account(mode="refresh-fail", auth_json=json.dumps(AUTH))
        self.assertNotIn("fixture-refresh-secret", str(failure.exception))
        self.assertEqual(json.loads((self.home / "auth.json").read_text())["last_refresh"], "new")

    def test_secret_round_trip_saves_rotated_auth_on_success_and_failure(self):
        for mode in ("refresh", "refresh-fail"):
            stored = []
            options = dict(auth_mode="chatgpt", auth_json=json.dumps(AUTH), persist_auth=stored.append)
            if mode == "refresh-fail":
                with self.assertRaisesRegex(RuntimeError, "exited with 42"):
                    self.run_cli(mode=mode, **options)
            else:
                self.run_cli(mode=mode, **options)
            self.assertEqual(len(stored), 1)
            self.assertEqual(json.loads(stored[0])["tokens"]["refresh_token"], "fixture-rotated-secret")
            self.assertFalse(self.home.exists())
            self.run_cli(auth_mode="chatgpt", auth_json=stored[0], persist_auth=stored.append)
            self.assertEqual(len(stored), 1, "Unchanged auth does not need another write-back")

    def test_secret_write_failure_cannot_publish_a_successful_review(self):
        with self.assertRaisesRegex(RuntimeError, "write-back failed"):
            self.run_cli(mode="refresh", auth_mode="chatgpt", auth_json=json.dumps(AUTH),
                         persist_auth=mock.Mock(side_effect=RuntimeError("write-back failed")))

    def test_missing_or_corrupt_auth_fails_without_replacing_it(self):
        with self.assertRaisesRegex(ValueError, "seed it once"):
            self.account()
        path = self.home / "auth.json"
        path.write_text("corrupt", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            self.account(auth_json=json.dumps(AUTH))
        self.assertEqual(path.read_text(), "corrupt")

    def test_lock_prevents_concurrent_refresh_and_releases_after_errors(self):
        with codex.account_lock(self.home, time.monotonic() + 1):
            with self.assertRaisesRegex(TimeoutError, "auth lock"):
                with codex.account_lock(self.home, time.monotonic() + 0.02):
                    self.fail("Second job acquired the same auth cache")
        self.account(auth_json=json.dumps(AUTH))

    def test_explicit_api_and_access_tokens_and_environment_isolation(self):
        with mock.patch.dict(os.environ, {"GCR_GITEA_TOKEN": "bot-secret", "GITHUB_TOKEN": "job-secret",
                                         "CLAUDE_CODE_OAUTH_TOKEN": "claude-secret", "OPENAI_API_KEY": "ambient"}):
            answer = self.run_cli(mode="env", auth_mode="api-key", api_key="fixture-api-secret")
        self.assertEqual(answer.summary, "No leaked secrets")
        self.run_cli(auth_mode="access-token", access_token="fixture-access-token")
        self.assertFalse(self.home.exists())

    def test_failures_missing_output_and_invalid_output_never_return_success(self):
        for mode, error in (("fail", RuntimeError), ("missing", ValueError), ("bad-json", ValueError)):
            with self.subTest(mode=mode), self.assertRaises(error):
                self.run_cli(mode=mode, auth_mode="api-key", api_key="fixture-api-secret")
        result = copy.deepcopy(RESULT)
        result["summary"] = "leaked fixture-api-secret"
        with self.assertRaisesRegex(ValueError, "contained credentials"):
            self.run_cli(result=result, auth_mode="api-key", api_key="fixture-api-secret")

    def test_timeout_never_publishes_and_releases_account_lock(self):
        with self.assertRaisesRegex(TimeoutError, "timeout_minutes"):
            self.account(mode="timeout", timeout_s=1, auth_json=json.dumps(AUTH))
        self.account()

    def test_bad_auth_options_are_rejected(self):
        options = [
            {"auth_mode": "typo"}, {"auth_mode": "api-key"}, {"auth_mode": "access-token"},
            {"auth_mode": "api-key", "api_key": "a", "access_token": "b"},
            {"auth_mode": "chatgpt", "auth_storage": "directory", "account_home": str(self.workspace)},
            {"auth_mode": "chatgpt", "auth_storage": "directory", "account_home": str(self.workspace / "cache")},
            {"auth_mode": "chatgpt", "auth_storage": "directory", "account_home": "relative"},
            {"auth_mode": "chatgpt", "account_home": str(self.home), "api_key": "a"},
            {"auth_mode": "chatgpt", "auth_json": json.dumps(AUTH)},
            {"auth_mode": "chatgpt", "auth_json": "invalid", "persist_auth": mock.Mock()},
            {"auth_mode": "chatgpt", "auth_storage": "typo"},
        ]
        for option in options:
            with self.subTest(option=option), self.assertRaises(ValueError):
                self.run_cli(**option)

    def test_codex_errors_fail_the_action_even_with_legacy_warning_option(self):
        cfg = config(str(self.workspace), "pull_request", {"pull_request": {"number": 7}}, provider="codex")
        with mock.patch.object(main, "load_config", return_value=cfg), mock.patch.object(main, "Gitea") as client, \
                mock.patch.object(main, "dispatch", side_effect=ValueError("invalid Codex result")):
            client.return_value.repository_info.return_value = {"private": True}
            self.assertEqual(main.main(), 1)

    def test_public_repository_cannot_use_account_auth(self):
        cfg = config(str(self.workspace), "pull_request", {}, provider="codex")
        with mock.patch.object(main, "load_config", return_value=cfg), mock.patch.object(main, "Gitea") as client, \
                mock.patch.object(main, "dispatch") as dispatch:
            client.return_value.repository_info.return_value = {"private": False}
            self.assertEqual(main.main(), 1)
            dispatch.assert_not_called()


class CodexFlowTest(test_flows.FlowTest):
    """Run the existing posting/deduplication/stale-result suite with Codex too."""

    def review(self, **overrides):
        overrides["provider"] = "codex"
        return super().review(**overrides)

    def test_codex_labels_keep_existing_comment_markers(self):
        self.review()
        self.assertIn("Codex review", self.gitea.comments[0]["body"])
        self.assertIn("Codex review", self.gitea.reviews[0]["body"])

    def test_codex_mention_uses_configured_trigger(self):
        event = {"action": "created", "comment": {"id": 55, "body": "@codex review", "user": {"login": "alice"}},
                 "issue": {"number": 7, "title": "Add feature", "pull_request": {"merged": False}}}
        cfg = config(self.workspace, "issue_comment", event, provider="codex", trigger_phrase="@codex")
        self.assertEqual(main.dispatch(cfg, self.gitea.client()), 0)
        self.assertIn("Codex review", self.gitea.comments[-1]["body"])
        self.assertIn("Codex:", self.gitea.reviews[-1]["body"])


if __name__ == "__main__":
    unittest.main()
