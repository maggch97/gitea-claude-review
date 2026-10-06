"""Fake Gitea server for tests: routes REST calls to an in-memory state."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gitea_claude_review.gitea import Gitea  # noqa: E402

DIFF = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,5 @@
 import os
-x = 1
+x = 2
+y = 3

 def main():
diff --git a/docs/old.md b/docs/old.md
deleted file mode 100644
--- a/docs/old.md
+++ /dev/null
@@ -1,2 +0,0 @@
-# Old
-text
"""


class FakeGitea:
    def __init__(self, bot: str = "review-bot"):
        self.bot = bot
        self.permissions: dict[str, str] = {"alice": "write", "mallory": "read"}
        self.pull = {
            "number": 7, "title": "Add feature", "body": "Implements it", "draft": False,
            "head": {"sha": "abcdef1234567890", "ref": "feature"}, "base": {"ref": "main"},
        }
        self.diff = DIFF
        self.comments: list[dict] = []
        self.reviews: list[dict] = []
        self.calls: list[tuple[str, str, object]] = []
        self.next_id = 100

    def transport(self, method: str, url: str, headers: dict, data: bytes | None):
        assert headers["Authorization"] == "token secret-token"
        path = url.split("/api/v1", 1)[1]
        body = json.loads(data) if data else None
        self.calls.append((method, path, body))
        base = "/repos/team/app"
        p = path.split("?", 1)[0]
        query = path.split("?", 1)[1] if "?" in path else ""
        if p == "/user":
            return 200, json.dumps({"login": self.bot}).encode()
        m = re.fullmatch(base + r"/collaborators/([^/]+)/permission", p)
        if m:
            perm = self.permissions.get(m.group(1))
            return (200, json.dumps({"permission": perm}).encode()) if perm else (404, b"{}")
        if p == f"{base}/pulls/7":
            return 200, json.dumps(self.pull).encode()
        if p == f"{base}/pulls/7.diff":
            return 200, self.diff.encode()
        if p == f"{base}/issues/7/comments" and method == "GET":
            page = int(re.search(r"page=(\d+)", query).group(1)) if "page=" in query else 1
            return 200, json.dumps(self.comments if page == 1 else []).encode()
        if p == f"{base}/issues/7/comments" and method == "POST":
            self.next_id += 1
            item = {"id": self.next_id, "body": body["body"], "user": {"login": self.bot}}
            self.comments.append(item)
            return 201, json.dumps(item).encode()
        m = re.fullmatch(base + r"/issues/comments/(\d+)", p)
        if m and method == "PATCH":
            for c in self.comments:
                if c["id"] == int(m.group(1)):
                    c["body"] = body["body"]
                    return 200, json.dumps(c).encode()
            return 404, b"{}"
        if p == f"{base}/pulls/7/reviews" and method == "GET":
            page = int(re.search(r"page=(\d+)", query).group(1)) if "page=" in query else 1
            data = [{"id": r["id"], "user": r["user"]} for r in self.reviews]
            return 200, json.dumps(data if page == 1 else []).encode()
        if p == f"{base}/pulls/7/reviews" and method == "POST":
            self.next_id += 1
            review = {"id": self.next_id, "user": {"login": self.bot}, "body": body["body"], "commit_id": body["commit_id"],
                      "comments": body["comments"]}
            self.reviews.append(review)
            return 200, json.dumps({"id": review["id"]}).encode()
        m = re.fullmatch(base + r"/pulls/7/reviews/(\d+)/comments", p)
        if m:
            for r in self.reviews:
                if r["id"] == int(m.group(1)):
                    return 200, json.dumps(r["comments"]).encode()
            return 404, b"[]"
        return 404, json.dumps({"message": f"no route {method} {path}"}).encode()

    def client(self) -> Gitea:
        return Gitea("https://git.example.com", "secret-token", "team", "app", transport=self.transport)
