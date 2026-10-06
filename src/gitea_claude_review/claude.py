"""Run Claude Code headless and read its structured answer.

The model never receives the Gitea token: the subprocess gets an allowlisted
environment, only read-only tools, and returns JSON that this action posts.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

BEGIN = "BEGIN_GITEA_REVIEW_JSON"
END = "END_GITEA_REVIEW_JSON"

READ_ONLY_TOOLS = ["Read", "Grep", "Glob", "LS"]
DENIED_TOOLS = ["Edit", "MultiEdit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Task"]
SEVERITIES = ("blocker", "high", "medium", "low")

# Environment variables Claude Code may see. Everything else (GITEA_TOKEN,
# INPUT_*, GITHUB_TOKEN, runner secrets …) is dropped.
_ENV_ALLOW = {
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "TERM", "USER", "SHELL",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
}
_ENV_ALLOW_PREFIXES = ("CLAUDE_CODE_", "ANTHROPIC_")


@dataclass
class Finding:
    path: str
    line: int
    side: str
    severity: str
    title: str
    body: str


@dataclass
class ClaudeAnswer:
    summary: str
    findings: list[Finding] = field(default_factory=list)
    structured: bool = True
    cost_usd: float | None = None
    turns: int | None = None


def claude_env(source: dict[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if source is None else source)
    return {
        k: v
        for k, v in source.items()
        if (k in _ENV_ALLOW or k.startswith(_ENV_ALLOW_PREFIXES)) and v != ""
    }


def build_command(claude_bin: str, model: str, max_turns: int, allowed_bash: list[str]) -> list[str]:
    allowed = READ_ONLY_TOOLS + [f"Bash({pattern})" for pattern in allowed_bash]
    cmd = [
        claude_bin, "-p",
        "--output-format", "json",
        "--max-turns", str(max_turns),
        "--allowedTools", ",".join(allowed),
        "--disallowedTools", ",".join(DENIED_TOOLS),
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def _finding(raw: Any) -> Finding | None:
    if not isinstance(raw, dict):
        return None
    try:
        line = int(raw.get("line"))
    except (TypeError, ValueError):
        line = 0
    severity = str(raw.get("severity", "medium")).lower()
    return Finding(
        path=str(raw.get("path", "")).strip().removeprefix("a/").removeprefix("b/"),
        line=line,
        side="old" if str(raw.get("side", "new")).lower() == "old" else "new",
        severity=severity if severity in SEVERITIES else "medium",
        title=str(raw.get("title", "")).strip()[:200] or "Finding",
        body=str(raw.get("body", "")).strip(),
    )


def parse_answer(text: str) -> ClaudeAnswer:
    """Extract the JSON block; fall back to posting the raw text as the summary."""
    match = re.search(re.escape(BEGIN) + r"\s*(\{.*?\})\s*" + re.escape(END), text, re.S)
    if not match:
        return ClaudeAnswer(summary=text.strip(), structured=False)
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ClaudeAnswer(summary=text.strip(), structured=False)
    findings = [f for f in (_finding(x) for x in data.get("findings") or []) if f]
    summary = str(data.get("summary") or "").strip()
    if not summary:
        # The model put prose outside the block: keep it.
        summary = (text[: match.start()] + text[match.end():]).strip()
    return ClaudeAnswer(summary=summary, findings=findings)


def run_claude(prompt: str, cmd: list[str], cwd: str, timeout_s: int) -> ClaudeAnswer:
    proc = subprocess.run(
        cmd, input=prompt, text=True, capture_output=True, cwd=cwd, env=claude_env(), timeout=timeout_s
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError(f"Claude Code exited with {proc.returncode}: {proc.stderr.strip()[-1500:]}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return parse_answer(proc.stdout)
    if envelope.get("is_error"):
        raise RuntimeError(f"Claude Code reported an error: {str(envelope.get('result', ''))[:1500]}")
    answer = parse_answer(str(envelope.get("result", "")))
    answer.cost_usd = envelope.get("total_cost_usd")
    answer.turns = envelope.get("num_turns")
    return answer
