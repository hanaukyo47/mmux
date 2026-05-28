# mmux

基于 tmux 的确定性多智能体结对编程监督器。

[English README](README.md)

![mmux alpha demo recording](docs/assets/mmux-demo.gif)

- 两个不同的 LLM 围绕同一个任务结对：一个写，一个 review。
- 是否完成由确定性状态机决定，不交给另一个 LLM 仲裁。
- 每次运行都有 `--minutes N` 时间盒和 checkpoint，不会无限烧 token。

mmux 不是让大模型互相投票的 agent 群聊，而是把裁判从模型手里拿出来。Codex 和 Claude Code 可以在可观察、可接管的 tmux pane 里写代码、review、讨论；状态流转由确定性 supervisor 根据计时器、role lease、resource lock、git facts 和测试结果决定。

mmux 也在 dogfood 项目级 `AGENTS.md` 简报：常驻 agent 会收到和人类协作者相同类型的仓库上下文，避免长跑时跑偏。

> Alpha：mmux 现在适合受控实验、演示和早期用户试用；还不应宣传为生产级无人值守编码系统。

一句话安装：

```bash
curl -fsSL https://raw.githubusercontent.com/hanaukyo47/mmux/main/install.sh | sh
```

演示和发布材料：

- [Demo 指南](docs/demo.md)
- [发布文案](docs/launch.md)

五分钟沙盒：

```bash
git clone https://github.com/hanaukyo47/mmux-example-todo.git
cd mmux-example-todo
./demo.sh
```

这个沙盒使用确定性的 fake `codex` / `claude` 命令，不消耗模型 token，但会跑真实的 mmux supervisor 流程：frontier 发现任务、driver 写隔离 diff、peer reviewer 先 request changes、driver 修正、reviewer approve、tester gate 通过后再 apply patch。

## 当前状态

mmux 目前是一个可试用的 MVP，适合受控执行明确任务，还不建议无人值守长时间自我迭代。

已经具备：

- `mmux init`：在目标工程里创建 `.mmux/` 本地状态目录。
- `mmux doctor`：检查 `tmux`、`codex`、`claude`、`git` 等依赖。
- `mmux inspect`：不调用模型，识别项目生态、语言、marker、默认启用检查和建议检查。
- `mmux frontier`：不调用模型，根据 TODO、测试缺口和 suggested checks 展示确定性的下一步候选任务。
- `mmux start`：启动四窗格 tmux 工作区：supervisor、Codex worker、Claude worker、日志。
- `mmux run --minutes N`：启动一个有时间上限的 tmux 工作区；如果 open queue 为空且时间足够，会持续补保守默认任务；运行期间定期 checkpoint，到点自动停止并打印任务汇总。
- `mmux start/run --resident-agents`：打开真实常驻 Codex / Claude 交互 pane，为它们固定 `.mmux/resident/` 下的 resident worktree，并在项目存在 `AGENTS.md` 时注入一份有长度上限的项目简报。
- `mmux tell`：通过 tmux 向常驻 agent pane 发送 `MMUX_TASK`、`MMUX_REVIEW` 或 `MMUX_NOTE` 协议行。
- `mmux report done|blocked`：让常驻 agent 通过状态库通道上报任务结果，不必把关键通信押在终端输出上。
- supervisor 仍会从 tmux pane 捕获常驻 agent 输出的 `MMUX_DONE` 和 `MMUX_BLOCKED` 行，作为可观察的 fallback，并去重记录为确定性事件。
- 常驻 agent 输出 `MMUX_DONE task=#N` 后，mmux 会把该 agent 的 resident diff 冻结到普通 task worktree，重置 resident worktree，并把任务推进到 `awaiting_review`；review note 和最终 tester gate 继续决定能否进入主工作区。
- 常驻 agent 输出 `MMUX_BLOCKED task=#N` 后，mmux 会记录阻塞原因，并通过 tmux 给另一个常驻 agent 发确定性的 `MMUX_TASK` 接管请求。
- 同一任务第二次收到 resident `MMUX_BLOCKED` 后会升级为 `blocked`，让限时运行继续处理其他工作。
- driver diff 会先经过 peer reviewer，再进入 tester；review 可以 approve、request changes，也可以在格式错或超时时被 bypass，避免卡死运行。
- `mmux status`：查看 `.mmux/state.db` 里的确定性状态。
- `mmux tasks` / `mmux task add`：查看和添加任务。
- `mmux task requeue #N`：把 blocked 或 failed 任务重新放回 `pending`。
- `mmux roles` / `mmux lease`：查看和管理角色租约。
- `mmux locks` / `mmux lock`：查看和管理资源锁。
- `mmux start --execute-agents`：允许持有 `driver` 的 worker 执行任务。

