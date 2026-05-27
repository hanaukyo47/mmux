# mmux Design

[简体中文设计文档](design.zh-CN.md)

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
- Local project markers and file names.

Disallowed supervisor inputs:

- LLM judgement calls.
- Free-form terminal summaries as policy facts.
- Agent claims that bypass git/test evidence.

## Project Inspection

Before a timed run, mmux profiles the local repository without calling a model.
The profile is based on deterministic markers such as `pyproject.toml`,
`package.json`, `Cargo.toml`, `go.mod`, `pom.xml`, Gradle files, `.sln`/
`.csproj`, `composer.json`, `Gemfile`, `Package.swift`, `Makefile`, and file
extensions.

The profile separates:

- Active checks: conservative local checks that mmux can run by default.
- Suggested checks: likely project commands that may need dependencies,
  toolchains, or an offline cache before they should gate patches.

This lets `mmux run` do enough local reconnaissance to avoid blind execution
while keeping the supervisor deterministic and non-model-based.

## Frontier Discovery

mmux can generate next-task candidates without asking a model. `mmux frontier`
and timed-run queue replenishment inspect repository facts such as:

- TODO/FIXME/XXX markers in tracked text files.
- Source files without obvious nearby tests.
- Suggested checks detected by project inspection.

Candidates are stored in `frontier_items` with evidence and score. When the open
queue is empty, mmux prefers the highest-scoring new frontier candidate before
falling back to a generic conservative default task. This keeps long runs moving
toward visible project boundaries while leaving final judgement to git facts and
tester gates.

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
  resident/
    codex/
    claude/
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

The baseline role plan still rotates across role pairs to avoid fixed agent
ownership. When executable work is present, the project plan overrides that
rotation deterministically: `awaiting_test` tasks prioritize `tester`,
`awaiting_review` tasks prioritize the peer `reviewer`, and `pending` tasks
prioritize `driver`. Agent assignment still alternates by wall-clock slot, so
Codex and Claude Code do not permanently own any role.

Resource locks are exclusive path-prefix leases. A lock on `src` conflicts with
`src/mmux/cli.py`, and a lock on `.` conflicts with every project file. When a
worker acquires a resource lock under a role, the current role generation is
stored with the lock so stale driver work can be rejected deterministically.

## Task Execution

Task execution is explicit. `mmux start` only observes by default. With
`--execute-agents`, the worker holding `driver` claims one pending task, acquires
that task's resource lock, creates an isolated git worktree, and runs its local
agent CLI non-interactively:

- Codex: `codex exec`
- Claude Code: `claude -p --verbose --output-format stream-json --include-partial-messages`

Claude uses streaming JSON output so long-running reasoning or tool use keeps
the adapter heartbeat alive. Plain text mode only prints a final response, which
can look silent to the no-output watchdog during substantial tasks.

After driver execution, deterministic policy checks inspect the worktree diff:

- No diff becomes `no_change`.
- Protected paths such as `.git`, `.mmux`, and `.env*` are rejected.
- Every changed path must be inside the task resource lock.
- Accepted driver diffs move to `awaiting_review`.

The worker holding `reviewer` then inspects the same task worktree. Reviewer
output is intentionally narrow and structured:

```text
MMUX_REVIEW APPROVE
MMUX_REVIEW REQUEST_CHANGES: <short reason>
```

`APPROVE` moves the task to `awaiting_test`. `REQUEST_CHANGES` moves it back to
`pending` with the review note in task payload. A reviewer may not review its
own driver work, and reviewer edits to the task diff are discarded. Invalid
review output, adapter failures, and timeouts are logged and bypassed to
`awaiting_test` so review cannot become a second model-owned referee or a
deadlock point.

The worker holding `tester` then runs deterministic checks in the same task
worktree:

- `git diff --check HEAD --`
- `python -m py_compile` for changed Python files
- `sh -n` for changed shell scripts
- `python -m json.tool` for changed JSON files
- `python -m unittest discover -s tests` when a `tests/` tree exists
- local package tests when the profile says they are available without
  dependency installation, such as a Node `test` script with `node_modules`

Diff-scoped checks such as whitespace and changed-file syntax must pass on the
patched task worktree. Suite-level checks such as `unittest` and local package
tests are baseline-aware: mmux first runs the same command in a temporary
`HEAD` worktree. If the baseline is already failing, the patched suite result is
logged as diagnostic output and the task payload records
`tester_baseline_failures`; pre-existing suite failures do not by themselves
reject the patch. If the baseline passes, the patched suite must pass.

Only tester-passed patches are applied back to the main worktree, and only if
the main worktree has no tracked changes.

The supervisor still does not call a model. It only grants leases, evaluates
file facts, and records outcomes; model work happens inside worker adapters.

## Resident Agents

