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
        driver_attempts = 0

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
        try:
            print("mmux alpha deterministic loop demo")
            print("project: <temporary demo repository>")
            print(f"task: #{task_id} Resolve TODO in src/todo_core.py")
            print()

            driver = cli.acquire_role(project, "driver", "codex", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.execute_driver_task(project, "codex", driver.generation)
            task = cli.get_task(project, task_id)
            print(f"driver   codex  -> {task.status if task else 'missing'}  (trim-only diff)")

            reviewer = cli.acquire_role(project, "reviewer", "claude", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.execute_reviewer_task(project, "claude", reviewer.generation)
            task = cli.get_task(project, task_id)
            print(f"reviewer claude -> {task.status if task else 'missing'}  (request changes)")
            print("                  reason: empty titles still accepted")

            driver = cli.acquire_role(project, "driver", "codex", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.execute_driver_task(project, "codex", driver.generation)
            task = cli.get_task(project, task_id)
            print(f"driver   codex  -> {task.status if task else 'missing'}  (reject blank titles)")

            reviewer = cli.acquire_role(project, "reviewer", "claude", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.execute_reviewer_task(project, "claude", reviewer.generation)
            task = cli.get_task(project, task_id)
            print(f"reviewer claude -> {task.status if task else 'missing'}  (approve)")

            tester = cli.acquire_role(project, "tester", "claude", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                cli.execute_tester_task(project, "claude", tester.generation)
            task = cli.get_task(project, task_id)
            print(f"tester   claude -> {task.status if task else 'missing'}  (tests passed, patch applied)")

            final_source = (project / "src" / "todo_core.py").read_text(encoding="utf-8")
            print()
            print(f"final task status: {task.status if task else 'missing'}")
            print("main worktree src/todo_core.py: rejects blank titles")
            print(f"contains ValueError: {'ValueError' in final_source}")
        finally:
            cli.invoke_agent_adapter = original_driver
            cli.invoke_reviewer_adapter = original_reviewer

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
