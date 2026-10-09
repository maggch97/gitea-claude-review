# Gitea Claude Review

[English](README.md)

给 **Gitea Actions** 用的 PR 代码审查和 `@mention` 回复，底层可选官方
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) 或
[Codex](https://learn.chatgpt.com/docs/non-interactive-mode) 命令行工具。
旧 workflow 默认继续使用 Claude，新增 `provider: codex` 切换后端。

- **每个 PR 只有一条总评论**，每次推送原地更新，不刷屏。
- **行级评论**挂在 diff 对应行上，多次推送不重复；不在 diff 里的问题写进总评论，不丢。
- **修好的问题自动收尾**：每次推送复核仍未解决的行级评论，确认已修复的加上 ✅ 标记并点“已解决”
  （需 Gitea 1.26+，这是第一个有解决对话 API 的版本；更早的版本只加标记）。被人手动解决的评论不动、也不再重发。
- **在任意 issue 或 PR 评论里 `@claude`**，可以提问或要求重新审查。
- **不依赖第三方审查 action**：一个组合 action，安装所选 CLI 后运行只用标准库的 Python。
- **Gitea 令牌不交给模型**：Claude 用只读工具，Codex 默认用只读 sandbox，也可显式开启 YOLO，由本 action 负责发评论。

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

## 接入 Codex 订阅

需要 Linux runner、Python 3.9+，以及 Node.js/npm（也可用 `codex_executable` 指定已有 CLI）。
完整示例见 [examples/workflows/codex.yml](examples/workflows/codex.yml)。

1. 在可信电脑上配置 `cli_auth_credentials_store = "file"`，用订阅账号运行 `codex login`。
   为 CI 单独登录；不要在另一台电脑继续使用同一份 token，否则刷新会互相冲突。
2. 将生成的 `auth.json` 放入 Gitea 仓库 Secret `CODEX_AUTH_JSON`。
3. 将专用的 Secret 管理 token 放入 `CODEX_SECRETS_TOKEN`。需要 `write:repository` 和
   仓库 Actions Secret 管理权限；上游 Gitea 要求仓库所有者或站点管理员，普通机器人写权限不够。
4. 所有使用同一份凭据的任务必须在领取任务前串行。完整示例用固定的 workflow `concurrency`
   分组和 `cancel-in-progress: false`，不能按 PR 分组。上游 Gitea 1.26+ 支持此功能，
   自有服务器需确认；旧版本可用独占标签、capacity 为 1 的单个专用 runner。凭据不能跨仓库共用。
5. 沿用原机器人账号和审查 token，可以继续更新旧的总评论并对行级评论去重。

```yaml
- uses: https://github.com/maggch97/gitea-claude-review@c6f939b0a55bf22ca7194d690fb4a862f3f418ff
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

默认 `gitea-secret` 模式不需要持久目录。Action 先校验凭据，并以原值 PUT 验证 Secret 写权限，
再将凭据恢复到临时隔离目录，运行 CLI，让 CLI 自行刷新，最后把变化后的凭据写回仓库 Secret。
审查失败也执行写回；写回失败明确报错。Gitea 在服务端加密存储，API 不返回 Secret 明文。
`codex_secret_name` 默认 `CODEX_AUTH_JSON`，必须与输入引用的 Secret 一致。

[Gitea 源码](https://github.com/go-gitea/gitea/blob/main/services/actions/task.go)在构造 runner
任务时读取 Secret，因此服务端串行后，后一个任务能拿到更新值；任务领取后的锁不能修复旧快照。
[GitHub 的仓库 Secret](https://docs.github.com/en/actions/reference/security/secrets)在工作流入队时
就读取，所以不能直接照搬：GitHub 应用持久 self-hosted runner、任务开始时读取的 environment Secret，
或获得串行执行权后再读取的外部 Secret 管理服务。原生 concurrency 可能替换旧的等待任务，
不是保留每条评论的 FIFO 队列；需要每次触发都执行时，用专用 capacity 为 1 的 runner。

每次运行用隔离的 `HOME` / `CODEX_HOME`，不复制用户配置、MCP 或旧会话。
不要把凭据放入源码、日志、artifact 或 Actions cache。强制取消、runner 掉线可能中断写回；
写回失败或刷新凭据失效时，先停任务，重新登录并替换 `CODEX_AUTH_JSON`。
根据 [OpenAI 账号认证 CI 文档](https://learn.chatgpt.com/docs/auth/ci-cd-auth)，订阅认证只用于可信私有自动化；
Action 会检查仓库是否私有。Action 源码可继续在公开 GitHub 仓库维护，认证失败不会自动转为付费 API。

也可选 `codex_auth_storage: directory`，填 checkout 外的持久绝对路径 `codex_home`，
不填 `codex_secrets_token`。Docker 任务必须明确挂载宿主目录或 volume；仅填写路径不会自动持久化。
此模式的 `codex_auth_json` 只初始化缺少的文件，已有文件不被旧 Secret 覆盖。
同一目录用系统文件锁串行，并原子保存刷新结果；等待锁计入超时。目录建议权限 `0700`、由单个 runner
独占，文件锁不能协调不同机器上的独立副本。重新初始化需手工替换目录内文件。

另外两种认证需要显式指定，不能混用，也不要填写 `codex_home` / `codex_auth_json`：

| 认证 | 配置 | 计费/权限 |
|---|---|---|
| API Key | `codex_auth_mode: api-key` + `openai_api_key` | OpenAI API 单独计费，不抵扣订阅 |
| 工作空间 token | `codex_auth_mode: access-token` + `codex_access_token` | Business / Enterprise 的 Codex 访问令牌 |

Codex 按 JSON Schema 输出，Action 再校验字段、枚举、路径和行号。进程失败、超时、缺少结果或非法 JSON
都会让任务失败，不受旧的 `fail_on_error: false` 影响，也不会发布不完整结果。
`max_turns`、`allowed_bash` 只对 Claude 生效；Codex 用超时限制，不伪造金额或 Claude 轮数。
设置 `codex_yolo: true` 后使用 `--dangerously-bypass-approvals-and-sandbox`，关闭沙盒和审批；
此时只做审查由提示词约束，不再由文件系统或网络隔离保证。默认值为 `false`，非法值直接报错。

切换为 `@codex` 时还要同步修改 workflow 的评论事件 `if` 条件。总评论原地更新、行级去重、已修复问题收尾、
权限检查和过时审查丢弃继续复用已有逻辑。

## 参数

完整参数表见 [README.md](README.md#inputs)。常用的：

| 参数 | 默认 | 说明 |
|---|---|---|
| `provider` | `claude` | 后端：`claude` / `codex` |
| `model` | 所选 CLI 默认 | 模型 id 或别名 |
| `claude_code_version` | `stable` | 建议固定版本（如 `2.1.284`），结果可复现 |
| `rules_file` | `.gitea/claude/REVIEW.md` | 仓库里的审查规则文件，不存在时用内置通用规则 |
| `language` | — | 评论语言，例如 `Simplified Chinese` |
| `review_id` | — | 评论标记的命名空间。同一个 PR 上要跑多条互不干扰的 review（如正确性 + 设计）时，给每条审查不同的 id，并配不同的 `trigger_phrase` |
| `mention_permission` | `write` | 评论里 @ 触发所需的最低仓库权限 |
| `wip_policy` | `skip` | 草稿或 `WIP:` 标题的 PR：跳过或让流水线失败 |
| `fail_on` | `none` | 有问题达到该级别时让任务失败，可做合并门禁 |

### 同一个 PR 上的独立审查

正确性与设计审查可以共用机器人，用 `review_id: design` 隔离设计总评论、行级问题和修复标记；
空 id 保持已有评论标记，Claude 切到 Codex 后也能继续更新。触发词分别设置，避免同一条评论启动两种审查。

共用同一份 ChatGPT 登录时，在同一个工作流内设置 workflow 级并发分组，并用 `needs` 串行安排审查 job。
独立工作流共用并发分组会竞争等待位置，可能取消对方的等待运行。
两条审查使用同一个固定 Action 版本和相同认证配置，并可分别设为必需检查。
设计审查可配置 `fail_on: high`、`inline_comments: false`；文件级问题用 `line: 0` 明确表示没有单行位置，
只发到总评论，不伪造代码行号。

## 安全设计

- 模型环境采用白名单，排除 Gitea 审查和 Secret 管理 token。Codex 只接收显式选择的认证凭据。
- 运行审查前清掉 checkout 写进 `.git/config` 的凭证；拉取 PR 分支时用一次性的请求头，不落盘。
- PR 内容视为不可信输入；提示词要求忽略其中的指令，CLI 限制为只读，Action 校验并发布结果。
- 评论触发需要 `mention_permission`（默认写权限），陌生人无法通过评论消耗模型额度。

## 开发

```bash
python -m unittest discover -s tests
```

## 许可证

MIT
