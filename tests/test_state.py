import contextlib
import datetime as dt
import io
import sys
import tempfile
import unittest
from pathlib import Path

from mmux.cli import (
    acquire_role,
    acquire_resource_lock,
    build_agent_command,
    claim_next_task,
    database,
    enqueue_task,
    ensure_layout,
    format_utc,
    list_resource_locks,
    list_worker_heartbeats,
    release_role,
    release_resource_lock,
    resources_overlap,
    session_name,
    state_path,
    stream_agent_command,
    supervisor_role_plan,
    update_worker_heartbeat,
    utc_now_dt,
)


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


if __name__ == "__main__":
    unittest.main()
