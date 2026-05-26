import contextlib
import datetime as dt
import io
import sys
import tempfile
import unittest
from pathlib import Path

import mmux.cli as cli
from mmux.cli import (
    acquire_role,
    acquire_resource_lock,
    build_agent_command,
    check_diff_policy,
    claim_next_task,
    create_task_worktree,
    database,
    enqueue_task,
    ensure_layout,
    execute_driver_task,
    export_worktree_patch,
    format_utc,
    apply_worktree_patch,
    list_resource_locks,
    list_worker_heartbeats,
    release_role,
    release_resource_lock,
    resources_overlap,
    run,
    session_name,
    state_path,
    stream_agent_command,
    supervisor_role_plan,
    list_tasks,
    update_worker_heartbeat,
    utc_now_dt,
)


def init_git_project(project: Path) -> None:
    run(["git", "init"], cwd=project)
    run(["git", "config", "user.email", "test@example.com"], cwd=project)
    run(["git", "config", "user.name", "Test User"], cwd=project)
    (project / ".gitignore").write_text(".mmux/\n", encoding="utf-8")
    (project / "src").mkdir()
    (project / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
    (project / "README.md").write_text("# test\n", encoding="utf-8")
    run(["git", "add", ".gitignore", "src/app.py", "README.md"], cwd=project)
    run(["git", "commit", "-m", "init"], cwd=project)


def claim_driver_task(project: Path, *, resource: str = "."):
    enqueue_task(project, "driver task", resource=resource)
    lease = acquire_role(project, "driver", "codex", ttl_seconds=60)
    task = claim_next_task(project, "codex", "driver", lease.generation)
    assert task is not None
    return task


class StateTests(unittest.TestCase):
    def test_ensure_layout_creates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project, "test task")

            self.assertTrue((project / ".mmux" / "config.json").exists())
            self.assertTrue(state_path(project).exists())

    def test_session_name_is_stable(self) -> None:
        project = Path("/tmp/example-project").resolve()
        self.assertEqual(session_name(project), session_name(project))
        self.assertTrue(session_name(project).startswith("mmux-example-project-"))

    def test_acquire_role_blocks_same_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)

            first = acquire_role(project, "driver", "codex", ttl_seconds=60)
            second = acquire_role(project, "driver", "claude", ttl_seconds=60)

            self.assertTrue(first.ok)
            self.assertFalse(second.ok)
            self.assertEqual(second.status, "conflict")
            self.assertEqual(second.holder, "codex")

    def test_release_role_allows_new_holder_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)

            first = acquire_role(project, "reviewer", "codex", ttl_seconds=60)
            released = release_role(project, "reviewer", holder="codex", generation=first.generation)
            second = acquire_role(project, "reviewer", "claude", ttl_seconds=60)

            self.assertTrue(released.ok)
            self.assertTrue(second.ok)
            self.assertEqual(second.holder, "claude")
            self.assertEqual(second.generation, first.generation + 1)

    def test_expired_role_can_be_reacquired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)
            first = acquire_role(project, "tester", "codex", ttl_seconds=60)
            expired_at = format_utc(utc_now_dt() - dt.timedelta(seconds=1))
            with database(project) as db:
                db.execute(
                    "update role_leases set lease_until = ? where role = ?",
                    (expired_at, "tester"),
                )

            second = acquire_role(project, "tester", "claude", ttl_seconds=60)

            self.assertTrue(second.ok)
            self.assertEqual(second.holder, "claude")
            self.assertEqual(second.generation, first.generation + 1)

    def test_worker_heartbeat_records_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)

            update_worker_heartbeat(project, "codex", "parked")
            heartbeats = list_worker_heartbeats(project)

            self.assertEqual(len(heartbeats), 1)
            agent, _pid, status, role, generation, _updated_at = heartbeats[0]
            self.assertEqual(agent, "codex")
            self.assertEqual(status, "parked")
            self.assertIsNone(role)
            self.assertIsNone(generation)

    def test_supervisor_plan_rotates_agent_roles(self) -> None:
        first = dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
        second = dt.datetime.fromtimestamp(5 * 60, tz=dt.timezone.utc)

        self.assertEqual(supervisor_role_plan(first), (("scout", "codex"), ("reviewer", "claude")))
        self.assertEqual(supervisor_role_plan(second), (("driver", "claude"), ("tester", "codex")))

    def test_resource_overlap_detects_directory_prefixes(self) -> None:
        self.assertTrue(resources_overlap(".", "src/mmux/cli.py"))
        self.assertTrue(resources_overlap("src", "src/mmux/cli.py"))
        self.assertTrue(resources_overlap("src/mmux/cli.py", "src"))
        self.assertFalse(resources_overlap("src/mmux/cli.py", "tests/test_state.py"))

    def test_resource_lock_blocks_overlapping_holder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "src" / "mmux").mkdir(parents=True)
            (project / "src" / "mmux" / "cli.py").write_text("", encoding="utf-8")
            ensure_layout(project)

            first = acquire_resource_lock(project, "src", "codex", ttl_seconds=60)
            second = acquire_resource_lock(project, "src/mmux/cli.py", "claude", ttl_seconds=60)

            self.assertTrue(first.ok)
            self.assertFalse(second.ok)
            self.assertEqual(second.status, "conflict")
            self.assertEqual(second.conflict_resource, "src")

    def test_resource_lock_requires_matching_role_generation_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)
            driver = acquire_role(project, "driver", "codex", ttl_seconds=60)

            stale = acquire_resource_lock(
                project,
                ".",
                "codex",
                ttl_seconds=60,
                role="driver",
                role_generation=driver.generation + 1,
            )
            current = acquire_resource_lock(
                project,
                ".",
                "codex",
                ttl_seconds=60,
                role="driver",
                role_generation=driver.generation,
            )

            self.assertFalse(stale.ok)
            self.assertEqual(stale.status, "stale_role")
            self.assertTrue(current.ok)

    def test_release_resource_lock_allows_reacquire(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)

            first = acquire_resource_lock(project, ".", "codex", ttl_seconds=60)
            released = release_resource_lock(project, ".", holder="codex")
            second = acquire_resource_lock(project, ".", "claude", ttl_seconds=60)
            locks = list_resource_locks(project)

            self.assertTrue(first.ok)
            self.assertTrue(released.ok)
            self.assertTrue(second.ok)
            self.assertEqual(locks[-1][1], "claude")

    def test_claim_next_task_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)
            task_id = enqueue_task(project, "write tests", resource=".")
            driver = acquire_role(project, "driver", "codex", ttl_seconds=60)

            first = claim_next_task(project, "codex", "driver", driver.generation)
            second = claim_next_task(project, "codex", "driver", driver.generation)

            self.assertIsNotNone(first)
            assert first is not None
            self.assertEqual(first.id, task_id)
            self.assertIsNone(second)

    def test_build_agent_commands_use_noninteractive_modes(self) -> None:
        project = Path("/tmp/example-project").resolve()
        output = project / ".mmux" / "runs" / "out.txt"

        codex = build_agent_command("codex", project, "do work", output)
        claude = build_agent_command("claude", project, "do work", output)

        self.assertEqual(codex[:2], ["codex", "exec"])
        self.assertIn("--ask-for-approval", codex)
        self.assertIn("never", codex)
        self.assertEqual(claude[:2], ["claude", "-p"])
        self.assertIn("--permission-mode", claude)

    def test_stream_agent_command_writes_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)
            log_file = project / ".mmux" / "runs" / "stream.log"

            with contextlib.redirect_stdout(io.StringIO()):
                result = stream_agent_command(
                    project,
                    [sys.executable, "-c", "print('adapter ok')"],
                    log_file,
                    timeout_seconds=5,
                )

            self.assertTrue(result.ok)
            self.assertEqual(result.returncode, 0)
            self.assertIn("adapter ok", log_file.read_text(encoding="utf-8"))

    def test_create_task_worktree_checks_out_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task = claim_driver_task(project)

            worktree = create_task_worktree(project, task, "codex")

            self.assertTrue((worktree / "src" / "app.py").exists())
            self.assertEqual((worktree / "src" / "app.py").read_text(encoding="utf-8"), "value = 1\n")

    def test_diff_policy_accepts_changes_inside_resource(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task_id = enqueue_task(project, "change src", resource="src")
            lease = acquire_role(project, "driver", "codex", ttl_seconds=60)
            task = claim_next_task(project, "codex", "driver", lease.generation)
            assert task is not None
            self.assertEqual(task.id, task_id)
            worktree = create_task_worktree(project, task, "codex")
            (worktree / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")

            policy = check_diff_policy(project, worktree, "src")

            self.assertTrue(policy.ok)
            self.assertEqual(policy.status, "ok")
            self.assertEqual(policy.changed_files, ("src/app.py",))

    def test_diff_policy_rejects_changes_outside_resource(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task = claim_driver_task(project, resource="src")
            worktree = create_task_worktree(project, task, "codex")
            (worktree / "README.md").write_text("# changed\n", encoding="utf-8")

            policy = check_diff_policy(project, worktree, "src")

            self.assertFalse(policy.ok)
            self.assertEqual(policy.status, "resource_violation")

    def test_diff_policy_rejects_protected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task = claim_driver_task(project)
            worktree = create_task_worktree(project, task, "codex")
            (worktree / ".env").write_text("SECRET=1\n", encoding="utf-8")

            policy = check_diff_policy(project, worktree, ".")

            self.assertFalse(policy.ok)
            self.assertEqual(policy.status, "protected_violation")

    def test_diff_policy_marks_no_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task = claim_driver_task(project)
            worktree = create_task_worktree(project, task, "codex")

            policy = check_diff_policy(project, worktree, ".")

            self.assertTrue(policy.ok)
            self.assertEqual(policy.status, "no_change")
            self.assertEqual(policy.changed_files, ())

    def test_apply_worktree_patch_updates_main_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task = claim_driver_task(project, resource="src")
            worktree = create_task_worktree(project, task, "codex")
            (worktree / "src" / "app.py").write_text("value = 3\n", encoding="utf-8")
            policy = check_diff_policy(project, worktree, "src")
            self.assertTrue(policy.ok)

            patch = export_worktree_patch(worktree)
            apply_worktree_patch(project, patch)

            self.assertEqual((project / "src" / "app.py").read_text(encoding="utf-8"), "value = 3\n")

    def test_execute_driver_task_uses_worktree_and_applies_policy_accepted_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            enqueue_task(project, "change src", resource="src")
            lease = acquire_role(project, "driver", "codex", ttl_seconds=60)

            original = cli.invoke_agent_adapter

            def fake_adapter(_project, execution_root, _agent, task, _generation, _resource):
                self.assertEqual(task.title, "change src")
                (execution_root / "src" / "app.py").write_text("value = 4\n", encoding="utf-8")
                return cli.AdapterResult(True, 0, ".mmux/runs/fake.log", "ok")

            cli.invoke_agent_adapter = fake_adapter
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    message = execute_driver_task(project, "codex", lease.generation)
            finally:
                cli.invoke_agent_adapter = original

            self.assertIn("completed", message)
            self.assertEqual((project / "src" / "app.py").read_text(encoding="utf-8"), "value = 4\n")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "completed")
            self.assertEqual(tasks[-1].payload["diff_policy"], "ok")
            self.assertEqual(tasks[-1].payload["patch_applied"], True)


if __name__ == "__main__":
    unittest.main()