`--resident-agents` opens long-lived interactive Codex and Claude panes instead
of showing the non-interactive worker adapters in the main window. Each resident
agent gets a fixed git worktree:

```text
.mmux/resident/codex/
.mmux/resident/claude/
```

These worktrees are reset to `HEAD` when the resident session starts. They are
for stable context, conversation, exploration, and human takeover; the
deterministic gate still decides which diffs may affect the main worktree.

The tmux session is also the communication surface. mmux can send one-line
control messages into stable resident panes:

```text
MMUX_TASK from=mmux task=#12 ...
MMUX_REVIEW from=mmux task=#12 ...
MMUX_NOTE from=mmux ...
```

Human operators and future deterministic dispatchers use the same path:

```bash
mmux tell claude note "Please review the Codex plan" --project PROJECT
```

Resident agents are prompted to report outcomes through an explicit state
channel:

```bash
mmux report done --task-id 12 "implemented" --agent codex --project PROJECT
mmux report blocked --task-id 12 "needs API decision" --agent claude --project PROJECT
```

When the command is run from inside `.mmux/resident/codex/` or
`.mmux/resident/claude/`, mmux can infer both the owning project and resident
agent. The CLI report path and the tmux screen fallback create the same
deduplicated `resident_agent_done` / `resident_agent_blocked` events, so the
downstream gate remains single-path.

A done report for a pending task makes mmux inspect that agent's resident
worktree diff. If deterministic diff policy accepts it, mmux freezes the patch
into a normal task worktree under `.mmux/worktrees/`, resets the resident
worktree back to `HEAD`, and moves the task to `awaiting_review`; reviewer
notes and the existing tester gate still decide whether the patch can be
applied to the main worktree. A blocked report records the blocked reason on
the task payload and sends the peer resident agent a deterministic `MMUX_TASK`
takeover request
through tmux. It does not fail the task or apply any partial diff from the
blocked agent. A second resident block for the same task escalates the task to
`blocked`, which removes it from the open queue and lets timed runs continue
with other work. After a human resolves the ambiguity, `mmux task requeue #N`
can move the task back to `pending`.

If the report command is unavailable, resident agents may still emit
`MMUX_DONE task=#N`, `MMUX_BLOCKED task=#N`, or sentinel lines such as
`<<MMUX:DONE task=#N ...>>` in tmux. The supervisor captures those lines from
tmux panes, deduplicates them against reports, and processes them through the
same gate. When `--resident-agents --execute-agents` are used together, mmux
opens an extra `automation` tmux window for the existing non-interactive
workers, preserving the deterministic driver/reviewer/tester gate while the resident
panes keep their long-lived context.

## Timed Runs

`mmux run PROJECT --minutes N` is the bounded top-level entry point for normal
use. It initializes the local `.mmux/` layout when needed, refuses to reuse an
existing tmux session, profiles the project, and adds one conservative default
task when the queue has no pending or in-progress work. During the timed
window, checkpoints replenish another conservative default task when all open
work is exhausted and enough execution budget remains. `--no-default-task`
disables that queue bootstrap and replenishment for observation-only runs. The
command records a `run_started` event, starts the same four-pane workspace as
`mmux start`, and then lets wall-clock time drive the window.

During the window, it writes periodic checkpoints with remaining time and task
status counts to stdout and the supervisor log. At the deadline, or on
`KeyboardInterrupt`, it stops the tmux session, clears runtime leases, locks, and
heartbeats, requeues unfinished `running` tasks to `pending`, requeues unfinished
`running_review` tasks to `awaiting_review`, requeues unfinished `running_test`
tasks to `awaiting_test`, records `run_finished`, and prints before/after/delta
task counts.

By default, a timed run observes only. `--execute-agents` enables non-interactive
Codex and Claude Code adapters inside the same deterministic gates described
above.

Timed runs pass their absolute deadline to workers. Before claiming work, a
worker computes a remaining execution budget from that deadline, the configured
adapter timeout, and a shutdown grace period. Workers refuse to start a new
driver, reviewer, or tester action when the remaining budget is too small.
Agent adapters also have a no-output timeout; if a CLI produces no stdout/stderr
for the configured interval, mmux terminates it and records the reason in the
task log.
Timeout and no-output adapter failures are treated as agent health failures:
mmux requeues the task, records an agent cooldown in deterministic state, and
skips that agent for future driver leases until the cooldown expires.

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

With `--resident-agents`, the same visible pane positions hold persistent Codex
and Claude sessions, and worker automation, if enabled, moves to the
`automation` window.

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

## Current Limits

The current implementation supports controlled task execution, but it is not a
complete unattended system yet. Remaining work:

- A real `reviewer` gate.
- User-configurable tester commands.
- Worktree cleanup and archival policy.
- Commit, checkpoint, and rollback policy.
- Automatic commit and remote collaboration policy.
