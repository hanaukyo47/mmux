import contextlib
import datetime as dt
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

import mmux.cli as cli
from mmux.cli import (
    acquire_role,
    acquire_resource_lock,
    build_tester_checks,
    build_agent_command,
    build_resident_command,
    build_resident_prompt,
    check_diff_policy,
    claim_next_task,
    cleanup_runtime_state,
    create_task_worktree,
    database,
    enqueue_task,
    ensure_layout,
    ensure_default_task,
    execute_driver_task,
    execute_reviewer_task,
    execute_tester_task,
    execute_worker_available_task,
    export_worktree_patch,
    format_resident_control_message,
    format_task_counts,
    format_task_delta,
    format_utc,
    get_task,
    apply_worktree_patch,
    discover_frontier_candidates,
    inspect_project,
    list_resource_locks,
    list_worker_heartbeats,
    mark_agent_cooldown,
    maybe_replenish_default_task,
    maybe_replenish_reflection_task,
    MIN_EXECUTION_BUDGET_SECONDS,
    module_command,
    parse_resident_protocol_line,
    parse_planner_decision,
    parse_reflection_tasks,
    parse_reviewer_decision,
    perform_reflection,
    evidence_is_concrete,
    recent_completions_with_summary,
    parse_tmux_version,
    process_resident_protocol_events,
    read_agent_brief,
    report_resident_protocol_event,
    resident_mode_enabled,
    resident_context_from_path,
    release_role,
    release_resource_lock,
    requeue_task,
    resources_overlap,
    run,
    scan_resident_agent_output,
    session_name,
    send_tmux_message,
    set_resident_mode,
    state_path,
    stream_agent_command,
    supervisor_role_plan,
    supervisor_role_plan_for_project,
    task_status_counts,
    todo_marker_in_line,
    version_at_least,
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


def set_task_status_for_test(
    project: Path,
    task_id: int,
    status: str,
    *,
    payload: Optional[dict[str, object]] = None,
    claimed_by: str = "",
    claimed_role: str = "",
    claimed_generation: int = 0,
) -> None:
    state = cli.task_state_for_status(status)
    with database(project) as db:
        cli.set_task_state_db(
            db,
            task_id,
            stage=str(state["stage"]),
            outcome=str(state["outcome"]),
            in_progress=bool(state["in_progress"]),
            check_step=str(state["check_step"]),
            payload=payload,
            claimed_by=claimed_by,
            claimed_role=claimed_role,
            claimed_generation=claimed_generation,
            claimed_at="now" if claimed_by else "",
        )


def insert_task_for_test(project: Path, title: str, status: str, payload: dict[str, object]) -> int:
    now = "now"
    state = cli.task_state_for_status(status)
    with database(project) as db:
        cursor = db.execute(
            """
            insert into tasks(title, status, stage, outcome, in_progress, check_step, payload, created_at, updated_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                status,
                state["stage"],
                state["outcome"],
                state["in_progress"],
                state["check_step"],
                cli.encode_payload(payload),
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)


def approve_review_task(project: Path, *, agent: str = "claude") -> str:
    reviewer = acquire_role(project, "reviewer", agent, ttl_seconds=60)
    original = cli.invoke_reviewer_adapter

    def fake_review(_project, _worktree, _agent, _task, _generation, _resource, _changed_files, **kwargs):
        return cli.ReviewResult("approve", ".mmux/runs/fake-review.log", "ok", True)

    cli.invoke_reviewer_adapter = fake_review
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return execute_reviewer_task(project, agent, reviewer.generation)
    finally:
        cli.invoke_reviewer_adapter = original


_FAKE_PLAN_JSON = '{"read": ["src/app.py"], "plan": ["update value in src/app.py"], "risks": ["none"]}'


def _fake_planner_proceed(_project, _worktree, _agent, _task, _generation, _resource, **kwargs):
    return cli.PlanResult(
        "proceed",
        ".mmux/runs/fake-plan.log",
        f"{_FAKE_PLAN_JSON}\nMMUX_PLAN PROCEED",
        "",
        True,
    )


def _fake_plan_reviewer_approve(_project, _worktree, _agent, _task, _generation, _resource, _plan_text, **kwargs):
    return cli.ReviewResult("approve", ".mmux/runs/fake-plan-review.log", "", True)


def _fake_summarizer_ok(_project, _worktree, _agent, _task, _context, **kwargs):
    return cli.ActSummaryResult(
        "- did the thing\n- tested it\n- nothing surprising\n- watch the next one",
        ".mmux/runs/fake-summary.log",
        "ok",
        True,
    )


@contextlib.contextmanager
def patched_planner(stub=_fake_planner_proceed, plan_reviewer_stub=_fake_plan_reviewer_approve):
    original_planner = cli.invoke_planner_adapter
    original_reviewer = cli.invoke_plan_reviewer_adapter
    cli.invoke_planner_adapter = stub
    cli.invoke_plan_reviewer_adapter = plan_reviewer_stub
    try:
        yield
    finally:
        cli.invoke_planner_adapter = original_planner
        cli.invoke_plan_reviewer_adapter = original_reviewer


@contextlib.contextmanager
def patched_summarizer(stub=_fake_summarizer_ok):
    original = cli.invoke_summarizer_adapter
    cli.invoke_summarizer_adapter = stub
    try:
        yield
    finally:
        cli.invoke_summarizer_adapter = original


def drive_task_to_review(project: Path, *, value: str = "value = 4\n", resource: str = "src") -> int:
    task_id = enqueue_task(project, "change src", resource=resource)
    driver = acquire_role(project, "driver", "codex", ttl_seconds=60)
    original = cli.invoke_agent_adapter

    def fake_adapter(_project, execution_root, _agent, _task, _generation, _resource, **kwargs):
        (execution_root / "src" / "app.py").write_text(value, encoding="utf-8")
        return cli.AdapterResult(True, 0, ".mmux/runs/fake-driver.log", "ok")

    cli.invoke_agent_adapter = fake_adapter
    try:
        with patched_planner(), contextlib.redirect_stdout(io.StringIO()):
            execute_driver_task(project, "codex", driver.generation)
    finally:
        cli.invoke_agent_adapter = original
    return task_id


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

    def test_renewing_same_role_does_not_shorten_existing_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)

            first = acquire_role(project, "driver", "codex", ttl_seconds=300)
            renewed = acquire_role(project, "driver", "codex", ttl_seconds=30, renew_if_same=True)

            self.assertTrue(renewed.ok)
            self.assertEqual(renewed.status, "renewed")
            self.assertEqual(renewed.generation, first.generation)
            self.assertGreaterEqual(cli.parse_utc(renewed.lease_until), cli.parse_utc(first.lease_until))

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

    def test_tmux_version_parsing_accepts_patch_suffixes(self) -> None:
        self.assertEqual(parse_tmux_version("tmux 3.6b"), (3, 6))
        self.assertEqual(parse_tmux_version("tmux 3.2a"), (3, 2))
        self.assertIsNone(parse_tmux_version("not tmux"))
        self.assertTrue(version_at_least((3, 2), (3, 0)))
        self.assertTrue(version_at_least((3, 0), (3, 0)))
        self.assertFalse(version_at_least((2, 9), (3, 0)))

    def test_todo_marker_matching_ignores_words_and_paths(self) -> None:
        self.assertEqual(todo_marker_in_line("# TODO: fix this"), "todo")
        self.assertEqual(todo_marker_in_line("/* FIXME handle this */"), "fixme")
        self.assertEqual(todo_marker_in_line("src/todo_core.py"), "")
        self.assertEqual(todo_marker_in_line("todoist integration"), "")
        self.assertEqual(todo_marker_in_line("# mmux example todo"), "")

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

            mark_agent_cooldown(project, "claude", "silent adapter", ttl_seconds=60)
            self.assertEqual(supervisor_role_plan_for_project(project, second), (("driver", "codex"), ("tester", "claude")))

            set_task_status_for_test(project, 1, "awaiting_test")
            self.assertEqual(supervisor_role_plan_for_project(project, first), (("tester", "claude"), ("driver", "codex")))

            set_task_status_for_test(project, 1, "awaiting_review", payload={"driver_agent": "codex"})
            self.assertEqual(supervisor_role_plan_for_project(project, first), (("reviewer", "claude"), ("driver", "codex")))

    def test_supervisor_project_plan_prioritizes_summary_backlog_when_queue_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)
            now = dt.datetime.fromtimestamp(0, tz=dt.timezone.utc)
            insert_task_for_test(project, "completed without summary", "completed", {"resource": "src"})

            self.assertEqual(supervisor_role_plan_for_project(project, now), (("summarizer", "codex"), ("scout", "claude")))

    def test_worker_scout_role_falls_back_to_frontier_when_model_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            (project / "src" / "app.py").write_text("value = 1\n# TODO: cover edge case\n", encoding="utf-8")
            ensure_layout(project)
            scout = acquire_role(project, "scout", "codex", ttl_seconds=60)

            def unavailable(_project, _agent, _profile, _files, **_kwargs):
                return cli.ReflectionResult((), ".mmux/runs/fake-scout.log", "model unavailable", False)

            original = cli.invoke_scout_adapter
            cli.invoke_scout_adapter = unavailable
            try:
                message = execute_worker_available_task(
                    project,
                    "codex",
                    [("scout", scout.generation, scout.lease_until)],
                    run_deadline="",
                    agent_timeout_seconds=60,
                    agent_no_output_seconds=30,
                    test_timeout_seconds=60,
                    shutdown_grace_seconds=15,
                )
            finally:
                cli.invoke_scout_adapter = original

            self.assertIsNotNone(message)
            assert message is not None
            self.assertIn("scout added frontier task", message)
            tasks = list_tasks(project)
            self.assertEqual(len(tasks), 1)
            self.assertIn("Resolve TODO", tasks[0].title)

    def test_scout_model_promotes_proposal_with_concrete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            scout = acquire_role(project, "scout", "codex", ttl_seconds=60)

            def proposes(_project, _agent, _profile, _files, **_kwargs):
                proposal = cli.ReflectionProposal(
                    title="Add tests for app.py",
                    resource="src",
                    evidence="src/app.py",
                )
                return cli.ReflectionResult((proposal,), ".mmux/runs/fake-scout.log", "ok", True)

            original = cli.invoke_scout_adapter
            cli.invoke_scout_adapter = proposes
            try:
                message = execute_worker_available_task(
                    project,
                    "codex",
                    [("scout", scout.generation, scout.lease_until)],
                    run_deadline="",
                    agent_timeout_seconds=60,
                    agent_no_output_seconds=30,
                    test_timeout_seconds=60,
                    shutdown_grace_seconds=15,
                )
            finally:
                cli.invoke_scout_adapter = original

            self.assertIsNotNone(message)
            assert message is not None
            self.assertIn("scout proposed 1 task", message)
            tasks = list_tasks(project)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].status, "pending")
            self.assertEqual(tasks[0].payload["kind"], "reflection")
            self.assertEqual(tasks[0].payload["source"], "scout")

    def test_scout_model_holds_vague_proposal_as_proposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            scout = acquire_role(project, "scout", "codex", ttl_seconds=60)

            def proposes(_project, _agent, _profile, _files, **_kwargs):
                proposal = cli.ReflectionProposal(
                    title="Improve the whole codebase",
                    resource=".",
                    evidence="general cleanup",
                )
                return cli.ReflectionResult((proposal,), ".mmux/runs/fake-scout.log", "ok", True)

            original = cli.invoke_scout_adapter
            cli.invoke_scout_adapter = proposes
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    message = execute_worker_available_task(
                        project,
                        "codex",
                        [("scout", scout.generation, scout.lease_until)],
                        run_deadline="",
                        agent_timeout_seconds=60,
                        agent_no_output_seconds=30,
                        test_timeout_seconds=60,
                        shutdown_grace_seconds=15,
                    )
            finally:
                cli.invoke_scout_adapter = original

            self.assertIsNotNone(message)
            tasks = list_tasks(project)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].status, "proposed")
            self.assertEqual(tasks[0].payload["source"], "scout")

    def test_worker_summarizer_role_backfills_missing_act_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task_id = insert_task_for_test(
                project,
                "completed without summary",
                "completed",
                {"resource": "src", "changed_files": ["src/app.py"], "last_message": "tester passed"},
            )
            summarizer = acquire_role(project, "summarizer", "claude", ttl_seconds=60)

            def fake_summary(_project, _worktree, _agent, _task, _context, **_kwargs):
                return cli.ActSummaryResult(
                    "- backfilled summary\n- tester passed",
                    ".mmux/runs/fake-summary.log",
                    "ok",
                    True,
                )

            original = cli.invoke_summarizer_adapter
            cli.invoke_summarizer_adapter = fake_summary
            try:
                message = execute_worker_available_task(
                    project,
                    "claude",
                    [("summarizer", summarizer.generation, summarizer.lease_until)],
                    run_deadline="",
                    agent_timeout_seconds=60,
                    agent_no_output_seconds=30,
                    test_timeout_seconds=60,
                    shutdown_grace_seconds=15,
                )
            finally:
                cli.invoke_summarizer_adapter = original

            self.assertIsNotNone(message)
            assert message is not None
            self.assertIn("act_summary backfilled", message)
            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.status, "completed")
            self.assertTrue(task.payload["act_summary_adapter_ok"])
            self.assertTrue(task.payload["act_summary_backfilled"])
            self.assertIn("backfilled summary", task.payload["act_summary"])

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
            review_id = enqueue_task(project, "review task", resource=".")
            test_id = enqueue_task(project, "tester task", resource=".")
            set_task_status_for_test(
                project,
                running_id,
                "running",
                claimed_by="codex",
                claimed_role="driver",
                claimed_generation=1,
            )
            set_task_status_for_test(
                project,
                review_id,
                "running_review",
                claimed_by="claude",
                claimed_role="reviewer",
                claimed_generation=3,
            )
            set_task_status_for_test(
                project,
                test_id,
                "running_test",
                claimed_by="claude",
                claimed_role="tester",
                claimed_generation=2,
            )

            cleanup_runtime_state(project)
            tasks = {task.id: task for task in list_tasks(project)}

            self.assertEqual(tasks[running_id].status, "pending")
            self.assertEqual(tasks[running_id].claimed_by, "")
            self.assertEqual(tasks[review_id].status, "awaiting_review")
            self.assertEqual(tasks[review_id].claimed_by, "")
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

    def test_release_resource_lock_rejects_stale_role_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)
            first_role = acquire_role(project, "driver", "codex", ttl_seconds=60)
            first_lock = acquire_resource_lock(
                project,
                ".",
                "codex",
                ttl_seconds=60,
                role="driver",
                role_generation=first_role.generation,
            )
            self.assertTrue(first_lock.ok)

            expired_at = format_utc(utc_now_dt() - dt.timedelta(seconds=1))
            with database(project) as db:
                db.execute(
                    "update role_leases set lease_until = ? where role = ?",
                    (expired_at, "driver"),
                )
                db.execute(
                    "update resource_locks set lease_until = ? where resource = ?",
                    (expired_at, "."),
                )

            second_role = acquire_role(project, "driver", "codex", ttl_seconds=60)
            second_lock = acquire_resource_lock(
                project,
                ".",
                "codex",
                ttl_seconds=60,
                role="driver",
                role_generation=second_role.generation,
            )
            stale_release = release_resource_lock(
                project,
                ".",
                holder="codex",
                role="driver",
                role_generation=first_role.generation,
            )
            active_lock = list_resource_locks(project)[-1]

            self.assertEqual(second_role.generation, first_role.generation + 1)
            self.assertTrue(second_lock.ok)
            self.assertFalse(stale_release.ok)
            self.assertEqual(stale_release.status, "stale_role")
            self.assertEqual(active_lock[4], "driver")
            self.assertEqual(active_lock[5], second_role.generation)
            self.assertEqual(active_lock[6], "active")

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

    def test_discover_frontier_candidates_prefers_todo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            (project / "src" / "app.py").write_text("value = 1\n# TODO: cover edge case\n", encoding="utf-8")
            run(["git", "add", "src/app.py"], cwd=project)
            run(["git", "commit", "-m", "todo"], cwd=project)
            profile = inspect_project(project)

            candidates = discover_frontier_candidates(project, profile)

            self.assertTrue(candidates)
            self.assertEqual(candidates[0].resource, "src/app.py")
            self.assertIn("TODO", candidates[0].title)

    def test_requeued_driver_task_gets_unique_worktree_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task_id = enqueue_task(project, "change src twice", resource="src")
            attempts = {"count": 0}
            original_driver = cli.invoke_agent_adapter
            original_reviewer = cli.invoke_reviewer_adapter
            original_utc_now = cli.utc_now

            def fake_driver(_project, execution_root, _agent, _task, _generation, _resource, **kwargs):
                attempts["count"] += 1
                (execution_root / "src" / "app.py").write_text(
                    f"value = {attempts['count'] + 1}\n",
                    encoding="utf-8",
                )
                return cli.AdapterResult(True, 0, ".mmux/runs/fake-driver.log", "ok")

            def fake_review(_project, _worktree, _agent, _task, _generation, _resource, _changed_files, **kwargs):
                return cli.ReviewResult("request_changes", ".mmux/runs/fake-review.log", "try again", True)

            cli.invoke_agent_adapter = fake_driver
            cli.invoke_reviewer_adapter = fake_review
            cli.utc_now = lambda: "2026-01-01T00:00:00+00:00"
            try:
                first_driver = acquire_role(project, "driver", "codex", ttl_seconds=60)
                with patched_planner(), contextlib.redirect_stdout(io.StringIO()):
                    execute_driver_task(project, "codex", first_driver.generation)
                first_task = get_task(project, task_id)
                self.assertIsNotNone(first_task)
                assert first_task is not None
                first_worktree = first_task.payload.get("worktree")

                reviewer = acquire_role(project, "reviewer", "claude", ttl_seconds=60)
                with contextlib.redirect_stdout(io.StringIO()):
                    execute_reviewer_task(project, "claude", reviewer.generation)

                second_driver = acquire_role(project, "driver", "codex", ttl_seconds=60)
                with patched_planner(), contextlib.redirect_stdout(io.StringIO()):
                    message = execute_driver_task(project, "codex", second_driver.generation)
                second_task = get_task(project, task_id)
            finally:
                cli.invoke_agent_adapter = original_driver
                cli.invoke_reviewer_adapter = original_reviewer
                cli.utc_now = original_utc_now

            self.assertIn("awaiting_review", message)
            self.assertIsNotNone(second_task)
            assert second_task is not None
            self.assertEqual(second_task.status, "awaiting_review")
            self.assertNotEqual(first_worktree, second_task.payload.get("worktree"))

    def test_ensure_default_task_uses_frontier_before_generic_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            (project / "src" / "app.py").write_text("value = 1\n# FIXME: handle empty input\n", encoding="utf-8")
            run(["git", "add", "src/app.py"], cwd=project)
            run(["git", "commit", "-m", "frontier"], cwd=project)
            ensure_layout(project)
            profile = inspect_project(project)

            task_id = ensure_default_task(project, profile)

            self.assertIsNotNone(task_id)
            task = get_task(project, int(task_id or 0))
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.payload.get("origin"), "frontier")
            self.assertIn("FIXME", task.title)
            with database(project) as db:
                row = db.execute("select status from frontier_items where status = ?", ("enqueued",)).fetchone()
            self.assertIsNotNone(row)

    def test_cmd_frontier_lists_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            (project / "src" / "app.py").write_text("value = 1\n# TODO: cover edge case\n", encoding="utf-8")
            run(["git", "add", "src/app.py"], cwd=project)
            run(["git", "commit", "-m", "todo"], cwd=project)

            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli.main(["frontier", str(project)])

            self.assertEqual(code, 0)
            self.assertIn("frontier candidates:", output.getvalue())
            self.assertIn("Resolve TODO", output.getvalue())

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

    def test_cmd_run_smoke_invokes_timed_cli_with_fake_tmux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()
            tmux_log = root / "tmux.log"
            tmux_state = root / "tmux.sessions"
            fake_tmux = bin_dir / "tmux"
            fake_tmux.write_text(
                """#!/bin/sh
log=${MMUX_FAKE_TMUX_LOG:?}
state=${MMUX_FAKE_TMUX_STATE:?}
printf '%s\\n' "$*" >> "$log"
cmd=$1
case "$cmd" in
  has-session)
    target=""
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "-t" ]; then
        shift
        target=$1
        break
      fi
      shift
    done
    [ -f "$state" ] && grep -Fxq "$target" "$state"
    exit $?
    ;;
  new-session)
    target=""
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "-s" ]; then
        shift
        target=$1
        break
      fi
      shift
    done
    printf '%s\\n' "$target" > "$state"
    exit 0
    ;;
  split-window|select-layout)
    exit 0
    ;;
  kill-session)
    rm -f "$state"
    exit 0
    ;;
