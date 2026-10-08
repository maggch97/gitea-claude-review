# Gitea Claude Review

[简体中文](README.zh-CN.md)

Pull request review and `@claude` replies for **Gitea Actions**, powered by the
official [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI.

- **One summary comment per pull request**, edited in place on every push.
- **Inline findings** anchored to diff lines, de-duplicated across pushes; findings
  outside the diff go to the summary instead of being dropped.
- **Fixed findings are closed out**: on every push Claude re-checks the open
  findings; the fixed ones get a ✅ banner and are resolved (Gitea 1.26+, the first
  version with a resolve API; older versions get the banner only).
- **`@claude` in any issue or pull request comment** asks a question or a re-review.
- **No third-party action and no dependencies**: a composite action that installs
  the Claude Code CLI and runs a small standard-library Python module.
- **The Gitea token never reaches the model.** Claude runs with read-only tools and
  an allowlisted environment and only returns JSON; this action posts it.

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

## Inputs

| Input | Default | Description |
|---|---|---|
| `gitea_token` | — | Bot token used to read the pull request and post comments. Required. |
| `claude_code_oauth_token` / `anthropic_api_key` | — | Claude credentials (one of them). |
| `anthropic_base_url` | — | Anthropic-compatible endpoint. |
| `model` | Claude Code default | Model id or alias. |
| `claude_code_version` | `stable` | Version passed to the official installer; pin it (e.g. `2.1.284`) for reproducible runs. |
| `claude_code_executable` | — | Use an existing binary instead of installing. |
| `rules_file` | `.gitea/claude/REVIEW.md` | Review rules read from the checkout; built-in generic rules when missing. |
| `review_id` | — | Namespace for this review's comment markers. Give each workflow its own id to run several independent reviews on one pull request with one bot account (see below). |
| `extra_prompt` | — | Extra instructions appended to the rules. |
| `language` | — | Language of the comments, e.g. `Simplified Chinese`. |
| `trigger_phrase` | `@claude` | Phrase that triggers a reply in comments. |
| `mention_permission` | `write` | Minimum repository permission of a commenter allowed to trigger Claude (`read` / `write` / `admin`). |
| `max_turns` | `30` | Agent turn limit. |
| `timeout_minutes` | `30` | Timeout of the Claude run. |
| `allowed_bash` | read-only git | Bash patterns Claude may run. |
| `inline_comments` | `true` | Post anchored findings as inline review comments. |
| `wip_policy` | `skip` | Draft or `WIP:` pull requests: `skip` or `fail`. |
| `fail_on` | `none` | Fail the job when a finding reaches `low` / `medium` / `high` / `blocker`. |
| `fail_on_error` | `false` | Fail the job when the review itself errors (otherwise a warning). |
| `show_cost` | `false` | Show cost and turns in the comment footer. |
| `server_url` | the workflow's server | Gitea base URL. |

## How it works

| Event | What happens |
|---|---|
| `pull_request` | Skip runs whose commit is no longer the PR head (a newer push reviews it), and `edited` events unless the title lost its `WIP:` marker → skip drafts / `WIP:` titles → check out the PR head → write the diff to `.gitea-claude-review/pr-<n>.diff` → run Claude with the rules and the bot's open findings → if the PR moved on meanwhile, discard the results → mark the findings Claude verified as fixed (banner, and resolve on Gitea 1.26+) → edit the summary comment → post new inline findings in one review. Findings a person resolved are never touched or posted again. |
| `issue_comment` with the trigger phrase | Ignore the bot's own comments and commenters below `mention_permission` → on pull requests check out the head and provide the diff → Claude answers the request → reply comment, plus inline findings if any. |

Claude is invoked as `claude -p --output-format json` with `Read`, `Grep`,
`Glob`, `LS` and the `allowed_bash` patterns; `Edit`, `Write`, `WebFetch`,
`WebSearch` and sub-agents are denied. It must end its answer with a JSON block
(`summary` + `findings[]` + `resolved[]`, the ids of earlier findings it verified
as fixed); a missing block is posted as plain text with a warning.

### Several reviews on one pull request

Two workflows can review the same pull request with different rules, e.g. a
correctness review that gates merges and a design review that does not. Give the
second one its own `review_id`: the summary comment, inline findings and fixed
banners are then tracked separately, and neither review edits or re-checks the
other's comments. Give it its own `trigger_phrase` too, otherwise both reply to the
same mention. Without `review_id` the markers are the ones earlier versions used,
so existing comments stay recognized.

```yaml
- uses: https://github.com/maggch97/gitea-claude-review@v0
  with:
    gitea_token: ${{ secrets.CLAUDE_GITEA_TOKEN }}
    claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
    review_id: design
    rules_file: .gitea/claude/DESIGN_REVIEW.md
    trigger_phrase: "@design-review"
    inline_comments: "false"
```

## Security notes

- The model process receives only `PATH`, `HOME`, locale, proxy, `ANTHROPIC_*`
  and `CLAUDE_CODE_*` variables. `gitea_token` and other secrets are not in its
  environment.
- Checkout credentials persisted in `.git/config` are removed before Claude runs;
  fetching the PR head uses a one-shot header via `GIT_CONFIG_*`.
- Pull request content is untrusted. The prompt tells Claude to ignore embedded
  instructions, and with read-only tools and no token the worst case is a bad
  comment, not a pushed change.
- Comment triggers require `mention_permission` (default `write`), so strangers
  cannot spend your Claude quota.
- Gitea may not pass secrets to workflows triggered from forks; reviews of fork
  pull requests then fail with a warning.

## Development

```bash
python -m unittest discover -s tests
```

## License

MIT
