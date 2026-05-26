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
  window, adds a conservative default task when the queue is empty, writes
  checkpoints, stops it automatically, and prints a task summary.
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

This observes only. It initializes `.mmux/`, profiles the project, adds one
conservative default task if the queue is empty, starts tmux, writes
checkpoints, and stops at the deadline. To let Codex and Claude Code actually
edit code inside the deterministic gates:

```bash
mmux run . --minutes 30 --execute-agents
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
mmux run /path/to/project --minutes 30 --no-default-task
mmux start /path/to/project
mmux start /path/to/project --execute-agents
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
- Diff policy rejects protected paths and files outside the task resource.
- Tester gate infers zero-config local checks before applying accepted patches.
- Time windows drive the loop; round counts are only internal diagnostics.
- tmux is the observation layer, not the source of truth.

See [docs/design.md](docs/design.md) for the initial architecture.
