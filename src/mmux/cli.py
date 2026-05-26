from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import select
import sqlite3
import subprocess
import sys
import time
from typing import Iterable, Optional


ROLES = ("driver", "reviewer", "scout", "tester", "summarizer")
AGENTS = ("codex", "claude")
ROLE_LEASE_TTL_SECONDS = 2 * 60
RESOURCE_LOCK_TTL_SECONDS = 10 * 60
WORKER_REFRESH_SECONDS = 5
SUPERVISOR_TICK_SECONDS = 30
SUPERVISOR_ASSIGNMENT_SECONDS = 5 * 60
AGENT_TIMEOUT_SECONDS = 20 * 60
ASSIGNMENT_ROLE_PAIRS = (
    ("scout", "reviewer"),
    ("driver", "tester"),
    ("summarizer", "scout"),
    ("reviewer", "driver"),
    ("tester", "summarizer"),
)


@dataclass(frozen=True)
class LeaseOutcome:
    ok: bool
    role: str
    holder: str
    generation: int
    lease_until: str
    status: str
    reason: str = ""


@dataclass(frozen=True)
class LockOutcome:
    ok: bool
    resource: str
    holder: str
    mode: str
    lease_until: str
    status: str
    reason: str = ""
    conflict_resource: str = ""


@dataclass(frozen=True)
class TaskRecord:
    id: int
    title: str
    status: str
    payload: dict[str, object]
    claimed_by: str = ""
    claimed_role: str = ""
    claimed_generation: int = 0


@dataclass(frozen=True)
class AdapterResult:
    ok: bool
    returncode: int
    log_file: str
    message: str


@dataclass(frozen=True)
class DiffPolicyResult:
    ok: bool
    status: str
    changed_files: tuple[str, ...]
    reason: str = ""


def utc_now_dt() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def format_utc(moment: dt.datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def utc_now() -> str:
    return format_utc(utc_now_dt())


def parse_utc(value: str) -> dt.datetime:
    normalized = value
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0)


