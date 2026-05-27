# mmux

基于 tmux 的确定性多智能体结对编程监督器。

[English README](README.md)

mmux 用 tmux 提供可观察、可接管的运行界面，让 Codex 和 Claude Code 这类代码智能体在同一个工程里协作。它的核心原则是：调度、安全和验收由确定性机制负责，模型只负责提出、实现、审阅和总结，不负责当裁判。

## 当前状态

mmux 目前是一个可试用的 MVP，适合受控执行明确任务，还不建议无人值守长时间自我迭代。

已经具备：

- `mmux init`：在目标工程里创建 `.mmux/` 本地状态目录。
- `mmux doctor`：检查 `tmux`、`codex`、`claude`、`git` 等依赖。
- `mmux inspect`：不调用模型，识别项目生态、语言、marker、默认启用检查和建议检查。
- `mmux start`：启动四窗格 tmux 工作区：supervisor、Codex worker、Claude worker、日志。
- `mmux run --minutes N`：启动一个有时间上限的 tmux 工作区；如果 open queue 为空且时间足够，会持续补保守默认任务；运行期间定期 checkpoint，到点自动停止并打印任务汇总。
- `mmux start/run --resident-agents`：打开真实常驻 Codex / Claude 交互 pane，并为它们固定 `.mmux/resident/` 下的 resident worktree。
- `mmux tell`：通过 tmux 向常驻 agent pane 发送 `MMUX_TASK`、`MMUX_REVIEW` 或 `MMUX_NOTE` 协议行。
- supervisor 会从 tmux pane 捕获常驻 agent 输出的 `MMUX_DONE` 和 `MMUX_BLOCKED` 行，并去重记录为确定性事件。
- 常驻 agent 输出 `MMUX_DONE task=#N` 后，mmux 会把该 agent 的 resident diff 冻结到普通 task worktree，重置 resident worktree，并把任务推进到 `awaiting_test`；最终验收仍然由 tester gate 决定。
- `mmux status`：查看 `.mmux/state.db` 里的确定性状态。
- `mmux tasks` / `mmux task add`：查看和添加任务。
- `mmux roles` / `mmux lease`：查看和管理角色租约。
- `mmux locks` / `mmux lock`：查看和管理资源锁。
- `mmux start --execute-agents`：允许持有 `driver` 的 worker 执行任务。

默认情况下，worker 只记录 heartbeat 和 role lease，不会改代码。推荐用 `mmux run --minutes N --execute-agents` 开启受控的限时自主执行窗口；需要手动观察和接管时，也可以用 `mmux start --execute-agents`。启用执行后，持有 `driver` 的 worker 会领取 pending task、获取 resource lock、创建独立 git worktree，并在 worktree 中非交互运行 Codex 或 Claude Code。

driver 产出的 diff 不会直接进入主工作区。通过路径策略检查后，任务进入 `awaiting_test`；持有 `tester` 的 worker 会运行确定性检查，通过后才把 patch 应用回主工作区。

常驻模式服务于可观察性和固定上下文。加上 `--resident-agents` 后，界面里的 Codex 和 Claude pane 是真实交互式会话，tmux session 同时也是它们的通信面。当前确定性 worker/gate 仍然负责真正应用代码：当同时使用 `--resident-agents --execute-agents` 时，mmux 会额外打开一个 `automation` tmux window 跑现有非交互 worker，常驻 agent 则用于讨论、观察和人工接管。

## 一句话安装

```bash
curl -fsSL https://raw.githubusercontent.com/hanaukyo47/mmux/main/install.sh | sh
```

安装脚本会把源码 clone 到 `~/.local/share/mmux/repo`，在 `~/.local/share/mmux/venv` 创建独立虚拟环境，并把 `mmux` 链接到 `~/.local/bin`。整个过程不使用 `sudo`。

如果 macOS 上缺少 `tmux`，并且已经装了 Homebrew，可以让安装脚本顺手安装依赖：

```bash
curl -fsSL https://raw.githubusercontent.com/hanaukyo47/mmux/main/install.sh | MMUX_INSTALL_DEPS=1 sh
```

## 快速开始

最低摩擦的第一次运行：

```bash
cd /path/to/project
mmux doctor
mmux inspect .
mmux run . --minutes 30
```

这只是观察模式。它会初始化 `.mmux/`、生成项目画像、在任务队列为空时补一个保守默认任务、启动 tmux、写 checkpoint，并在到点后自动停止。要允许 Codex 和 Claude Code 在确定性 gate 内实际改代码：

