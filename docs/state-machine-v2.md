# State Machine v2: PDCA + Orthogonal Concerns

## Why this doc

The current task state machine in `src/mmux/cli.py` exposes **11 task statuses**.
Most of that surface area is not conceptual — it is three different concerns
piled into one enum:

1. The conceptual loop work goes through (Plan → Do → Check → Act).
2. Concurrency control (who currently holds which role, which paths are locked).
3. Outcome / failure variants (blocked, rejected, no_change, escalated).

This doc pulls them apart. Goals:

- Show that the **conceptual** state machine is 4 stages.
- Show what is **leaking** into the task status enum from the other two layers.
- Identify the missing **Act → Plan** feedback edge — the actual gap behind
  "self-iteration capability" on this branch.

This is a design discussion doc, not a refactor plan. No code changes follow
from merging it.

## Today: 11 task statuses

From `cli.py` (status column on the `tasks` table):

`pending`, `running`, `awaiting_review`, `running_review`, `awaiting_test`,
`running_test`, `completed`, `failed`, `no_change`, `rejected`, `blocked`.

Grouped by what they actually encode:

| Layer being encoded         | Statuses that live here                          |
| --------------------------- | ------------------------------------------------ |
| Conceptual stage of work    | `pending`, `awaiting_review`, `awaiting_test`, `completed` |
| "Someone is working on it"  | `running`, `running_review`, `running_test`      |
| Outcome / failure variant   | `failed`, `no_change`, `rejected`, `blocked`     |

The three `running_*` statuses are pure concurrency bookkeeping — they answer
"is a worker currently inside this stage" and have no transitions other than
"the same stage, but done". They double the visible state count without adding
any conceptual structure.

The four outcome statuses are not stages either. They are *results* of a stage
deciding it cannot continue. Today they sit next to real stages in the same
enum, so when reading the code you cannot tell at a glance whether `rejected`
is "a place the task is in" or "a verdict on the task".

## Proposed v2: 4 conceptual stages (PDCA)

```
                ┌──────────────────────────────────┐
                ▼                                  │
   ┌──────┐  ┌────┐  ┌───────┐  ┌─────┐            │
   │ Plan │→ │ Do │→ │ Check │→ │ Act │────────────┘
   └──────┘  └────┘  └───────┘  └─────┘
```

| Stage  | Purpose                                                       | Today's equivalent                                                          |
| ------ | ------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Plan   | Read what's needed, decide what to change, size the change    | **Missing** — driver inlines plan+do inside one adapter invocation          |
| Do     | Produce the diff                                              | `running` (driver)                                                          |
| Check  | Verify the diff is reasonable and does not break things       | `running_review` + `running_test`                                           |
| Act    | Apply patch back to main; emit a summary for next Plan        | `completed` applies the patch but emits nothing the loop consumes           |

Three notable changes versus today:

1. **Plan becomes its own stage with a structured contract.** The Plan adapter
   produces three artifacts together:

   - `read` — the files / greps / tests it actually consulted.
   - `plan` — what to change, how big a change, why.
   - `risks` — areas not read but plausibly relevant.

   A plan reviewer checks that `read` is sufficient to justify `plan`, and
   that `risks` doesn't hide load-bearing surprises. This is the gate that
   catches "the LLM is guessing without reading enough" — without making
   reading its own stage that no one can verify directly. A non-trivial share
   of today's `failed` / `no_change` / `rejected` outcomes are plan-stage
   problems the driver could not catch because there is no plan stage.

2. **Check unifies reviewer + tester.** Both are Check sub-steps with different
   fidelity — LLM review is fast, cheap, semantic; deterministic tests are
   slow, precise, syntactic. They do not need separate top-level stages; they
   are a check pipeline whose internal ordering is an implementation detail.

3. **Act gains a feedback edge.** Today `completed` is terminal. In v2, Act
   has two responsibilities: apply the patch, *and* emit a structured summary
   that the next Plan stage can consume. This single edge is what turns the
   current PDC**A**→stop pipeline into a real closed loop, and is the
   structural prerequisite for self-iteration.

## Rejected alternative: 5-stage RPDCA

An earlier draft of this doc proposed Research → Plan → Do → Check → Act,
splitting "read code / gather context" out as its own stage to force the LLM
to read before deciding. We rejected it. Three reasons:

1. **Research and Plan have no clean boundary.** Reading code already forms
   hypotheses. Splitting them either makes Research secretly Plan, or makes
   Plan re-read what Research already read. Both are waste.

2. **Research has no actionable gate.** Plan can be checked by a reviewer
   ("is this plan sane"). Do can be checked by "did it produce a diff".
   Check has explicit outcomes. Act has "patch applied". Research only has
   "did it read enough" — which has no actionable answer outside of "does
   the resulting plan hold up". A stage that can only be verified through
   the next stage is not a real stage; it's an input.

3. **Stage granularity should match verification granularity, not activity
   granularity.** Industrial PDCA stages are days-long because the
   verification cycle is days-long. mmux Plan calls are seconds-long. At
   this granularity, Research is just the input to Plan, not a peer of it.

