# mmux 设计

[English design document](design.md)

mmux 不是一个更聪明的 agent。它是多个代码 agent worker 的确定性监督器。

## 问题

把 Codex 和 Claude Code 并排放进 tmux 很容易。真正困难的是让它们连续工作数小时，同时不互相踩踏、不提前停摆、不陷入无意义打磨。

系统必须防止：

- 两个 agent 同时占用同一个角色。
- 两个 agent 同时写同一片文件或模块。
- 某个 agent 做完手头任务后让整体循环提前停止。
- agent 在同一个小区域里反复内卷。
- 基于大模型的 supervisor 变成第三个不稳定裁判。

## 核心模型

agent 是 worker，role 是 seat。

```text
codex  -> 可以持有 driver/reviewer/scout/tester lease
claude -> 可以持有 driver/reviewer/scout/tester lease

driver lease   -> 只有持有者可以写代码
reviewer lease -> 读取 diff 并写 review
scout lease    -> 提出 frontier candidate
tester lease   -> 运行确定性验证
```

supervisor 负责：

- 时间窗口。
- 角色租约。
- 资源锁。
- 策略检查。
- Git diff 检查。
- 测试执行。
- 日志和 checkpoint。

supervisor 不负责：

- 架构品味。
- 具体代码实现。
- 自然语言 review 的质量判断。
- 总结文案。

这些属于 worker 的职责。

## 确定性 Supervisor

允许作为 supervisor 输入的事实：

- 墙钟时间。
- 进程状态。
- tmux pane 存活状态。
- agent hook event 文件。
- SQLite state。
- Git status/diff。
- test/lint exit code。
- schema-valid JSON proposal。
- 本地项目 marker 和文件名。

不允许作为 supervisor 输入的内容：

- LLM 判断。
- 自由格式终端总结。
- 绕过 git/test 证据的 agent 自述。

## 项目画像

每次限时运行前，mmux 会先对本地仓库做一次不调用模型的项目画像。画像只基于确定性 marker，例如 `pyproject.toml`、`package.json`、`Cargo.toml`、`go.mod`、`pom.xml`、Gradle 文件、`.sln` / `.csproj`、`composer.json`、`Gemfile`、`Package.swift`、`Makefile` 和文件扩展名。

画像会区分：

- 默认启用检查：足够保守、可以零配置运行的本地检查。
- 建议检查：很可能有用，但可能需要依赖、工具链或离线缓存，不默认作为 patch gate。

这样 `mmux run` 在执行前会先做足够的本地调研，避免盲跑，同时 supervisor 仍然保持确定性，不依赖大模型裁判。

## 状态

项目状态保存在目标工程的 `.mmux/` 下：

```text
.mmux/
  config.json
  state.db
  logs/
    supervisor.log
  runs/
  worktrees/
  sessions/
  inbox/
```

SQLite 表：

- `meta`
- `tasks`
- `role_leases`
- `worker_heartbeats`
- `resource_locks`
- `events`
- `frontier_items`

role lease 是以 role 为 key 的单行租约。持有者执行 stale-sensitive 动作时必须带上当前 generation token；过期 lease 可以被其他 worker 重新获取。worker heartbeat 只表示运行状态，用于 CLI 和 tmux pane 可观察性，不参与策略判断。

基础 role plan 仍然按 role pair 轮转，避免 agent 固定占用某个角色。当存在可执行工作时，项目级 plan 会确定性覆盖轮转：有 `awaiting_test` 任务时优先 `tester/driver`，有 `pending` 任务时优先 `driver/tester`。agent 分配仍然按墙钟 slot 交替，所以 Codex 和 Claude Code 不会永久拥有某个角色。

resource lock 是排他的路径前缀租约。锁住 `src` 会与 `src/mmux/cli.py` 冲突；锁住 `.` 会与整个项目冲突。worker 在某个 role 下获取 resource lock 时，当前 role generation 会写入 lock，防止过期 driver 工作继续提交。

## 任务执行

