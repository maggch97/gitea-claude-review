from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import FakeGitea

from gitea_claude_review import main, publish
from gitea_claude_review.claude import ClaudeAnswer, Finding


def config(workspace: str, event_name: str, event: dict, **overrides) -> main.Config:
    cfg = main.Config(
        gitea_token="secret-token", server_url="https://git.example.com", repository="team/app",
        event_name=event_name, event=event, workspace=workspace, model="opus",
        allowed_bash=["git diff:*"],
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


ANSWER = ClaudeAnswer(
    summary="One real problem.",
    findings=[
        Finding("src/app.py", 3, "new", "high", "Wrong default", "y must be 2."),
        Finding("src/app.py", 40, "new", "low", "Outside the diff", "still worth knowing"),
    ],
    cost_usd=0.42, turns=5,
)


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = self.tmp.name
        self.gitea = FakeGitea()
        self.prompts: list[str] = []
        patches = [
            mock.patch.object(main, "checkout_pull_head"),
            mock.patch.object(main, "strip_checkout_credentials"),
            mock.patch.object(main, "run_model", side_effect=self._fake_model),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.answer = ANSWER

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_model(self, cfg, prompt):
        self.prompts.append(prompt)
        return self.answer

    def review(self, **overrides) -> int:
        cfg = config(self.workspace, "pull_request", {"action": "synchronize", "pull_request": {"number": 7}}, **overrides)
        return main.dispatch(cfg, self.gitea.client())

    def test_review_posts_summary_and_inline_findings_once(self):
        self.assertEqual(self.review(), 0)
        summaries = [c for c in self.gitea.comments if publish.SUMMARY_MARKER in c["body"]]
        self.assertEqual(len(summaries), 1)
        self.assertIn("One real problem.", summaries[0]["body"])
        self.assertIn("src/app.py:40", summaries[0]["body"])  # outside the diff → summary
        self.assertEqual(len(self.gitea.reviews), 1)
        inline = self.gitea.reviews[0]["comments"]
        self.assertEqual([(c["path"], c["new_position"], c["old_position"]) for c in inline], [("src/app.py", 3, 0)])
        self.assertEqual(self.gitea.reviews[0]["commit_id"], "abcdef1234567890")
        # The diff is handed to Claude as a file inside the workspace, ignored by git.
        diff_file = Path(self.workspace) / ".gitea-claude-review" / "pr-7.diff"
        self.assertTrue(diff_file.read_text(encoding="utf-8").startswith("diff --git"))
        self.assertIn(".gitea-claude-review/pr-7.diff", self.prompts[0])

        # Second run on a new commit: summary edited in place, no duplicate inline comment.
        self.gitea.pull["head"]["sha"] = "1234567890abcdef"
        self.answer = ClaudeAnswer(summary="Still one problem.", findings=[Finding("src/app.py", 2, "new", "high", "wrong  DEFAULT", "again")])
        self.assertEqual(self.review(), 0)
        self.assertEqual(len([c for c in self.gitea.comments if publish.SUMMARY_MARKER in c["body"]]), 1)
        self.assertIn("Still one problem.", self.gitea.comments[0]["body"])
        self.assertEqual(len(self.gitea.reviews), 1)
        self.assertIn("src/app.py: Wrong default", self.prompts[1])  # earlier finding given as context

    def test_repository_rules_file_is_used(self):
        rules = Path(self.workspace) / ".gitea" / "claude"
        rules.mkdir(parents=True)
        (rules / "REVIEW.md").write_text("Public functions need docstrings.", encoding="utf-8")
        self.review(extra_prompt="Mind the units.")
        self.assertIn("Public functions need docstrings.", self.prompts[0])
        self.assertIn("Mind the units.", self.prompts[0])
        self.assertNotIn("Review priorities, highest first", self.prompts[0])

    def test_wip_pull_requests_are_skipped_or_failed(self):
        self.gitea.pull["title"] = "WIP: half done"
        self.assertEqual(self.review(), 0)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.review(wip_policy="fail"), 1)

    def test_fail_on_gates_the_job(self):
        self.assertEqual(self.review(fail_on="high"), 1)
        self.assertEqual(self.review(fail_on="blocker"), 0)

    def mention(self, author: str, body: str, is_pull: bool = True) -> int:
        event = {
            "action": "created",
            "comment": {"id": 55, "body": body, "user": {"login": author}},
            "issue": {"number": 7, "title": "Add feature", "body": "", "pull_request": {"merged": False} if is_pull else None},
        }
        return main.dispatch(config(self.workspace, "issue_comment", event), self.gitea.client())

    def test_mention_reply_requires_permission(self):
        self.answer = ClaudeAnswer(summary="Because x is read twice.")
        self.assertEqual(self.mention("mallory", "@claude why?"), 0)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.mention("stranger", "@claude why?"), 0)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.mention("alice", "@claude why is y 3?"), 0)
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("why is y 3?", self.prompts[0])
        reply = self.gitea.comments[-1]["body"]
        self.assertTrue(reply.startswith("> @claude why is y 3?"))
        self.assertIn("@alice Because x is read twice.", reply)
        self.assertNotIn(publish.SUMMARY_MARKER, reply)

    def test_mentions_without_trigger_or_from_the_bot_are_ignored(self):
        self.assertEqual(self.mention("alice", "no trigger here"), 0)
        self.assertEqual(self.mention("review-bot", "@claude loop?"), 0)
        self.assertEqual(self.prompts, [])

    def test_errors_become_warnings_unless_fail_on_error(self):
        broken = config(self.workspace, "pull_request", {"pull_request": {"number": 404}})
        with mock.patch.object(main, "load_config", return_value=broken), mock.patch.object(main, "Gitea", return_value=self.gitea.client()):
            self.assertEqual(main.main(), 0)
            broken.fail_on_error = True
            self.assertEqual(main.main(), 1)


class CredentialTest(unittest.TestCase):
    def test_persisted_checkout_header_is_removed(self):
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q", repo], check=True)
            key = "http.https://git.example.com/.extraheader"
            subprocess.run(["git", "-C", repo, "config", "--local", key, "AUTHORIZATION: basic c2VjcmV0"], check=True)
            main.strip_checkout_credentials(repo)
            out = subprocess.run(["git", "-C", repo, "config", "--local", "--get-regexp", "extraheader"],
                                 capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