The defensive need behind the 5-stage version (force the LLM to actually
read) is satisfied by the structured `read` + `plan` + `risks` contract on
Plan, which is **inspectable** — a reviewer can verify the `read` list
against the `plan`. The 5-stage version was inspectable only by hoping the
LLM self-regulated.

One scenario keeps the 5-stage version alive in our heads: if Research
artifacts become **cacheable and shareable across tasks** ("module X read
notes" reused by 5 different plans), Research has a lifecycle separate from
the task and deserves to be a peer stage. This only pays off under long-run,
multi-task-per-module workloads. mmux is not there yet, and `AGENTS.md`
already absorbs a piece of that role. Revisit if and when we are.

## Concurrency control: a separate layer

Move it out of the status enum entirely. The state machine cares about *what
stage the work is in*, not *who is currently doing it*.

The concurrency layer owns:

- Role leases (`driver`, `reviewer`, `tester` — plus the dormant `scout` and
  `summarizer` defined in `ASSIGNMENT_ROLE_PAIRS`).
- Resource locks with path-prefix conflict detection.
- The 5-minute assignment slot rotation.
- Worker heartbeats and lease TTLs.

A task in `Do` is just a task in `Do`. Separately, the supervisor knows the
driver lease is held by codex and the resource lock on `src/foo.py` expires in
N seconds. These two views change independently. Today they are entangled —
`running` versus `pending` is partly a stage and partly a "someone is working"
flag, and you cannot read one without the other.

Concrete proposal: collapse `running` / `running_review` / `running_test` into
a single `in_progress` boolean attached to the **worker lease**, not the task.
Task transitions only on stage boundaries.

The dormant `summarizer` role from `ASSIGNMENT_ROLE_PAIRS` finds a home in v2
as the Act-stage summarizer: it compresses reviewer notes, tester logs, and
agent struggles into the structured `act_summary` that feeds the next Plan.
Without this compression, feeding raw Check output back into Plan would blow
the context window on any non-trivial codebase.

## Outcome: a separate column

`failed`, `no_change`, `rejected`, `blocked` are not stages; they are outcomes
of a stage. Move them to an `outcome` column orthogonal to `stage`.

A task is then described by two questions:

- `stage=Check, outcome=null` — currently in Check.
- `stage=Check, outcome=rejected_policy` — Check rejected the diff for policy.
- `stage=Do, outcome=no_change` — Do produced nothing.
- `stage=Plan, outcome=blocked` — planner could not form a plan that reviewer
  would approve, even after retries.

Two columns, two questions, no ambiguity about whether `rejected` is a place
the task sits or a verdict on the task.

## The missing edge: Act → Plan

This is the actual deliverable behind the branch name.

Today's Act applies the patch back to the main worktree and stops. The next
task is picked from `discover_frontier_candidates()`, which enumerates exactly
three sources, all static: TODO/FIXME/XXX markers, source files without a
matching test file, and suggested checks from the project profile. Nothing
the previous run produced influences what the next run picks up.

V2's Act adds two responsibilities:

1. **Emit a structured `act_summary` event** (via the `summarizer` role)
   capturing what was done, what tests revealed, what the reviewer flagged,
   and how the driver struggled (multiple attempts, peer takeover,
   escalation, baseline failures, etc.).
2. **Feed those summaries into a fourth task-discovery source** — a
   reflection source alongside the existing three static ones.

The reflection source is LLM-driven, unlike the static three, so it must go
through admission control before it can pollute the task queue:

- Reflection tasks default to a `proposed` state that is **not** part of the
  conceptual stage machine — it is a queue admission gate, not a stage.
- A reflection task that cites concrete evidence (a file, a failure event,
  a reviewer note) gets auto-promoted to the Plan stage.
- A reflection task that is vague stays in `proposed` until a human looks at
  it, or until it is dropped by a TTL.

This is the only place where LLM judgment is allowed to influence the task
queue. Every stage transition after admission is still owned by the
deterministic referee.

Tasks whose patches would touch `src/mmux/` itself (the self-iteration case)
need a stricter admission policy on top of this: smaller diff cap, mandatory
reviewer (no `review_bypassed=True` shortcut on adapter failure), and a
separate resource-lock namespace so self-modifying runs can't race with
ordinary work. That belongs in a follow-up doc, not here.

## What this proposal does not commit to

- Schema changes to `.mmux/state.db`. The `stage` + `outcome` split is a
  conceptual claim; the migration plan is a separate doc.
- The exact JSON shape of the Plan adapter's `read` / `plan` / `risks`
  contract, or of `act_summary`.
- Whether `proposed` is stored in the same table as `tasks` or its own.

Those follow once we agree the conceptual split is correct.

## Open questions

1. Is the plan reviewer the **same** adapter call as today's diff reviewer,
   just with a different prompt and artifact? Or a distinct adapter so we
   can swap models (cheaper model for plan review, stronger for diff
   review)?
2. Reflection tasks as a separate task **kind** with its own policy, or
   ordinary tasks with a `source=reflection` tag?
3. Should Plan-stage `outcome=blocked` (planner + plan reviewer can't
   converge) trigger the same peer-takeover escalation that resident
   `MMUX_BLOCKED` does today, or fail faster?
4. Migration shape: dual-write `status` (v1) and `stage`+`outcome` (v2) for
   one release, then drop v1, or hard cut?