def supervisor_assignment_slot(now: Optional[dt.datetime] = None) -> int:
    moment = now or utc_now_dt()
    return int(moment.timestamp() // SUPERVISOR_ASSIGNMENT_SECONDS)


def supervisor_role_plan(now: Optional[dt.datetime] = None) -> tuple[tuple[str, str], ...]:
    slot = supervisor_assignment_slot(now)
    roles = ASSIGNMENT_ROLE_PAIRS[slot % len(ASSIGNMENT_ROLE_PAIRS)]
    agents = AGENTS if slot % 2 == 0 else tuple(reversed(AGENTS))
    return tuple(zip(roles, agents))


def seconds_until_next_assignment(now: Optional[dt.datetime] = None) -> int:
    moment = now or utc_now_dt()
    slot = supervisor_assignment_slot(moment)
    next_boundary = (slot + 1) * SUPERVISOR_ASSIGNMENT_SECONDS
    return max(1, int(next_boundary - moment.timestamp()))


def resolve_project(path: str) -> Path:
    return Path(path).expanduser().resolve()


def mmux_dir(project: Path) -> Path:
    return project / ".mmux"


def state_path(project: Path) -> Path:
    return mmux_dir(project) / "state.db"


def config_path(project: Path) -> Path:
    return mmux_dir(project) / "config.json"


def log_path(project: Path) -> Path:
    return mmux_dir(project) / "logs" / "supervisor.log"


def session_name(project: Path) -> str:
    digest = hashlib.sha1(str(project).encode("utf-8")).hexdigest()[:8]
    safe_name = "".join(ch if ch.isalnum() else "-" for ch in project.name.lower()).strip("-")
    return f"mmux-{safe_name or 'project'}-{digest}"


def connect(project: Path) -> sqlite3.Connection:
    return sqlite3.connect(state_path(project), timeout=30)


@contextmanager
def database(project: Path) -> Iterable[sqlite3.Connection]:
    db = connect(project)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row[1] for row in db.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        db.execute(f"alter table {table} add column {column} {declaration}")


def apply_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        create table if not exists meta (
          key text primary key,
          value text not null
        );

        create table if not exists tasks (
          id integer primary key autoincrement,
          title text not null,
          status text not null,
          payload text not null default '{}',
          created_at text not null,
          updated_at text not null,
          claimed_by text not null default '',
          claimed_role text not null default '',
          claimed_generation integer not null default 0,
          claimed_at text not null default '',
          finished_at text not null default '',
          last_error text not null default ''
        );

        create table if not exists role_leases (
          role text primary key,
          holder text not null,
          generation integer not null,
          lease_until text not null,
          status text not null
        );

        create table if not exists worker_heartbeats (
          agent text primary key,
          pid integer not null,
          status text not null,
          role text,
          generation integer,
          updated_at text not null
        );

        create table if not exists resource_locks (
          resource text primary key,
          holder text not null,
          mode text not null,
          lease_until text not null,
          role text not null default '',
          role_generation integer not null default 0,
          status text not null default 'active'
        );

        create table if not exists events (
          id integer primary key autoincrement,
          kind text not null,
          payload text not null,
          created_at text not null
        );

        create table if not exists frontier_items (
          id integer primary key autoincrement,
          title text not null,
          status text not null,
          evidence text not null,
          score integer not null default 0,
          created_at text not null
        );
        """
    )
    ensure_column(db, "tasks", "claimed_by", "text not null default ''")
    ensure_column(db, "tasks", "claimed_role", "text not null default ''")
    ensure_column(db, "tasks", "claimed_generation", "integer not null default 0")
    ensure_column(db, "tasks", "claimed_at", "text not null default ''")
    ensure_column(db, "tasks", "finished_at", "text not null default ''")
    ensure_column(db, "tasks", "last_error", "text not null default ''")
    ensure_column(db, "resource_locks", "role", "text not null default ''")
    ensure_column(db, "resource_locks", "role_generation", "integer not null default 0")
    ensure_column(db, "resource_locks", "status", "text not null default 'active'")


def ensure_existing_schema(project: Path) -> None:
    require_project_state(project)
    with database(project) as db:
        apply_schema(db)


def record_event(db: sqlite3.Connection, kind: str, payload: dict[str, object]) -> None:
    db.execute(
        "insert into events(kind, payload, created_at) values (?, ?, ?)",
        (kind, json.dumps(payload, sort_keys=True), utc_now()),
    )


def ensure_layout(project: Path, task: str = "") -> None:
    root = mmux_dir(project)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    (root / "worktrees").mkdir(parents=True, exist_ok=True)
    (root / "inbox").mkdir(parents=True, exist_ok=True)

    config = {
        "version": 1,
        "project_root": str(project),
        "session_name": session_name(project),
        "created_at": utc_now(),
        "task": task,
        "agents": list(AGENTS),
        "roles": list(ROLES),
        "supervisor": {
            "deterministic": True,
            "llm_referee": False,
        },
        "timers": {
            "slice_seconds": 25 * 60,
            "idle_timeout_seconds": 5 * 60,
            "checkpoint_seconds": 30 * 60,
        },
    }

    if not config_path(project).exists():
        config_path(project).write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    with database(project) as db:
        apply_schema(db)
        db.execute(
            "insert or replace into meta(key, value) values (?, ?)",
            ("project_root", str(project)),
        )
        db.execute(
            "insert or replace into meta(key, value) values (?, ?)",
            ("session_name", session_name(project)),
        )
        if task:
            enqueue_task_db(db, task, resource=".")
        record_event(db, "init", {"task": task})


def validate_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")


def validate_agent(agent: str) -> None:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent: {agent}")


def validate_lock_mode(mode: str) -> None:
    if mode != "write":
        raise ValueError(f"unknown lock mode: {mode}")


def decode_payload(payload: str) -> dict[str, object]:
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def encode_payload(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True)


def normalize_resource(project: Path, resource: str) -> str:
    value = resource.strip()
    if not value:
        raise ValueError("resource must not be empty")
    raw_path = Path(value)
    candidate = raw_path if raw_path.is_absolute() else project / raw_path
    resolved_project = project.resolve()
    resolved_candidate = candidate.resolve(strict=False)
    try:
        relative = resolved_candidate.relative_to(resolved_project)
    except ValueError as exc:
        raise ValueError(f"resource escapes project: {resource}") from exc
    normalized = relative.as_posix()
    return normalized or "."


def resource_parts(resource: str) -> tuple[str, ...]:
    if resource == ".":
        return ()
    return tuple(part for part in PurePosixPath(resource).parts if part and part != ".")


def resource_is_prefix(prefix: str, resource: str) -> bool:
    prefix_parts = resource_parts(prefix)
    resource_parts_value = resource_parts(resource)
    return len(prefix_parts) <= len(resource_parts_value) and resource_parts_value[: len(prefix_parts)] == prefix_parts


def resources_overlap(left: str, right: str) -> bool:
    return resource_is_prefix(left, right) or resource_is_prefix(right, left)


def resource_contains(resource: str, path: str) -> bool:
    return resource_is_prefix(resource, path)


def is_protected_path(path: str) -> bool:
    return (
        path == ".git"
        or path.startswith(".git/")
        or path == ".mmux"
        or path.startswith(".mmux/")
        or path == ".env"
        or path.startswith(".env.")
    )


def expire_role_leases_db(db: sqlite3.Connection, now: dt.datetime) -> None:
    db.execute(
        "update role_leases set status = ? where status = ? and lease_until <= ?",
        ("expired", "active", format_utc(now)),
    )


def acquire_role(
    project: Path,
    role: str,
    holder: str,
    ttl_seconds: int = ROLE_LEASE_TTL_SECONDS,
    *,
    renew_if_same: bool = False,
) -> LeaseOutcome:
    validate_role(role)
    validate_agent(holder)
    now = utc_now_dt()
    lease_until = format_utc(now + dt.timedelta(seconds=ttl_seconds))

    db = connect(project)
    try:
        db.execute("begin immediate")
        apply_schema(db)
        expire_role_leases_db(db, now)
        row = db.execute(
            "select holder, generation, lease_until, status from role_leases where role = ?",
            (role,),
        ).fetchone()
        if row and row[3] == "active":
            current_holder, generation, current_until, _status = row
            if renew_if_same and current_holder == holder:
                db.execute(
                    "update role_leases set lease_until = ?, status = ? where role = ?",
                    (lease_until, "active", role),
                )
                record_event(
                    db,
                    "role_renewed",
                    {
                        "role": role,
                        "holder": holder,
                        "generation": generation,
                        "lease_until": lease_until,
                    },
                )
                db.commit()
                return LeaseOutcome(True, role, holder, generation, lease_until, "renewed")
            db.rollback()
            return LeaseOutcome(
                False,
                role,
                current_holder,
                generation,
                current_until,
                "conflict",
                f"{role} is held by {current_holder}",
            )

        generation = (row[1] if row else 0) + 1
        db.execute(
            """
            insert into role_leases(role, holder, generation, lease_until, status)
            values (?, ?, ?, ?, ?)
            on conflict(role) do update set
              holder = excluded.holder,
              generation = excluded.generation,
              lease_until = excluded.lease_until,
              status = excluded.status
            """,
            (role, holder, generation, lease_until, "active"),
        )
        record_event(
            db,
            "role_acquired",
            {
                "role": role,
                "holder": holder,
                "generation": generation,
                "lease_until": lease_until,
            },
        )
        db.commit()
        return LeaseOutcome(True, role, holder, generation, lease_until, "acquired")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def release_role(
    project: Path,
    role: str,
    *,
    holder: Optional[str] = None,
    generation: Optional[int] = None,
) -> LeaseOutcome:
    validate_role(role)
    if holder is not None:
        validate_agent(holder)

    db = connect(project)
    try:
        db.execute("begin immediate")
        apply_schema(db)
        expire_role_leases_db(db, utc_now_dt())
        row = db.execute(
            "select holder, generation, lease_until, status from role_leases where role = ?",
            (role,),
        ).fetchone()
        if not row:
            db.rollback()
            return LeaseOutcome(False, role, holder or "", 0, "", "missing", f"{role} is not leased")

        current_holder, current_generation, lease_until, status = row
        if status != "active":
            db.rollback()
            return LeaseOutcome(
                False,
                role,
                current_holder,
                current_generation,
                lease_until,
                status,
                f"{role} is not active",
            )
        if holder is not None and current_holder != holder:
            db.rollback()
            return LeaseOutcome(
                False,
                role,
                current_holder,
                current_generation,
                lease_until,
                "conflict",
                f"{role} is held by {current_holder}",
            )
        if generation is not None and current_generation != generation:
            db.rollback()
            return LeaseOutcome(
                False,
                role,
                current_holder,
                current_generation,
                lease_until,
                "stale",
                f"{role} generation is {current_generation}",
            )

        db.execute("update role_leases set status = ? where role = ?", ("released", role))
        record_event(
            db,
            "role_released",
            {
                "role": role,
                "holder": current_holder,
                "generation": current_generation,
            },
        )
        db.commit()
        return LeaseOutcome(True, role, current_holder, current_generation, lease_until, "released")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def list_role_leases(project: Path) -> list[tuple[str, str, int, str, str]]:
    with database(project) as db:
        apply_schema(db)
        expire_role_leases_db(db, utc_now_dt())
        return db.execute(
            "select role, holder, generation, lease_until, status from role_leases order by role"
        ).fetchall()


def active_roles_for_agent(project: Path, agent: str) -> list[tuple[str, int, str]]:
    validate_agent(agent)
    with database(project) as db:
        apply_schema(db)
        expire_role_leases_db(db, utc_now_dt())
        return db.execute(
            """
            select role, generation, lease_until
            from role_leases
            where holder = ? and status = ?
            order by role
            """,
            (agent, "active"),
        ).fetchall()


def active_role_matches_db(
    db: sqlite3.Connection,
    role: str,
    holder: str,
    generation: Optional[int] = None,
) -> bool:
    row = db.execute(
        """
        select generation
        from role_leases
        where role = ? and holder = ? and status = ?
        """,
        (role, holder, "active"),
    ).fetchone()
    if not row:
        return False
    return generation is None or row[0] == generation


def expire_resource_locks_db(db: sqlite3.Connection, now: dt.datetime) -> None:
    db.execute(
        "update resource_locks set status = ? where status = ? and lease_until <= ?",
        ("expired", "active", format_utc(now)),
    )


def acquire_resource_lock(
    project: Path,
    resource: str,
    holder: str,
    ttl_seconds: int = RESOURCE_LOCK_TTL_SECONDS,
    *,
    mode: str = "write",
    role: str = "",
    role_generation: int = 0,
    renew_if_same: bool = False,
) -> LockOutcome:
    validate_agent(holder)
    validate_lock_mode(mode)
    if role:
        validate_role(role)
    normalized = normalize_resource(project, resource)
    now = utc_now_dt()
    lease_until = format_utc(now + dt.timedelta(seconds=ttl_seconds))

    db = connect(project)
    try:
        db.execute("begin immediate")
        apply_schema(db)
        expire_role_leases_db(db, now)
        expire_resource_locks_db(db, now)
        if role and role_generation <= 0:
            db.rollback()
            return LockOutcome(
                False,
                normalized,
                holder,
                mode,
                "",
                "stale_role",
                f"{holder} must provide a positive {role} generation",
            )
        if role and not active_role_matches_db(db, role, holder, role_generation):
            db.rollback()
            return LockOutcome(
                False,
                normalized,
                holder,
                mode,
                "",
                "stale_role",
                f"{holder} does not hold active {role} lease generation {role_generation}",
            )

        locks = db.execute(
            """
            select resource, holder, mode, lease_until, role, role_generation
            from resource_locks
            where status = ?
            order by resource
            """,
            ("active",),
        ).fetchall()
        for current_resource, current_holder, current_mode, current_until, current_role, current_generation in locks:
            if not resources_overlap(normalized, current_resource):
                continue
            if current_holder == holder and current_resource != normalized:
                continue
            if current_holder == holder and renew_if_same:
                db.execute(
                    """
                    update resource_locks
                    set lease_until = ?, mode = ?, role = ?, role_generation = ?, status = ?
                    where resource = ?
                    """,
                    (
                        lease_until,
                        mode,
                        role or current_role,
                        role_generation or current_generation,
                        "active",
                        current_resource,
                    ),
                )
                record_event(
                    db,
                    "resource_lock_renewed",
                    {
                        "resource": current_resource,
                        "holder": holder,
                        "mode": mode,
                        "lease_until": lease_until,
                    },
                )
                db.commit()
                return LockOutcome(True, current_resource, holder, mode, lease_until, "renewed")
            db.rollback()
            return LockOutcome(
                False,
                normalized,
                current_holder,
                current_mode,
                current_until,
                "conflict",
                f"{normalized} overlaps active lock {current_resource}",
                conflict_resource=current_resource,
            )

        db.execute(
            """
            insert into resource_locks(resource, holder, mode, lease_until, role, role_generation, status)
            values (?, ?, ?, ?, ?, ?, ?)
            on conflict(resource) do update set
              holder = excluded.holder,
              mode = excluded.mode,
              lease_until = excluded.lease_until,
              role = excluded.role,
              role_generation = excluded.role_generation,
              status = excluded.status
            """,
            (normalized, holder, mode, lease_until, role, role_generation, "active"),
        )
        record_event(
            db,
            "resource_lock_acquired",
            {
                "resource": normalized,
                "holder": holder,
                "mode": mode,
                "role": role,
                "role_generation": role_generation,
                "lease_until": lease_until,
            },
        )
        db.commit()
        return LockOutcome(True, normalized, holder, mode, lease_until, "acquired")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def release_resource_lock(
    project: Path,
    resource: str,
    *,
    holder: Optional[str] = None,
) -> LockOutcome:
    if holder is not None:
        validate_agent(holder)
    normalized = normalize_resource(project, resource)

    db = connect(project)
    try:
        db.execute("begin immediate")
        apply_schema(db)
        expire_resource_locks_db(db, utc_now_dt())
        row = db.execute(
            "select holder, mode, lease_until, status from resource_locks where resource = ?",
            (normalized,),
        ).fetchone()
        if not row:
            db.rollback()
            return LockOutcome(False, normalized, holder or "", "", "", "missing", f"{normalized} is not locked")

        current_holder, mode, lease_until, status = row
        if status != "active":
            db.rollback()
            return LockOutcome(
                False,
                normalized,
                current_holder,
                mode,
                lease_until,
                status,
                f"{normalized} is not active",
            )
        if holder is not None and holder != current_holder:
            db.rollback()
            return LockOutcome(
                False,
                normalized,
                current_holder,
                mode,
                lease_until,
                "conflict",
                f"{normalized} is held by {current_holder}",
            )

        db.execute("update resource_locks set status = ? where resource = ?", ("released", normalized))
        record_event(
            db,
            "resource_lock_released",
            {
                "resource": normalized,
                "holder": current_holder,
                "mode": mode,
            },
        )
        db.commit()
        return LockOutcome(True, normalized, current_holder, mode, lease_until, "released")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def list_resource_locks(project: Path) -> list[tuple[str, str, str, str, str, int, str]]:
    with database(project) as db:
        apply_schema(db)
        expire_resource_locks_db(db, utc_now_dt())
        return db.execute(
            """
            select resource, holder, mode, lease_until, role, role_generation, status
            from resource_locks
            order by resource
            """
        ).fetchall()


def update_worker_heartbeat(
    project: Path,
    agent: str,
    status: str,
    *,
    role: Optional[str] = None,
    generation: Optional[int] = None,
) -> None:
    validate_agent(agent)
    with database(project) as db:
        apply_schema(db)
        db.execute(
            """
            insert into worker_heartbeats(agent, pid, status, role, generation, updated_at)
            values (?, ?, ?, ?, ?, ?)
            on conflict(agent) do update set
              pid = excluded.pid,
              status = excluded.status,
              role = excluded.role,
              generation = excluded.generation,
              updated_at = excluded.updated_at
            """,
            (agent, os.getpid(), status, role, generation, utc_now()),
        )


def list_worker_heartbeats(project: Path) -> list[tuple[str, int, str, Optional[str], Optional[int], str]]:
    with database(project) as db:
        apply_schema(db)
        return db.execute(
            "select agent, pid, status, role, generation, updated_at from worker_heartbeats order by agent"
        ).fetchall()


def enqueue_task_db(db: sqlite3.Connection, title: str, *, resource: str = ".") -> int:
    now = utc_now()
    payload = encode_payload({"resource": resource})
    cursor = db.execute(
        """
        insert into tasks(title, status, payload, created_at, updated_at)
        values (?, ?, ?, ?, ?)
        """,
        (title, "pending", payload, now, now),
    )
    task_id = int(cursor.lastrowid)
    record_event(db, "task_added", {"id": task_id, "title": title, "resource": resource})
    return task_id


def enqueue_task(project: Path, title: str, *, resource: str = ".") -> int:
    normalized = normalize_resource(project, resource)
    with database(project) as db:
        apply_schema(db)
        return enqueue_task_db(db, title, resource=normalized)


def task_from_row(row: tuple[object, ...]) -> TaskRecord:
    return TaskRecord(
        id=int(row[0]),
        title=str(row[1]),
        status=str(row[2]),
        payload=decode_payload(str(row[3])),
        claimed_by=str(row[4] or ""),
        claimed_role=str(row[5] or ""),
        claimed_generation=int(row[6] or 0),
    )


def list_tasks(project: Path) -> list[TaskRecord]:
    with database(project) as db:
        apply_schema(db)
        rows = db.execute(
            """
            select id, title, status, payload, claimed_by, claimed_role, claimed_generation
            from tasks
            order by id
            """
        ).fetchall()
        return [task_from_row(row) for row in rows]


def claim_next_task(project: Path, agent: str, role: str, generation: int) -> Optional[TaskRecord]:
    validate_agent(agent)
    validate_role(role)
    db = connect(project)
    try:
        db.execute("begin immediate")
        apply_schema(db)
        expire_role_leases_db(db, utc_now_dt())
        if not active_role_matches_db(db, role, agent, generation):
            db.rollback()
            return None
        row = db.execute(
            """
            select id, title, status, payload, claimed_by, claimed_role, claimed_generation
            from tasks
            where status = ?
            order by id
            limit 1
            """,
            ("pending",),
        ).fetchone()
        if not row:
            db.rollback()
            return None
        task = task_from_row(row)
        now = utc_now()
        db.execute(
            """
            update tasks
            set status = ?, claimed_by = ?, claimed_role = ?, claimed_generation = ?,
                claimed_at = ?, updated_at = ?, last_error = ''
            where id = ? and status = ?
            """,
            ("running", agent, role, generation, now, now, task.id, "pending"),
        )
        record_event(
            db,
            "task_claimed",
            {"id": task.id, "agent": agent, "role": role, "generation": generation},
        )
        db.commit()
        return TaskRecord(task.id, task.title, "running", task.payload, agent, role, generation)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def release_task_claim(project: Path, task_id: int, reason: str) -> None:
    with database(project) as db:
        apply_schema(db)
        now = utc_now()
        db.execute(
            """
            update tasks
            set status = ?, claimed_by = '', claimed_role = '', claimed_generation = 0,
                claimed_at = '', updated_at = ?, last_error = ?
            where id = ? and status = ?
            """,
            ("pending", now, reason, task_id, "running"),
        )
        record_event(db, "task_requeued", {"id": task_id, "reason": reason})


def update_task_payload(project: Path, task_id: int, updates: dict[str, object]) -> None:
    with database(project) as db:
        apply_schema(db)
        row = db.execute("select payload from tasks where id = ?", (task_id,)).fetchone()
        payload = decode_payload(row[0]) if row else {}
        payload.update(updates)
        db.execute(
            "update tasks set payload = ?, updated_at = ? where id = ?",
            (encode_payload(payload), utc_now(), task_id),
        )
        record_event(db, "task_payload_updated", {"id": task_id, "keys": sorted(updates)})


def finish_task(
    project: Path,
    task_id: int,
    status: str,
    *,
    message: str = "",
    log_file: str = "",
    payload_updates: Optional[dict[str, object]] = None,
) -> None:
    if status not in {"completed", "failed", "no_change", "rejected"}:
        raise ValueError(f"unknown task terminal status: {status}")
    with database(project) as db:
        apply_schema(db)
        row = db.execute("select payload from tasks where id = ?", (task_id,)).fetchone()
        payload = decode_payload(row[0]) if row else {}
        if payload_updates:
            payload.update(payload_updates)
        if log_file:
            payload["last_run_log"] = log_file
        if message:
            payload["last_message"] = message[:2000]
        now = utc_now()
        db.execute(
            """
            update tasks
            set status = ?, payload = ?, updated_at = ?, finished_at = ?, last_error = ?
            where id = ?
            """,
            (status, encode_payload(payload), now, now, "" if status == "completed" else message, task_id),
        )
        record_event(db, "task_finished", {"id": task_id, "status": status, "log_file": log_file})


def write_log(project: Path, message: str) -> None:
    path = log_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utc_now()} {message}\n")


def require_project_state(project: Path) -> None:
    if not state_path(project).exists():
        raise SystemExit(f"mmux is not initialized in {project}. Run `mmux init {project}` first.")


def run(cmd: Iterable[str], *, cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def run_with_input(cmd: Iterable[str], *, cwd: Path, input_text: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd),
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def run_log_path(project: Path, agent: str, task_id: int) -> Path:
    stamp = utc_now().replace(":", "").replace("+", "Z")
    path = mmux_dir(project) / "runs" / f"{stamp}-{agent}-task-{task_id}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def relative_to_project(project: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(project.resolve()).as_posix()
    except ValueError:
        return str(path)


def worktree_root(project: Path) -> Path:
    root = mmux_dir(project) / "worktrees"
    root.mkdir(parents=True, exist_ok=True)
    return root


def create_task_worktree(project: Path, task: TaskRecord, agent: str) -> Path:
    validate_agent(agent)
    run(["git", "rev-parse", "--show-toplevel"], cwd=project)
    run(["git", "rev-parse", "--verify", "HEAD"], cwd=project)
    stamp = utc_now().replace(":", "").replace("+", "Z")
    path = worktree_root(project) / f"task-{task.id}-{agent}-{os.getpid()}-{stamp}"
    run(["git", "worktree", "add", "--detach", str(path), "HEAD"], cwd=project)
    return path


def split_nul_paths(output: str) -> list[str]:
    return [part for part in output.split("\0") if part]


def collect_changed_files(worktree: Path) -> list[str]:
    tracked = split_nul_paths(run(["git", "diff", "--name-only", "-z", "HEAD", "--"], cwd=worktree).stdout)
    untracked = split_nul_paths(
        run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=worktree).stdout
    )
    return sorted(set(tracked + untracked))


def check_diff_policy(project: Path, worktree: Path, resource: str) -> DiffPolicyResult:
    normalized_resource = normalize_resource(project, resource)
    changed_files = tuple(collect_changed_files(worktree))
    if not changed_files:
        return DiffPolicyResult(True, "no_change", changed_files, "no file changes")

    protected = [path for path in changed_files if is_protected_path(path)]
    if protected:
        return DiffPolicyResult(
            False,
            "protected_violation",
            changed_files,
            "protected paths changed: " + ", ".join(protected),
        )

    outside = [path for path in changed_files if not resource_contains(normalized_resource, path)]
    if outside:
        return DiffPolicyResult(
            False,
            "resource_violation",
            changed_files,
            f"changes outside locked resource {normalized_resource}: " + ", ".join(outside),
        )

    return DiffPolicyResult(True, "ok", changed_files)


def export_worktree_patch(worktree: Path) -> str:
    run(["git", "add", "-A"], cwd=worktree)
    return run(["git", "diff", "--cached", "--binary", "HEAD"], cwd=worktree).stdout


def main_worktree_is_clean(project: Path) -> bool:
    return run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=project).stdout.strip() == ""


def apply_worktree_patch(project: Path, patch: str) -> None:
    if not patch.strip():
        return
    if not main_worktree_is_clean(project):
        raise RuntimeError("main worktree has tracked changes; refusing to apply task patch")
    run_with_input(["git", "apply", "--binary", "-"], cwd=project, input_text=patch)


def build_agent_prompt(agent: str, task: TaskRecord, role_generation: int, resource: str) -> str:
    return "\n".join(
        [
            f"You are the {agent} worker running under mmux.",
            "",
            "The mmux supervisor is deterministic and has already granted you:",
            f"- role: driver",
            f"- role generation: {role_generation}",
            f"- resource lock: {resource}",
            "- execution root: isolated git worktree",
            "",
            f"Task #{task.id}: {task.title}",
            "",
            "Work only inside this repository. Keep the change scoped to the task and the locked resource.",
            "A deterministic diff policy gate will reject changes outside the locked resource.",
            "Do not start a long-running service. Run focused tests or checks when practical.",
            "When done, leave a concise final summary including changed files and verification.",
        ]
    )


def build_agent_command(agent: str, project: Path, prompt: str, output_file: Path) -> list[str]:
    if agent == "codex":
        return [
            "codex",
            "exec",
            "--cd",
            str(project),
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            "--output-last-message",
            str(output_file),
            prompt,
        ]
    if agent == "claude":
        return [
            "claude",
            "-p",
            "--permission-mode",
            "acceptEdits",
            "--output-format",
            "text",
            prompt,
        ]
    raise ValueError(f"unknown agent: {agent}")


def stream_agent_command(
    project: Path,
    cmd: list[str],
    log_file: Path,
    *,
    timeout_seconds: int = AGENT_TIMEOUT_SECONDS,
) -> AdapterResult:
    started = time.monotonic()
    command_text = " ".join(sh_quote(part) for part in cmd)
    with log_file.open("w", encoding="utf-8") as handle:
        handle.write(f"{utc_now()} command: {command_text}\n\n")
        handle.flush()
        print(f"running: {command_text}")
        sys.stdout.flush()
        process = subprocess.Popen(
            cmd,
            cwd=str(project),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        timed_out = False
        while True:
            if time.monotonic() - started > timeout_seconds:
                timed_out = True
                process.kill()
            ready, _write, _error = select.select([process.stdout], [], [], 1)
            if ready:
                chunk = process.stdout.readline()
                if chunk:
                    print(chunk, end="")
                    handle.write(chunk)
                    handle.flush()
                    sys.stdout.flush()
            returncode = process.poll()
            if returncode is not None:
                rest = process.stdout.read()
                if rest:
                    print(rest, end="")
                    handle.write(rest)
                break
            if timed_out:
                process.wait()
                break
        process.stdout.close()
        returncode = process.returncode if process.returncode is not None else 124
        if timed_out:
            message = f"agent command timed out after {timeout_seconds}s"
            handle.write(f"\n{utc_now()} {message}\n")
            return AdapterResult(False, 124, relative_to_project(project, log_file), message)
        message = f"agent command exited {returncode}"
        handle.write(f"\n{utc_now()} {message}\n")
        return AdapterResult(returncode == 0, returncode, relative_to_project(project, log_file), message)


def invoke_agent_adapter(
    project: Path,
    execution_root: Path,
    agent: str,
    task: TaskRecord,
    role_generation: int,
    resource: str,
) -> AdapterResult:
    prompt = build_agent_prompt(agent, task, role_generation, resource)
    log_file = run_log_path(project, agent, task.id)
    output_file = log_file.with_suffix(".last-message.txt")
    cmd = build_agent_command(agent, execution_root, prompt, output_file)
    result = stream_agent_command(execution_root, cmd, log_file)
    if output_file.exists():
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write("\n--- last message ---\n")
            handle.write(output_file.read_text(encoding="utf-8", errors="replace"))
            handle.write("\n")
    return result


def tmux_has_session(name: str) -> bool:
    result = run(["tmux", "has-session", "-t", name], check=False)
    return result.returncode == 0


def module_command(project: Path, subcommand: str, *args: str) -> str:
    package_root = Path(__file__).resolve().parents[1]
    python = sh_quote(sys.executable)
    pieces = [
        f"PYTHONPATH={sh_quote(str(package_root))}",
        python,
        "-m",
        "mmux.cli",
        subcommand,
        "--project",
        sh_quote(str(project)),
    ]
    pieces.extend(sh_quote(arg) for arg in args)
    return " ".join(pieces)


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def cmd_init(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    if not project.exists():
        raise SystemExit(f"Project path does not exist: {project}")
    ensure_layout(project, args.task or "")
    print(f"Initialized mmux at {mmux_dir(project)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    checks = [
        ("tmux", "required"),
        ("codex", "worker"),
        ("claude", "worker"),
        ("git", "required"),
    ]
    failed_required = False
    for name, level in checks:
        found = shutil.which(name)
        status = "ok" if found else "missing"
        print(f"{name:8} {status:8} {found or ''}")
        if level == "required" and not found:
            failed_required = True
    return 1 if failed_required else 0


def cmd_status(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_existing_schema(project)
    with database(project) as db:
        apply_schema(db)
        expire_role_leases_db(db, utc_now_dt())
        expire_resource_locks_db(db, utc_now_dt())
        tasks = db.execute("select status, count(*) from tasks group by status").fetchall()
        leases = db.execute(
            "select role, holder, generation, lease_until, status from role_leases order by role"
        ).fetchall()
        locks = db.execute(
            """
            select resource, holder, mode, lease_until, role, role_generation, status
            from resource_locks
            order by resource
            """
        ).fetchall()
        heartbeats = db.execute(
            "select agent, pid, status, role, generation, updated_at from worker_heartbeats order by agent"
        ).fetchall()
        events = db.execute("select kind, created_at from events order by id desc limit 5").fetchall()

    print(f"project: {project}")
    print(f"session: {session_name(project)}")
    print("tasks:")
    if tasks:
        for status, count in tasks:
            print(f"  {status}: {count}")
    else:
        print("  none")
    print("role leases:")
    if leases:
        for role, holder, generation, lease_until, status in leases:
            print(f"  {role}: {holder} gen={generation} until={lease_until} status={status}")
    else:
        print("  none")
    print("resource locks:")
    if locks:
        for resource, holder, mode, lease_until, role, role_generation, status in locks:
            role_text = f" role={role} gen={role_generation}" if role else ""
            print(f"  {resource}: {holder} mode={mode}{role_text} until={lease_until} status={status}")
    else:
        print("  none")
    print("workers:")
    if heartbeats:
        for agent, pid, status, role, generation, updated_at in heartbeats:
            lease = f"{role} gen={generation}" if role else "none"
            print(f"  {agent}: pid={pid} status={status} role={lease} updated={updated_at}")
    else:
        print("  none")
    print("recent events:")
    for kind, created_at in events:
        print(f"  {created_at} {kind}")
    return 0


def cmd_tasks(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_existing_schema(project)
    tasks = list_tasks(project)

    print(f"project: {project}")
    print("tasks:")
    if not tasks:
        print("  none")
        return 0
    for task in tasks:
        resource = str(task.payload.get("resource", "."))
        claim = ""
        if task.claimed_by:
            claim = f" claimed_by={task.claimed_by} role={task.claimed_role} gen={task.claimed_generation}"
        print(f"  #{task.id} {task.status} resource={resource}{claim} {task.title}")
    return 0


def cmd_task_add(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_existing_schema(project)
    task_id = enqueue_task(project, args.title, resource=args.resource)
    print(f"task added: #{task_id} resource={normalize_resource(project, args.resource)}")
    return 0


def cmd_roles(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_existing_schema(project)
    leases = list_role_leases(project)
    heartbeats = list_worker_heartbeats(project)

    print(f"project: {project}")
    print("role leases:")
    if leases:
        for role, holder, generation, lease_until, status in leases:
            print(f"  {role}: {holder} gen={generation} until={lease_until} status={status}")
    else:
        print("  none")
    print("workers:")
    if heartbeats:
        for agent, pid, status, role, generation, updated_at in heartbeats:
            lease = f"{role} gen={generation}" if role else "none"
            print(f"  {agent}: pid={pid} status={status} role={lease} updated={updated_at}")
    else:
        print("  none")
    return 0


def cmd_locks(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_existing_schema(project)
    locks = list_resource_locks(project)

    print(f"project: {project}")
    print("resource locks:")
    if not locks:
        print("  none")
        return 0
    for resource, holder, mode, lease_until, role, role_generation, status in locks:
        role_text = f" role={role} gen={role_generation}" if role else ""
        print(f"  {resource}: {holder} mode={mode}{role_text} until={lease_until} status={status}")
    return 0


def positive_seconds(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def cmd_lease_acquire(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_existing_schema(project)
    outcome = acquire_role(project, args.role, args.agent, args.ttl, renew_if_same=args.renew)
    if not outcome.ok:
        print(f"lease denied: {outcome.reason}")
        print(
            f"current: role={outcome.role} holder={outcome.holder} "
            f"gen={outcome.generation} until={outcome.lease_until} status={outcome.status}"
        )
        return 2
    print(
        f"lease {outcome.status}: role={outcome.role} holder={outcome.holder} "
        f"gen={outcome.generation} until={outcome.lease_until}"
    )
    return 0


def cmd_lease_release(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_existing_schema(project)
    outcome = release_role(project, args.role, holder=args.agent, generation=args.generation)
    if not outcome.ok:
        print(f"release denied: {outcome.reason}")
        if outcome.holder:
            print(
                f"current: role={outcome.role} holder={outcome.holder} "
                f"gen={outcome.generation} until={outcome.lease_until} status={outcome.status}"
            )
        return 2
    print(
        f"lease released: role={outcome.role} holder={outcome.holder} "
        f"gen={outcome.generation}"
    )
    return 0


def cmd_lock_acquire(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_existing_schema(project)
    if (args.role is None) != (args.generation is None):
        raise SystemExit("--role and --generation must be provided together")
    outcome = acquire_resource_lock(
        project,
        args.resource,
        args.agent,
        args.ttl,
        role=args.role or "",
        role_generation=args.generation if args.generation is not None else 0,
        renew_if_same=args.renew,
    )
    if not outcome.ok:
        print(f"lock denied: {outcome.reason}")
        if outcome.conflict_resource:
            print(f"conflict: {outcome.conflict_resource}")
        return 2
    print(
        f"lock {outcome.status}: resource={outcome.resource} holder={outcome.holder} "
        f"mode={outcome.mode} until={outcome.lease_until}"
    )
    return 0


def cmd_lock_release(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_existing_schema(project)
    outcome = release_resource_lock(project, args.resource, holder=args.agent)
    if not outcome.ok:
        print(f"release denied: {outcome.reason}")
        if outcome.holder:
            print(
                f"current: resource={outcome.resource} holder={outcome.holder} "
                f"mode={outcome.mode} until={outcome.lease_until} status={outcome.status}"
            )
        return 2
    print(f"lock released: resource={outcome.resource} holder={outcome.holder}")
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_layout(project, args.task or "")
    name = session_name(project)
    if tmux_has_session(name):
        print(f"tmux session already exists: {name}")
        return 0

    supervisor_cmd = module_command(project, "supervisor")
    worker_flags = ["--execute-agents"] if args.execute_agents else []
    codex_cmd = module_command(project, "worker", *worker_flags, "codex")
    claude_cmd = module_command(project, "worker", *worker_flags, "claude")
    log_cmd = f"mkdir -p .mmux/logs; touch .mmux/logs/supervisor.log; tail -f .mmux/logs/supervisor.log"

    run(["tmux", "new-session", "-d", "-s", name, "-c", str(project), supervisor_cmd])
    run(["tmux", "split-window", "-h", "-t", f"{name}:0.0", "-c", str(project), codex_cmd])
    run(["tmux", "split-window", "-v", "-t", f"{name}:0.0", "-c", str(project), log_cmd])
    run(["tmux", "split-window", "-v", "-t", f"{name}:0.1", "-c", str(project), claude_cmd])
    run(["tmux", "select-layout", "-t", name, "tiled"], check=False)

    write_log(project, f"started tmux session {name}")
    print(f"started tmux session: {name}")
    print(f"attach with: tmux attach -t {name}")
    if args.execute_agents:
        print("agent execution: enabled")
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    require_project_state(project)
    name = session_name(project)
    os.execvp("tmux", ["tmux", "attach", "-t", name])
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    name = session_name(project)
    if not shutil.which("tmux"):
        raise SystemExit("tmux is not installed")
    if not tmux_has_session(name):
        print(f"tmux session not running: {name}")
        return 0
    run(["tmux", "kill-session", "-t", name])
    write_log(project, f"stopped tmux session {name}")
    print(f"stopped tmux session: {name}")
    return 0


def cmd_supervisor(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    require_project_state(project)
    print("mmux deterministic supervisor")
    print(f"project: {project}")
    print("LLM referee: disabled")
    print(f"role rotation: {SUPERVISOR_ASSIGNMENT_SECONDS}s slots")
    write_log(project, "supervisor online")
    try:
        while True:
            now = utc_now_dt()
            plan = supervisor_role_plan(now)
            ttl = min(ROLE_LEASE_TTL_SECONDS, seconds_until_next_assignment(now))
            ensured: list[str] = []
            conflicts: list[str] = []
            for role, agent in plan:
                outcome = acquire_role(
                    project,
                    role,
                    agent,
                    ttl,
                    renew_if_same=True,
                )
                if outcome.ok:
                    ensured.append(f"{role}:{agent}:gen{outcome.generation}:{outcome.status}")
                else:
                    conflicts.append(f"{role}:{outcome.holder}:gen{outcome.generation}")
            status = " ".join(ensured) or "none"
            conflict_text = f" conflicts={' '.join(conflicts)}" if conflicts else ""
            slot = supervisor_assignment_slot(now)
            write_log(project, f"heartbeat slot={slot} ttl={ttl} leases={status}{conflict_text}")
            print(f"{utc_now()} slot={slot} ttl={ttl} leases={status}{conflict_text}")
            sys.stdout.flush()
            time.sleep(SUPERVISOR_TICK_SECONDS)
    except KeyboardInterrupt:
        write_log(project, "supervisor interrupted")
    return 0


def task_resource(task: TaskRecord) -> str:
    resource = task.payload.get("resource", ".")
    return str(resource) if isinstance(resource, str) else "."


def execute_driver_task(project: Path, agent: str, generation: int) -> str:
    task = claim_next_task(project, agent, "driver", generation)
    if task is None:
        return "no pending task"
    role = acquire_role(project, "driver", agent, AGENT_TIMEOUT_SECONDS + 60, renew_if_same=True)
    if not role.ok or role.generation != generation:
        release_task_claim(project, task.id, "driver role lease is stale")
        return f"task #{task.id} requeued: driver role lease is stale"

    resource = task_resource(task)
    lock = acquire_resource_lock(
        project,
        resource,
        agent,
        max(RESOURCE_LOCK_TTL_SECONDS, AGENT_TIMEOUT_SECONDS + 60),
        role="driver",
        role_generation=generation,
        renew_if_same=True,
    )
    if not lock.ok:
        release_task_claim(project, task.id, lock.reason)
        release_role(project, "driver", holder=agent, generation=generation)
        return f"task #{task.id} requeued: {lock.reason}"

    try:
        worktree = create_task_worktree(project, task, agent)
    except Exception as exc:
        release_task_claim(project, task.id, f"worktree creation failed: {exc}")
        release_resource_lock(project, lock.resource, holder=agent)
        release_role(project, "driver", holder=agent, generation=generation)
        return f"task #{task.id} requeued: worktree creation failed: {exc}"

    try:
        update_task_payload(project, task.id, {"worktree": relative_to_project(project, worktree)})
        write_log(project, f"{agent} executing task #{task.id} resource={lock.resource}")
        update_worker_heartbeat(project, agent, "running", role="driver", generation=generation)
        print(f"executing task #{task.id}: {task.title}")
        print(f"resource lock: {lock.resource}")
        print(f"worktree: {worktree}")
        sys.stdout.flush()
        try:
            result = invoke_agent_adapter(project, worktree, agent, task, generation, lock.resource)
        except Exception as exc:
            message = f"agent adapter failed: {exc}"
            finish_task(
                project,
                task.id,
                "failed",
                message=message,
                payload_updates={"worktree": relative_to_project(project, worktree)},
            )
            write_log(project, f"{agent} failed task #{task.id}: {exc}")
            return f"task #{task.id} failed: {exc}"

        policy = check_diff_policy(project, worktree, lock.resource)
        payload_updates = {
            "worktree": relative_to_project(project, worktree),
            "diff_policy": policy.status,
            "changed_files": list(policy.changed_files),
        }
        if result.ok and policy.status == "no_change":
            finish_task(
                project,
                task.id,
                "no_change",
                message=policy.reason,
                log_file=result.log_file,
                payload_updates=payload_updates,
            )
            write_log(project, f"{agent} produced no changes for task #{task.id}")
            return f"task #{task.id} no_change log={result.log_file}"
        if result.ok and not policy.ok:
            finish_task(
                project,
                task.id,
                "rejected",
                message=policy.reason,
                log_file=result.log_file,
                payload_updates=payload_updates,
            )
            write_log(project, f"{agent} rejected task #{task.id}: {policy.reason}")
            return f"task #{task.id} rejected: {policy.reason}"
        if result.ok:
            try:
                patch = export_worktree_patch(worktree)
                apply_worktree_patch(project, patch)
                payload_updates["patch_applied"] = True
            except Exception as exc:
                message = f"patch apply failed: {exc}"
                payload_updates["patch_applied"] = False
                finish_task(
                    project,
                    task.id,
                    "failed",
                    message=message,
                    log_file=result.log_file,
                    payload_updates=payload_updates,
                )
                write_log(project, f"{agent} failed to apply task #{task.id}: {exc}")
                return f"task #{task.id} failed: {message}"

        finish_task(
            project,
            task.id,
            "completed" if result.ok else "failed",
            message=result.message,
            log_file=result.log_file,
            payload_updates=payload_updates,
        )
        write_log(project, f"{agent} finished task #{task.id} ok={result.ok} log={result.log_file}")
        return f"task #{task.id} {'completed' if result.ok else 'failed'} log={result.log_file}"
    finally:
        release_resource_lock(project, lock.resource, holder=agent)
        release_role(project, "driver", holder=agent, generation=generation)


def cmd_worker(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    agent = args.agent
    require_project_state(project)
    write_log(project, f"{agent} worker online")
    last_rendered = ""
    try:
        while True:
            roles = active_roles_for_agent(project, agent)
            primary = roles[0] if roles else None
            status = "leased" if primary else "parked"
            update_worker_heartbeat(
                project,
                agent,
                status,
                role=primary[0] if primary else None,
                generation=primary[1] if primary else None,
            )
            role_lines = [
                f"  {role} gen={generation} until={lease_until}" for role, generation, lease_until in roles
            ]
            if not role_lines:
                role_lines = ["  none"]
            rendered = "\n".join(
                [
                    f"mmux {agent} worker",
                    f"project: {project}",
                    f"status: {status}",
                    f"agent execution: {'enabled' if args.execute_agents else 'disabled'}",
                    "role leases:",
                    *role_lines,
                    "",
                    "adapter: driver role executes pending tasks when enabled",
                ]
            )
            if rendered != last_rendered:
                print("\033[2J\033[H" + rendered)
                sys.stdout.flush()
                last_rendered = rendered
            if args.execute_agents:
                driver = next((role for role in roles if role[0] == "driver"), None)
                if driver:
                    message = execute_driver_task(project, agent, driver[1])
                    if message != "no pending task":
                        print(message)
                        sys.stdout.flush()
                        last_rendered = ""
            time.sleep(WORKER_REFRESH_SECONDS)
    except KeyboardInterrupt:
        write_log(project, f"{agent} worker interrupted")
        update_worker_heartbeat(project, agent, "stopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mmux")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    subparsers = parser.add_subparsers(dest="command")

    init = subparsers.add_parser("init", help="initialize .mmux state in a project")
    init.add_argument("project", nargs="?", default=".")
    init.add_argument("--task", default="")
    init.set_defaults(func=cmd_init)

    doctor = subparsers.add_parser("doctor", help="check local tool dependencies")
    doctor.set_defaults(func=cmd_doctor)

    status = subparsers.add_parser("status", help="show deterministic project state")
    status.add_argument("project", nargs="?", default=".")
    status.set_defaults(func=cmd_status)

    tasks = subparsers.add_parser("tasks", help="show task queue")
    tasks.add_argument("project", nargs="?", default=".")
    tasks.set_defaults(func=cmd_tasks)

    task = subparsers.add_parser("task", help="manage tasks")
    task_subparsers = task.add_subparsers(dest="task_command", required=True)

    task_add = task_subparsers.add_parser("add", help="add a pending task")
    task_add.add_argument("title")
    task_add.add_argument("--resource", default=".")
    task_add.add_argument("--project", default=".")
    task_add.set_defaults(func=cmd_task_add)

    roles = subparsers.add_parser("roles", help="show role leases and worker heartbeats")
    roles.add_argument("project", nargs="?", default=".")
    roles.set_defaults(func=cmd_roles)

    locks = subparsers.add_parser("locks", help="show resource locks")
    locks.add_argument("project", nargs="?", default=".")
    locks.set_defaults(func=cmd_locks)

    lease = subparsers.add_parser("lease", help="manage deterministic role leases")
    lease_subparsers = lease.add_subparsers(dest="lease_command", required=True)

    lease_acquire = lease_subparsers.add_parser("acquire", help="acquire a role lease")
    lease_acquire.add_argument("role", choices=ROLES)
    lease_acquire.add_argument("--agent", choices=AGENTS, required=True)
    lease_acquire.add_argument("--project", default=".")
    lease_acquire.add_argument("--ttl", type=positive_seconds, default=ROLE_LEASE_TTL_SECONDS)
    lease_acquire.add_argument(
        "--renew",
        action="store_true",
        help="renew the lease if the same agent already holds it",
    )
    lease_acquire.set_defaults(func=cmd_lease_acquire)

    lease_release = lease_subparsers.add_parser("release", help="release a role lease")
    lease_release.add_argument("role", choices=ROLES)
    lease_release.add_argument("--agent", choices=AGENTS)
    lease_release.add_argument("--generation", type=int)
    lease_release.add_argument("--project", default=".")
    lease_release.set_defaults(func=cmd_lease_release)

    lock = subparsers.add_parser("lock", help="manage resource locks")
    lock_subparsers = lock.add_subparsers(dest="lock_command", required=True)

    lock_acquire = lock_subparsers.add_parser("acquire", help="acquire a resource lock")
    lock_acquire.add_argument("resource")
    lock_acquire.add_argument("--agent", choices=AGENTS, required=True)
    lock_acquire.add_argument("--role", choices=ROLES)
    lock_acquire.add_argument("--generation", type=nonnegative_int)
    lock_acquire.add_argument("--ttl", type=positive_seconds, default=RESOURCE_LOCK_TTL_SECONDS)
    lock_acquire.add_argument("--renew", action="store_true")
    lock_acquire.add_argument("--project", default=".")
    lock_acquire.set_defaults(func=cmd_lock_acquire)

    lock_release = lock_subparsers.add_parser("release", help="release a resource lock")
    lock_release.add_argument("resource")
    lock_release.add_argument("--agent", choices=AGENTS)
    lock_release.add_argument("--project", default=".")
    lock_release.set_defaults(func=cmd_lock_release)

    start = subparsers.add_parser("start", help="start the tmux observation workspace")
    start.add_argument("project", nargs="?", default=".")
    start.add_argument("--task", default="")
    start.add_argument(
        "--execute-agents",
        action="store_true",
        help="allow workers to run codex/claude non-interactively when they hold driver",
    )
    start.set_defaults(func=cmd_start)

    attach = subparsers.add_parser("attach", help="attach to the project tmux session")
    attach.add_argument("project", nargs="?", default=".")
    attach.set_defaults(func=cmd_attach)

    stop = subparsers.add_parser("stop", help="stop the project tmux session")
    stop.add_argument("project", nargs="?", default=".")
    stop.set_defaults(func=cmd_stop)

    supervisor = subparsers.add_parser("supervisor", help=argparse.SUPPRESS)
    supervisor.add_argument("--project", default=".")
    supervisor.set_defaults(func=cmd_supervisor)

    worker = subparsers.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("agent", choices=AGENTS)
    worker.add_argument("--project", default=".")
    worker.add_argument("--execute-agents", action="store_true")
    worker.set_defaults(func=cmd_worker)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    from mmux import __version__

    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