esac
exit 1
""",
                encoding="utf-8",
            )
            fake_tmux.chmod(0o755)

            env = dict(os.environ)
            repo_src = Path(__file__).resolve().parents[1] / "src"
            existing_pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                f"{repo_src}{os.pathsep}{existing_pythonpath}" if existing_pythonpath else str(repo_src)
            )
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
            env["MMUX_FAKE_TMUX_LOG"] = str(tmux_log)
            env["MMUX_FAKE_TMUX_STATE"] = str(tmux_state)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mmux.cli",
                    "run",
                    str(project),
                    "--seconds",
                    "1",
                    "--checkpoint-seconds",
                    "1",
                    "--no-default-task",
                ],
                cwd=project,
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("started timed run:", result.stdout)
            self.assertIn("run checkpoint", result.stdout)
            self.assertIn("run summary:", result.stdout)
            self.assertFalse(tmux_state.exists())
            tmux_calls = tmux_log.read_text(encoding="utf-8")
            self.assertIn("new-session", tmux_calls)
            self.assertIn("split-window", tmux_calls)
            self.assertIn("kill-session", tmux_calls)
            with database(project.resolve()) as db:
                events = [
                    row[0]
                    for row in db.execute(
                        "select kind from events where kind in (?, ?) order by id",
                        ("run_started", "run_finished"),
                    ).fetchall()
                ]
            self.assertEqual(events, ["run_started", "run_finished"])

    def test_start_tmux_session_can_open_resident_agent_panes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            ensure_layout(project)
            calls = []
            original_run = cli.run
            original_tmux_has_session = cli.tmux_has_session
            original_prepare = cli.prepare_resident_worktree

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            def fake_prepare(_project, agent):
                path = _project / ".mmux" / "resident" / agent
                path.mkdir(parents=True, exist_ok=True)
                return path

            cli.run = fake_run
            cli.tmux_has_session = lambda _name: False
            cli.prepare_resident_worktree = fake_prepare
            try:
                started = cli.start_tmux_session(project, execute_agents=True, resident_agents=True)
            finally:
                cli.run = original_run
                cli.tmux_has_session = original_tmux_has_session
                cli.prepare_resident_worktree = original_prepare

            joined_calls = [" ".join(str(part) for part in cmd) for cmd in calls]
            self.assertTrue(started)
            self.assertTrue(any("codex" in call and "--no-alt-screen" in call for call in joined_calls))
            self.assertTrue(any("claude" in call and "--permission-mode" in call for call in joined_calls))
            self.assertTrue(any("new-window" in call and "automation" in call for call in joined_calls))
            self.assertTrue(any("select-window" in call for call in joined_calls))
            self.assertTrue(any("select-pane" in call and "codex" in call for call in joined_calls))
            self.assertTrue(resident_mode_enabled(project))

    def test_send_tmux_message_targets_resident_pane(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            ensure_layout(project)
            calls = []
            original_run = cli.run
            original_tmux_has_session = cli.tmux_has_session

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            cli.run = fake_run
            cli.tmux_has_session = lambda _name: True
            try:
                send_tmux_message(project, "claude", "MMUX_NOTE from=test hello")
            finally:
                cli.run = original_run
                cli.tmux_has_session = original_tmux_has_session

            self.assertEqual(calls[0][:4], ["tmux", "send-keys", "-t", f"{session_name(project)}:0.3"])
            self.assertEqual(calls[0][4:], ["-l", "MMUX_NOTE from=test hello"])
            self.assertEqual(calls[1], ["tmux", "send-keys", "-t", f"{session_name(project)}:0.3", "Enter"])

    def test_format_resident_control_message(self) -> None:
        message = format_resident_control_message("task", "  add tests\nnow  ", task_id=12)
        self.assertEqual(message, "MMUX_TASK from=mmux task=#12 add tests now")

    def test_parse_resident_protocol_line(self) -> None:
        event = parse_resident_protocol_line("codex", "  MMUX_BLOCKED from=codex task=#12 needs input  ")
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.agent, "codex")
        self.assertEqual(event.kind, "blocked")
        self.assertEqual(event.task_id, 12)
        self.assertEqual(event.message, "needs input")
        self.assertIsNone(parse_resident_protocol_line("codex", "MMUX_NOTE from=claude hi"))

    def test_parse_resident_protocol_line_accepts_sentinel(self) -> None:
        event = parse_resident_protocol_line("claude", "  <<MMUX:DONE task=#9 implemented>>  ")
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.agent, "claude")
        self.assertEqual(event.kind, "done")
        self.assertEqual(event.task_id, 9)
        self.assertEqual(event.message, "implemented")

    def test_scan_resident_agent_output_records_protocol_events_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            ensure_layout(project)
            set_resident_mode(project, True)
            original_capture = cli.capture_resident_pane_output
            original_tmux_has_session = cli.tmux_has_session

            def fake_capture(_project, agent):
                if agent == "codex":
                    return "\n".join(
                        [
                            "ordinary terminal output",
                            "MMUX_DONE from=codex task=#7 implemented",
                            "MMUX_BLOCKED from=codex task=#8 needs decision",
                        ]
                    )
                return ""

            cli.capture_resident_pane_output = fake_capture
            cli.tmux_has_session = lambda _name: True
            try:
                first = scan_resident_agent_output(project)
                second = scan_resident_agent_output(project)
            finally:
                cli.capture_resident_pane_output = original_capture
                cli.tmux_has_session = original_tmux_has_session

            self.assertEqual(first, 2)
            self.assertEqual(second, 0)
            with database(project) as db:
                rows = db.execute(
                    "select kind, payload from events where kind like ? order by id",
                    ("resident_agent_%",),
                ).fetchall()
            self.assertEqual([row[0] for row in rows], ["resident_agent_done", "resident_agent_blocked"])
            payload = cli.decode_payload(rows[0][1])
            self.assertEqual(payload["agent"], "codex")
            self.assertEqual(payload["task_id"], 7)

    def test_process_resident_done_snapshots_diff_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task_id = enqueue_task(project, "resident change", resource="src")
            resident = cli.prepare_resident_worktree(project, "codex")
            (resident / "src" / "app.py").write_text("value = 2\n", encoding="utf-8")
            event = parse_resident_protocol_line("codex", f"MMUX_DONE from=codex task=#{task_id} implemented")
            assert event is not None

            messages = process_resident_protocol_events(project, [event])

            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.status, "awaiting_review")
            self.assertIn("awaiting_review", messages[0])
            worktree_value = task.payload.get("worktree")
            self.assertIsInstance(worktree_value, str)
            snapshot = project / str(worktree_value)
            self.assertTrue(snapshot.exists())
            self.assertEqual((snapshot / "src" / "app.py").read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual((resident / "src" / "app.py").read_text(encoding="utf-8"), "value = 1\n")
            self.assertEqual((project / "src" / "app.py").read_text(encoding="utf-8"), "value = 1\n")

    def test_report_resident_done_uses_state_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_git_project(project)
            ensure_layout(project)
            task_id = enqueue_task(project, "resident report change", resource="src")
            resident = cli.prepare_resident_worktree(project, "codex")
            (resident / "src" / "app.py").write_text("value = 3\n", encoding="utf-8")

            recorded, messages, event = report_resident_protocol_event(
                project,
                agent="codex",
                kind="done",
                task_id=task_id,
                message="implemented through cli",
            )

            task = get_task(project, task_id)
            self.assertTrue(recorded)
            self.assertEqual(event.line, f"MMUX_DONE from=codex task=#{task_id} implemented through cli")
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.status, "awaiting_review")
            self.assertIn("awaiting_review", messages[0])
            with database(project) as db:
                rows = db.execute(
                    "select kind from events where kind = ?",
                    ("resident_agent_done",),
                ).fetchall()
            self.assertEqual(len(rows), 1)

    def test_report_resident_event_dedupes_screen_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            ensure_layout(project)
            first, _, event = report_resident_protocol_event(
                project,
                agent="codex",
                kind="blocked",
                task_id=42,
                message="need input",
            )
            fallback = parse_resident_protocol_line("codex", "<<MMUX:BLOCKED task=#42 need input>>")
            assert fallback is not None
            second = cli.record_resident_protocol_events(project, [fallback])

            self.assertTrue(first)
            self.assertEqual(second, [])

    def test_resident_context_from_path_detects_owner_project_and_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_git_project(project)
            ensure_layout(project)
            resident = cli.prepare_resident_worktree(project, "claude")
            nested = resident / "src"

            context = resident_context_from_path(nested)

            self.assertEqual(context, (project, "claude"))

    def test_cmd_report_auto_detects_resident_project_and_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_git_project(project)
            ensure_layout(project)
            task_id = enqueue_task(project, "resident cli blocked", resource="src")
            resident = cli.prepare_resident_worktree(project, "claude")
            previous_cwd = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(resident)
                with contextlib.redirect_stdout(output):
                    code = cli.main(["report", "blocked", "need", "input", "--task-id", str(task_id)])
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(code, 0)
            self.assertIn("reported: MMUX_BLOCKED from=claude", output.getvalue())
            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.payload.get("resident_blocked_by"), "claude")

    def test_process_resident_done_rejects_resource_violation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task_id = enqueue_task(project, "resident change", resource="src")
            resident = cli.prepare_resident_worktree(project, "codex")
            (resident / "README.md").write_text("# changed\n", encoding="utf-8")
            event = parse_resident_protocol_line("codex", f"MMUX_DONE from=codex task=#{task_id} implemented")
            assert event is not None

            messages = process_resident_protocol_events(project, [event])

            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.status, "rejected")
            self.assertIn("rejected", messages[0])
            self.assertIn("resource_violation", task.payload.get("diff_policy"))

    def test_process_resident_blocked_requests_peer_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_git_project(project)
            ensure_layout(project)
            set_resident_mode(project, True)
            task_id = enqueue_task(project, "blocked task", resource="src")
            event = parse_resident_protocol_line("codex", f"MMUX_BLOCKED from=codex task=#{task_id} need architecture call")
            assert event is not None
            calls = []
            original_run = cli.run
            original_tmux_has_session = cli.tmux_has_session

            def fake_run(cmd, **kwargs):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "", "")

            cli.run = fake_run
            cli.tmux_has_session = lambda _name: True
            try:
                messages = process_resident_protocol_events(project, [event])
            finally:
                cli.run = original_run
                cli.tmux_has_session = original_tmux_has_session

            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.payload.get("resident_blocked_by"), "codex")
            self.assertEqual(task.payload.get("resident_takeover_agent"), "claude")
            self.assertEqual(task.payload.get("resident_blocked_count"), 1)
            self.assertEqual(task.payload.get("resident_blocked_agents"), ["codex"])
            self.assertIn("takeover requested from claude", messages[0])
            joined_calls = [" ".join(str(part) for part in cmd) for cmd in calls]
            self.assertTrue(any("send-keys" in call and "MMUX_TASK" in call for call in joined_calls))
            with database(project) as db:
                row = db.execute(
                    "select payload from events where kind = ? order by id desc limit 1",
                    ("resident_blocked_takeover_requested",),
                ).fetchone()
            self.assertIsNotNone(row)
            payload = cli.decode_payload(row[0])
            self.assertEqual(payload["peer"], "claude")
            self.assertTrue(payload["delivered"])

    def test_second_resident_blocked_escalates_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_git_project(project)
            ensure_layout(project)
            task_id = enqueue_task(project, "twice blocked task", resource="src")
            first = parse_resident_protocol_line("codex", f"MMUX_BLOCKED from=codex task=#{task_id} missing context")
            second = parse_resident_protocol_line("claude", f"MMUX_BLOCKED from=claude task=#{task_id} also stuck")
            assert first is not None
            assert second is not None

            first_messages = process_resident_protocol_events(project, [first])
            second_messages = process_resident_protocol_events(project, [second])

            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.status, "blocked")
            self.assertEqual(task.payload.get("resident_blocked_count"), 2)
            self.assertEqual(task.payload.get("resident_blocked_agents"), ["codex", "claude"])
            self.assertIn("takeover requested", first_messages[0])
            self.assertIn("escalated to blocked", second_messages[0])
            with database(project) as db:
                row = db.execute(
                    "select payload from events where kind = ? order by id desc limit 1",
                    ("resident_blocked_escalated",),
                ).fetchone()
            self.assertIsNotNone(row)
            payload = cli.decode_payload(row[0])
            self.assertEqual(payload["blocked_count"], 2)

    def test_requeue_task_moves_blocked_task_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_git_project(project)
            ensure_layout(project)
            task_id = enqueue_task(project, "blocked task", resource="src")
            cli.finish_task(project, task_id, "blocked", message="needs human decision")

            outcome = requeue_task(project, task_id, reason="decision made")

            self.assertTrue(outcome.ok)
            self.assertEqual(outcome.from_status, "blocked")
            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.payload.get("requeued_from"), "blocked")
            self.assertEqual(task.payload.get("requeued_reason"), "decision made")
            with database(project) as db:
                row = db.execute(
                    "select payload from events where kind = ? order by id desc limit 1",
                    ("task_requeued",),
                ).fetchone()
            self.assertIsNotNone(row)
            payload = cli.decode_payload(row[0])
            self.assertEqual(payload["from"], "blocked")

    def test_requeue_task_denies_active_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_git_project(project)
            ensure_layout(project)
            task_id = enqueue_task(project, "running task", resource="src")
            set_task_status_for_test(project, task_id, "running")

            outcome = requeue_task(project, task_id)

            self.assertFalse(outcome.ok)
            self.assertEqual(outcome.from_status, "running")

    def test_cmd_task_requeue_accepts_hash_prefixed_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve()
            init_git_project(project)
            ensure_layout(project)
            task_id = enqueue_task(project, "blocked task", resource="src")
            cli.finish_task(project, task_id, "blocked", message="needs human decision")

            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli.main(["task", "requeue", f"#{task_id}", "--project", str(project), "--reason", "fixed"])

            self.assertEqual(code, 0)
            self.assertIn("task requeued", output.getvalue())
            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertEqual(task.status, "pending")

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

    def test_replenish_default_task_respects_open_work_and_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)
            profile = inspect_project(project)

            first = maybe_replenish_default_task(
                project,
                profile,
                disabled=False,
                remaining_seconds=MIN_EXECUTION_BUDGET_SECONDS + 15,
                shutdown_grace_seconds=15,
            )
            blocked_by_open = maybe_replenish_default_task(
                project,
                profile,
                disabled=False,
                remaining_seconds=MIN_EXECUTION_BUDGET_SECONDS + 15,
                shutdown_grace_seconds=15,
            )
            set_task_status_for_test(project, int(first or 0), "completed")
            too_late = maybe_replenish_default_task(
                project,
                profile,
                disabled=False,
                remaining_seconds=MIN_EXECUTION_BUDGET_SECONDS + 14,
                shutdown_grace_seconds=15,
            )
            disabled = maybe_replenish_default_task(
                project,
                profile,
                disabled=True,
                remaining_seconds=MIN_EXECUTION_BUDGET_SECONDS + 15,
                shutdown_grace_seconds=15,
            )
            second = maybe_replenish_default_task(
                project,
                profile,
                disabled=False,
                remaining_seconds=MIN_EXECUTION_BUDGET_SECONDS + 15,
                shutdown_grace_seconds=15,
            )

            self.assertEqual(first, 1)
            self.assertIsNone(blocked_by_open)
            self.assertIsNone(too_late)
            self.assertIsNone(disabled)
            self.assertEqual(second, 2)
            self.assertEqual(task_status_counts(project), {"pending": 1, "completed": 1})

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
        self.assertIn("--sandbox", codex)
        self.assertIn("workspace-write", codex)
        self.assertEqual(claude[:2], ["claude", "-p"])
        self.assertIn("--permission-mode", claude)
        self.assertIn("--verbose", claude)
        self.assertIn("stream-json", claude)
        self.assertIn("--include-partial-messages", claude)

        resident_prompt = build_resident_prompt("codex", project)
        resident_codex = build_resident_command("codex", project / ".mmux" / "resident" / "codex", resident_prompt)
        resident_claude = build_resident_command("claude", project / ".mmux" / "resident" / "claude", resident_prompt)

        self.assertIn("MMUX_TASK", resident_prompt)
        self.assertIn("MMUX_DONE", resident_prompt)
        self.assertIn("report", resident_prompt)
        self.assertIn("PATH=", resident_codex)
        self.assertIn("PATH=", resident_claude)
        self.assertIn("--no-alt-screen", resident_codex)
        self.assertIn("--permission-mode", resident_claude)

        worker_command = module_command(project, "worker", "codex")
        self.assertIn("PATH=", worker_command)

    def test_resident_prompt_includes_project_agent_brief(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "AGENTS.md").write_text("# Local brief\nStay scoped.\n", encoding="utf-8")

            prompt = build_resident_prompt("codex", project)

            self.assertIn("Project AGENTS.md brief:", prompt)
            self.assertIn("Stay scoped.", prompt)
            self.assertIn("Stay scoped.", read_agent_brief(project))

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

    def test_finish_task_removes_and_archives_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task = claim_driver_task(project, resource="src")
            worktree = create_task_worktree(project, task, "codex")
            (worktree / "src" / "app.py").write_text("value = 99\n", encoding="utf-8")
            rel = cli.relative_to_project(project, worktree)

            cli.finish_task(project, task.id, "completed", payload_updates={"worktree": rel})

            self.assertFalse(worktree.exists())
            archives = list(cli.archive_root(project).glob(f"task-{task.id}-*.patch"))
            self.assertEqual(len(archives), 1)
            self.assertIn("value = 99", archives[0].read_text(encoding="utf-8"))

    def test_prune_orphan_worktrees_keeps_pipeline_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            keep_task = claim_driver_task(project, resource="src")
            keep_wt = create_task_worktree(project, keep_task, "codex")
            set_task_status_for_test(
                project,
                keep_task.id,
                "awaiting_test",
                payload={"worktree": cli.relative_to_project(project, keep_wt)},
            )
            orphan = cli.worktree_root(project) / "task-999-codex-gen0-1-stamp"
            cli.run(["git", "worktree", "add", "--detach", str(orphan), "HEAD"], cwd=project)

            removed = cli.prune_orphan_worktrees(project)

            self.assertEqual(removed, 1)
            self.assertTrue(keep_wt.exists())
            self.assertFalse(orphan.exists())

    def test_keep_worktrees_env_preserves_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task = claim_driver_task(project, resource="src")
            worktree = create_task_worktree(project, task, "codex")
            rel = cli.relative_to_project(project, worktree)
            previous = os.environ.get("MMUX_KEEP_WORKTREES")
            os.environ["MMUX_KEEP_WORKTREES"] = "1"
            try:
                cli.finish_task(project, task.id, "completed", payload_updates={"worktree": rel})
            finally:
                if previous is None:
                    os.environ.pop("MMUX_KEEP_WORKTREES", None)
                else:
                    os.environ["MMUX_KEEP_WORKTREES"] = previous

            self.assertTrue(worktree.exists())

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

    def test_execute_driver_task_moves_accepted_patch_to_awaiting_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            enqueue_task(project, "change src", resource="src")
            lease = acquire_role(project, "driver", "codex", ttl_seconds=60)

            original = cli.invoke_agent_adapter
            captured_plan: list[str] = []

            def fake_adapter(_project, execution_root, _agent, task, _generation, _resource, **kwargs):
                self.assertEqual(task.title, "change src")
                captured_plan.append(kwargs.get("plan_text", ""))
                (execution_root / "src" / "app.py").write_text("value = 4\n", encoding="utf-8")
                return cli.AdapterResult(True, 0, ".mmux/runs/fake.log", "ok")

            cli.invoke_agent_adapter = fake_adapter
            try:
                with patched_planner(), contextlib.redirect_stdout(io.StringIO()):
                    message = execute_driver_task(project, "codex", lease.generation)
            finally:
                cli.invoke_agent_adapter = original

            self.assertIn("awaiting_review", message)
            self.assertEqual((project / "src" / "app.py").read_text(encoding="utf-8"), "value = 1\n")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "awaiting_review")
            self.assertEqual(tasks[-1].payload["diff_policy"], "ok")
            self.assertEqual(tasks[-1].payload["driver_agent"], "codex")
            self.assertIn("worktree", tasks[-1].payload)
            self.assertEqual(tasks[-1].payload["plan_decision"], "proceed")
            self.assertEqual(tasks[-1].payload["plan_contract"]["read"], ["src/app.py"])
            self.assertEqual(captured_plan, [f"{_FAKE_PLAN_JSON}\nMMUX_PLAN PROCEED"])

    def test_execute_driver_task_requeues_adapter_health_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            enqueue_task(project, "change src", resource="src")
            lease = acquire_role(project, "driver", "claude", ttl_seconds=60)

            original = cli.invoke_agent_adapter

            def fake_adapter(_project, _execution_root, _agent, _task, _generation, _resource, **kwargs):
                return cli.AdapterResult(False, 124, ".mmux/runs/fake-driver.log", "agent command produced no output for 30s")

            cli.invoke_agent_adapter = fake_adapter
            try:
                with patched_planner(), contextlib.redirect_stdout(io.StringIO()):
                    message = execute_driver_task(project, "claude", lease.generation)
            finally:
                cli.invoke_agent_adapter = original

            self.assertIn("requeued", message)
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "pending")
            self.assertEqual(tasks[-1].claimed_by, "")
            self.assertEqual(tasks[-1].payload["adapter_cooldown_agent"], "claude")
            cooldowns = cli.list_agent_cooldowns(project)
            self.assertEqual(cooldowns[0][0], "claude")

    def test_execute_driver_task_aborts_on_plan_abort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            enqueue_task(project, "change src", resource="src")
            lease = acquire_role(project, "driver", "codex", ttl_seconds=60)

            adapter_called = []

            def fake_adapter(*_args, **_kwargs):
                adapter_called.append(True)
                return cli.AdapterResult(True, 0, ".mmux/runs/should-not-run.log", "ok")

            def fake_planner_abort(_project, _worktree, _agent, _task, _generation, _resource, **kwargs):
                return cli.PlanResult(
                    "abort",
                    ".mmux/runs/fake-plan-abort.log",
                    "READ: none\nPLAN: cannot scope\nRISKS: none\nMMUX_PLAN ABORT: out of scope",
                    "out of scope",
                    True,
                )

            original_adapter = cli.invoke_agent_adapter
            cli.invoke_agent_adapter = fake_adapter
            try:
                with patched_planner(fake_planner_abort), contextlib.redirect_stdout(io.StringIO()):
                    message = execute_driver_task(project, "codex", lease.generation)
            finally:
                cli.invoke_agent_adapter = original_adapter

            self.assertIn("no_change", message)
            self.assertIn("plan abort", message)
            self.assertEqual(adapter_called, [], "driver adapter must not run after plan abort")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "no_change")
            self.assertEqual(tasks[-1].payload["plan_decision"], "abort")
            self.assertIn("MMUX_PLAN ABORT", tasks[-1].payload["plan_text"])

    def test_execute_driver_task_requeues_on_plan_review_request_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            enqueue_task(project, "change src", resource="src")
            lease = acquire_role(project, "driver", "codex", ttl_seconds=60)

            adapter_called = []

            def fake_adapter(*_args, **_kwargs):
                adapter_called.append(True)
                return cli.AdapterResult(True, 0, ".mmux/runs/should-not-run.log", "ok")

            def fake_plan_review_request_changes(_project, _worktree, _agent, _task, _generation, _resource, _plan_text, **kwargs):
                return cli.ReviewResult("request_changes", ".mmux/runs/fake-plan-review.log", "READ list is empty", True)

            original_adapter = cli.invoke_agent_adapter
            cli.invoke_agent_adapter = fake_adapter
            try:
                with patched_planner(plan_reviewer_stub=fake_plan_review_request_changes), contextlib.redirect_stdout(io.StringIO()):
                    message = execute_driver_task(project, "codex", lease.generation)
            finally:
                cli.invoke_agent_adapter = original_adapter

            self.assertIn("requeued", message)
            self.assertIn("plan rejected", message)
            self.assertEqual(adapter_called, [], "driver adapter must not run after plan rejection")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "pending")
            self.assertEqual(tasks[-1].payload["plan_review_decision"], "request_changes")
            self.assertEqual(tasks[-1].payload["plan_review_attempts"], 1)
            self.assertEqual(tasks[-1].payload["plan_review_last_reason"], "READ list is empty")

    def test_execute_driver_task_blocks_after_repeated_plan_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            enqueue_task(project, "change src", resource="src")

            adapter_called = []

            def fake_adapter(*_args, **_kwargs):
                adapter_called.append(True)
                return cli.AdapterResult(True, 0, ".mmux/runs/should-not-run.log", "ok")

            def fake_plan_review_request_changes(_project, _worktree, _agent, _task, _generation, _resource, _plan_text, **kwargs):
                return cli.ReviewResult("request_changes", ".mmux/runs/fake-plan-review.log", "scope unclear", True)

            original_adapter = cli.invoke_agent_adapter
            cli.invoke_agent_adapter = fake_adapter
            try:
                with patched_planner(plan_reviewer_stub=fake_plan_review_request_changes):
                    with contextlib.redirect_stdout(io.StringIO()):
                        first_lease = acquire_role(project, "driver", "codex", ttl_seconds=60)
                        first_message = execute_driver_task(project, "codex", first_lease.generation)
                    with contextlib.redirect_stdout(io.StringIO()):
                        second_lease = acquire_role(project, "driver", "claude", ttl_seconds=60)
                        second_message = execute_driver_task(project, "claude", second_lease.generation)
            finally:
                cli.invoke_agent_adapter = original_adapter

            self.assertIn("requeued", first_message)
            self.assertIn("blocked", second_message)
            self.assertEqual(adapter_called, [], "driver adapter must not run when plan keeps being rejected")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "blocked")
            self.assertEqual(tasks[-1].payload["plan_review_attempts"], 2)

    def test_execute_driver_task_requeues_on_invalid_plan_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            enqueue_task(project, "change src", resource="src")
            lease = acquire_role(project, "driver", "codex", ttl_seconds=60)

            adapter_called = []
            reviewer_called = []

            def fake_adapter(*_args, **_kwargs):
                adapter_called.append(True)
                return cli.AdapterResult(True, 0, ".mmux/runs/should-not-run.log", "ok")

            def fake_planner_empty_read(_project, _worktree, _agent, _task, _generation, _resource, **kwargs):
                return cli.PlanResult(
                    "proceed",
                    ".mmux/runs/fake-plan.log",
                    '{"read": [], "plan": ["change it"], "risks": ["none"]}\nMMUX_PLAN PROCEED',
                    "",
                    True,
                )

            def recording_plan_reviewer(*_args, **_kwargs):
                reviewer_called.append(True)
                return cli.ReviewResult("approve", ".mmux/runs/fake-plan-review.log", "", True)

            original_adapter = cli.invoke_agent_adapter
            cli.invoke_agent_adapter = fake_adapter
            try:
                with patched_planner(fake_planner_empty_read, plan_reviewer_stub=recording_plan_reviewer), contextlib.redirect_stdout(io.StringIO()):
                    message = execute_driver_task(project, "codex", lease.generation)
            finally:
                cli.invoke_agent_adapter = original_adapter

            self.assertIn("requeued", message)
            self.assertEqual(reviewer_called, [], "deterministic gate must reject before the LLM plan reviewer runs")
            self.assertEqual(adapter_called, [], "driver adapter must not run after an invalid plan contract")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "pending")
            self.assertEqual(tasks[-1].payload["plan_review_decision"], "request_changes")
            self.assertEqual(tasks[-1].payload["plan_review_attempts"], 1)
            self.assertIn("plan contract invalid", tasks[-1].payload["plan_review_last_reason"])
            self.assertEqual(tasks[-1].payload["plan_contract"]["plan"], ["change it"])

    def test_execute_driver_task_bypasses_plan_review_adapter_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            enqueue_task(project, "change src", resource="src")
            lease = acquire_role(project, "driver", "codex", ttl_seconds=60)

            def fake_adapter(_project, execution_root, _agent, task, _generation, _resource, **kwargs):
                (execution_root / "src" / "app.py").write_text("value = 4\n", encoding="utf-8")
                return cli.AdapterResult(True, 0, ".mmux/runs/fake-driver.log", "ok")

            def fake_plan_review_adapter_failed(_project, _worktree, _agent, _task, _generation, _resource, _plan_text, **kwargs):
                return cli.ReviewResult("adapter_failed", ".mmux/runs/fake-plan-review.log", "boom", False)

            original_adapter = cli.invoke_agent_adapter
            cli.invoke_agent_adapter = fake_adapter
            try:
                with patched_planner(plan_reviewer_stub=fake_plan_review_adapter_failed), contextlib.redirect_stdout(io.StringIO()):
                    message = execute_driver_task(project, "codex", lease.generation)
            finally:
                cli.invoke_agent_adapter = original_adapter

            self.assertIn("awaiting_review", message)
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "awaiting_review")
            self.assertEqual(tasks[-1].payload["plan_review_decision"], "adapter_failed")
            self.assertTrue(tasks[-1].payload["plan_review_bypassed"])

    def test_execute_driver_task_requeues_planner_health_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            enqueue_task(project, "change src", resource="src")
            lease = acquire_role(project, "driver", "claude", ttl_seconds=60)

            adapter_called = []

            def fake_adapter(*_args, **_kwargs):
                adapter_called.append(True)
                return cli.AdapterResult(True, 0, ".mmux/runs/should-not-run.log", "ok")

            def fake_planner_health_fail(_project, _worktree, _agent, _task, _generation, _resource, **kwargs):
                return cli.PlanResult(
                    "adapter_failed",
                    ".mmux/runs/fake-plan.log",
                    "",
                    "agent command produced no output for 30s",
                    False,
                )

            original_adapter = cli.invoke_agent_adapter
            cli.invoke_agent_adapter = fake_adapter
            try:
                with patched_planner(fake_planner_health_fail), contextlib.redirect_stdout(io.StringIO()):
                    message = execute_driver_task(project, "claude", lease.generation)
            finally:
                cli.invoke_agent_adapter = original_adapter

            self.assertIn("requeued", message)
            self.assertEqual(adapter_called, [], "driver adapter must not run after planner health failure")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "pending")
            self.assertEqual(tasks[-1].payload["plan_decision"], "adapter_failed")
            self.assertEqual(tasks[-1].payload["adapter_cooldown_agent"], "claude")
            cooldowns = cli.list_agent_cooldowns(project)
            self.assertEqual(cooldowns[0][0], "claude")

    def test_worker_falls_through_to_driver_when_tester_has_no_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            ensure_layout(project)
            enqueue_task(project, "driver task", resource=".")
            roles = [
                ("driver", 11, "2099-01-01T00:00:00+00:00"),
                ("tester", 10, "2099-01-01T00:00:00+00:00"),
            ]

            original_driver = cli.execute_driver_task
            original_tester = cli.execute_tester_task
            calls = []

            def fake_tester(*args, **kwargs):
                raise AssertionError("tester should be skipped when no task awaits testing")

            def fake_driver(_project, _agent, generation, **kwargs):
                calls.append(("driver", generation, kwargs["timeout_seconds"]))
                return "task #1 awaiting_review log=.mmux/runs/fake.log"

            cli.execute_tester_task = fake_tester
            cli.execute_driver_task = fake_driver
            try:
                message = execute_worker_available_task(
                    project,
                    "codex",
                    roles,
                    run_deadline="",
                    agent_timeout_seconds=60,
                    agent_no_output_seconds=30,
                    test_timeout_seconds=60,
                    shutdown_grace_seconds=15,
                )
            finally:
                cli.execute_driver_task = original_driver
                cli.execute_tester_task = original_tester

            self.assertIn("awaiting_review", message)
            self.assertEqual(calls, [("driver", 11, 60)])

    def test_parse_reviewer_decision_accepts_protocol_line(self) -> None:
        decision, message = parse_reviewer_decision("Looks fine\nMMUX_REVIEW APPROVE")
        self.assertEqual(decision, "approve")
        self.assertEqual(message, "")

        decision, message = parse_reviewer_decision("MMUX_REVIEW REQUEST_CHANGES: missing test")
        self.assertEqual(decision, "request_changes")
        self.assertEqual(message, "missing test")

        decision, message = parse_reviewer_decision("no protocol")
        self.assertEqual(decision, "invalid")
        self.assertIn("missing", message)

    def test_parse_planner_decision_accepts_protocol_line(self) -> None:
        decision, message = parse_planner_decision("READ: foo\nPLAN: bar\nRISKS: none\nMMUX_PLAN PROCEED")
        self.assertEqual(decision, "proceed")
        self.assertEqual(message, "")

        decision, message = parse_planner_decision("MMUX_PLAN ABORT: out of scope")
        self.assertEqual(decision, "abort")
        self.assertEqual(message, "out of scope")

        decision, message = parse_planner_decision("PROCEED")
        self.assertEqual(decision, "invalid")
        self.assertIn("missing", message)

        decision, message = parse_planner_decision("MMUX_PLAN WHATEVER")
        self.assertEqual(decision, "invalid")

    def test_parse_plan_contract_reads_plain_and_fenced_json(self) -> None:
        plain = cli.parse_plan_contract(
            '{"read": ["src/app.py"], "plan": ["do it"], "risks": ["none"]}\nMMUX_PLAN PROCEED'
        )
        self.assertEqual(plain["read"], ["src/app.py"])
        self.assertEqual(plain["plan"], ["do it"])

        fenced = cli.parse_plan_contract(
            'prose\n```json\n{"read": ["a.py"], "plan": ["x"], "risks": []}\n```\nMMUX_PLAN PROCEED'
        )
        self.assertEqual(fenced["read"], ["a.py"])
        self.assertEqual(fenced["risks"], [])

        missing = cli.parse_plan_contract("READ: foo\nPLAN: bar\nMMUX_PLAN PROCEED")
        self.assertEqual(missing, {"read": [], "plan": [], "risks": []})

    def test_plan_contract_problems_flags_missing_read_and_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)

            self.assertEqual(
                cli.plan_contract_problems(project, {"read": ["src/app.py"], "plan": ["change value"], "risks": []}),
                [],
            )
            empty_read = cli.plan_contract_problems(project, {"read": [], "plan": ["x"], "risks": []})
            self.assertTrue(any("read list is empty" in problem for problem in empty_read))

            hallucinated = cli.plan_contract_problems(project, {"read": ["does/not/exist.py"], "plan": ["x"], "risks": []})
            self.assertTrue(any("no real path" in problem for problem in hallucinated))

            empty_plan = cli.plan_contract_problems(project, {"read": ["src/app.py"], "plan": [], "risks": []})
            self.assertTrue(any("plan list is empty" in problem for problem in empty_plan))

    def test_parse_reflection_tasks_extracts_quoted_fields(self) -> None:
        text = "\n".join(
            [
                "preamble",
                'MMUX_REFLECT_TASK title="add empty-string test" resource="tests/test_todo_core.py" evidence="task #1"',
                'MMUX_REFLECT_TASK resource="src" evidence="no title here"',
                'MMUX_REFLECT_TASK title="vague proposal" resource="." evidence=""',
                "MMUX_REFLECT END",
            ]
        )
        proposals = parse_reflection_tasks(text)
        self.assertEqual(len(proposals), 2)
        self.assertEqual(proposals[0].title, "add empty-string test")
        self.assertEqual(proposals[0].resource, "tests/test_todo_core.py")
        self.assertEqual(proposals[0].evidence, "task #1")
        self.assertEqual(proposals[1].title, "vague proposal")
        self.assertEqual(proposals[1].evidence, "")

    def test_evidence_is_concrete_for_task_reference_and_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / "src").mkdir()
            (project / "src" / "app.py").write_text("value = 1\n", encoding="utf-8")
            self.assertTrue(evidence_is_concrete(project, "task #7"))
            self.assertTrue(evidence_is_concrete(project, "see src/app.py:3"))
            self.assertFalse(evidence_is_concrete(project, "vibes"))
            self.assertFalse(evidence_is_concrete(project, "src/missing.py"))
            self.assertFalse(evidence_is_concrete(project, "../../etc/passwd"))

    def test_recent_completions_with_summary_returns_only_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            insert_task_for_test(project, "with summary", "completed", {"resource": "src", "act_summary": "did the thing"})
            insert_task_for_test(project, "no summary", "completed", {"resource": "src"})
            insert_task_for_test(project, "not done", "pending", {"resource": "src", "act_summary": "shouldn't count"})
            completions = recent_completions_with_summary(project, limit=10)
            self.assertEqual([entry["title"] for entry in completions], ["with summary"])
            self.assertEqual(completions[0]["act_summary"], "did the thing")

    def test_perform_reflection_auto_promotes_evidence_and_proposes_vague_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            insert_task_for_test(
                project,
                "Resolve TODO in src/app.py",
                "completed",
                {"resource": "src", "act_summary": "trimmed whitespace; no empty-string test"},
            )

            def fake_reflection(_project, _agent, _completions, **_kwargs):
                proposals = (
                    cli.ReflectionProposal("add empty-string test", "tests/", "task #1"),
                    cli.ReflectionProposal("audit overall vibes", ".", ""),
                )
                return cli.ReflectionResult(proposals, ".mmux/runs/fake-reflect.log", "ok", True)

            original = cli.invoke_reflection_adapter
            cli.invoke_reflection_adapter = fake_reflection
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    outcome = perform_reflection(project, "claude")
            finally:
                cli.invoke_reflection_adapter = original

            self.assertTrue(outcome.adapter_ok)
            self.assertEqual(len(outcome.promoted_ids), 1)
            self.assertEqual(len(outcome.proposed_ids), 1)
            tasks_by_id = {task.id: task for task in list_tasks(project)}
            promoted = tasks_by_id[outcome.promoted_ids[0]]
            self.assertEqual(promoted.status, "pending")
            self.assertEqual(promoted.payload["kind"], "reflection")
            self.assertTrue(promoted.payload["reflection_auto_promoted"])
            self.assertEqual(promoted.payload["reflection_evidence"], "task #1")
            proposed_task = tasks_by_id[outcome.proposed_ids[0]]
            self.assertEqual(proposed_task.status, "proposed")
            self.assertFalse(proposed_task.payload["reflection_auto_promoted"])

    def test_reflection_task_touching_mmux_marks_self_mutation_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)

            task_id, status = cli.enqueue_proposal(
                project,
                cli.ReflectionProposal("tighten mmux state policy", "src/mmux", "task #1"),
                [1],
                auto_promote=True,
            )

            task = get_task(project, task_id)
            assert task is not None
            self.assertEqual(status, "pending")
            self.assertTrue(task.payload["reflection_self_modifying"])
            self.assertEqual(task.payload["reflection_policy"], "self_mutation")
            self.assertEqual(task.payload["reflection_lock_namespace"], ".mmux/self-mutation")
            self.assertTrue(task.payload["reflection_requires_explicit_review"])

    def test_perform_reflection_handles_no_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            calls = []

            def fake_reflection(*_args, **_kwargs):
                calls.append(True)
                return cli.ReflectionResult((), "", "should-not-run", True)

            original = cli.invoke_reflection_adapter
            cli.invoke_reflection_adapter = fake_reflection
            try:
                outcome = perform_reflection(project, "claude")
            finally:
                cli.invoke_reflection_adapter = original

            self.assertTrue(outcome.adapter_ok)
            self.assertEqual(outcome.source_task_ids, ())
            self.assertEqual(outcome.promoted_ids, ())
            self.assertEqual(outcome.proposed_ids, ())
            self.assertEqual(calls, [], "reflection adapter must not run when there are no summaries")

    def test_auto_reflection_replenishes_before_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            insert_task_for_test(
                project,
                "completed task",
                "completed",
                {"resource": "src", "act_summary": "task #1 left a test gap"},
            )

            def fake_reflection(_project, _agent, _completions, **_kwargs):
                return cli.ReflectionResult(
                    (cli.ReflectionProposal("add missing regression test", "tests", "task #1"),),
                    ".mmux/runs/fake-reflect.log",
                    "ok",
                    True,
                )

            original = cli.invoke_reflection_adapter
            cli.invoke_reflection_adapter = fake_reflection
            try:
                outcome = maybe_replenish_reflection_task(
                    project,
                    agent="claude",
                    disabled=False,
                    remaining_seconds=MIN_EXECUTION_BUDGET_SECONDS + 20,
                    shutdown_grace_seconds=15,
                    timeout_seconds=5,
                )
                repeated = maybe_replenish_reflection_task(
                    project,
                    agent="claude",
                    disabled=False,
                    remaining_seconds=MIN_EXECUTION_BUDGET_SECONDS + 20,
                    shutdown_grace_seconds=15,
                    timeout_seconds=5,
                )
            finally:
                cli.invoke_reflection_adapter = original

            self.assertIsNotNone(outcome)
            assert outcome is not None
            self.assertEqual(len(outcome.promoted_ids), 1)
            self.assertIsNone(repeated)
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "pending")
            self.assertEqual(tasks[-1].payload["kind"], "reflection")

    def test_auto_reflection_advances_without_skipping_large_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            for index in range(12):
                insert_task_for_test(
                    project,
                    f"completed task {index + 1}",
                    "completed",
                    {"resource": "src", "act_summary": f"summary {index + 1}"},
                )

            calls = []

            def fake_reflection(_project, _agent, completions, **_kwargs):
                calls.append([int(item["id"]) for item in completions])
                return cli.ReflectionResult((), ".mmux/runs/fake-reflect.log", "ok", True)

            original = cli.invoke_reflection_adapter
            cli.invoke_reflection_adapter = fake_reflection
            try:
                first = maybe_replenish_reflection_task(
                    project,
                    agent="claude",
                    disabled=False,
                    remaining_seconds=MIN_EXECUTION_BUDGET_SECONDS + 20,
                    shutdown_grace_seconds=15,
                    timeout_seconds=5,
                )
                second = maybe_replenish_reflection_task(
                    project,
                    agent="claude",
                    disabled=False,
                    remaining_seconds=MIN_EXECUTION_BUDGET_SECONDS + 20,
                    shutdown_grace_seconds=15,
                    timeout_seconds=5,
                )
                third = maybe_replenish_reflection_task(
                    project,
                    agent="claude",
                    disabled=False,
                    remaining_seconds=MIN_EXECUTION_BUDGET_SECONDS + 20,
                    shutdown_grace_seconds=15,
                    timeout_seconds=5,
                )
            finally:
                cli.invoke_reflection_adapter = original

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertIsNone(third)
            self.assertEqual(calls, [list(range(1, 11)), [11, 12]])

    def test_requeue_promotes_proposed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task_id = insert_task_for_test(
                project,
                "vague reflection proposal",
                "proposed",
                {"resource": ".", "kind": "reflection"},
            )
            outcome = cli.requeue_task(project, task_id, reason="human approves the proposal")
            self.assertTrue(outcome.ok)
            self.assertEqual(outcome.from_status, "proposed")
            self.assertEqual(outcome.to_status, "pending")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "pending")
            self.assertEqual(tasks[-1].payload["requeued_from"], "proposed")

    def test_cmd_proposed_lists_only_proposals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            insert_task_for_test(project, "ordinary task", "pending", {"resource": "."})
            insert_task_for_test(
                project,
                "vague reflection proposal",
                "proposed",
                {
                    "resource": "src",
                    "kind": "reflection",
                    "reflection_evidence": "",
                    "reflection_from_tasks": [1],
                },
            )

            args = type("Args", (), {"project": str(project), "json": False})()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                cli.cmd_proposed(args)

            text = output.getvalue()
            self.assertIn("proposed tasks:", text)
            self.assertIn("vague reflection proposal", text)
            self.assertIn("approve: mmux task requeue", text)
            self.assertNotIn("ordinary task", text)

    def test_self_mutating_reflection_rejects_large_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            (project / "src" / "mmux").mkdir()
            (project / "src" / "mmux" / "cli.py").write_text("value = 1\n", encoding="utf-8")
            run(["git", "add", "src/mmux/cli.py"], cwd=project)
            run(["git", "commit", "-m", "add mmux file"], cwd=project)
            ensure_layout(project)
            task_id = insert_task_for_test(
                project,
                "self mutate",
                "pending",
                {
                    "resource": "src/mmux",
                    "kind": "reflection",
                    "reflection_self_modifying": True,
                    "reflection_diff_max_lines": 2,
                },
            )
            task = get_task(project, task_id)
            assert task is not None
            worktree = create_task_worktree(project, task, "codex")
            try:
                (worktree / "src" / "mmux" / "cli.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
                policy = check_diff_policy(project, worktree, "src/mmux", task)
            finally:
                cli.remove_git_worktree(project, worktree)

            self.assertFalse(policy.ok)
            self.assertEqual(policy.status, "self_mutation_diff_too_large")

    def test_self_mutating_reflection_requires_extra_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            (project / "src" / "mmux").mkdir()
            (project / "src" / "mmux" / "cli.py").write_text("value = 1\n", encoding="utf-8")
            run(["git", "add", "src/mmux/cli.py"], cwd=project)
            run(["git", "commit", "-m", "add mmux file"], cwd=project)
            ensure_layout(project)
            insert_task_for_test(
                project,
                "self mutate",
                "pending",
                {"resource": "src/mmux", "kind": "reflection", "reflection_self_modifying": True},
            )
            external = acquire_resource_lock(project, ".mmux/self-mutation", "claude", ttl_seconds=60)
            self.assertTrue(external.ok)
            driver = acquire_role(project, "driver", "codex", ttl_seconds=60)

            with contextlib.redirect_stdout(io.StringIO()):
                message = execute_driver_task(project, "codex", driver.generation)

            task = list_tasks(project)[-1]
            self.assertIn("requeued", message)
            self.assertIn(".mmux/self-mutation", message)
            self.assertEqual(task.status, "pending")

    def test_execute_reviewer_task_approves_patch_for_tester(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task_id = drive_task_to_review(project)

            message = approve_review_task(project)

            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertIn("awaiting_test", message)
            self.assertEqual(task.status, "awaiting_test")
            self.assertEqual(task.payload.get("review_decision"), "approve")

    def test_self_mutating_reflection_does_not_bypass_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            (project / "src" / "mmux").mkdir()
            (project / "src" / "mmux" / "cli.py").write_text("value = 1\n", encoding="utf-8")
            run(["git", "add", "src/mmux/cli.py"], cwd=project)
            run(["git", "commit", "-m", "add mmux file"], cwd=project)
            ensure_layout(project)
            task_id = insert_task_for_test(
                project,
                "self mutate",
                "awaiting_review",
                {
                    "resource": "src/mmux",
                    "kind": "reflection",
                    "reflection_self_modifying": True,
                    "driver_agent": "codex",
                },
            )
            task = get_task(project, task_id)
            assert task is not None
            worktree = create_task_worktree(project, task, "codex")
            (worktree / "src" / "mmux" / "cli.py").write_text("value = 2\n", encoding="utf-8")
            cli.update_task_payload(project, task_id, {"worktree": cli.relative_to_project(project, worktree)})
            reviewer = acquire_role(project, "reviewer", "claude", ttl_seconds=60)
            original = cli.invoke_reviewer_adapter

            def fake_review(_project, _worktree, _agent, _task, _generation, _resource, _changed_files, **kwargs):
                return cli.ReviewResult("invalid", ".mmux/runs/fake-review.log", "missing protocol", True)

            cli.invoke_reviewer_adapter = fake_review
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    message = execute_reviewer_task(project, "claude", reviewer.generation)
            finally:
                cli.invoke_reviewer_adapter = original

            reviewed = get_task(project, task_id)
            self.assertIsNotNone(reviewed)
            assert reviewed is not None
            self.assertIn("awaiting_review", message)
            self.assertIn("review_required", message)
            self.assertEqual(reviewed.status, "awaiting_review")
            self.assertTrue(reviewed.payload.get("review_required"))
            self.assertNotIn("review_bypassed", reviewed.payload)

    def test_execute_reviewer_task_requests_changes_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task_id = drive_task_to_review(project)
            reviewer = acquire_role(project, "reviewer", "claude", ttl_seconds=60)
            original = cli.invoke_reviewer_adapter

            def fake_review(_project, _worktree, _agent, _task, _generation, _resource, _changed_files, **kwargs):
                return cli.ReviewResult("request_changes", ".mmux/runs/fake-review.log", "needs narrower diff", True)

            cli.invoke_reviewer_adapter = fake_review
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    message = execute_reviewer_task(project, "claude", reviewer.generation)
            finally:
                cli.invoke_reviewer_adapter = original

            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertIn("pending", message)
            self.assertEqual(task.status, "pending")
            self.assertEqual(task.payload.get("review_decision"), "request_changes")

    def test_execute_reviewer_task_bypasses_invalid_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task_id = drive_task_to_review(project)
            reviewer = acquire_role(project, "reviewer", "claude", ttl_seconds=60)
            original = cli.invoke_reviewer_adapter

            def fake_review(_project, _worktree, _agent, _task, _generation, _resource, _changed_files, **kwargs):
                return cli.ReviewResult("invalid", ".mmux/runs/fake-review.log", "bad format", True)

            cli.invoke_reviewer_adapter = fake_review
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    message = execute_reviewer_task(project, "claude", reviewer.generation)
            finally:
                cli.invoke_reviewer_adapter = original

            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertIn("awaiting_test", message)
            self.assertEqual(task.status, "awaiting_test")
            self.assertEqual(task.payload.get("review_bypassed"), True)

    def test_execute_reviewer_task_bypasses_and_restores_modified_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task_id = drive_task_to_review(project, value="value = 4\n")
            reviewer = acquire_role(project, "reviewer", "claude", ttl_seconds=60)
            original = cli.invoke_reviewer_adapter

            def fake_review(_project, worktree, _agent, _task, _generation, _resource, _changed_files, **kwargs):
                (worktree / "src" / "app.py").write_text("value = 99\n", encoding="utf-8")
                return cli.ReviewResult("approve", ".mmux/runs/fake-review.log", "ok", True)

            cli.invoke_reviewer_adapter = fake_review
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    message = execute_reviewer_task(project, "claude", reviewer.generation)
            finally:
                cli.invoke_reviewer_adapter = original

            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertIn("awaiting_test", message)
            self.assertEqual(task.status, "awaiting_test")
            self.assertEqual(task.payload.get("review_bypassed"), True)
            self.assertEqual(task.payload.get("reviewer_modified_diff"), True)
            worktree_value = task.payload.get("worktree")
            self.assertIsInstance(worktree_value, str)
            self.assertEqual((project / str(worktree_value) / "src" / "app.py").read_text(encoding="utf-8"), "value = 4\n")

    def test_execute_reviewer_task_requeues_self_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            ensure_layout(project)
            task_id = drive_task_to_review(project)
            reviewer = acquire_role(project, "reviewer", "codex", ttl_seconds=60)

            with contextlib.redirect_stdout(io.StringIO()):
                message = execute_reviewer_task(project, "codex", reviewer.generation)

            task = get_task(project, task_id)
            self.assertIsNotNone(task)
            assert task is not None
            self.assertIn("cannot review own work", message)
            self.assertEqual(task.status, "awaiting_review")

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
                with patched_planner(), contextlib.redirect_stdout(io.StringIO()):
                    execute_driver_task(project, "codex", driver.generation)
            finally:
                cli.invoke_agent_adapter = original

            review_message = approve_review_task(project)
            self.assertIn("awaiting_test", review_message)
            tester = acquire_role(project, "tester", "claude", ttl_seconds=60)
            with patched_summarizer(), contextlib.redirect_stdout(io.StringIO()):
                message = execute_tester_task(project, "claude", tester.generation)

            self.assertIn("completed", message)
            self.assertEqual((project / "src" / "app.py").read_text(encoding="utf-8"), "value = 4\n")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "completed")
            self.assertEqual(tasks[-1].payload["patch_applied"], True)
            self.assertIn("tester_log", tasks[-1].payload)
            self.assertIn("act_summary", tasks[-1].payload)
            self.assertTrue(tasks[-1].payload["act_summary_adapter_ok"])
            self.assertEqual(tasks[-1].payload["act_summary_agent"], "claude")

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
                with patched_planner(), contextlib.redirect_stdout(io.StringIO()):
                    execute_driver_task(project, "codex", driver.generation)
            finally:
                cli.invoke_agent_adapter = original

            review_message = approve_review_task(project)
            self.assertIn("awaiting_test", review_message)
            tester = acquire_role(project, "tester", "claude", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                message = execute_tester_task(project, "claude", tester.generation)

            self.assertIn("failed", message)
            self.assertEqual((project / "src" / "app.py").read_text(encoding="utf-8"), "value = 1\n")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "failed")
            self.assertEqual(tasks[-1].payload["patch_applied"] if "patch_applied" in tasks[-1].payload else False, False)

    def test_execute_tester_task_tolerates_preexisting_unittest_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            (project / "tests").mkdir()
            (project / "tests" / "test_existing.py").write_text(
                "\n".join(
                    [
                        "import unittest",
                        "",
                        "class ExistingFailure(unittest.TestCase):",
                        "    def test_existing_failure(self):",
                        "        self.assertEqual(1, 2)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            run(["git", "add", "tests/test_existing.py"], cwd=project)
            run(["git", "commit", "-m", "add existing failing test"], cwd=project)
            ensure_layout(project)
            enqueue_task(project, "change src with existing failing suite", resource="src")
            driver = acquire_role(project, "driver", "codex", ttl_seconds=60)

            original = cli.invoke_agent_adapter

            def fake_adapter(_project, execution_root, _agent, _task, _generation, _resource, **kwargs):
                (execution_root / "src" / "app.py").write_text("value = 5\n", encoding="utf-8")
                return cli.AdapterResult(True, 0, ".mmux/runs/fake-driver.log", "ok")

            cli.invoke_agent_adapter = fake_adapter
            try:
                with patched_planner(), contextlib.redirect_stdout(io.StringIO()):
                    execute_driver_task(project, "codex", driver.generation)
            finally:
                cli.invoke_agent_adapter = original

            review_message = approve_review_task(project)
            self.assertIn("awaiting_test", review_message)
            tester = acquire_role(project, "tester", "claude", ttl_seconds=60)
            with patched_summarizer(), contextlib.redirect_stdout(io.StringIO()):
                message = execute_tester_task(project, "claude", tester.generation)

            self.assertIn("completed", message)
            self.assertEqual((project / "src" / "app.py").read_text(encoding="utf-8"), "value = 5\n")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "completed")
            failures = tasks[-1].payload.get("tester_baseline_failures")
            self.assertIsInstance(failures, list)
            self.assertIn("unittest", str(failures))

    def test_execute_tester_task_records_summarizer_failure_without_blocking_completion(self) -> None:
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
                with patched_planner(), contextlib.redirect_stdout(io.StringIO()):
                    execute_driver_task(project, "codex", driver.generation)
            finally:
                cli.invoke_agent_adapter = original

            approve_review_task(project)

            def fake_summary_fail(_project, _worktree, _agent, _task, _context, **kwargs):
                return cli.ActSummaryResult("", ".mmux/runs/fake-summary.log", "agent command timed out", False)

            tester = acquire_role(project, "tester", "claude", ttl_seconds=60)
            with patched_summarizer(fake_summary_fail), contextlib.redirect_stdout(io.StringIO()):
                message = execute_tester_task(project, "claude", tester.generation)

            self.assertIn("completed", message)
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "completed")
            self.assertEqual(tasks[-1].payload["patch_applied"], True)
            self.assertEqual(tasks[-1].payload["act_summary"], "")
            self.assertFalse(tasks[-1].payload["act_summary_adapter_ok"])
            self.assertIn("timed out", tasks[-1].payload["act_summary_failure"])

    def test_execute_tester_task_fails_unittest_regression_when_baseline_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init_git_project(project)
            (project / "src" / "__init__.py").write_text("", encoding="utf-8")
            (project / "tests").mkdir()
            (project / "tests" / "test_app.py").write_text(
                "\n".join(
                    [
                        "import unittest",
                        "from src import app",
                        "",
                        "class AppTest(unittest.TestCase):",
                        "    def test_value(self):",
                        "        self.assertEqual(app.value, 1)",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            run(["git", "add", "src/__init__.py", "tests/test_app.py"], cwd=project)
            run(["git", "commit", "-m", "add passing test"], cwd=project)
            ensure_layout(project)
            enqueue_task(project, "break tested behavior", resource="src")
            driver = acquire_role(project, "driver", "codex", ttl_seconds=60)

            original = cli.invoke_agent_adapter

            def fake_adapter(_project, execution_root, _agent, _task, _generation, _resource, **kwargs):
                (execution_root / "src" / "app.py").write_text("value = 9\n", encoding="utf-8")
                return cli.AdapterResult(True, 0, ".mmux/runs/fake-driver.log", "ok")

            cli.invoke_agent_adapter = fake_adapter
            try:
                with patched_planner(), contextlib.redirect_stdout(io.StringIO()):
                    execute_driver_task(project, "codex", driver.generation)
            finally:
                cli.invoke_agent_adapter = original

            review_message = approve_review_task(project)
            self.assertIn("awaiting_test", review_message)
            tester = acquire_role(project, "tester", "claude", ttl_seconds=60)
            with contextlib.redirect_stdout(io.StringIO()):
                message = execute_tester_task(project, "claude", tester.generation)

            self.assertIn("failed", message)
            self.assertEqual((project / "src" / "app.py").read_text(encoding="utf-8"), "value = 1\n")
            tasks = list_tasks(project)
            self.assertEqual(tasks[-1].status, "failed")
            self.assertNotIn("tester_baseline_failures", tasks[-1].payload)


if __name__ == "__main__":
    unittest.main()


class DemoSmokeTests(unittest.TestCase):
    def test_alpha_demo_runs_to_completion(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        demo = repo_root / "scripts" / "demo_alpha.py"
        env = dict(os.environ)
        # The demo creates throwaway git repos and commits; neutralize any
        # global git config (e.g. commit.gpgsign) so the smoke test is stable.
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env.setdefault("GIT_AUTHOR_NAME", "t")
        env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
        env.setdefault("GIT_COMMITTER_NAME", "t")
        env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
        env["PYTHONPATH"] = str(repo_root / "src")
        result = subprocess.run(
            [sys.executable, str(demo)],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"demo exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("final task status:           completed", result.stdout)