默认情况下，worker 只记录 heartbeat 和 role lease，不会改代码。推荐用 `mmux run --minutes N --execute-agents` 开启受控的限时自主执行窗口；需要手动观察和接管时，也可以用 `mmux start --execute-agents`。启用执行后，持有 `driver` 的 worker 会领取 pending task、获取 resource lock、创建独立 git worktree，并在 worktree 中先跑一次结构化 plan 步骤：Codex 或 Claude Code 输出 `READ` / `PLAN` / `RISKS` 三段，末尾以 `MMUX_PLAN PROCEED` 或 `MMUX_PLAN ABORT` 结束。`PROCEED` 的 plan 紧接着会过一次 plan reviewer（复用 `MMUX_REVIEW APPROVE` / `REQUEST_CHANGES` 协议）；approve 通过后 plan 会作为上下文喂给随后的 diff 步骤；第一次 `REQUEST_CHANGES` 会把任务退回 pending 等下一个 driver 重新 plan，第二次拒绝直接进 `blocked`。`ABORT` 让任务直接进入 `no_change`，不再消耗 diff 阶段。

driver 产出的 diff 不会直接进入主工作区。通过路径策略检查后，任务进入 `awaiting_review`；peer `reviewer` 可以结构化 approve 或 request changes，随后通过或 bypass 的 diff 才进入 `awaiting_test`。持有 `tester` 的 worker 会运行确定性检查，通过后才把 patch 应用回主工作区。

常驻模式服务于可观察性和固定上下文。加上 `--resident-agents` 后，界面里的 Codex 和 Claude pane 是真实交互式会话，tmux session 同时也是它们的通信面。当前确定性 worker/gate 仍然负责真正应用代码：当同时使用 `--resident-agents --execute-agents` 时，mmux 会额外打开一个 `automation` tmux window 跑现有非交互 worker，常驻 agent 则用于讨论、观察和人工接管。

## 一句话安装

README 顶部也保留了同一条安装命令：

```bash
curl -fsSL https://raw.githubusercontent.com/hanaukyo47/mmux/main/install.sh | sh
```

安装脚本会把源码 clone 到 `~/.local/share/mmux/repo`，在 `~/.local/share/mmux/venv` 创建独立虚拟环境，并把 `mmux` 链接到 `~/.local/bin`。整个过程不使用 `sudo`。

如果 macOS 上缺少 `tmux`，并且已经装了 Homebrew，可以让安装脚本顺手安装依赖：

```bash
curl -fsSL https://raw.githubusercontent.com/hanaukyo47/mmux/main/install.sh | MMUX_INSTALL_DEPS=1 sh
```

## 快速开始

如果只是想先确认 mmux 好不好使，不要直接拿真实工程试，先跑 example repo：

```bash
git clone https://github.com/hanaukyo47/mmux-example-todo.git
cd mmux-example-todo
./demo.sh
```

这是推荐的第一次运行。它会把本地 fake agent 命令临时加到 `PATH`，然后执行一个两分钟的 `mmux run --execute-agents` 窗口，用于稳定复现完整控制流。

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
mmux frontier /path/to/project

mmux run /path/to/project --minutes 30
mmux run /path/to/project --minutes 30 --execute-agents
mmux run /path/to/project --minutes 30 --resident-agents
mmux run /path/to/project --minutes 30 --resident-agents --execute-agents
mmux run /path/to/project --minutes 30 --no-default-task
mmux run /path/to/project --minutes 30 --agent-no-output-seconds 120

