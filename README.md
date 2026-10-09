# Gitea Claude Review

[简体中文](README.zh-CN.md)

Pull request review and configurable `@mention` replies for **Gitea Actions**,
powered by the official [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
or [Codex](https://learn.chatgpt.com/docs/non-interactive-mode) CLI.
Existing workflows continue to use Claude; select `provider: codex` to switch.

- **One summary comment per pull request**, edited in place on every push.
- **Inline findings** anchored to diff lines, de-duplicated across pushes; findings
  outside the diff go to the summary instead of being dropped.
- **Fixed findings are closed out**: on every push the reviewer re-checks the open
  findings; the fixed ones get a ✅ banner and are resolved (Gitea 1.26+, the first
  version with a resolve API; older versions get the banner only).
- **`@claude` in any issue or pull request comment** asks a question or a re-review.
- **No third-party action and no dependencies**: a composite action that installs
  selected CLI and runs a small standard-library Python module.
- **The Gitea token is excluded from the model process environment.** Claude uses
  read-only tools; Codex defaults to a read-only sandbox, with an explicit YOLO option.
  This action posts the results, not the model.

It is written for Gitea's API (line-number anchors, `/pulls/{n}.diff`, no
GraphQL) rather than ported from a GitHub-only action.

## Quick start

1. Create a Gitea account for the bot (e.g. `claude-bot`), add it to the
   repository with write access, and create an access token with
   `write:repository`, `write:issue` and `read:user` scopes. Save it as the
   repository secret `CLAUDE_GITEA_TOKEN`.
2. Save Claude credentials as a secret: `CLAUDE_CODE_OAUTH_TOKEN` (from
   `claude setup-token`) or `ANTHROPIC_API_KEY`.
3. Add [`examples/workflows/claude.yml`](examples/workflows/claude.yml) to
   `.gitea/workflows/` in your repository.
4. Optional: put your review rules in `.gitea/claude/REVIEW.md`
   ([example](examples/REVIEW.md)).

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
    persist-credentials: false
- uses: https://github.com/maggch97/gitea-claude-review@v0
  with:
    gitea_token: ${{ secrets.CLAUDE_GITEA_TOKEN }}
    claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

Gitea resolves `uses:` URLs directly. If your runners cannot reach GitHub,
mirror this repository to your Gitea instance and point `uses:` at the mirror.

## Codex quick start

Use a **Linux runner** with Python 3.9+ and Node.js/npm (or supply `codex_executable`).
[`examples/workflows/codex.yml`](examples/workflows/codex.yml) shows subscription
auth for a private Gitea repository. The action repository may stay public on
GitHub; account credentials belong only on your trusted private runner.

### ChatGPT subscription auth

1. On a trusted machine, configure `cli_auth_credentials_store = "file"`, then
   run `codex login`. Use a dedicated CI login; do not use the same token bundle
   from your desktop or another runner.
2. Save the resulting `auth.json` as the Gitea repository Secret `CODEX_AUTH_JSON`.
3. Save a dedicated Gitea token as `CODEX_SECRETS_TOKEN`. It needs
   `write:repository` and permission to manage repository Actions Secrets
   (upstream Gitea requires repository ownership or site admin). The ordinary
   review bot's write access is insufficient. This token is never passed to Codex.
4. Serialize every job using this bundle **before task assignment**. Use the
   same fixed workflow `concurrency` group across all PRs/workflows, with
   `cancel-in-progress: false`. Upstream Gitea supports this in 1.26+; verify
   support on your server. On older servers use one dedicated runner with
   capacity 1 and a unique label. Do not share the bundle across repositories.
5. Use the inputs below. Keep the same review bot token to retain ownership of
   existing summary comments and findings.

```yaml
- uses: https://github.com/maggch97/gitea-claude-review@c74ebc676ad430824cd534a99957bd934f26b158
  with:
    provider: codex
    gitea_token: ${{ secrets.CLAUDE_GITEA_TOKEN }}
    codex_auth_mode: chatgpt
    codex_auth_storage: gitea-secret
    codex_auth_json: ${{ secrets.CODEX_AUTH_JSON }}
    codex_secrets_token: ${{ secrets.CODEX_SECRETS_TOKEN }}
    trigger_phrase: '@codex'
    rules_file: .gitea/claude/REVIEW.md
    language: Simplified Chinese
```

Default `gitea-secret` storage requires no persistent runner directory. Before
Codex starts, the action validates the cache and tests Secret write permission
with a same-value PUT. It restores auth into an isolated temporary home, lets
Codex refresh normally, and writes changed auth back even when the review fails.
Gitea encrypts the stored value; its API never returns the plaintext.
`codex_secret_name` defaults to `CODEX_AUTH_JSON` and must match the input Secret.

Gitea reads Secrets when constructing the runner task, so server-side
serialization lets the next task receive the updated value. A lock inside an
already-assigned job cannot fix a stale snapshot. This is **not a GitHub
repository-Secret recipe**: GitHub snapshots those Secrets when a workflow is
queued. There, use a secret manager read after serialization, environment Secrets
read at job start, or persistent self-hosted storage. See
[Gitea task construction](https://github.com/go-gitea/gitea/blob/main/services/actions/task.go),
[Gitea concurrency support](https://github.com/go-gitea/gitea/pull/32751), and
[GitHub Secret timing](https://docs.github.com/en/actions/reference/security/secrets).
Native concurrency can replace older pending runs; it is not a durable FIFO
queue for every comment. Use a dedicated capacity-1 runner if every trigger
must execute.

User configuration, MCP settings and old sessions are not copied. Never put auth
in source, logs, artifacts or dependency caches. Hard cancellation or runner loss
can interrupt write-back. Stop jobs and replace `CODEX_AUTH_JSON` after failed
persistence or revoked/expired credentials. The action never falls back to paid
API usage. Public repositories are rejected for `chatgpt` mode, following the
[official account-auth CI guidance](https://learn.chatgpt.com/docs/auth/ci-cd-auth).

### Optional persistent directory

Set `codex_auth_storage: directory`, provide an absolute `codex_home` outside the
checkout, and omit `codex_secrets_token`. For container jobs explicitly mount a
private host directory/volume into each job at that path; setting a path alone
does not persist it. Optional `codex_auth_json` only bootstraps a missing file.
Existing refreshed auth is never overwritten by the seed. A local OS lock
serializes calls, and changed auth is saved atomically even on failed reviews.
Lock wait counts towards `timeout_minutes`. Use one runner and a private directory
(mode `0700`); a file lock does not coordinate independent machines. Reseeding
requires replacing the stored file, not just changing the bootstrap Secret.

### API key or workspace access token

Choose exactly one auth mode and credential; omit `codex_home` and `codex_auth_json`:

```yaml
provider: codex
codex_auth_mode: api-key
openai_api_key: ${{ secrets.OPENAI_API_KEY }}
```

API usage is billed separately from subscriptions. For Business/Enterprise
[Codex access tokens](https://learn.chatgpt.com/docs/enterprise/access-tokens), use
`codex_auth_mode: access-token` and `codex_access_token` instead.
Neither mode reuses ambient runner credentials.

Codex enforces a JSON Schema with `summary`, `findings[]` and `resolved[]`; the
action validates fields, enum values, line numbers and paths again. Missing or
invalid results and nonzero CLI exits **always fail the job**, even when the
legacy Claude `fail_on_error` option is false. No incomplete/plain-text Codex
answer is published. Codex does not report monetary cost or a Claude-style turn
count, so those fields are omitted. `max_turns` and `allowed_bash` apply only to
Claude; Codex uses `timeout_minutes`. Set `codex_yolo: true` to pass
`--dangerously-bypass-approvals-and-sandbox`, which disables sandboxing and
approval prompts. Without it, Codex uses a read-only sandbox. In YOLO mode,
review-only behavior is a prompt instruction, not a filesystem/network boundary.

Changing `trigger_phrase` requires changing the workflow's comment-event `if`
condition too. Bot self-replies, permission checks, summary updates, inline
deduplication, fixed-finding resolution and stale-PR checks remain the same.

## Inputs

| Input | Default | Description |
|---|---|---|
| `provider` | `claude` | Backend: `claude` or `codex`. |
| `gitea_token` | — | Bot token used to read the pull request and post comments. Required. |
| `claude_code_oauth_token` / `anthropic_api_key` | — | Claude credentials (one of them). |
| `anthropic_base_url` | — | Anthropic-compatible endpoint. |
| `model` | selected CLI default | Model id or alias. |
| `claude_code_version` | `stable` | Version passed to the official installer; pin it (e.g. `2.1.284`) for reproducible runs. |
| `claude_code_executable` | — | Use an existing binary instead of installing. |
| `codex_version` | `0.162.0` | Pinned npm CLI version; needs Node.js/npm on Linux. |
| `codex_executable` | — | Existing CLI, supporting `--ignore-user-config` / `--ignore-rules`. |
| `codex_yolo` | `false` | Run without sandboxing or approval prompts. Values must be `true` / `false`. |
| `codex_effort` | model default | `minimal`, `low`, `medium`, `high`, `xhigh` (model must support it). |
| `codex_auth_mode` | `chatgpt` | `chatgpt`, `api-key` or `access-token`; never auto-switches. |
| `codex_home` | — | Directory storage only: persistent absolute path outside checkout. |
| `codex_auth_json` | — | Current ChatGPT cache; directory storage uses it only for bootstrap. |
| `codex_auth_storage` | `gitea-secret` | ChatGPT persistence: repository Secret or `directory`. |
| `codex_secret_name` | `CODEX_AUTH_JSON` | Repository Secret to update; must match the input Secret. |
| `codex_secrets_token` | — | Dedicated Gitea Secret-management token. |
| `openai_api_key` | — | API-key mode only; separate usage billing. |
| `codex_access_token` | — | Access-token mode only; Business/Enterprise workspace token. |
| `rules_file` | `.gitea/claude/REVIEW.md` | Review rules read from the checkout; built-in generic rules when missing. |
| `extra_prompt` | — | Extra instructions appended to the rules. |
| `language` | — | Language of the comments, e.g. `Simplified Chinese`. |
| `trigger_phrase` | `@claude` | Phrase that triggers a reply in comments. |
| `mention_permission` | `write` | Minimum repository permission of a commenter allowed to trigger Claude (`read` / `write` / `admin`). |
| `max_turns` | `30` | Claude-only agent turn limit. |
| `timeout_minutes` | `30` | Model timeout, including waiting for the account auth lock. |
| `allowed_bash` | read-only git | Bash patterns Claude may run. |
| `inline_comments` | `true` | Post anchored findings as inline review comments. |
| `wip_policy` | `skip` | Draft or `WIP:` pull requests: `skip` or `fail`. |
| `fail_on` | `none` | Fail the job when a finding reaches `low` / `medium` / `high` / `blocker`. |
| `fail_on_error` | `false` | Claude errors: fail or warn. Codex errors always fail. |
| `show_cost` | `false` | Show cost and turns in the comment footer. |
| `server_url` | the workflow's server | Gitea base URL. |

## How it works

| Event | What happens |
|---|---|
| `pull_request` | Skip runs whose commit is no longer the PR head (a newer push reviews it), and `edited` events unless the title lost its `WIP:` marker → skip drafts / `WIP:` titles → check out the PR head → write the diff to `.gitea-claude-review/pr-<n>.diff` → run the selected reviewer with the rules and the bot's open findings → if the PR moved on meanwhile, discard the results → mark the findings the reviewer verified as fixed (banner, and resolve on Gitea 1.26+) → edit the summary comment → post new inline findings in one review. Findings a person resolved are never touched or posted again. |
| `issue_comment` with the trigger phrase | Ignore the bot's own comments and commenters below `mention_permission` → on pull requests check out the head and provide the diff → the reviewer answers the request → reply comment, plus inline findings if any. |

Claude is invoked as `claude -p --output-format json` with `Read`, `Grep`,
`Glob`, `LS` and the `allowed_bash` patterns; `Edit`, `Write`, `WebFetch`,
`WebSearch` and sub-agents are denied. It must end its answer with a JSON block
(`summary` + `findings[]` + `resolved[]`, the ids of earlier findings it verified
as fixed); a missing block is posted as plain text with a warning.

## Security notes

- The Claude process receives only `PATH`, `HOME`, locale, proxy, `ANTHROPIC_*
  and `CLAUDE_CODE_*` variables. `gitea_token` and other secrets are not in its
  environment.
  For Codex, `HOME` and `CODEX_HOME` point to an isolated temporary directory;
  only explicit Codex credentials and an allowlist of runtime/network variables
  reach the CLI. API/access tokens are excluded from model-generated shell
  command environments. Known auth tokens in final output cause the run to fail.
- Checkout credentials persisted in `.git/config` are removed before the reviewer runs;
  fetching the PR head uses a one-shot header via `GIT_CONFIG_*`.
- Pull request content is untrusted. The prompt tells the reviewer to ignore
  embedded instructions. A read-only sandbox is not a credential vault: account
  auth is readable by the CLI, so use only trusted private infrastructure, avoid
  unrelated secrets on disk and do not run untrusted builds in the auth job.
- Comment triggers require `mention_permission` (default `write`), so strangers
  cannot spend your model quota.
- Gitea may not pass secrets to workflows triggered from forks; reviews of fork
  pull requests then fail (Claude can warn according to `fail_on_error`).

## Development

```bash
python -m unittest discover -s tests
```

## License

MIT
