# mmux

Deterministic multi-agent pair programming over tmux.

mmux is a local supervisor for long-running coding-agent collaboration. It uses
tmux for visibility and human takeover, but keeps orchestration deterministic:
timers, role leases, resource locks, git facts, and test results decide what can
happen. LLMs propose, implement, review, and summarize. They do not referee the
system.

## Current Scope

This repository starts as the control-plane skeleton:

- `mmux init` creates a project-local `.mmux/` state directory.
- `mmux doctor` checks local dependencies such as `tmux`, `codex`, and `claude`.
- `mmux start` creates a four-pane tmux workspace for supervisor, Codex worker,
  Claude worker, and logs.
- `mmux status` prints deterministic state from `.mmux/state.db`.

The first implementation intentionally does not let agents edit code. That comes
after role leases, resource locks, and policy checks are in place.

## Install For Local Development

```bash
cd /Users/hubo-gimpo/mmux
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Run without installation:

```bash
cd /Users/hubo-gimpo/mmux
PYTHONPATH=src python3 -m mmux.cli doctor
```

## Commands

```bash
mmux init /path/to/project --task "Improve this project continuously"
mmux doctor
mmux start /path/to/project
mmux attach /path/to/project
mmux status /path/to/project
mmux stop /path/to/project
```

## Design Rules

- The supervisor is deterministic and does not call a model.
- Agents are workers, not owners of global control.
- Roles are leased, not hard-coded to specific agents.
- A role lease has a generation token; stale work is ignored.
- Resource locks prevent concurrent writes to the same files or modules.
- Time windows drive the loop; round counts are only internal diagnostics.
- tmux is the observation layer, not the source of truth.

See [docs/design.md](docs/design.md) for the initial architecture.