mmux task add "Add focused tests" --resource tests --project /path/to/project
mmux task requeue #12 --project /path/to/project --reason "human decision made"
mmux tasks /path/to/project

mmux start /path/to/project
mmux start /path/to/project --execute-agents
mmux start /path/to/project --resident-agents
mmux tell claude note "Please review the Codex plan" --project /path/to/project
mmux report done --task-id 12 "implemented" --agent codex --project /path/to/project
mmux report blocked --task-id 12 "needs API decision" --agent claude --project /path/to/project
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
  -> awaiting_review
  -> running_review
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
- `reviewer`：检查同一个 task worktree，只能结构化 approve 或 request changes；格式错、超时或试图改 diff 会记录日志并 bypass 到 tester，避免卡死。
- `tester`：运行确定性测试，通过后才 apply patch。

tester 当前会根据项目画像和 changed files 自动选择零配置本地检查，包括：

- `git diff --check HEAD --`
- changed Python files 的 `python -m py_compile`
- changed shell scripts 的 `sh -n`
- changed JSON files 的 `python -m json.tool`
- 如果存在 `tests/`，运行 `python -m unittest discover -s tests`
- 如果 Node 项目已有 `node_modules` 和真实 `test` script，运行本地 package test

其中 diff whitespace 和 changed-file 语法检查必须在 patched worktree 上通过；`unittest` / package test 这类 suite 级检查会先跑临时 `HEAD` baseline。如果 baseline 本来就红，mmux 会把它记录到 `tester_baseline_failures`，但不会仅因为历史 suite 失败拒绝当前 patch。

## 设计原则

- supervisor 是确定性的，不调用模型做裁判。
- agent 是 worker，不拥有全局控制权。
- role 是 lease，不固定绑定到 Codex 或 Claude。
- role lease 有 generation token，过期工作会被拒绝。
- resource lock 防止多个 worker 同时写同一区域。
- agent 执行发生在 `.mmux/worktrees/` 下的 task worktree。
- 常驻 agent 上下文固定在 `.mmux/resident/` 下的 resident worktree。
- 常驻 agent 结果上报优先走 `mmux report` 状态库通道；tmux 协议行保留为可观察 fallback。
- 常驻 agent 的 `MMUX_DONE` 可以把任务交给 review，但不等于验收通过。
- 常驻 agent 的 `MMUX_BLOCKED` 会请求 peer 接管，但不会让任务失败。
- repeated resident blocked 会把任务移到 `blocked`，避免两个 agent 无限踢球。
- 人工处理 blocked 任务后，可以用 `mmux task requeue` 放回队列。
- diff policy 拒绝 `.git`、`.mmux`、`.env*` 等保护路径。
- reviewer 输出只作为结构化建议层，不是最终裁判；无效 review 会记录并放行到 tester，避免 review 层死锁。
- tester gate 在 patch 应用前做确定性、零配置的项目检查。
- suite 级 tester 检查会做 baseline 对照，避免历史失败直接卡死无关 patch。
- timed run 中只要存在 pending / awaiting-review / awaiting-test 工作，就确定性优先分配 `driver/reviewer/tester`。
- agent adapter 有总运行超时和无输出超时，并且会受 timed run 剩余时间约束。
- adapter 超时或无输出时会重新排队任务，并把该 agent 放入 cooldown，让另一个 agent 接手 driver。
- 停止运行会把未完成的 `running` / `running_review` / `running_test` 任务恢复到可继续状态。
- 队列自动补给会优先使用确定性 frontier candidate，其次才使用通用默认任务。
- tmux 是观察层，不是事实源；事实源是 SQLite state。

## 当前边界

mmux 现在可以安全地执行少量明确任务，但还不是成熟的无人值守系统。后续还需要补：

- reviewer 的真实 review 流程。
- 可配置 tester 命令。
- worktree 清理策略。
- checkpoint / commit / rollback 策略。
- 自动提交和远端协作策略。

更多架构说明见 [中文设计文档](docs/design.zh-CN.md)，英文版见 [docs/design.md](docs/design.md)。