```bash
mmux run . --minutes 30 --execute-agents
```

如果想先看常驻 agent 形态：

```bash
mmux run . --minutes 30 --resident-agents
```

## 本地开发安装

```bash
cd /path/to/mmux
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

不安装也可以运行：

```bash
cd /path/to/mmux
PYTHONPATH=src python3 -m mmux.cli doctor
```

## 常用命令

```bash
mmux init /path/to/project --task "Improve this project continuously"
mmux doctor
mmux inspect /path/to/project

mmux run /path/to/project --minutes 30
mmux run /path/to/project --minutes 30 --execute-agents
mmux run /path/to/project --minutes 30 --resident-agents
mmux run /path/to/project --minutes 30 --resident-agents --execute-agents
mmux run /path/to/project --minutes 30 --no-default-task
mmux run /path/to/project --minutes 30 --agent-no-output-seconds 120

mmux task add "Add focused tests" --resource tests --project /path/to/project
mmux tasks /path/to/project

mmux start /path/to/project
mmux start /path/to/project --execute-agents
mmux start /path/to/project --resident-agents
mmux tell claude note "Please review the Codex plan" --project /path/to/project
mmux attach /path/to/project
mmux stop /path/to/project

mmux status /path/to/project
mmux roles /path/to/project
mmux locks /path/to/project
```

手动调试租约和锁：

```bash
mmux lease acquire scout --agent codex --project /path/to/project
mmux lease release scout --agent codex --project /path/to/project

mmux lock acquire src --agent codex --project /path/to/project
mmux lock release src --agent codex --project /path/to/project
```

## 任务状态机

```text
pending
  -> running
  -> awaiting_test
  -> running_test
  -> completed
```

失败或非接受路径：

```text
failed / rejected / no_change
```

- `driver`：在隔离 worktree 中执行模型，产生 diff。
- diff policy：拒绝保护路径和越过 resource lock 的改动。
- `tester`：运行确定性测试，通过后才 apply patch。

tester 当前会根据项目画像和 changed files 自动选择零配置本地检查，包括：

- `git diff --check HEAD --`
- changed Python files 的 `python -m py_compile`
- changed shell scripts 的 `sh -n`
- changed JSON files 的 `python -m json.tool`
- 如果存在 `tests/`，运行 `python -m unittest discover -s tests`
- 如果 Node 项目已有 `node_modules` 和真实 `test` script，运行本地 package test

## 设计原则

- supervisor 是确定性的，不调用模型做裁判。
- agent 是 worker，不拥有全局控制权。
- role 是 lease，不固定绑定到 Codex 或 Claude。
- role lease 有 generation token，过期工作会被拒绝。
- resource lock 防止多个 worker 同时写同一区域。
- agent 执行发生在 `.mmux/worktrees/` 下的 task worktree。
- 常驻 agent 上下文固定在 `.mmux/resident/` 下的 resident worktree。
- 常驻 agent 通信走 tmux 协议行，不把模型输出当裁判。
- 常驻 agent 的 `MMUX_DONE` 可以把任务交给 tester，但不等于验收通过。
- diff policy 拒绝 `.git`、`.mmux`、`.env*` 等保护路径。
- tester gate 在 patch 应用前做确定性、零配置的项目检查。
- timed run 中只要存在 pending 或 awaiting-test 工作，就确定性优先分配 `driver/tester`。
- agent adapter 有总运行超时和无输出超时，并且会受 timed run 剩余时间约束。
- adapter 超时或无输出时会重新排队任务，并把该 agent 放入 cooldown，让另一个 agent 接手 driver。
- 停止运行会把未完成的 `running` / `running_test` 任务恢复到可继续状态。
- tmux 是观察层，不是事实源；事实源是 SQLite state。

## 当前边界

mmux 现在可以安全地执行少量明确任务，但还不是成熟的无人值守系统。后续还需要补：

- 自动 frontier / scout 任务发现。
- resident blocked / retry / takeover 的完整恢复策略。
- reviewer 的真实 review 流程。
- 可配置 tester 命令。
- worktree 清理策略。
- checkpoint / commit / rollback 策略。
- 自动提交和远端协作策略。

更多架构说明见 [中文设计文档](docs/design.zh-CN.md)，英文版见 [docs/design.md](docs/design.md)。
