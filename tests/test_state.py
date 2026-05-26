import datetime as dt
import tempfile
import unittest
from pathlib import Path

from mmux.cli import (
    acquire_role,
    connect,
    ensure_layout,
    format_utc,
    list_worker_heartbeats,
    release_role,
    session_name,
    state_path,
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
            with connect(project) as db:
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


if __name__ == "__main__":
    unittest.main()
