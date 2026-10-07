# Gitea Claude Review

[English](README.md)

给 **Gitea Actions** 用的 PR 代码审查和 `@claude` 回复，底层是官方
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) 命令行工具。

- **每个 PR 只有一条总评论**，每次推送原地更新，不刷屏。
- **行级评论**挂在 diff 对应行上，多次推送不重复；不在 diff 里的问题写进总评论，不丢。
- **修好的问题自动收尾**：每次推送 Claude 会复核仍未解决的行级评论，确认已修复的加上 ✅ 标记并点“已解决”
  （需 Gitea 1.26+，这是第一个有解决对话 API 的版本；更早的版本只加标记）。被人手动解决的评论不动、也不再重发。
- **在任意 issue 或 PR 评论里 `@claude`**，可以提问或要求重新审查。
- **不依赖第三方 action，没有任何依赖**：一个组合 action，安装 Claude Code 后运行一小段只用标准库的 Python。
- **Gitea 令牌不交给模型**：Claude 只有只读工具和白名单环境变量，只输出 JSON，由本 action 负责发评论。

按 Gitea 的 API 设计（行号锚点、`/pulls/{n}.diff`、没有 GraphQL），不是从只支持 GitHub 的 action 移植过来的。

## 快速开始

1. 给机器人建一个 Gitea 账号（例如 `claude-bot`），加入仓库并给写权限；生成访问令牌，勾选
   `write:repository`、`write:issue`、`read:user`，存为仓库密钥 `CLAUDE_GITEA_TOKEN`。
2. 把 Claude 凭证存为密钥：`CLAUDE_CODE_OAUTH_TOKEN`（`claude setup-token` 生成）或 `ANTHROPIC_API_KEY`。
3. 把 [`examples/workflows/claude.yml`](examples/workflows/claude.yml) 放到仓库的 `.gitea/workflows/`。
4. 可选：把审查规则写在 `.gitea/claude/REVIEW.md`（[示例](examples/REVIEW.md)）。

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
    persist-credentials: false
- uses: https://github.com/maggch97/gitea-claude-review@v0
  with:
    gitea_token: ${{ secrets.CLAUDE_GITEA_TOKEN }}
    claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
    language: Simplified Chinese
```

Gitea 可以直接用 URL 引用 action。Runner 访问不了 GitHub 时，把本仓库镜像到自己的 Gitea，`uses:` 指向镜像即可。

## 参数

完整参数表见 [README.md](README.md#inputs)。常用的：

| 参数 | 默认 | 说明 |
|---|---|---|
| `model` | Claude Code 默认 | 模型 id 或别名 |
| `claude_code_version` | `stable` | 建议固定版本（如 `2.1.284`），结果可复现 |
| `rules_file` | `.gitea/claude/REVIEW.md` | 仓库里的审查规则文件，不存在时用内置通用规则 |
| `language` | — | 评论语言，例如 `Simplified Chinese` |
| `mention_permission` | `write` | 评论里 @ 触发所需的最低仓库权限 |
| `wip_policy` | `skip` | 草稿或 `WIP:` 标题的 PR：跳过或让流水线失败 |
| `fail_on` | `none` | 有问题达到该级别时让任务失败，可做合并门禁 |

## 安全设计

- 模型进程只拿到 `PATH`、`HOME`、语言、代理、`ANTHROPIC_*`、`CLAUDE_CODE_*` 这些环境变量，拿不到 Gitea 令牌和其他密钥。
- 运行 Claude 前会清掉 checkout 写进 `.git/config` 的凭证；拉取 PR 分支时用一次性的请求头，不落盘。
- PR 内容一律视为不可信输入。提示词要求忽略其中的指令；模型只有只读工具、没有令牌，最坏情况是发了一条不准确的评论，不会改代码。
- 评论触发需要 `mention_permission`（默认写权限），陌生人无法消耗你的 Claude 额度。

## 开发

```bash
python -m unittest discover -s tests
```

## 许可证

MIT
