"""Entry point: route the Gitea Actions event to a review or a mention reply."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import claude, codex, prompts, publish
from .diff import changed_files, parse_unified_diff
from .gitea import Gitea

WORK_DIR = ".gitea-claude-review"
PERMISSION_RANK = {"none": 0, "read": 1, "write": 2, "admin": 3, "owner": 4}
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "blocker": 4}
WIP_RE = re.compile(r"^\s*(wip:|\[wip\])", re.I)


@dataclass
class Config:
    gitea_token: str
    server_url: str
    repository: str
    event_name: str
    event: dict
    workspace: str
    model: str = ""
    claude_bin: str = "claude"
    rules_file: str = ".gitea/claude/REVIEW.md"
    review_id: str = ""
    extra_prompt: str = ""
    language: str = ""
    trigger_phrase: str = "@claude"
    mention_permission: str = "write"
    max_turns: int = 30
    timeout_minutes: int = 30
    allowed_bash: list[str] = field(default_factory=list)
    inline_comments: bool = True
    wip_policy: str = "skip"
    fail_on: str = "none"
    fail_on_error: bool = False
    show_cost: bool = False
    provider: str = "claude"
    codex_bin: str = "codex"
    codex_effort: str = ""
    codex_yolo: bool = False
    codex_auth_mode: str = "chatgpt"
    codex_home: str = ""
    codex_auth_json: str = ""
    openai_api_key: str = ""
    codex_access_token: str = ""
    codex_auth_storage: str = "gitea-secret"
    codex_secret_name: str = "CODEX_AUTH_JSON"
    codex_secrets_token: str = ""

    @property
    def reviewer(self) -> str:
        return "Codex" if self.provider == "codex" else "Claude"


def _bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _codex_yolo(value: str) -> bool:
    # Permission settings must reject typos instead of silently selecting a mode.
    normalized = value.strip().lower()
    if normalized not in ("", "true", "false"):
        raise ValueError("codex_yolo must be true or false")
    return normalized == "true"


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    get = lambda name, default="": env.get(f"GCR_{name}", default).strip()  # noqa: E731
    event_path = env.get("GITHUB_EVENT_PATH") or env.get("GITEA_EVENT_PATH") or ""
    event = json.loads(Path(event_path).read_text(encoding="utf-8")) if event_path else {}
    allowed = get("ALLOWED_BASH") or "git diff:*,git log:*,git show:*,git blame:*,git grep:*,git status:*"
    return Config(
        gitea_token=get("GITEA_TOKEN"),
        server_url=get("SERVER_URL") or env.get("GITHUB_SERVER_URL") or env.get("GITEA_SERVER_URL", ""),
        repository=env.get("GITHUB_REPOSITORY") or env.get("GITEA_REPOSITORY", ""),
        event_name=env.get("GITHUB_EVENT_NAME") or env.get("GITEA_EVENT_NAME", ""),
        event=event,
        workspace=env.get("GITHUB_WORKSPACE") or os.getcwd(),
        model=get("MODEL"),
        claude_bin=get("CLAUDE_BIN") or "claude",
        rules_file=get("RULES_FILE") or ".gitea/claude/REVIEW.md",
        review_id=get("REVIEW_ID"),
        extra_prompt=get("EXTRA_PROMPT"),
        language=get("LANGUAGE"),
        trigger_phrase=get("TRIGGER_PHRASE") or "@claude",
        mention_permission=(get("MENTION_PERMISSION") or "write").lower(),
        max_turns=int(get("MAX_TURNS") or 30),
        timeout_minutes=int(get("TIMEOUT_MINUTES") or 30),
        allowed_bash=[p.strip() for p in allowed.split(",") if p.strip()],
        inline_comments=_bool(get("INLINE_COMMENTS"), True),
        wip_policy=(get("WIP_POLICY") or "skip").lower(),
        fail_on=(get("FAIL_ON") or "none").lower(),
        fail_on_error=_bool(get("FAIL_ON_ERROR"), False),
        show_cost=_bool(get("SHOW_COST"), False),
        provider=(get("PROVIDER") or "claude").lower(),
        codex_bin=get("CODEX_BIN") or "codex",
        codex_effort=get("CODEX_EFFORT"),
        codex_yolo=_codex_yolo(get("CODEX_YOLO")),
        codex_auth_mode=get("CODEX_AUTH_MODE") or "chatgpt",
        codex_home=get("CODEX_HOME"),
        codex_auth_json=get("CODEX_AUTH_JSON"),
        openai_api_key=get("OPENAI_API_KEY"),
        codex_access_token=get("CODEX_ACCESS_TOKEN"),
        codex_auth_storage=get("CODEX_AUTH_STORAGE") or "gitea-secret",
        codex_secret_name=get("CODEX_SECRET_NAME") or "CODEX_AUTH_JSON",
        codex_secrets_token=get("CODEX_SECRETS_TOKEN"),
    )


def log(message: str) -> None:
    print(message, flush=True)


def annotate(kind: str, message: str) -> None:
    print(f"::{kind}::{message}", flush=True)


# ── git helpers ──────────────────────────────────────────────────────────

def git(workspace: str, *args: str, check: bool = True, token: str = "") -> str:
    env = None
    if token:
        # One-shot auth header through GIT_CONFIG_* (git >= 2.31): never written to
        # .git/config and not visible in the process arguments.
        env = dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: token {token}")
    proc = subprocess.run(["git", *args], cwd=workspace, text=True, capture_output=True, env=env)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def checkout_pull_head(workspace: str, number: int, sha: str, token: str = "") -> None:
    """Move the checkout to the PR head (comment events start on the default branch)."""
    current = git(workspace, "rev-parse", "HEAD", check=False).strip()
    if current == sha:
        return
    git(workspace, "fetch", "--no-tags", "origin", f"+refs/pull/{number}/head:refs/remotes/origin/pr-{number}", token=token)
    git(workspace, "checkout", "--detach", "--quiet", sha)


def strip_checkout_credentials(workspace: str) -> None:
    """actions/checkout may persist the job token in .git/config; agents can read files."""
    out = git(workspace, "config", "--local", "--name-only", "--get-regexp", r"^http\..*\.extraheader$", check=False)
    for key in sorted({line.strip() for line in out.splitlines() if line.strip()}):
        git(workspace, "config", "--local", "--unset-all", key, check=False)
    if out.strip():
        log("Removed persisted checkout credentials from .git/config before running the reviewer.")


def read_rules(cfg: Config) -> str:
    path = Path(cfg.workspace) / cfg.rules_file
    rules = path.read_text(encoding="utf-8") if cfg.rules_file and path.is_file() else ""
    if cfg.extra_prompt:
        rules = (rules + "\n\n" + cfg.extra_prompt).strip()
    return rules


def write_diff(cfg: Config, number: int, diff_text: str) -> str:
    folder = Path(cfg.workspace) / WORK_DIR
    folder.mkdir(exist_ok=True)
    (folder / ".gitignore").write_text("*\n", encoding="utf-8")
    rel = f"{WORK_DIR}/pr-{number}.diff"
    (Path(cfg.workspace) / rel).write_text(diff_text, encoding="utf-8")
    return rel


def run_model(cfg: Config, prompt: str) -> claude.ClaudeAnswer:
    started = time.monotonic()
    if cfg.provider == "codex":
        mode = "YOLO" if cfg.codex_yolo else "read-only"
        log(f"Running Codex ({cfg.model or 'default model'}, auth: {cfg.codex_auth_mode}, mode: {mode})")
        # Codex enforces JSON Schema directly; sentinel lines are Claude's contract.
        prompt = prompt.replace(prompts.OUTPUT_CONTRACT, prompts.CODEX_OUTPUT_CONTRACT)
        persist_auth = None
        if cfg.codex_auth_mode == "chatgpt" and cfg.codex_auth_storage == "gitea-secret":
            if not cfg.codex_secrets_token or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", cfg.codex_secret_name):
                raise ValueError("Secret auth requires codex_secrets_token and an uppercase codex_secret_name")
            if not cfg.server_url.startswith("https://"):
                raise ValueError("Codex Secret write-back requires an HTTPS Gitea server")
            if cfg.codex_home or cfg.openai_api_key or cfg.codex_access_token:
                raise ValueError("Secret auth cannot use codex_home or API/access tokens")
            codex.read_auth(cfg.codex_auth_json)
            owner, repo = cfg.repository.split("/", 1)
            secret_client = Gitea(cfg.server_url, cfg.codex_secrets_token, owner, repo)
            secret_client.check_secret_access(cfg.codex_secret_name)
            # Listing Secrets alone does not prove the token has write scopes.
            # Verify a same-value PUT before the CLI can rotate the credential.
            secret_client.update_secret(cfg.codex_secret_name, cfg.codex_auth_json)
            persist_auth = lambda value: secret_client.update_secret(cfg.codex_secret_name, value)
        answer = codex.run_codex(
            prompt, cfg.codex_bin, cfg.model, cfg.codex_effort, cfg.workspace, cfg.timeout_minutes * 60,
            auth_mode=cfg.codex_auth_mode, account_home=cfg.codex_home, auth_json=cfg.codex_auth_json,
            api_key=cfg.openai_api_key, access_token=cfg.codex_access_token,
            auth_storage=cfg.codex_auth_storage, persist_auth=persist_auth,
            yolo=cfg.codex_yolo,
        )
    elif cfg.provider == "claude":
        cmd = claude.build_command(cfg.claude_bin, cfg.model, cfg.max_turns, cfg.allowed_bash)
        log(f"Running Claude Code ({cfg.model or 'default model'}, up to {cfg.max_turns} turns)")
        answer = claude.run_claude(prompt, cmd, cfg.workspace, cfg.timeout_minutes * 60)
    else:
        raise ValueError("provider must be claude or codex")
    log(f"{cfg.reviewer} finished in {time.monotonic() - started:.0f}s with {len(answer.findings)} finding(s)")
    if not answer.structured:
        annotate("warning", "Claude did not return the JSON block; posting its text as the summary.")
    return answer


def gate(cfg: Config, answer: claude.ClaudeAnswer) -> int:
    if cfg.fail_on not in SEVERITY_RANK:
        return 0
    worst = max((SEVERITY_RANK.get(f.severity, 0) for f in answer.findings), default=0)
    if worst >= SEVERITY_RANK[cfg.fail_on]:
        annotate("error", f"{cfg.reviewer} review reported a finding at or above '{cfg.fail_on}'.")
        return 1
    return 0


# ── modes ────────────────────────────────────────────────────────────────

def review_pull(cfg: Config, gitea: Gitea, number: int, event_sha: str = "") -> int:
    log(f"Reviewing pull request #{number}")
    pr = gitea.pull(number)
    if pr.get("draft") or WIP_RE.match(pr.get("title") or ""):
        if cfg.wip_policy == "fail":
            annotate("error", "Draft / WIP pull request: remove the WIP marker to run the review.")
            return 1
        log("Draft / WIP pull request: review skipped.")
        return 0
    head_sha = (pr.get("head") or {}).get("sha", "")
    # Review only the commit this run was triggered for. If the branch moved on,
    # the run for the newer push reviews it; two runs never review one commit.
    if event_sha and head_sha and event_sha != head_sha:
        log(f"Pull request moved from {event_sha[:12]} to {head_sha[:12]}; the newer run reviews it.")
        return 0
    log(f"Checking out head {head_sha[:12]}")
    checkout_pull_head(cfg.workspace, number, head_sha, cfg.gitea_token)
    strip_checkout_credentials(cfg.workspace)

    bot = gitea.current_user()["login"]
    markers = publish.Markers(cfg.review_id)
    diff_text = gitea.pull_diff(number)
    posted = publish.posted_findings(gitea, number, bot, markers)
    still_open = [p for p in posted if p.open]
    log(f"Diff: {len(changed_files(diff_text))} file(s); @{bot} posted {len(posted)} finding(s) before, "
        f"{len(still_open)} still open")
    prompt = prompts.review_prompt(
        pr=pr, diff_file=write_diff(cfg, number, diff_text), changed=changed_files(diff_text),
        repo_rules=read_rules(cfg), language=cfg.language, previous=still_open,
    )
    answer = run_model(cfg, prompt)
    # A push during the run makes these results stale; the run for the new head
    # posts instead, so an older review never overwrites a newer summary.
    latest = (gitea.pull(number).get("head") or {}).get("sha", "")
    if latest and latest != head_sha:
        log(f"Pull request moved to {latest[:12]} while reviewing {head_sha[:12]}; discarding stale results.")
        return 0
    resolve_api = False
    if answer.resolved:
        version = gitea.version()
        resolve_api = publish.supports_resolve_api(version)
        if not resolve_api:
            log(f"Gitea {version or '(unknown version)'} has no resolve API (needs 1.26+): "
                "fixed findings get a banner but stay unresolved.")
    stats = publish.publish_review(
        gitea, number, head_sha, bot, answer, parse_unified_diff(diff_text),
        publish.Footer(head_sha, cfg.model, answer.cost_usd, answer.turns, cfg.show_cost, cfg.reviewer),
        posted, resolve_api, inline=cfg.inline_comments, markers=markers,
    )
    log(f"Review posted: {stats}")
    return gate(cfg, answer)


def reply_mention(cfg: Config, gitea: Gitea) -> int:
    comment = cfg.event.get("comment") or {}
    issue = cfg.event.get("issue") or {}
    author = (comment.get("user") or {}).get("login", "")
    body = comment.get("body") or ""
    if cfg.trigger_phrase.lower() not in body.lower():
        log("Comment does not mention the trigger phrase; nothing to do.")
        return 0
    bot = gitea.current_user()["login"]
    if author.lower() == bot.lower():
        log("Comment written by the bot itself; ignored.")
        return 0
    permission = gitea.permission(author)
    if PERMISSION_RANK.get(permission, 0) < PERMISSION_RANK.get(cfg.mention_permission, 2):
        log(f"@{author} has '{permission}' permission; '{cfg.mention_permission}' is required to trigger {cfg.reviewer}.")
        return 0

    number = int(issue.get("number"))
    pr = diff_file = None
    lines = {}
    head_sha = ""
    if issue.get("pull_request"):
        pr = gitea.pull(number)
        head_sha = (pr.get("head") or {}).get("sha", "")
        checkout_pull_head(cfg.workspace, number, head_sha, cfg.gitea_token)
        diff_text = gitea.pull_diff(number)
        diff_file = write_diff(cfg, number, diff_text)
        lines = parse_unified_diff(diff_text)
    strip_checkout_credentials(cfg.workspace)

    thread = [
        f"@{(c.get('user') or {}).get('login', '')}: {(c.get('body') or '')[:1500]}"
        for c in gitea.issue_comments(number)
        if c.get("id") != comment.get("id") and not publish.ANY_SUMMARY_RE.search(c.get("body") or "")
    ]
    request = body.replace(cfg.trigger_phrase, "", 1).strip() or body
    prompt = prompts.mention_prompt(
        request=request, author=author, issue=issue, pr=pr, diff_file=diff_file,
        thread=thread, repo_rules=read_rules(cfg), language=cfg.language,
    )
    answer = run_model(cfg, prompt)

    inline = cfg.inline_comments and pr is not None
    anchored, loose = publish.split_anchorable(answer.findings, lines) if inline else ([], list(answer.findings))
    quote = "\n".join("> " + line for line in body.strip().splitlines()[:6])
    footer = publish.Footer(head_sha, cfg.model, answer.cost_usd, answer.turns, cfg.show_cost, cfg.reviewer)
    text = f"{quote}\n\n@{author} {answer.summary}".strip() + publish.loose_section(loose) + publish.footer_text(footer)
    gitea.create_comment(number, text)
    if anchored:
        markers = publish.Markers(cfg.review_id)
        already = publish.suppressed(publish.posted_findings(gitea, number, bot, markers))
        publish.post_inline(gitea, number, head_sha, anchored, already,
                            cfg.reviewer + ": {count} inline finding(s) for @" + author + ".", markers)
    return 0


def title_unmarked_wip(event: dict) -> bool:
    """An "edited" event that removed the WIP marker from the title."""
    old = ((event.get("changes") or {}).get("title") or {}).get("from")
    new = (event.get("pull_request") or {}).get("title") or ""
    return old is not None and bool(WIP_RE.match(old)) and not WIP_RE.match(new)


def dispatch(cfg: Config, gitea: Gitea) -> int:
    if cfg.event_name in ("pull_request", "pull_request_target"):
        pull = cfg.event.get("pull_request") or {}
        if cfg.event.get("action") == "edited" and not title_unmarked_wip(cfg.event):
            log("Edit did not remove a WIP marker from the title; nothing to review.")
            return 0
        number = int(pull.get("number") or cfg.event.get("number"))
        return review_pull(cfg, gitea, number, (pull.get("head") or {}).get("sha", ""))
    if cfg.event_name in ("issue_comment", "pull_request_review_comment"):
        if (cfg.event.get("action") or "created") != "created":
            log("Only newly created comments trigger the reviewer.")
            return 0
        return reply_mention(cfg, gitea)
    log(f"Event '{cfg.event_name}' is not handled; use pull_request or issue_comment.")
    return 0


def main() -> int:
    cfg = load_config()
    if not cfg.gitea_token:
        annotate("error", "gitea_token is required.")
        return 1
    if "/" not in cfg.repository or not cfg.server_url:
        annotate("error", "Could not determine the repository or server URL from the environment.")
        return 1
    if cfg.review_id and not publish.REVIEW_ID_RE.fullmatch(cfg.review_id):
        annotate("error", f"review_id {cfg.review_id!r} is invalid: use 1-32 lowercase letters, digits, - or _.")
        return 1
    owner, repo = cfg.repository.split("/", 1)
    gitea = Gitea(cfg.server_url, cfg.gitea_token, owner, repo)
    try:
        if cfg.provider not in ("claude", "codex"):
            raise ValueError("provider must be claude or codex")
        if cfg.provider == "codex" and cfg.codex_auth_mode == "chatgpt":
            # OpenAI's account-auth CI workflow is restricted to private automation.
            if gitea.repository_info().get("private") is not True:
                raise ValueError("Codex ChatGPT account auth requires a private repository and trusted runner")
        return dispatch(cfg, gitea)
    except Exception as error:  # noqa: BLE001 - surface every failure as one annotation
        # Keep the published Claude option; new Codex failures always fail the job.
        failed = cfg.fail_on_error or cfg.provider != "claude"
        annotate("error" if failed else "warning", f"{cfg.reviewer} review failed: {error}")
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
