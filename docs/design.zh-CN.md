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

## Frontier 发现

mmux 可以不调用模型生成下一步候选任务。`mmux frontier` 和限时运行的队列补给会检查这些确定性事实：

- tracked 文本文件里的 TODO/FIXME/XXX。
- 缺少明显对应测试的源码文件。
- 项目画像发现的 suggested checks。

candidate 会写入 `frontier_items`，附带 evidence 和 score。当 open queue 为空时，mmux 会优先选择分数最高且未入队的 frontier candidate；没有候选时才退回通用保守默认任务。这样长时间运行会更倾向于探索真实项目边界，而不是反复生成同一句模糊任务。

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
  resident/
    codex/
    claude/
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

基础 role plan 仍然按 role pair 轮转，避免 agent 固定占用某个角色。当存在可执行工作时，项目级 plan 会确定性覆盖轮转：有 `awaiting_test` 任务时优先 `tester`，有 `awaiting_review` 任务时优先 peer `reviewer`，有 `pending` 任务时优先 `driver`。agent 分配仍然按墙钟 slot 交替，所以 Codex 和 Claude Code 不会永久拥有某个角色。

resource lock 是排他的路径前缀租约。锁住 `src` 会与 `src/mmux/cli.py` 冲突；锁住 `.` 会与整个项目冲突。worker 在某个 role 下获取 resource lock 时，当前 role generation 会写入 lock，防止过期 driver 工作继续提交。

## 任务执行

任务执行是显式开启的。默认 `mmux start` 只观察，不让 agent 改代码。使用 `--execute-agents` 后，持有 `driver` 的 worker 会领取一个 pending task、获取该任务的 resource lock、创建隔离 git worktree，并非交互运行本地 agent CLI：

- Codex：`codex exec`
- Claude Code：`claude -p --verbose --output-format stream-json --include-partial-messages`

Claude 使用流式 JSON 输出，这样长时间推理或工具调用时 adapter 仍会持续收到心跳。纯文本模式通常只在最终响应时输出，复杂任务期间容易被 no-output watchdog 误判为静默。

driver 执行结束后，确定性策略会检查 worktree diff：

- 无 diff -> `no_change`
- 修改 `.git`、`.mmux`、`.env*` 等保护路径 -> `rejected`
- 任意 changed path 不在任务 resource lock 内 -> `rejected`
- 通过 driver diff policy -> `awaiting_review`

持有 `reviewer` 的 worker 随后检查同一个 task worktree。reviewer 输出被限制为结构化协议：

```text
MMUX_REVIEW APPROVE
MMUX_REVIEW REQUEST_CHANGES: <short reason>
```

`APPROVE` 会把任务推进到 `awaiting_test`。`REQUEST_CHANGES` 会把任务放回 `pending`，并把 review note 写入 task payload。reviewer 不能 review 自己作为 driver 产出的工作，也不能改 task diff；如果 reviewer 输出格式错误、adapter 失败或超时，mmux 会记录日志并 bypass 到 `awaiting_test`，避免 review 层变成第二个模型裁判或新的死锁点。

持有 `tester` 的 worker 随后在同一个 task worktree 里运行确定性检查：

- `git diff --check HEAD --`
- 对 changed Python files 运行 `python -m py_compile`
- 对 changed shell scripts 运行 `sh -n`
- 对 changed JSON files 运行 `python -m json.tool`
- 如果存在 `tests/`，运行 `python -m unittest discover -s tests`
- 当项目画像确认无需安装依赖即可运行时，执行本地 package test，例如已有 `node_modules` 的 Node `test` script

diff 范围内的检查，例如 whitespace 和 changed-file 语法检查，必须在 patched task worktree 上通过。suite 级检查，例如 `unittest` 和本地 package test，会先在临时 `HEAD` baseline worktree 里跑同一条命令。如果 baseline 本来就失败，patched suite 结果只作为诊断日志记录，任务 payload 会写入 `tester_baseline_failures`；已有的 suite 失败本身不会直接拒绝 patch。如果 baseline 通过，则 patched suite 必须通过。

只有 tester 通过的 patch 才会应用回主工作区，并且主工作区必须没有 tracked changes。

supervisor 仍然不调用模型。它只发放租约、检查文件事实、记录结果；模型工作只发生在 worker adapter 内部。

## 常驻 Agent

`--resident-agents` 会在主窗口里打开长期存在的交互式 Codex 和 Claude pane，而不是把非交互 worker adapter 放在可见 pane 里。每个常驻 agent 都有固定 git worktree：

```text
.mmux/resident/codex/
.mmux/resident/claude/
```

这些 worktree 会在常驻 session 启动时 reset 到 `HEAD`。它们用于稳定上下文、讨论、探索和人工接管；真正能不能影响主工作区，仍然由确定性 gate 决定。

