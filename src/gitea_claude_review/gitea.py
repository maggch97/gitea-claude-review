"""Minimal Gitea REST client (API v1), standard library only."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

Transport = Callable[[str, str, "dict[str, str]", Optional[bytes]], "tuple[int, bytes]"]


class GiteaError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: str):
        what = f"returned HTTP {status}" if status else "got no response"
        super().__init__(f"Gitea API {method} {path} {what}: {body[:300]}")
        self.status = status


REQUEST_TIMEOUT_S = 120
RETRY_DELAYS_S = (3, 10)  # reads only: 3 attempts in total
RETRYABLE_STATUS = {502, 503, 504}


def _urllib_transport(method: str, url: str, headers: dict[str, str], data: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class Gitea:
    def __init__(self, server_url: str, token: str, owner: str, repo: str, transport: Transport | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.api = server_url.rstrip("/") + "/api/v1"
        self.owner = owner
        self.repo = repo
        self._token = token
        self._transport = transport or _urllib_transport
        self._sleep = sleep

    # ── plumbing ──────────────────────────────────────────────────────

    def _send(self, method: str, path: str, headers: dict[str, str], data: bytes | None) -> tuple[int, bytes]:
        """Reads are retried on timeouts and gateway errors; writes never (no double posts)."""
        attempts = 1 + (len(RETRY_DELAYS_S) if method == "GET" else 0)
        for attempt in range(attempts):
            try:
                status, payload = self._transport(method, self.api + path, headers, data)
            except (OSError, urllib.error.URLError) as error:  # timeouts, resets, DNS
                if attempt + 1 >= attempts:
                    raise GiteaError(0, method, path, f"request failed after {attempts} attempt(s): {error}") from error
            else:
                if status not in RETRYABLE_STATUS or attempt + 1 >= attempts:
                    return status, payload
            delay = RETRY_DELAYS_S[attempt]
            print(f"Gitea {method} {path} did not answer in time; retrying in {delay}s", flush=True)
            self._sleep(delay)
        raise AssertionError("unreachable")

    def _request(self, method: str, path: str, body: Any = None, *, raw: bool = False, accept_404: bool = False) -> Any:
        headers = {"Authorization": f"token {self._token}", "Accept": "text/plain" if raw else "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        status, payload = self._send(method, path, headers, data)
        if accept_404 and status == 404:
            return None
        if status < 200 or status >= 300:
            raise GiteaError(status, method, path, payload.decode("utf-8", "replace"))
        if raw:
            return payload.decode("utf-8", "replace")
        if not payload:
            return None
        return json.loads(payload.decode("utf-8"))

    @property
    def _repo(self) -> str:
        return f"/repos/{urllib.parse.quote(self.owner, safe='')}/{urllib.parse.quote(self.repo, safe='')}"

    def _paged(self, path: str, limit: int = 50, max_pages: int = 20) -> list[Any]:
        items: list[Any] = []
        sep = "&" if "?" in path else "?"
        for page in range(1, max_pages + 1):
            batch = self._request("GET", f"{path}{sep}page={page}&limit={limit}") or []
            items.extend(batch)
            if len(batch) < limit:
                break
        return items

    # ── calls ─────────────────────────────────────────────────────────

    def current_user(self) -> dict[str, Any]:
        return self._request("GET", "/user")

    def permission(self, username: str) -> str:
        """Repository permission of a user: none / read / write / admin / owner."""
        data = self._request(
            "GET", f"{self._repo}/collaborators/{urllib.parse.quote(username, safe='')}/permission", accept_404=True
        )
        return (data or {}).get("permission", "none") or "none"

    def pull(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"{self._repo}/pulls/{number}")

    def pull_diff(self, number: int) -> str:
        return self._request("GET", f"{self._repo}/pulls/{number}.diff", raw=True)

    def issue(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"{self._repo}/issues/{number}")

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        return self._paged(f"{self._repo}/issues/{number}/comments")

    def create_comment(self, number: int, body: str) -> dict[str, Any]:
        return self._request("POST", f"{self._repo}/issues/{number}/comments", {"body": body})

    def edit_comment(self, comment_id: int, body: str) -> dict[str, Any]:
        return self._request("PATCH", f"{self._repo}/issues/comments/{comment_id}", {"body": body})

    def reviews(self, number: int) -> list[dict[str, Any]]:
        return self._paged(f"{self._repo}/pulls/{number}/reviews")

    def review_comments(self, number: int, review_id: int) -> list[dict[str, Any]]:
        return self._request("GET", f"{self._repo}/pulls/{number}/reviews/{review_id}/comments") or []

    def create_review(self, number: int, commit_id: str, body: str, comments: list[dict[str, Any]]) -> dict[str, Any]:
        return self._request(
            "POST",
            f"{self._repo}/pulls/{number}/reviews",
            {"event": "COMMENT", "commit_id": commit_id, "body": body, "comments": comments},
        )
