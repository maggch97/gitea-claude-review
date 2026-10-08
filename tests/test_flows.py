from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import FakeGitea

from gitea_claude_review import main, publish
from gitea_claude_review.claude import ClaudeAnswer, Finding, Resolution


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
        summaries = [c for c in self.gitea.comments if publish.Markers().summary in c["body"]]
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
        self.assertEqual(len([c for c in self.gitea.comments if publish.Markers().summary in c["body"]]), 1)
        self.assertIn("Still one problem.", self.gitea.comments[0]["body"])
        self.assertEqual(len(self.gitea.reviews), 1)
        posted_id = self.gitea.reviews[0]["comments"][0]["id"]
        self.assertIn(f"id {posted_id}: `src/app.py` (line 3 when posted) — Wrong default", self.prompts[1])

    def first_finding(self) -> dict:
        """Run one review that posts the "Wrong default" finding; return its review comment."""
        self.review()
        return self.gitea.reviews[0]["comments"][0]

    def push(self, sha: str, answer: ClaudeAnswer) -> None:
        self.gitea.pull["head"]["sha"] = sha
        self.answer = answer
        self.assertEqual(self.review(), 0)

    def test_fixed_findings_are_marked_and_resolved_on_gitea_1_26(self):
        self.gitea.version = "1.26.0"
        comment = self.first_finding()
        self.push("1111111111110000", ClaudeAnswer(summary="Fixed.", resolved=[Resolution(comment["id"], "y is now 2.")]))
        self.assertTrue(comment["body"].startswith("<!-- gitea-claude-review:fixed:111111111111 -->\n✅ **Fixed in `111111111111`**: y is now 2."))
        self.assertIn("**[high] Wrong default**", comment["body"])  # original text kept below the banner
        self.assertEqual(comment["resolver"], {"login": "review-bot"})
        summary = self.gitea.comments[0]["body"]
        self.assertIn("#### Fixed since the previous review\n- ✅ `src/app.py` — Wrong default\n  y is now 2.", summary)

        # A fixed finding is no longer handed to Claude; if it comes back, it is posted again.
        self.push("2222222222220000", ClaudeAnswer(summary="Regressed.", findings=[Finding("src/app.py", 3, "new", "high", "Wrong default", "back")]))
        self.assertNotIn(f"id {comment['id']}", self.prompts[-1])
        self.assertEqual(len(self.gitea.reviews), 2)
        self.assertNotIn("Fixed since", self.gitea.comments[0]["body"])

    def test_on_gitea_1_25_fixed_findings_only_get_the_banner(self):
        comment = self.first_finding()
        self.push("1111111111110000", ClaudeAnswer(summary="Fixed.", resolved=[Resolution(comment["id"], "")]))
        self.assertIn("✅ **Fixed in `111111111111`**\n", comment["body"])
        self.assertIsNone(comment["resolver"])
        self.assertFalse(any(path.endswith("/resolve") for _, path, _ in self.gitea.calls))
        self.push("2222222222220000", ClaudeAnswer(summary="ok"))
        self.assertNotIn(f"id {comment['id']}", self.prompts[-1])

    def test_only_open_findings_of_the_bot_can_be_marked_fixed(self):
        self.gitea.version = "1.27.3"
        comment = self.first_finding()
        comment["resolver"] = {"login": "alice"}  # a person resolved it: their decision stands
        before = comment["body"]
        self.push("1111111111110000", ClaudeAnswer(
            summary="s", resolved=[Resolution(comment["id"], "x"), Resolution(999999, "made up")],
            findings=[Finding("src/app.py", 3, "new", "high", "Wrong default", "again")],
        ))
        self.assertEqual(comment["body"], before)
        self.assertNotIn(f"id {comment['id']}", self.prompts[-1])
        self.assertEqual(len(self.gitea.reviews), 1)  # accepted by a person: not posted again
        self.assertFalse(any(m == "PATCH" and "/issues/comments/" in p and p.endswith(str(comment["id"]))
                             for m, p, _ in self.gitea.calls))

    def test_reviews_with_different_ids_share_a_pull_request_without_touching_each_other(self):
        self.gitea.version = "1.26.0"
        self.review()  # the default review posts its summary and the "Wrong default" finding
        default_finding = self.gitea.reviews[0]["comments"][0]
        design_answer = ClaudeAnswer(summary="Wrong layer.", findings=[Finding("src/app.py", 3, "new", "medium", "Belongs elsewhere", "move it")])
        self.answer = design_answer
        self.assertEqual(self.review(review_id="design"), 0)
        summaries = sorted(c["body"].splitlines()[0] for c in self.gitea.comments)
        self.assertEqual(summaries, ["<!-- gitea-claude-review:design:summary -->", "<!-- gitea-claude-review:summary -->"])
        self.assertEqual(len(self.gitea.reviews), 2)
        design_finding = self.gitea.reviews[1]["comments"][0]
        self.assertIn("<!-- gitea-claude-review:design:finding:", design_finding["body"])
        # The design review was not told about the default review's open finding.
        self.assertNotIn(f"id {default_finding['id']}", self.prompts[1])

        # Each review edits only its own summary, and may only close out its own findings.
        self.gitea.pull["head"]["sha"] = "1234567890abcdef"
        self.answer = ClaudeAnswer(summary="Design ok now.", resolved=[Resolution(default_finding["id"], "not mine")])
        self.assertEqual(self.review(review_id="design"), 0)
        self.assertEqual(len(self.gitea.comments), 2)
        self.assertIn("Design ok now.", [c["body"] for c in self.gitea.comments if "design:summary" in c["body"]][0])
        self.assertIn("One real problem.", [c["body"] for c in self.gitea.comments if "design:summary" not in c["body"]][0])
        self.assertNotIn("Fixed in", default_finding["body"])
        self.assertIn(f"id {design_finding['id']}", self.prompts[2])
        self.assertNotIn(f"id {default_finding['id']}", self.prompts[2])

        self.answer = ClaudeAnswer(summary="Still one problem.")
        self.assertEqual(self.review(), 0)
        self.assertIn(f"id {default_finding['id']}", self.prompts[3])
        self.assertNotIn(f"id {design_finding['id']}", self.prompts[3])
        self.assertEqual(len(self.gitea.comments), 2)

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

    def test_results_are_discarded_when_the_pull_request_moved_on(self):
        def model(cfg, prompt):
            self.gitea.pull["head"]["sha"] = "fffffffffff00000"  # someone pushed meanwhile
            return self.answer
        with mock.patch.object(main, "run_model", side_effect=model):
            self.assertEqual(self.review(), 0)
        self.assertEqual(self.gitea.comments, [])
        self.assertEqual(self.gitea.reviews, [])

    def test_superseded_runs_do_not_review(self):
        event = {"action": "synchronize", "pull_request": {"number": 7, "head": {"sha": "0000000older0000"}}}
        self.assertEqual(main.dispatch(config(self.workspace, "pull_request", event), self.gitea.client()), 0)
        self.assertEqual(self.prompts, [])
        current = {"action": "synchronize", "pull_request": {"number": 7, "head": {"sha": "abcdef1234567890"}}}
        main.dispatch(config(self.workspace, "pull_request", current), self.gitea.client())
        self.assertEqual(len(self.prompts), 1)

    def test_edits_only_review_when_the_wip_marker_is_removed(self):
        def edited(old_title, new_title):
            self.gitea.pull["title"] = new_title
            event = {"action": "edited", "changes": {"title": {"from": old_title}},
                     "pull_request": {"number": 7, "title": new_title}}
            return main.dispatch(config(self.workspace, "pull_request", event), self.gitea.client())
        edited("Add feature", "Add feature v2")
        self.assertEqual(self.prompts, [])
        body_only = {"action": "edited", "changes": {"body": {"from": "x"}}, "pull_request": {"number": 7, "title": "Add"}}
        main.dispatch(config(self.workspace, "pull_request", body_only), self.gitea.client())
        self.assertEqual(self.prompts, [])
        edited("WIP: Add feature", "Add feature")
        self.assertEqual(len(self.prompts), 1)

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
        self.assertNotIn(publish.Markers().summary, reply)

    def test_mentions_without_trigger_or_from_the_bot_are_ignored(self):
        self.assertEqual(self.mention("alice", "no trigger here"), 0)
        self.assertEqual(self.mention("review-bot", "@claude loop?"), 0)
        self.assertEqual(self.prompts, [])

    def test_invalid_review_id_fails_before_anything_runs(self):
        bad = config(self.workspace, "pull_request", {"pull_request": {"number": 7}}, review_id="Design Review")
        with mock.patch.object(main, "load_config", return_value=bad), mock.patch.object(main, "Gitea", return_value=self.gitea.client()):
            self.assertEqual(main.main(), 1)
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.gitea.comments, [])

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
