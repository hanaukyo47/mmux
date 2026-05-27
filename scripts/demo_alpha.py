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
    (project / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    cli.run(["git", "add", ".gitignore", "src/app.py"], cwd=project)
    cli.run(["git", "commit", "-m", "init"], cwd=project)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mmux-alpha-demo-") as tmp:
        project = Path(tmp) / "demo"
        project.mkdir()
        init_repo(project)
        cli.ensure_layout(project)

        task_id = cli.enqueue_task(project, "Change a small Python value", resource="src")
        original_driver = cli.invoke_agent_adapter
        original_reviewer = cli.invoke_reviewer_adapter

        def fake_driver(_project, execution_root, _agent, _task, _generation, _resource, **_kwargs):
            (execution_root / "src" / "app.py").write_text("value = 7\n", encoding="utf-8")
            return cli.AdapterResult(True, 0, ".mmux/runs/demo-driver.log", "driver wrote a scoped diff")

        def fake_reviewer(_project, _worktree, _agent, _task, _generation, _resource, _changed_files, **_kwargs):
            return cli.ReviewResult("approve", ".mmux/runs/demo-review.log", "review approved", True)

        cli.invoke_agent_adapter = fake_driver
        cli.invoke_reviewer_adapter = fake_reviewer
        try:
            print("mmux alpha deterministic loop demo")
            print(f"project: {project}")
            print(f"task: #{task_id} Change a small Python value")
            print()

            driver = cli.acquire_role(project, "driver", "codex", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                driver_message = cli.execute_driver_task(project, "codex", driver.generation)
            print(f"driver   codex  -> {driver_message}")

            reviewer = cli.acquire_role(project, "reviewer", "claude", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                review_message = cli.execute_reviewer_task(project, "claude", reviewer.generation)
            print(f"reviewer claude -> {review_message}")

            tester = cli.acquire_role(project, "tester", "claude", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                test_message = cli.execute_tester_task(project, "claude", tester.generation)
            print(f"tester   claude -> {test_message}")

            task = cli.get_task(project, task_id)
            final_value = (project / "src" / "app.py").read_text(encoding="utf-8").strip()
            print()
            print(f"final task status: {task.status if task else 'missing'}")
            print(f"main worktree src/app.py: {final_value}")
        finally:
            cli.invoke_agent_adapter = original_driver
            cli.invoke_reviewer_adapter = original_reviewer

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