任务执行是显式开启的。默认 `mmux start` 只观察，不让 agent 改代码。使用 `--execute-agents` 后，持有 `driver` 的 worker 会领取一个 pending task、获取该任务的 resource lock、创建隔离 git worktree，并非交互运行本地 agent CLI：

- Codex：`codex exec`
- Claude Code：`claude -p`

driver 执行结束后，确定性策略会检查 worktree diff：

- 无 diff -> `no_change`
- 修改 `.git`、`.mmux`、`.env*` 等保护路径 -> `rejected`
- 任意 changed path 不在任务 resource lock 内 -> `rejected`
- 通过 driver diff policy -> `awaiting_test`

持有 `tester` 的 worker 随后在同一个 task worktree 里运行确定性检查：

- `git diff --check HEAD --`
- 对 changed Python files 运行 `python -m py_compile`
- 对 changed shell scripts 运行 `sh -n`
- 对 changed JSON files 运行 `python -m json.tool`
- 如果存在 `tests/`，运行 `python -m unittest discover -s tests`
- 当项目画像确认无需安装依赖即可运行时，执行本地 package test，例如已有 `node_modules` 的 Node `test` script

只有 tester 通过的 patch 才会应用回主工作区，并且主工作区必须没有 tracked changes。

supervisor 仍然不调用模型。它只发放租约、检查文件事实、记录结果；模型工作只发生在 worker adapter 内部。

## 限时运行

`mmux run PROJECT --minutes N` 是普通使用时的顶层受控入口。它会在需要时初始化本地 `.mmux/` 目录，拒绝复用已经存在的 tmux session，先生成项目画像；如果任务队列里没有 pending 或进行中的工作，会自动补一个保守默认任务。`--no-default-task` 可以关闭这个队列引导，用于纯观察运行。随后命令记录 `run_started` 事件，启动与 `mmux start` 相同的四窗格工作区，并用墙钟时间驱动运行窗口。

运行期间，它会定期把剩余时间和任务状态计数写到 stdout 与 supervisor log。到达时限或收到 `KeyboardInterrupt` 后，它会停止 tmux session，清理运行期 lease、lock、heartbeat，把未完成的 `running` 任务恢复为 `pending`，把未完成的 `running_test` 任务恢复为 `awaiting_test`，记录 `run_finished`，并打印 before/after/delta 任务汇总。

默认情况下，限时运行只观察，不让 agent 改代码。加上 `--execute-agents` 后，Codex 和 Claude Code 才会在上面描述的确定性 gate 内非交互执行任务。

限时运行会把绝对 deadline 传给 worker。worker 领取任务前，会根据 deadline、配置的 adapter 超时和停止前预留时间计算剩余执行预算；预算太小时不会再启动新的 driver 或 tester 动作。agent adapter 还有无输出超时：如果 CLI 在配置时间内没有产生 stdout/stderr，mmux 会终止它，并把原因写入 task log。adapter 超时和无输出会被视为 agent health failure：mmux 会重新排队任务，在确定性状态里记录 agent cooldown，并在 cooldown 过期前跳过该 agent 的 driver lease。

## Tmux 布局

当前工作区使用四个 pane：

```text
+----------------------+----------------------+
| deterministic         | codex worker         |
| supervisor            |                      |
+----------------------+----------------------+
| supervisor log        | claude worker        |
+----------------------+----------------------+
```

tmux 用于观察和人工接管。数据库仍然是事实源。

## Frontier 策略

任务在时间窗口结束前完成时，下一个任务必须走向未探索边界：

- 新的模块边界。
- 最近未触达的用户路径。
- 验证缺口。
- 失败测试或 CI 信号。
- 带证据的 TODO/FIXME。

最近触达的文件进入 cooldown。reviewer 可以 review 任务选择，但 supervisor 只接受 schema-valid 且通过确定性检查的 candidate。

## 当前边界

当前实现已经支持受控任务执行，但还不是完整无人值守系统。仍需补齐：

- `scout` 自动生成 frontier task。
- `reviewer` 的真实 review gate。
- 可配置 tester 命令。
- worktree 清理和归档策略。
- commit/checkpoint/rollback 策略。
- 自动提交和远端协作策略。
