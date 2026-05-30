#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import tempfile
from pathlib import Path

import mmux.cli as cli


def init_repo(project: Path) -> None:
    cli.run(["git", "init"], cwd=project)
    cli.run(["git", "config", "user.email", "demo@example.com"], cwd=project)
    cli.run(["git", "config", "user.name", "mmux Demo"], cwd=project)
    (project / ".gitignore").write_text(".mmux/\n", encoding="utf-8")
    (project / "src").mkdir()
    (project / "tests").mkdir()
    (project / "src" / "__init__.py").write_text("", encoding="utf-8")
    (project / "src" / "todo_core.py").write_text(
        "\n".join(
            [
                "def normalize_title(title: str) -> str:",
                "    # TODO: trim surrounding whitespace and reject empty titles.",
                "    return title",
                "",
                "",
                "def add_todo(items: list[str], title: str) -> list[str]:",
                "    return [*items, normalize_title(title)]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (project / "tests" / "test_todo_core.py").write_text(
        "\n".join(
            [
                "import unittest",
                "",
                "from src.todo_core import add_todo",
                "",
                "",
                "class TodoCoreTests(unittest.TestCase):",
                "    def test_add_todo_appends_title(self) -> None:",
                "        self.assertEqual(add_todo([], 'ship demo'), ['ship demo'])",
                "",
                "",
                "if __name__ == '__main__':",
                "    unittest.main()",
                "",
            ]
        ),
        encoding="utf-8",
    )
    cli.run(["git", "add", ".gitignore", "src", "tests"], cwd=project)
    cli.run(["git", "commit", "-m", "init"], cwd=project)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mmux-alpha-demo-") as tmp:
        project = Path(tmp) / "demo"
        project.mkdir()
        init_repo(project)
        cli.ensure_layout(project)

        task_id = cli.enqueue_task(project, "Resolve TODO in src/todo_core.py", resource="src")
        original_driver = cli.invoke_agent_adapter
        original_reviewer = cli.invoke_reviewer_adapter
        original_planner = cli.invoke_planner_adapter
        original_plan_reviewer = cli.invoke_plan_reviewer_adapter
        original_summarizer = cli.invoke_summarizer_adapter
        driver_attempts = 0
        plan_attempts = 0
        plan_text_seen: list[str] = []
        plan_review_decisions: list[str] = []
        summaries_seen: list[str] = []

        def fake_planner(_project, _worktree, _agent, _task, _generation, _resource, **_kwargs):
            nonlocal plan_attempts
            plan_attempts += 1
            # Emit the JSON {read, plan, risks} contract the deterministic plan
            # gate now expects. read must cite a real path under the project
            # (src/todo_core.py exists in the demo repo) and plan must be
            # non-empty, otherwise the gate rejects before the plan reviewer.
            plan = (
                '{\n'
                '  "read": ["src/todo_core.py", "tests/test_todo_core.py"],\n'
                '  "plan": ["trim whitespace in normalize_title", "reject empty titles after trim"],\n'
                '  "risks": ["none (resource locked to src/)"]\n'
                '}\n'
                "MMUX_PLAN PROCEED"
            )
            return cli.PlanResult("proceed", ".mmux/runs/demo-plan.log", plan, "", True)

        def fake_plan_reviewer(_project, _worktree, _agent, _task, _generation, _resource, plan_text, **_kwargs):
            plan_text_seen.append(plan_text)
            decision = "approve" if "reject empty" in plan_text else "request_changes"
            plan_review_decisions.append(decision)
            if decision == "approve":
                return cli.ReviewResult("approve", ".mmux/runs/demo-plan-review.log", "plan ok", True)
            return cli.ReviewResult(
                "request_changes",
                ".mmux/runs/demo-plan-review.log",
                "missing rejection of empty titles",
                True,
            )

        def fake_summarizer(_project, _worktree, _agent, _task, context, **_kwargs):
            summary = (
                "- normalize_title now trims whitespace and rejects empty titles\n"
                "- existing unittest still passes; no new tests added\n"
                "- reviewer caught the missing empty-title check on first attempt\n"
                "- next time: include an empty-string test in the plan"
            )
            summaries_seen.append(summary)
            return cli.ActSummaryResult(summary, ".mmux/runs/demo-summary.log", "ok", True)

        def fake_driver(_project, execution_root, _agent, _task, _generation, _resource, **_kwargs):
            nonlocal driver_attempts
            driver_attempts += 1
            if driver_attempts == 1:
                (execution_root / "src" / "todo_core.py").write_text(
                    "\n".join(
                        [
                            "def normalize_title(title: str) -> str:",
                            "    return title.strip()",
                            "",
                            "",
                            "def add_todo(items: list[str], title: str) -> list[str]:",
                            "    return [*items, normalize_title(title)]",
                            "",
                        ]
                    ),
                    encoding="utf-8",
                )
                return cli.AdapterResult(True, 0, ".mmux/runs/demo-driver.log", "driver wrote trim-only diff")
            (execution_root / "src" / "todo_core.py").write_text(
                "\n".join(
                    [
                        "def normalize_title(title: str) -> str:",
                        "    normalized = title.strip()",
                        "    if not normalized:",
                        "        raise ValueError('todo title cannot be empty')",
                        "    return normalized",
                        "",
                        "",
                        "def add_todo(items: list[str], title: str) -> list[str]:",
                        "    return [*items, normalize_title(title)]",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            return cli.AdapterResult(True, 0, ".mmux/runs/demo-driver.log", "driver fixed reviewer finding")

        def fake_reviewer(_project, _worktree, _agent, _task, _generation, _resource, _changed_files, **_kwargs):
            source = (_worktree / "src" / "todo_core.py").read_text(encoding="utf-8")
            if "ValueError" not in source:
                return cli.ReviewResult(
                    "request_changes",
                    ".mmux/runs/demo-review.log",
                    "empty titles still accepted after trimming",
                    True,
                )
            return cli.ReviewResult("approve", ".mmux/runs/demo-review.log", "review approved", True)

        cli.invoke_agent_adapter = fake_driver
        cli.invoke_reviewer_adapter = fake_reviewer
        cli.invoke_planner_adapter = fake_planner
        cli.invoke_plan_reviewer_adapter = fake_plan_reviewer
        cli.invoke_summarizer_adapter = fake_summarizer
        try:
            print("mmux alpha deterministic loop demo (PDCA pipeline)")
            print("project: <temporary demo repository>")
            print(f"task: #{task_id} Resolve TODO in src/todo_core.py")
            print()

            driver = cli.acquire_role(project, "driver", "codex", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.execute_driver_task(project, "codex", driver.generation)
            task = cli.get_task(project, task_id)
            print(f"plan + plan-review + drive  codex  -> {task.status if task else 'missing'}  (trim-only diff)")
            if plan_review_decisions:
                print(f"                                    plan_review_decisions={plan_review_decisions}")

            reviewer = cli.acquire_role(project, "reviewer", "claude", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.execute_reviewer_task(project, "claude", reviewer.generation)
            task = cli.get_task(project, task_id)
            print(f"reviewer                    claude -> {task.status if task else 'missing'}  (request changes)")
            print("                                    reason: empty titles still accepted")

            driver = cli.acquire_role(project, "driver", "codex", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.execute_driver_task(project, "codex", driver.generation)
            task = cli.get_task(project, task_id)
            print(f"plan + plan-review + drive  codex  -> {task.status if task else 'missing'}  (reject blank titles)")

            reviewer = cli.acquire_role(project, "reviewer", "claude", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.execute_reviewer_task(project, "claude", reviewer.generation)
            task = cli.get_task(project, task_id)
            print(f"reviewer                    claude -> {task.status if task else 'missing'}  (approve)")

            tester = cli.acquire_role(project, "tester", "claude", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.execute_tester_task(project, "claude", tester.generation)
            task = cli.get_task(project, task_id)
            print(f"tester + summarize          claude -> {task.status if task else 'missing'}  (tests passed, patch applied, act_summary captured)")

            final_source = (project / "src" / "todo_core.py").read_text(encoding="utf-8")
            print()
            print(f"final task status:           {task.status if task else 'missing'}")
            print(f"plan_attempts:               {plan_attempts}")
            print(f"plan_review_decisions:       {plan_review_decisions}")
            print(f"driver_attempts:             {driver_attempts}")
            print(f"contains ValueError:         {'ValueError' in final_source}")
            if task and task.payload.get("act_summary"):
                print()
                print("act_summary recorded in task payload:")
                for line in str(task.payload["act_summary"]).splitlines():
                    print(f"  {line}")

            original_reflection = cli.invoke_reflection_adapter

            def fake_reflection(_project, _agent, _completions, **_kwargs):
                proposals = (
                    cli.ReflectionProposal(
                        title="add empty-string unit test",
                        resource="tests/test_todo_core.py",
                        evidence="task #1",
                    ),
                    cli.ReflectionProposal(
                        title="audit normalize_title for unicode",
                        resource=".",
                        evidence="",
                    ),
                )
                return cli.ReflectionResult(proposals, ".mmux/runs/demo-reflect.log", "ok", True)

            cli.invoke_reflection_adapter = fake_reflection
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    reflection = cli.perform_reflection(project, "claude")
            finally:
                cli.invoke_reflection_adapter = original_reflection

            print()
            print("reflection over completed tasks:")
            print(f"  sources:  {[f'#{tid}' for tid in reflection.source_task_ids]}")
            print(f"  promoted: {[f'#{tid}' for tid in reflection.promoted_ids]}  (evidence cited)")
            print(f"  proposed: {[f'#{tid}' for tid in reflection.proposed_ids]}  (need human review)")

            for tid in (*reflection.promoted_ids, *reflection.proposed_ids):
                proposal_task = cli.get_task(project, tid)
                if proposal_task is None:
                    continue
                print(f"  task #{tid} [{proposal_task.status}]: {proposal_task.title}")
                evidence = proposal_task.payload.get("reflection_evidence") or "<none>"
                print(f"           evidence: {evidence}")

            # Guardrail: the demo is not part of the unittest suite, so assert
            # the expected end state here. A pipeline change that breaks the
            # happy path (e.g. a new gate the stubs do not satisfy) then makes
            # `python scripts/demo_alpha.py` exit non-zero instead of silently
            # printing a wrong trace.
            if task is None or task.status != "completed":
                raise SystemExit(
                    f"demo regression: expected task status 'completed', got "
                    f"{task.status if task else 'missing'}"
                )
            if not reflection.promoted_ids or not reflection.proposed_ids:
                raise SystemExit(
                    "demo regression: expected reflection to both promote and propose tasks"
                )
        finally:
            cli.invoke_agent_adapter = original_driver
            cli.invoke_reviewer_adapter = original_reviewer
            cli.invoke_planner_adapter = original_planner
            cli.invoke_plan_reviewer_adapter = original_plan_reviewer
            cli.invoke_summarizer_adapter = original_summarizer

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
