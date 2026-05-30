# State Machine v2: PDCA + Orthogonal Concerns

## Why this doc

The original task state machine in `src/mmux/cli.py` exposed **11 task
statuses**. Most of that surface area was not conceptual — it was three
different concerns piled into one enum:

1. The conceptual loop work goes through (Plan → Do → Check → Act).
2. Concurrency control (who currently holds which role, which paths are locked).
3. Outcome / failure variants (blocked, rejected, no_change, escalated).

The v2 implementation pulls them apart. Goals:

- Show that the **conceptual** state machine is 4 stages.
- Show what is **leaking** into the task status enum from the other two layers.
- Wire the **Act → Plan** feedback edge behind "self-iteration capability".

This is now the implementation note for the v2 model. The CLI still derives the
old status labels for readability, but the durable task state is
`stage + outcome + in_progress + check_step`.

## Previous model: 11 task statuses

The compatibility labels are:

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
deciding it cannot continue. In the old enum they sat next to real stages, so
when reading the code you could not tell at a glance whether `rejected` was "a
place the task is in" or "a verdict on the task".

## V2: 4 conceptual stages (PDCA)

```
                ┌──────────────────────────────────┐
                ▼                                  │
   ┌──────┐  ┌────┐  ┌───────┐  ┌─────┐            │
   │ Plan │→ │ Do │→ │ Check │→ │ Act │────────────┘
   └──────┘  └────┘  └───────┘  └─────┘
```

| Stage  | Purpose                                                       | Today's equivalent                                                          |
| ------ | ------------------------------------------------------------- | --------------------------------------------------------------------------- |
| Plan   | Read what's needed, decide what to change, size the change    | `stage=plan`                                                               |
| Do     | Produce the diff                                              | `running` (driver)                                                          |
| Check  | Verify the diff is reasonable and does not break things       | `running_review` + `running_test`                                           |
| Act    | Apply patch back to main; emit a summary for next Plan        | `stage=act, outcome=completed`                                             |

Three notable changes versus the old model:

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

   **Plan review reuses the existing reviewer adapter call** with a different
   prompt and a different expected artifact (plan verdict instead of diff
   verdict). No new adapter wiring; one less moving part. If we later want
   to swap a cheaper model in for plan review, that's a prompt-routing
   change, not a structural one.

   **Plan-stage `outcome=blocked` follows the existing peer-takeover
   escalation.** First time planner+reviewer cannot converge: rerouted to
   the peer agent (codex ↔ claude). Different models do plan differently;
   it's worth one retry on the other side before declaring the task
   ill-posed. Second time: `outcome=blocked`, surfaced to human for
   requeue or rescope. This mirrors what resident `MMUX_BLOCKED` does in
   `RESIDENT_BLOCKED_ESCALATION_EVENTS` today, so we get one escalation
   model across all stages instead of two.

2. **Check unifies reviewer + tester.** Both are Check sub-steps with different
   fidelity — LLM review is fast, cheap, semantic; deterministic tests are
   slow, precise, syntactic. They do not need separate top-level stages; they
   are a check pipeline whose internal ordering is an implementation detail.

3. **Act gains a feedback edge.** In the old model `completed` was terminal. In v2, Act
   has two responsibilities: apply the patch, *and* emit a structured summary
   that the next Plan stage can consume. This single edge is what turns the
   old PDC**A**→stop pipeline into a real closed loop, and is the
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

- Role leases (`driver`, `reviewer`, `tester`, `scout`, and `summarizer`).
- Resource locks with path-prefix conflict detection.
- The 5-minute assignment slot rotation.
- Worker heartbeats and lease TTLs.

A task in `Do` is just a task in `Do`. Separately, the supervisor knows the
driver lease is held by codex and the resource lock on `src/foo.py` expires in
N seconds. These two views change independently. Previously they were entangled —
`running` versus `pending` is partly a stage and partly a "someone is working"
flag, and you cannot read one without the other.

The implementation collapses `running` / `running_review` / `running_test` into
`in_progress` plus the active role lease. `check_step=review|test` records the
Check sub-step so scheduling can still prioritize reviewer and tester work
without making them top-level task stages.

