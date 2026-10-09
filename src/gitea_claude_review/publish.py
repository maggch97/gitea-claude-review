"""Turn a Claude answer into Gitea comments without piling up duplicates.

- The review summary is a single comment, found by a hidden marker and edited in
  place on every run. A review id namespaces the markers (see ``Markers``) so
  several reviews can live on one pull request.
- Each inline finding carries a fingerprint marker; a finding already posted by
  this bot (same file + title) is not posted again, unless it was marked fixed
  and came back.
- Earlier findings that Claude verified as fixed get a "fixed" banner and, on
  Gitea >= 1.26 (the first version with a resolve API), are resolved. Findings a
  person resolved are left alone.
- Findings whose line is not part of the diff cannot be anchored by Gitea; they
  are listed in the summary instead of being dropped.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .claude import ClaudeAnswer, Finding, Resolution
from .diff import FileLines
from .gitea import Gitea

# A review id may namespace the markers so several reviews can share one pull request.
REVIEW_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
# Summary marker of any review, namespaced or not: used to keep summaries out of
# the comment thread handed to the model on mentions.
ANY_SUMMARY_RE = re.compile(r"<!-- gitea-claude-review:(?:[a-z0-9_-]+:)?summary -->")


@dataclass(frozen=True)
class Markers:
    """Hidden HTML comments that identify this review's comments on Gitea.

    ``review_id`` namespaces them, so independent reviews (say a correctness review
    and a design review) can share one bot account and one pull request without
    editing each other's summary or re-checking each other's findings. The empty id
    keeps the original marker texts: comments posted before the option existed stay
    recognized.
    """

    review_id: str = ""

    @property
    def prefix(self) -> str:
        return "gitea-claude-review:" + (f"{self.review_id}:" if self.review_id else "")

    @property
    def summary(self) -> str:
        return f"<!-- {self.prefix}summary -->"

    def finding(self, fp: str) -> str:
        return f"<!-- {self.prefix}finding:{fp} -->"

    def fixed(self, sha: str) -> str:
        return f"<!-- {self.prefix}fixed:{sha} -->"

    @property
    def finding_re(self) -> re.Pattern:
        # The literal "<!-- " + prefix keeps one review from matching another's markers.
        return re.compile(rf"<!-- {re.escape(self.prefix)}finding:([0-9a-f]{{12}}) -->")

    @property
    def fixed_re(self) -> re.Pattern:
        return re.compile(rf"<!-- {re.escape(self.prefix)}fixed:[0-9a-f]+ -->")


_TITLE_RE = re.compile(r"\*\*\[[a-z]+\] (.+?)\*\*")
_SEVERITY_ICON = {"blocker": "🛑", "high": "🔴", "medium": "🟠", "low": "🟡"}


def fingerprint(finding: Finding) -> str:
    title = re.sub(r"\s+", " ", finding.title.strip().lower())
    return hashlib.sha1(f"{finding.path}\n{title}".encode("utf-8")).hexdigest()[:12]


def split_anchorable(findings: list[Finding], lines: dict[str, FileLines]) -> tuple[list[Finding], list[Finding]]:
    anchored, loose = [], []
    for f in findings:
        file_lines = lines.get(f.path)
        ok = file_lines is not None and f.line > 0 and f.line in (file_lines.new if f.side == "new" else file_lines.old)
        (anchored if ok else loose).append(f)
    return anchored, loose


def finding_text(f: Finding) -> str:
    return f"{_SEVERITY_ICON.get(f.severity, '•')} **[{f.severity}] {f.title}**\n\n{f.body}".strip()


def loose_section(loose: list[Finding]) -> str:
    if not loose:
        return ""
    # Line zero explicitly identifies a file-level design issue, not a fabricated code location.
    rows = "\n".join(
        f"- {_SEVERITY_ICON.get(f.severity, '•')} **[{f.severity}]** "
        f"`{f.path}{':' + str(f.line) if f.line > 0 else ''}` — {f.title}\n  {f.body}"
        for f in loose
    )
    return "\n\n#### Findings outside the diff\n" + rows


@dataclass
class Footer:
    head_sha: str
    model: str
    cost_usd: float | None = None
    turns: int | None = None
    show_cost: bool = False
    reviewer: str = "Claude"


def footer_text(footer: Footer) -> str:
    parts = [f"commit `{footer.head_sha[:12]}`"] if footer.head_sha else []
    if footer.model:
        parts.append(f"model `{footer.model}`")
    if footer.show_cost and footer.cost_usd is not None:
        parts.append(f"cost ${footer.cost_usd:.2f}")
    if footer.show_cost and footer.turns:
        parts.append(f"{footer.turns} turns")
    return "\n\n<sub>" + footer.reviewer + " review · " + " · ".join(parts) + "</sub>" if parts else ""


@dataclass
class PostedFinding:
    """An inline finding this bot posted earlier, in its current state on Gitea."""

    comment_id: int
    fingerprint: str
    path: str
    line: int
    title: str
    body: str
    fixed: bool  # this action already marked it fixed
    resolved_by: str  # login that resolved the conversation; "" while unresolved

    @property
    def open(self) -> bool:
        return not self.fixed and not self.resolved_by


def posted_findings(gitea: Gitea, number: int, bot_login: str, markers: Markers = Markers()) -> list[PostedFinding]:
    """Inline findings this bot posted on the pull request, oldest first."""
    found: list[PostedFinding] = []
    for review in gitea.reviews(number):
        if (review.get("user") or {}).get("login") != bot_login:
            continue
        for comment in gitea.review_comments(number, review["id"]):
            body = comment.get("body") or ""
            marks = markers.finding_re.findall(body)
            if not marks:
                continue
            title = _TITLE_RE.search(body)
            found.append(PostedFinding(
                comment_id=int(comment["id"]),
                fingerprint=marks[0],
                path=comment.get("path", ""),
                line=int(comment.get("position") or comment.get("original_position") or 0),
                title=title.group(1) if title else body[:80],
                body=body,
                fixed=bool(markers.fixed_re.search(body)),
                resolved_by=(comment.get("resolver") or {}).get("login") or "",
            ))
    return found


def suppressed(posted: list[PostedFinding], fixed_now: frozenset[int] | set[int] = frozenset()) -> set[str]:
    """Fingerprints not to post again: findings still open, or resolved by a person.

    A finding marked fixed (earlier, or ``fixed_now`` in this run) may be posted
    again when it comes back.
    """
    return {p.fingerprint for p in posted if not p.fixed and p.comment_id not in fixed_now}


def supports_resolve_api(version: str) -> bool:
    """POST /pulls/comments/{id}/resolve exists from Gitea 1.26 on (go-gitea/gitea#36441)."""
    match = re.match(r"(\d+)\.(\d+)", version or "")
    return bool(match) and (int(match.group(1)), int(match.group(2))) >= (1, 26)


def fixed_text(body: str, head_sha: str, note: str, markers: Markers = Markers()) -> str:
    """Put a fixed banner above an earlier finding; its text and markers stay below."""
    banner = f"✅ **Fixed in `{head_sha[:12]}`**" + (f": {note}" if note else "")
    return f"{markers.fixed(head_sha[:12])}\n{banner}\n\n---\n\n{body}"


def resolve_fixed(gitea: Gitea, head_sha: str, posted: list[PostedFinding], resolutions: list[Resolution],
                  resolve_api: bool, markers: Markers = Markers()) -> list[tuple[PostedFinding, str]]:
    """Mark the earlier findings Claude verified as fixed; returns (finding, note) for each.

    Only open findings of this bot are touched. Unknown ids, findings a person
    resolved (their call stands) and findings already marked fixed are skipped.
    """
    open_by_id = {p.comment_id: p for p in posted if p.open}
    done: list[tuple[PostedFinding, str]] = []
    for resolution in resolutions:
        finding = open_by_id.pop(resolution.comment_id, None)
        if finding is None:
            print(f"Ignoring resolution of comment {resolution.comment_id}: not an open finding of this bot", flush=True)
            continue
        gitea.edit_comment(finding.comment_id, fixed_text(finding.body, head_sha, resolution.note, markers))
        if resolve_api:
            gitea.resolve_review_comment(finding.comment_id)
        done.append((finding, resolution.note))
    return done


def resolved_section(done: list[tuple[PostedFinding, str]]) -> str:
    if not done:
        return ""
    rows = "\n".join(f"- ✅ `{p.path}` — {p.title}" + (f"\n  {note}" if note else "") for p, note in done)
    return "\n\n#### Fixed since the previous review\n" + rows


def upsert_summary(gitea: Gitea, number: int, bot_login: str, body: str, markers: Markers = Markers()) -> None:
    text = markers.summary + "\n" + body
    for comment in gitea.issue_comments(number):
        if (comment.get("user") or {}).get("login") == bot_login and markers.summary in (comment.get("body") or ""):
            gitea.edit_comment(comment["id"], text)
            return
    gitea.create_comment(number, text)


def post_inline(gitea: Gitea, number: int, head_sha: str, findings: list[Finding], already: set[str], body: str,
                markers: Markers = Markers()) -> int:
    """Post anchored findings not posted before as one review; returns how many were posted."""
    fresh = [f for f in findings if fingerprint(f) not in already]
    if not fresh:
        return 0
    comments = [
        {
            "path": f.path,
            "body": finding_text(f) + "\n\n" + markers.finding(fingerprint(f)),
            "new_position": f.line if f.side == "new" else 0,
            "old_position": f.line if f.side == "old" else 0,
        }
        for f in fresh
    ]
    gitea.create_review(number, head_sha, body.format(count=len(fresh)), comments)
    return len(fresh)


def publish_review(gitea: Gitea, number: int, head_sha: str, bot_login: str, answer: ClaudeAnswer,
                   lines: dict[str, FileLines], footer: Footer, posted: list[PostedFinding], resolve_api: bool,
                   inline: bool = True, markers: Markers = Markers()) -> dict[str, int]:
    anchored, loose = split_anchorable(answer.findings, lines) if inline else ([], list(answer.findings))
    done = resolve_fixed(gitea, head_sha, posted, answer.resolved, resolve_api, markers)
    summary = (answer.summary or "_No summary._") + loose_section(loose) + resolved_section(done) + footer_text(footer)
    upsert_summary(gitea, number, bot_login, summary, markers)
    already = suppressed(posted, {p.comment_id for p, _ in done})
    # An empty review id preserves existing comments when switching providers.
    new = post_inline(gitea, number, head_sha, anchored, already,
                      footer.reviewer + " review: {count} new inline finding(s).", markers)
    return {"inline": new, "duplicates": len(anchored) - new, "outside_diff": len(loose), "fixed": len(done)}
