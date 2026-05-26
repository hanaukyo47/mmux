# mmux Design

mmux is not a smarter agent. It is a deterministic supervisor for multiple
coding-agent workers.

## Problem

Running Codex and Claude Code side by side in tmux is easy. Making them work
together for hours without stepping on each other is the hard part.

The system must prevent:

- Two agents claiming the same role.
- Two agents writing the same file region at the same time.
- A finished agent stopping the whole loop before the time window ends.
- Agents polishing the same small area indefinitely.
- A model-based supervisor becoming a third unreliable judge.

## Core Model

Agents are workers. Roles are seats.

```text
codex  -> may hold driver/reviewer/scout/tester leases
claude -> may hold driver/reviewer/scout/tester leases

driver lease   -> only holder may write code
reviewer lease -> reads diff and writes review
scout lease    -> proposes frontier candidates
tester lease   -> runs deterministic validation
```

The supervisor owns:

- Time windows.
- Role leases.
- Resource locks.
- Policy checks.
- Git diff inspection.
- Test execution.
- Logs and checkpoints.

The supervisor does not own:

- Architecture taste.
- Code implementation.
- Natural-language review quality.
- Summary prose.

Those are worker responsibilities.

## Deterministic Supervisor

Allowed inputs:

- Wall-clock time.
- Process state.
- tmux pane liveness.
- Agent hook event files.
- SQLite state.
- Git status/diff.
- Test/lint exit codes.
- Schema-valid JSON proposals.

Disallowed supervisor inputs:

- LLM judgement calls.
- Free-form terminal summaries as policy facts.
- Agent claims that bypass git/test evidence.

## State

Project state lives under `.mmux/`:

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

SQLite tables:

- `meta`
- `tasks`
- `role_leases`
- `worker_heartbeats`
- `resource_locks`
- `events`
- `frontier_items`

Role leases are single-row leases keyed by role. A lease holder must present the
current generation token for stale-sensitive actions, and expired leases can be
claimed by another worker. Worker heartbeat rows are operational status only;
they make the tmux panes and CLI state observable but do not decide policy.

Resource locks are exclusive path-prefix leases. A lock on `src` conflicts with
`src/mmux/cli.py`, and a lock on `.` conflicts with every project file. When a
worker acquires a resource lock under a role, the current role generation is
stored with the lock so stale driver work can be rejected deterministically.

Task execution is explicit. `mmux start` only observes by default. With
`--execute-agents`, the worker holding `driver` claims one pending task, acquires
that task's resource lock, creates an isolated git worktree, and runs its local
agent CLI non-interactively:

- Codex: `codex exec`
- Claude Code: `claude -p`

After execution, deterministic policy checks inspect the worktree diff:

- No diff becomes `no_change`.
- Protected paths such as `.git`, `.mmux`, and `.env*` are rejected.
- Every changed path must be inside the task resource lock.
- Accepted patches are applied back to the main worktree only if the main
  worktree has no tracked changes.

The supervisor still does not call a model. It only grants leases, evaluates
file facts, and records outcomes; model work happens inside worker adapters.

## Tmux Layout

The first workspace uses four panes:

```text
+----------------------+----------------------+
| deterministic         | codex worker         |
| supervisor            |                      |
+----------------------+----------------------+
| supervisor log        | claude worker        |
+----------------------+----------------------+
```

tmux is for observation and takeover. The database remains the source of truth.

## Frontier Policy

When a task completes before the time window ends, the next task must move toward
an unexplored boundary:

- A new module boundary.
- A user path not recently touched.
- A verification gap.
- A failing test or CI signal.
- A documented TODO/FIXME with evidence.

Recent touched files enter cooldown. Reviewers also review task selection, but
the supervisor only accepts schema-valid candidates that pass deterministic
checks.
