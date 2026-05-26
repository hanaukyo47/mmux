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
    build_tester_checks,
    build_agent_command,
    check_diff_policy,
    claim_next_task,
    cleanup_runtime_state,
    create_task_worktree,
    database,
    enqueue_task,
    ensure_layout,
    execute_driver_task,
    execute_tester_task,
    export_worktree_patch,
    format_task_counts,
    format_task_delta,
    format_utc,
    apply_worktree_patch,
    inspect_project,
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
    supervisor_role_plan_for_project,
    task_status_counts,
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

    def test_supervisor_project_plan_prioritizes_executable_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)
            first = dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
            second = dt.datetime.fromtimestamp(5 * 60, tz=dt.timezone.utc)

            self.assertEqual(supervisor_role_plan_for_project(project, first), supervisor_role_plan(first))

            enqueue_task(project, "pending task", resource=".")
            self.assertEqual(supervisor_role_plan_for_project(project, first), (("driver", "codex"), ("tester", "claude")))
            self.assertEqual(supervisor_role_plan_for_project(project, second), (("driver", "claude"), ("tester", "codex")))

            with database(project) as db:
                db.execute("update tasks set status = ? where id = ?", ("awaiting_test", 1))
            self.assertEqual(supervisor_role_plan_for_project(project, first), (("tester", "codex"), ("driver", "claude")))

    def test_cleanup_runtime_state_releases_active_runtime_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)
            acquire_role(project, "driver", "codex", ttl_seconds=60)
            acquire_resource_lock(project, ".", "codex", ttl_seconds=60)
            update_worker_heartbeat(project, "codex", "leased", role="driver", generation=1)

            cleanup_runtime_state(project)

            with database(project) as db:
                role_status = db.execute("select status from role_leases where role = ?", ("driver",)).fetchone()[0]
                lock_status = db.execute("select status from resource_locks where resource = ?", (".",)).fetchone()[0]
                worker = db.execute(
                    "select status, role, generation from worker_heartbeats where agent = ?",
                    ("codex",),
                ).fetchone()
            self.assertEqual(role_status, "released")
            self.assertEqual(lock_status, "released")
            self.assertEqual(worker, ("stopped", None, None))

    def test_cleanup_runtime_state_requeues_inflight_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)
            running_id = enqueue_task(project, "driver task", resource=".")
            test_id = enqueue_task(project, "tester task", resource=".")
            with database(project) as db:
                db.execute(
                    """
                    update tasks
                    set status = ?, claimed_by = ?, claimed_role = ?, claimed_generation = ?
                    where id = ?
                    """,
                    ("running", "codex", "driver", 1, running_id),
                )
                db.execute(
                    """
                    update tasks
                    set status = ?, claimed_by = ?, claimed_role = ?, claimed_generation = ?
                    where id = ?
                    """,
                    ("running_test", "claude", "tester", 2, test_id),
                )

            cleanup_runtime_state(project)
            tasks = {task.id: task for task in list_tasks(project)}

            self.assertEqual(tasks[running_id].status, "pending")
            self.assertEqual(tasks[running_id].claimed_by, "")
            self.assertEqual(tasks[test_id].status, "awaiting_test")
            self.assertEqual(tasks[test_id].claimed_by, "")

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

    def test_task_status_summary_helpers_are_stable(self) -> None:
        before = {"pending": 2, "completed": 1}
        after = {"pending": 1, "completed": 2, "failed": 1}

        self.assertEqual(format_task_counts(before), "pending=2, completed=1")
        self.assertEqual(format_task_delta(before, after), "pending=-1, completed=+1, failed=+1")

    def test_inspect_project_detects_common_ecosystems(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")
            (project / "package.json").write_text('{"scripts": {"test": "vitest"}}\n', encoding="utf-8")
            (project / "Cargo.toml").write_text("[package]\nname = \"sample\"\n", encoding="utf-8")
            (project / "go.mod").write_text("module example.com/sample\n", encoding="utf-8")
            (project / "build.gradle").write_text("plugins { id 'java' }\n", encoding="utf-8")
            (project / "script.sh").write_text("echo ok\n", encoding="utf-8")
            (project / "data.json").write_text("{}\n", encoding="utf-8")
            (project / "tests").mkdir()
            (project / "tests" / "test_sample.py").write_text("def test_placeholder():\n    pass\n", encoding="utf-8")

            profile = inspect_project(project)
            active_names = {check.name for check in profile.active_checks}
            suggested_names = {check.name for check in profile.suggested_checks}

            self.assertIn("python", profile.ecosystems)
            self.assertIn("node", profile.ecosystems)
            self.assertIn("rust", profile.ecosystems)
            self.assertIn("go", profile.ecosystems)
            self.assertIn("gradle", profile.ecosystems)
            self.assertIn("shell", profile.languages)
            self.assertIn("json", profile.languages)
            self.assertIn("py-compile", active_names)
            self.assertIn("unittest", active_names)
            self.assertIn("shell-check", active_names)
            self.assertIn("json-syntax", active_names)
            self.assertIn("diff-check", active_names)
            self.assertIn("cargo-test", suggested_names)
            self.assertIn("go-test", suggested_names)
            self.assertIn("gradle-test", suggested_names)

    def test_build_tester_checks_adds_changed_file_syntax_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "app.py").write_text("value = 1\n", encoding="utf-8")
            (project / "script.sh").write_text("echo ok\n", encoding="utf-8")
            (project / "package.json").write_text("{}\n", encoding="utf-8")
            (project / "tests").mkdir()
            (project / "tests" / "test_app.py").write_text("import unittest\n", encoding="utf-8")

            checks = build_tester_checks(project, ["app.py", "script.sh", "package.json"])
            names = [check.name for check in checks]

            self.assertEqual(names[0], "diff-check")
            self.assertIn("py-compile", names)
            self.assertIn("shell-check:script.sh", names)
            self.assertIn("json-syntax:package.json", names)
            self.assertIn("unittest", names)

    def test_cmd_inspect_prints_json_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")

            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli.main(["inspect", str(project), "--json"])

            self.assertEqual(code, 0)
            decoded = cli.json.loads(output.getvalue())
            self.assertIn("python", decoded["ecosystems"])

    def test_cmd_run_records_timed_window_without_real_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            calls = []
            original_start = cli.start_tmux_session
            original_stop = cli.stop_tmux_session

            def fake_start(start_project, *, execute_agents=False, **kwargs):
                calls.append(("start", start_project, execute_agents, kwargs))
                return True

            def fake_stop(stop_project):
                calls.append(("stop", stop_project))
                cleanup_runtime_state(stop_project)
                return True

            cli.start_tmux_session = fake_start
            cli.stop_tmux_session = fake_stop
            try:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = cli.main(
                        [
                            "run",
                            str(project),
                            "--seconds",
                            "1",
                            "--checkpoint-seconds",
                            "1",
                            "--task",
                            "timed task",
                        ]
                    )
            finally:
                cli.start_tmux_session = original_start
                cli.stop_tmux_session = original_stop

            self.assertEqual(code, 0)
            self.assertEqual(calls[0][0:3], ("start", project.resolve(), False))
            self.assertIn("run_deadline", calls[0][3])
            self.assertEqual(calls[-1], ("stop", project.resolve()))
            self.assertIn("run summary:", output.getvalue())
            self.assertEqual(task_status_counts(project.resolve()), {"pending": 1})
            with database(project.resolve()) as db:
                events = [
                    row[0]
                    for row in db.execute(
                        "select kind from events where kind in (?, ?) order by id",
                        ("run_started", "run_finished"),
                    ).fetchall()
                ]
            self.assertEqual(events, ["run_started", "run_finished"])

    def test_cmd_run_adds_default_task_when_queue_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            original_start = cli.start_tmux_session
            original_stop = cli.stop_tmux_session

            def fake_start(_project, *, execute_agents=False, **kwargs):
                return True

            def fake_stop(stop_project):
                cleanup_runtime_state(stop_project)
                return True

            cli.start_tmux_session = fake_start
            cli.stop_tmux_session = fake_stop
            try:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = cli.main(["run", str(project), "--seconds", "1", "--checkpoint-seconds", "1"])
            finally:
                cli.start_tmux_session = original_start
                cli.stop_tmux_session = original_stop

            self.assertEqual(code, 0)
            self.assertIn("default task: added #1", output.getvalue())
            tasks = list_tasks(project.resolve())
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].status, "pending")
            self.assertEqual(tasks[0].payload["origin"], "auto_default")
            self.assertIn("Find one small, testable improvement", tasks[0].title)
            with database(project.resolve()) as db:
                event = db.execute("select kind from events where kind = ?", ("default_task_added",)).fetchone()
            self.assertEqual(event[0], "default_task_added")

    def test_cmd_run_can_disable_default_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            original_start = cli.start_tmux_session
            original_stop = cli.stop_tmux_session

            def fake_start(_project, *, execute_agents=False, **kwargs):
                return True

            def fake_stop(stop_project):
                cleanup_runtime_state(stop_project)
                return True

            cli.start_tmux_session = fake_start
            cli.stop_tmux_session = fake_stop
            try:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    code = cli.main(
                        [
                            "run",
                            str(project),
                            "--seconds",
                            "1",
                            "--checkpoint-seconds",
                            "1",
                            "--no-default-task",
                        ]
                    )
            finally:
                cli.start_tmux_session = original_start
                cli.stop_tmux_session = original_stop

            self.assertEqual(code, 0)
            self.assertIn("default task: disabled", output.getvalue())
            self.assertEqual(list_tasks(project.resolve()), [])

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

    def test_stream_agent_command_times_out_when_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)
            log_file = project / ".mmux" / "runs" / "silent.log"

            with contextlib.redirect_stdout(io.StringIO()):
                result = stream_agent_command(
                    project,
                    [sys.executable, "-c", "import time; time.sleep(5)"],
                    log_file,
                    timeout_seconds=10,
                    no_output_timeout_seconds=1,
                )

            self.assertFalse(result.ok)
            self.assertEqual(result.returncode, 124)
            self.assertIn("produced no output", result.message)
            self.assertIn("produced no output", log_file.read_text(encoding="utf-8"))

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

    def test_execute_driver_task_moves_accepted_patch_to_awaiting_test(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            enqueue_task(project, "change src", resource="src")
            lease = acquire_role(project, "driver", "codex", ttl_seconds=60)

            original = cli.invoke_agent_adapter

            def fake_adapter(_project, execution_root, _agent, task, _generation, _resource, **kwargs):
                self.assertEqual(task.title, "change src")
                (execution_root / "src" / "app.py").write_text("value = 4\n", encoding="utf-8")
                return cli.AdapterResult(True, 0, ".mmux/runs/fake.log", "ok")

            cli.invoke_agent_adapter = fake_adapter
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    message = execute_driver_task(project, "codex", lease.generation)
            finally:
                cli.invoke_agent_adapter = original

            self.assertIn("awaiting_test", message)
            self.assertEqual((project / "src" / "app.py").read_text(encoding="utf-8"), "value = 1\n")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "awaiting_test")
            self.assertEqual(tasks[-1].payload["diff_policy"], "ok")
            self.assertIn("worktree", tasks[-1].payload)

    def test_execute_tester_task_applies_awaiting_patch_after_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            enqueue_task(project, "change src", resource="src")
            driver = acquire_role(project, "driver", "codex", ttl_seconds=60)

            original = cli.invoke_agent_adapter

            def fake_adapter(_project, execution_root, _agent, _task, _generation, _resource, **kwargs):
                (execution_root / "src" / "app.py").write_text("value = 4\n", encoding="utf-8")
                return cli.AdapterResult(True, 0, ".mmux/runs/fake-driver.log", "ok")

            cli.invoke_agent_adapter = fake_adapter
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    execute_driver_task(project, "codex", driver.generation)
            finally:
                cli.invoke_agent_adapter = original

            tester = acquire_role(project, "tester", "claude", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                message = execute_tester_task(project, "claude", tester.generation)

            self.assertIn("completed", message)
            self.assertEqual((project / "src" / "app.py").read_text(encoding="utf-8"), "value = 4\n")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "completed")
            self.assertEqual(tasks[-1].payload["patch_applied"], True)
            self.assertIn("tester_log", tasks[-1].payload)

    def test_execute_tester_task_fails_invalid_python_without_applying_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            enqueue_task(project, "break src", resource="src")
            driver = acquire_role(project, "driver", "codex", ttl_seconds=60)

            original = cli.invoke_agent_adapter

            def fake_adapter(_project, execution_root, _agent, _task, _generation, _resource, **kwargs):
                (execution_root / "src" / "app.py").write_text("if True print('bad')\n", encoding="utf-8")
                return cli.AdapterResult(True, 0, ".mmux/runs/fake-driver.log", "ok")

            cli.invoke_agent_adapter = fake_adapter
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    execute_driver_task(project, "codex", driver.generation)
            finally:
                cli.invoke_agent_adapter = original

            tester = acquire_role(project, "tester", "claude", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                message = execute_tester_task(project, "claude", tester.generation)

            self.assertIn("failed", message)
            self.assertEqual((project / "src" / "app.py").read_text(encoding="utf-8"), "value = 1\n")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "failed")
            self.assertEqual(tasks[-1].payload["patch_applied"] if "patch_applied" in tasks[-1].payload else False, False)


if __name__ == "__main__":
    unittest.main()
