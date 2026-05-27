# mmux

Deterministic multi-agent pair programming over tmux.

[简体中文说明](README.zh-CN.md)

mmux is a local supervisor for long-running coding-agent collaboration. It uses
tmux for visibility and human takeover, but keeps orchestration deterministic:
timers, role leases, resource locks, git facts, and test results decide what can
happen. LLMs propose, implement, review, and summarize. They do not referee the
system.

## Current Scope

This repository starts as the control-plane skeleton:

- `mmux init` creates a project-local `.mmux/` state directory.
- `mmux doctor` checks local dependencies such as `tmux`, `codex`, and `claude`.
- `mmux inspect` detects project ecosystems, languages, markers, active checks,
  and suggested checks without using an LLM.
- `mmux start` creates a four-pane tmux workspace for supervisor, Codex worker,
  Claude worker, and logs.
- `mmux run --minutes N` starts the tmux workspace for a bounded wall-clock
  window, adds conservative default tasks when the open queue is empty and time
  remains, writes checkpoints, stops it automatically, and prints a task
  summary.
- `mmux start/run --resident-agents` opens persistent interactive Codex and
  Claude panes with fixed resident worktrees under `.mmux/resident/`.
- `mmux tell` sends `MMUX_TASK`, `MMUX_REVIEW`, or `MMUX_NOTE` protocol lines to
  a resident agent through tmux.
- The supervisor captures resident `MMUX_DONE` and `MMUX_BLOCKED` lines from
  tmux panes and records them as deterministic events.
- Resident `MMUX_DONE task=#N` freezes the agent's resident diff into a task
  worktree, resets the resident worktree, and moves the task to `awaiting_test`;
  tester still gates acceptance.
- Resident `MMUX_BLOCKED task=#N` records the blocked reason and sends the peer
  resident agent a deterministic `MMUX_TASK` takeover request through tmux.
- `mmux status` prints deterministic state from `.mmux/state.db`.
- `mmux tasks` prints the deterministic task queue.
- `mmux roles` prints role leases and worker heartbeats.
- `mmux locks` prints resource locks.
- `mmux lease acquire/release` exercises deterministic role leasing.
- `mmux lock acquire/release` exercises deterministic resource locking.

By default, workers record heartbeat and lease state without editing code. Use
`mmux run --minutes N --execute-agents` for a bounded autonomous window, or
`mmux start --execute-agents` for manual tmux control. With execution enabled,
the worker holding `driver` claims a pending task, acquires its resource lock,
creates an isolated git worktree, and runs Codex or Claude Code
non-interactively. Accepted driver diffs move to `awaiting_test`; the worker
holding `tester` runs deterministic checks and only then applies the patch back
to the main worktree.

Resident mode is for visibility and long-lived agent context. With
`--resident-agents`, the visible Codex and Claude panes are real interactive
sessions, and the tmux session becomes their shared coordination surface. The
current deterministic worker/gate path is still the authority for applying code:
when `--resident-agents --execute-agents` are used together, mmux opens a second
`automation` window for the existing non-interactive workers while the resident
agents remain available for discussion and human takeover.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/hanaukyo47/mmux/main/install.sh | sh
```

The installer clones mmux into `~/.local/share/mmux/repo`, creates an isolated
virtual environment in `~/.local/share/mmux/venv`, and links `mmux` into
`~/.local/bin`. It does not use `sudo`.

If `tmux` is missing on macOS and Homebrew is already installed:

```bash
curl -fsSL https://raw.githubusercontent.com/hanaukyo47/mmux/main/install.sh | MMUX_INSTALL_DEPS=1 sh
```

## Quick Start

For a low-friction first run:

```bash
cd /path/to/project
mmux doctor
mmux inspect .
mmux run . --minutes 30
```

This observes only. It initializes `.mmux/`, profiles the project, adds a
conservative default task if the queue is empty, starts tmux, writes
checkpoints, replenishes default tasks when the open queue is exhausted and time
remains, and stops at the deadline. To let Codex and Claude Code actually edit
code inside the deterministic gates:

```bash
mmux run . --minutes 30 --execute-agents
```

For the experimental resident-agent view:

```bash
mmux run . --minutes 30 --resident-agents
```

## Install For Local Development

```bash
cd /path/to/mmux
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Run without installation:

```bash
cd /path/to/mmux
PYTHONPATH=src python3 -m mmux.cli doctor
```

## Commands

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
mmux start /path/to/project
mmux start /path/to/project --execute-agents
mmux start /path/to/project --resident-agents
mmux tell claude note "Please review the Codex plan" --project /path/to/project
mmux attach /path/to/project
mmux status /path/to/project
mmux tasks /path/to/project
mmux task add "Add focused tests" --resource tests --project /path/to/project
mmux roles /path/to/project
mmux locks /path/to/project
mmux lease acquire scout --agent codex --project /path/to/project
mmux lease release scout --agent codex --project /path/to/project
mmux lock acquire src --agent codex --project /path/to/project
mmux lock release src --agent codex --project /path/to/project
mmux stop /path/to/project
```

## Design Rules

- The supervisor is deterministic and does not call a model.
- Agents are workers, not owners of global control.
- Roles are leased, not hard-coded to specific agents.
- A role lease has a generation token; stale work is ignored.
- Resource locks prevent concurrent writes to the same files or modules.
- Agent execution happens in task git worktrees under `.mmux/worktrees/`.
- Resident agent context lives in fixed git worktrees under `.mmux/resident/`.
- Resident agent communication is a tmux protocol line, not a model judge.
- Resident `MMUX_DONE` can hand work to tester; it is not treated as acceptance.
- Resident `MMUX_BLOCKED` requests peer takeover without failing the task.
- Diff policy rejects protected paths and files outside the task resource.
- Tester gate infers zero-config local checks before applying accepted patches.
- Pending or awaiting-test work gets deterministic `driver/tester` priority
  during timed runs.
- Agent adapters have bounded runtime and no-output timeouts tied to the timed
  run deadline.
- Adapter timeout/no-output failures requeue the task and put that agent on
  cooldown, so another agent can take the next driver lease.
- Stopping a run requeues unfinished `running` and `running_test` tasks.
- Time windows drive the loop; round counts are only internal diagnostics.
- tmux is the observation layer, not the source of truth.

See [docs/design.md](docs/design.md) for the initial architecture.
