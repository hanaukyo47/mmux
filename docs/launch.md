# mmux Launch Copy

Use this for alpha launch posts. Keep the claims narrow and testable.

## Positioning

mmux is a deterministic tmux supervisor for long-running Codex + Claude Code
pair programming.

The hook:

> Models write and review. Git, locks, leases, and tests decide.

The contrast:

> Unlike model-chat agent frameworks where LLMs negotiate outcomes, mmux keeps
> the referee outside the model loop.

## Claims To Make

- Local-first tmux workspace for visible coding-agent collaboration.
- Deterministic supervisor: timers, role leases, resource locks, git facts, and
  tester results drive state.
- A public example repo can reproduce the loop in minutes without spending
  model tokens.
- Codex and Claude Code can rotate through driver/reviewer/tester roles.
- Driver diffs pass through reviewer notes and deterministic tester gates before
  touching the main worktree.
- Resident mode keeps interactive agents visible and gives them stable
  worktrees.
- Alpha quality: ready for controlled experiments and feedback.

## Claims To Avoid

- Production-ready unattended coding.
- Guaranteed endless useful work.
- Works reliably on every repository.
- LLM-free coding. The supervisor is deterministic; the workers are still LLM
  agents.
- Better than every multi-agent framework in all cases.

## English One-Liners

> mmux is a deterministic tmux supervisor for Codex + Claude Code pair
> programming: models write and review; git, locks, leases, and tests decide.

> I built mmux because I do not want LLMs to referee other LLMs. The agents work
> in tmux; a deterministic state machine owns the loop.

> Show HN: mmux, a deterministic supervisor for multi-agent coding in tmux

## Chinese One-Liners

> 我做了个 mmux：让 Codex 和 Claude Code 在 tmux 里常驻结对干活，但裁判不是大模型，而是确定性的状态机、git diff、资源锁和测试结果。

> mmux 不是 agent 群聊，而是一个本地确定性 supervisor：模型负责写、review、讨论，系统负责租约、锁、diff 和测试 gate。

> 如果你也不想让 LLM 仲裁 LLM，可以看看 mmux。

## X / Twitter Draft

I built mmux, a deterministic tmux supervisor for long-running Codex + Claude
Code pair programming.

The key idea: LLMs are workers, not referees.

- driver writes in an isolated git worktree
- reviewer gives structured approve/request-changes notes
- tester gates with git facts and local checks
- tmux keeps everything visible

Alpha: looking for people who enjoy trying weird coding-agent infrastructure.

## Hacker News Draft

Title:

```text
Show HN: mmux, deterministic tmux supervision for multi-agent coding
```

Body:

```text
I built mmux, a local supervisor for long-running Codex + Claude Code pair work.

The thing I wanted to avoid is using one LLM to referee another LLM. In mmux,
the agents can write, review, and summarize, but the global control loop is a
deterministic state machine. It uses role leases, resource locks, git facts,
baseline-aware tester gates, and tmux for visibility/human takeover.

Current alpha loop:

pending -> driver -> awaiting_review -> reviewer -> awaiting_test -> tester -> completed

There is a small example repo with fake Codex/Claude commands for a zero-token
first run:

git clone https://github.com/hanaukyo47/mmux-example-todo.git
cd mmux-example-todo
./demo.sh

It is not production-ready unattended coding. I am sharing it now because I
think the control-plane shape is the interesting part, and I would like feedback
from people who have tried to make multi-agent coding reliable.
```

## V2EX / 即刻 Draft

```text
我做了一个叫 mmux 的小工具，想解决一个我自己很痛的问题：
多 agent 写代码的时候，如果让 LLM 仲裁 LLM，很快就会变成互相妥协、绕圈子、不可控。

mmux 的做法是：
- tmux 负责可观察和人工接管
- Codex / Claude Code 负责写代码、review、讨论
- 确定性 supervisor 负责 role lease、resource lock、git diff、tester gate
- driver 写完先过 peer reviewer，再过 tester，最后才 apply 到主工作区
- 有一个 example repo 可以零 token 复现 request-changes -> fix -> test -> apply

现在是 alpha，适合受控实验和看架构，不建议直接无人值守跑生产项目。
```

## Demo Caption

```text
In this run, Codex produces a scoped diff, Claude requests changes, Codex fixes
the issue, and mmux only applies the patch after peer review and deterministic
tester gates pass. The interesting part is not that two models can talk; it is
that they do not own global control.
```

## Before Posting Checklist

- `git status --short --branch` is clean and pushed.
- `PYTHONPATH=src python3 -m unittest discover -s tests` passes.
- `curl -fsSL https://raw.githubusercontent.com/hanaukyo47/mmux/main/install.sh | sh`
  has been smoke-tested recently.
- `git clone https://github.com/hanaukyo47/mmux-example-todo.git && cd
  mmux-example-todo && ./demo.sh` has been smoke-tested recently.
- Demo recording visibly shows reviewer request-changes, driver fix, reviewer
  approve, tester pass.
- The post says alpha explicitly.
