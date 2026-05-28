# State Machine v2: RPDCA + Orthogonal Concerns

## Why this doc

The current task state machine in `src/mmux/cli.py` exposes **11 task statuses**.
Most of that surface area is not conceptual — it is three different concerns
piled into one enum:

1. The conceptual loop work goes through (Research → Plan → Do → Check → Act).
2. Concurrency control (who currently holds which role, which paths are locked).
3. Outcome / failure variants (blocked, rejected, no_change, escalated).

This doc pulls them apart. Goals:

- Show that the **conceptual** state machine is 5 stages.
- Show what is **leaking** into the task status enum from the other two layers.
- Identify the missing **Act → Research** feedback edge — the actual gap behind
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

## Proposed v2: 5 conceptual stages

```
                ┌────────────────────────────────────────────────┐
                ▼                                                │
   ┌─────────┐  ┌──────┐  ┌────┐  ┌───────┐  ┌─────┐             │
   │Research │→ │ Plan │→ │ Do │→ │ Check │→ │ Act │─────────────┘
   └─────────┘  └──────┘  └────┘  └───────┘  └─────┘
        ▲                                       │
        └──────── feedback (currently absent) ──┘
```

| Stage    | Purpose                                                       | Today's equivalent                                                          |
| -------- | ------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Research | Gather context: code, tests, AGENTS.md, prior runs            | Implicit inside the driver adapter call; `mmux frontier` is fully static    |
| Plan     | Decide *what* to change and how big a change is acceptable    | **Missing** — driver inlines plan+do inside one adapter invocation          |
| Do       | Produce the diff                                              | `running` (driver)                                                          |
| Check    | Verify the diff is reasonable and does not break things       | `running_review` + `running_test`                                           |
| Act      | Apply patch back to main; emit a summary for next Research    | `completed` applies the patch but emits nothing the loop consumes           |

Three notable changes versus today:

1. **Plan becomes its own stage.** A non-trivial share of today's
   `failed` / `no_change` / `rejected` outcomes are plan-stage problems the
   driver could not catch because there is no plan stage. Making Plan explicit
   lets a cheap text-only planning pass reject or rescope a task before any Do
   attempt burns a worktree.

2. **Check unifies reviewer + tester.** Both are Check sub-steps with different
   fidelity — LLM review is fast, cheap, semantic; deterministic tests are
   slow, precise, syntactic. They do not need separate top-level stages; they
   are a check pipeline whose internal ordering is an implementation detail.

3. **Act gains a feedback edge.** Today `completed` is terminal. In v2, Act
   has two responsibilities: apply the patch, *and* emit a structured summary
   that Research can consume on the next iteration. This single edge is what
   turns the current PDC**A**→stop pipeline into a real closed loop, and is
   the structural prerequisite for self-iteration.

## Concurrency control: a separate layer

Move it out of the status enum entirely. The state machine cares about *what
stage the work is in*, not *who is currently doing it*.

The concurrency layer owns:

- Role leases (`driver`, `reviewer`, `tester` — plus the dormant `scout` and
  `summarizer` defined in `ASSIGNMENT_ROLE_PAIRS`, which map naturally onto
  RPDCA: `scout` → Research, `summarizer` → Act-feedback).
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

## Outcome: a separate column

`failed`, `no_change`, `rejected`, `blocked` are not stages; they are outcomes
of a stage. Move them to an `outcome` column orthogonal to `stage`.

A task is then described by two questions:

- `stage=Check, outcome=null` — currently in Check.
- `stage=Check, outcome=rejected_policy` — Check rejected the diff for policy.
- `stage=Do, outcome=no_change` — Do produced nothing.
- `stage=Plan, outcome=blocked` — planner could not form a plan.

Two columns, two questions, no ambiguity about whether `rejected` is a place
the task sits or a verdict on the task.

## The missing edge: Act → Research

This is the actual deliverable behind the branch name.

Today's Act applies the patch back to the main worktree and stops. The next
task is picked from `discover_frontier_candidates()`, which enumerates exactly
three sources, all static: TODO/FIXME/XXX markers, source files without a
matching test file, and suggested checks from the project profile. Nothing
the previous run produced influences what the next run picks up.

V2's Act adds two responsibilities:

1. **Emit an `act_summary` event** capturing what was done, what tests
   revealed, what the reviewer flagged, and how the driver struggled
   (multiple attempts, peer takeover, escalation, baseline failures, etc.).
2. **Feed those summaries into a fourth Research source** — a reflection
   source alongside the existing three.

The reflection source is LLM-driven, unlike the static three, so it must go
through admission control before it can pollute the task queue:

- Reflection tasks default to a `proposed` state that is **not** part of the
  conceptual stage machine — it is a queue admission gate, not a stage.
- A reflection task that cites concrete evidence (a file, a failure event, a
  reviewer note) gets auto-promoted to `pending`.
- A reflection task that is vague stays in `proposed` until a human looks at
  it, or until it is dropped by a TTL.

This is the only place where LLM judgment is allowed to influence the task
queue. Every stage transition after admission is still owned by the
deterministic referee.

Tasks whose patches would touch `src/mmux/` itself (the self-iteration case)
need a stricter admission policy on top of this: smaller diff cap, mandatory
reviewer, no `review_bypassed=True` shortcut on adapter failure. That belongs
in a follow-up doc, not here.

## What this proposal does not commit to

- Schema changes to `.mmux/state.db`. The `stage` + `outcome` split is a
  conceptual claim; the migration plan is a separate doc.
- Adapter prompt redesigns for explicit Plan.
- Reflection prompt design and act_summary schema.
- Whether `proposed` is stored in the same table as `tasks` or its own.

Those follow once we agree the conceptual split is correct.

## Open questions

1. Plan as a separate adapter invocation, or folded into a "structured
   driver" that emits plan + diff together and the supervisor parses them as
   two artifacts?
2. Reflection tasks as a separate task **kind** with its own policy, or
   ordinary tasks with a `source=reflection` tag?
3. How aggressively to summarize `act_summary` before it reaches the
   reflection prompt — streaming raw reviewer notes and tester logs into
   Research will blow the context window. The dormant `summarizer` role fits
   here cleanly.
4. Migration shape: dual-write `status` (v1) and `stage`+`outcome` (v2) for
   one release, then drop v1, or hard cut?