The Act-stage summarizer runs inline from tester on the hot path, and the
standalone `summarizer` lease backfills older completed tasks that do not yet
have an `act_summary`. It compresses reviewer notes, tester logs, and agent
struggles into the structured summary that feeds the next Plan. Without this
compression, feeding raw Check output back into Plan would blow the context
window on any non-trivial codebase.

## Outcome: a separate column

`failed`, `no_change`, `rejected`, `blocked`, and `completed` are not stages;
they are outcomes of a stage. They live in `outcome`, orthogonal to `stage`.

A task is then described by two questions:

- `stage=Check, outcome=null` — currently in Check.
- `stage=Check, outcome=rejected` — Check rejected the diff for policy.
- `stage=Do, outcome=no_change` — Do produced nothing.
- `stage=Plan, outcome=blocked` — planner could not form a plan that reviewer
  would approve, even after retries.

Two columns, two questions, no ambiguity about whether `rejected` is a place
the task sits or a verdict on the task.

## The Act → Plan edge

This is the actual deliverable behind the branch name.

Act applies the patch back to the main worktree, records `act_summary`, and can
feed the next Plan. Static frontier discovery still exists, but it is no longer
the only queue source.

V2's Act adds two responsibilities:

1. **Emit a structured `act_summary` event** (via the `summarizer` role)
   capturing what was done, what tests revealed, what the reviewer flagged,
   and how the driver struggled (multiple attempts, peer takeover,
   escalation, baseline failures, etc.).
2. **Feed those summaries into a fourth task-discovery source** — a
   reflection source alongside the existing three static ones.

The reflection source is LLM-driven, unlike the static three, so it must go
through admission control before it can pollute the task queue.

**Reflection tasks are modeled as a separate `kind=reflection`**, not as a
tag on ordinary tasks. Reasons:

- Admission policy is genuinely different: every reflection task has to
  cite concrete evidence (a file, a failure event, a reviewer note) before
  promotion from `proposed` to Plan.
- Self-modifying reflection tasks touching `src/mmux/` get a stricter execution
  policy: a smaller diff cap, an extra `.mmux/self-mutation` lock, and no
  reviewer-bypass path.
- Other execution-policy differences can hang off this boundary later without
  changing the core PDCA state model.
- Keeping the kind explicit prevents future policy decisions from silently
  treating reflection tasks and ordinary tasks as identical.

Reflection-kind tasks default to a `proposed` state that is **not** part of
the conceptual stage machine — it is a queue admission gate, not a stage.
Evidence-citing tasks auto-promote to Plan; vague ones stay in `proposed`
until a human looks at them, or until they age out by TTL.

This is the only place where LLM judgment is allowed to influence the task
queue. Every stage transition after admission is still owned by the
deterministic referee. Timed runs invoke this edge automatically once the open
queue is empty and enough budget remains; `mmux reflect` exposes the same path
manually.

## Migration

The v2 schema adds `stage`, `outcome`, `in_progress`, and `check_step` while
keeping `status` as a derived compatibility label. Existing alpha state rows are
mapped from their legacy status when the schema is opened.

- **Durable state is v2.** New transitions write `stage`, `outcome`,
  `in_progress`, and `check_step` first, then derive `status`.
- **Existing `.mmux/state.db` files survive.** Legacy rows are migrated in
  place from `pending`, `running`, `awaiting_review`, `awaiting_test`,
  `completed`, `failed`, `no_change`, `rejected`, `blocked`, or `proposed`.
- **In-flight work still resets on stop.** Runtime cleanup clears active
  Plan/Do/Check claims back to the appropriate waiting stage.

## What remains open

- The Plan adapter now emits a JSON `{read, plan, risks}` contract, gated
  deterministically before the LLM plan reviewer (`read` non-empty and citing a
  real path; `plan` non-empty); a deficient contract becomes a request-changes
  and follows the existing escalation. Still open: the gate does not yet check
  that `read` covers the files the `plan` proposes to touch.
- `scout` now calls a model to discover research-style tasks from the project
  profile and file tree, falling back to deterministic frontier work when the
  model is unavailable or proposes nothing. Its proposals flow through the same
  `kind=reflection` admission gate as the Act → Plan edge (concrete evidence
  auto-promotes; vague ones wait as `proposed`). What is still open: scout does
  not yet **cache research notes across tasks**, which is the precondition (noted
  above) for promoting research to a peer stage.