tmux session 同时也是通信面。mmux 可以向稳定的 resident pane 投递单行控制消息：

```text
MMUX_TASK from=mmux task=#12 ...
MMUX_REVIEW from=mmux task=#12 ...
MMUX_NOTE from=mmux ...
```

人和未来的确定性 dispatcher 走同一条路径：

```bash
mmux tell claude note "Please review the Codex plan" --project PROJECT
```

常驻 agent 会被提示优先通过显式状态库通道上报结果：

```bash
mmux report done --task-id 12 "implemented" --agent codex --project PROJECT
mmux report blocked --task-id 12 "needs API decision" --agent claude --project PROJECT
```

如果命令是在 `.mmux/resident/codex/` 或 `.mmux/resident/claude/` 里运行，mmux 可以自动推断所属 project 和 agent。CLI report 通道和 tmux 屏幕 fallback 会产生同一种去重后的 `resident_agent_done` / `resident_agent_blocked` 事件，后续 gate 仍然只有一条处理路径。

对于 pending 任务，done report 会触发 mmux 检查该 agent 的 resident worktree diff；如果确定性 diff policy 通过，mmux 会把 patch 冻结到 `.mmux/worktrees/` 下的普通 task worktree，随后把 resident worktree 重置回 `HEAD`，并把任务推进到 `awaiting_review`。review note 和最终 tester gate 继续决定能否应用到主工作区。blocked report 会把阻塞原因写入任务 payload，并通过 tmux 给另一个常驻 agent 发确定性的 `MMUX_TASK` 接管请求；它不会让任务失败，也不会应用 blocked agent 的半成品 diff。同一任务第二次 resident block 后会升级为 `blocked`，从 open queue 里让出位置，限时运行可以继续处理其他工作。人工处理清楚后，可以用 `mmux task requeue #N` 把任务重新放回 `pending`。

如果 report 命令不可用，常驻 agent 仍然可以在 tmux 中输出 `MMUX_DONE task=#N`、`MMUX_BLOCKED task=#N`，或者 `<<MMUX:DONE task=#N ...>>` 这类 sentinel 行。supervisor 会从 tmux pane 捕获这些行，并和 report 事件共用去重与 gate 处理。当同时使用 `--resident-agents --execute-agents` 时，mmux 会额外打开一个 `automation` tmux window 跑现有非交互 worker，从而保留确定性 driver/reviewer/tester gate，同时让常驻 pane 保持长期上下文。

## 限时运行

`mmux run PROJECT --minutes N` 是普通使用时的顶层受控入口。它会在需要时初始化本地 `.mmux/` 目录，拒绝复用已经存在的 tmux session，先生成项目画像；如果任务队列里没有 pending 或进行中的工作，会自动补一个保守默认任务。运行期间，每个 checkpoint 也会在 open work 耗尽且剩余执行预算足够时继续补下一个保守默认任务。`--no-default-task` 可以关闭这个队列引导与补给，用于纯观察运行。随后命令记录 `run_started` 事件，启动与 `mmux start` 相同的四窗格工作区，并用墙钟时间驱动运行窗口。

运行期间，它会定期把剩余时间和任务状态计数写到 stdout 与 supervisor log。到达时限或收到 `KeyboardInterrupt` 后，它会停止 tmux session，清理运行期 lease、lock、heartbeat，把未完成的 `running` 任务恢复为 `pending`，把未完成的 `running_review` 任务恢复为 `awaiting_review`，把未完成的 `running_test` 任务恢复为 `awaiting_test`，记录 `run_finished`，并打印 before/after/delta 任务汇总。

默认情况下，限时运行只观察，不让 agent 改代码。加上 `--execute-agents` 后，Codex 和 Claude Code 才会在上面描述的确定性 gate 内非交互执行任务。

限时运行会把绝对 deadline 传给 worker。worker 领取任务前，会根据 deadline、配置的 adapter 超时和停止前预留时间计算剩余执行预算；预算太小时不会再启动新的 driver、reviewer 或 tester 动作。agent adapter 还有无输出超时：如果 CLI 在配置时间内没有产生 stdout/stderr，mmux 会终止它，并把原因写入 task log。adapter 超时和无输出会被视为 agent health failure：mmux 会重新排队任务，在确定性状态里记录 agent cooldown，并在 cooldown 过期前跳过该 agent 的 driver lease。

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

加上 `--resident-agents` 后，同样的可见 pane 位置会放长期存在的 Codex 和 Claude 会话；如果同时启用执行，worker 自动化会移动到 `automation` window。

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

- `reviewer` 的真实 review gate。
- 可配置 tester 命令。
- worktree 清理和归档策略。
- commit/checkpoint/rollback 策略。
- 自动提交和远端协作策略。
