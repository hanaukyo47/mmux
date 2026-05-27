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
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Iterable, Optional


ROLES = ("driver", "reviewer", "scout", "tester", "summarizer")
AGENTS = ("codex", "claude")
RESIDENT_MESSAGE_KINDS = ("task", "review", "note")
RESIDENT_PANE_INDEXES = {"codex": "1", "claude": "3"}
RESIDENT_BLOCKED_ESCALATION_EVENTS = 2
REQUEUEABLE_TASK_STATUSES = ("blocked", "failed", "rejected", "no_change")
ROLE_LEASE_TTL_SECONDS = 2 * 60
RESOURCE_LOCK_TTL_SECONDS = 10 * 60
WORKER_REFRESH_SECONDS = 5
SUPERVISOR_TICK_SECONDS = 30
SUPERVISOR_ASSIGNMENT_SECONDS = 5 * 60
AGENT_TIMEOUT_SECONDS = 20 * 60
AGENT_NO_OUTPUT_TIMEOUT_SECONDS = 2 * 60
AGENT_ADAPTER_COOLDOWN_SECONDS = 10 * 60
TEST_TIMEOUT_SECONDS = 10 * 60
RUN_SHUTDOWN_GRACE_SECONDS = 15
MIN_EXECUTION_BUDGET_SECONDS = 30
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
class TaskRequeueOutcome:
    ok: bool
    task_id: int
    from_status: str
    to_status: str
    reason: str = ""


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


@dataclass(frozen=True)
class TestGateResult:
    ok: bool
    log_file: str
    message: str
    baseline_failures: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResidentProtocolEvent:
    agent: str
    kind: str
    task_id: int
    message: str
    line: str
    line_hash: str


@dataclass(frozen=True)
class ProjectCheck:
    name: str
    command: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class ProjectProfile:
    ecosystems: tuple[str, ...]
    languages: tuple[str, ...]
    markers: tuple[str, ...]
    active_checks: tuple[ProjectCheck, ...]
    suggested_checks: tuple[ProjectCheck, ...]


@dataclass(frozen=True)
class FrontierCandidate:
    title: str
    resource: str
    evidence: dict[str, object]
    score: int


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
    agents = agent_order_for_slot(now)
    return tuple(zip(roles, agents))


def agent_order_for_slot(now: Optional[dt.datetime] = None) -> tuple[str, ...]:
    slot = supervisor_assignment_slot(now)
    return AGENTS if slot % 2 == 0 else tuple(reversed(AGENTS))


def peer_agent(agent: str) -> str:
    validate_agent(agent)
    return next(candidate for candidate in AGENTS if candidate != agent)


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


def meta_json_db(db: sqlite3.Connection, key: str) -> dict[str, object]:
    row = db.execute("select value from meta where key = ?", (key,)).fetchone()
    if row is None:
        return {}
    try:
        decoded = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def set_meta_json_db(db: sqlite3.Connection, key: str, value: dict[str, object]) -> None:
    db.execute(
        "insert or replace into meta(key, value) values (?, ?)",
        (key, json.dumps(value, sort_keys=True)),
    )


def agent_cooldown_key(agent: str) -> str:
    validate_agent(agent)
    return f"agent_cooldown:{agent}"


def resident_seen_key(agent: str) -> str:
    validate_agent(agent)
    return f"resident_seen:{agent}"


def resident_mode_key() -> str:
    return "resident_mode"


def tmux_panes_key() -> str:
    return "tmux_panes"


def set_tmux_pane_targets(project: Path, panes: dict[str, str]) -> None:
    with database(project) as db:
        apply_schema(db)
        set_meta_json_db(db, tmux_panes_key(), {"panes": panes, "updated_at": utc_now()})


def tmux_pane_target_from_meta(project: Path, name: str) -> str:
    if not state_path(project).exists():
        return ""
    with database(project) as db:
        apply_schema(db)
        value = meta_json_db(db, tmux_panes_key())
    panes = value.get("panes")
    if not isinstance(panes, dict):
        return ""
    target = panes.get(name)
    return str(target) if isinstance(target, str) and target else ""


def set_resident_mode(project: Path, enabled: bool) -> None:
    with database(project) as db:
        apply_schema(db)
        set_meta_json_db(db, resident_mode_key(), {"enabled": enabled, "updated_at": utc_now()})
        record_event(db, "resident_mode_changed", {"enabled": enabled})


def resident_mode_enabled(project: Path) -> bool:
    if not state_path(project).exists():
        return False
    with database(project) as db:
        apply_schema(db)
        value = meta_json_db(db, resident_mode_key())
    return value.get("enabled") is True


def agent_cooldown_db(db: sqlite3.Connection, agent: str) -> dict[str, object]:
    return meta_json_db(db, agent_cooldown_key(agent))


def agent_is_cooled_down_db(db: sqlite3.Connection, agent: str, now: Optional[dt.datetime] = None) -> bool:
    cooldown = agent_cooldown_db(db, agent)
    until = cooldown.get("until")
    if not isinstance(until, str) or not until:
        return False
    return parse_utc(until) > (now or utc_now_dt())


def mark_agent_cooldown(project: Path, agent: str, reason: str, ttl_seconds: int = AGENT_ADAPTER_COOLDOWN_SECONDS) -> None:
    validate_agent(agent)
    until = format_utc(utc_now_dt() + dt.timedelta(seconds=ttl_seconds))
    payload = {
        "agent": agent,
        "reason": reason,
        "until": until,
    }
    with database(project) as db:
        apply_schema(db)
        set_meta_json_db(db, agent_cooldown_key(agent), payload)
        record_event(db, "agent_cooldown_started", payload)


def list_agent_cooldowns(project: Path) -> list[tuple[str, str, str]]:
    with database(project) as db:
        apply_schema(db)
        rows = db.execute(
            "select key, value from meta where key like ? order by key",
            ("agent_cooldown:%",),
        ).fetchall()
    cooldowns = []
    now = utc_now_dt()
    for key, value in rows:
        agent = str(key).split(":", 1)[1]
        try:
            decoded = json.loads(str(value))
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, dict):
            continue
        until = decoded.get("until")
        reason = decoded.get("reason")
        if isinstance(until, str) and parse_utc(until) > now:
            cooldowns.append((agent, until, str(reason or "")))
    return cooldowns


def ensure_layout(project: Path, task: str = "") -> None:
    root = mmux_dir(project)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "sessions").mkdir(parents=True, exist_ok=True)
    (root / "runs").mkdir(parents=True, exist_ok=True)
    (root / "worktrees").mkdir(parents=True, exist_ok=True)
    (root / "resident").mkdir(parents=True, exist_ok=True)
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
                effective_lease_until = (
                    current_until
                    if parse_utc(current_until) >= parse_utc(lease_until)
                    else lease_until
                )
                db.execute(
                    "update role_leases set lease_until = ?, status = ? where role = ?",
                    (effective_lease_until, "active", role),
                )
                record_event(
                    db,
                    "role_renewed",
                    {
                        "role": role,
                        "holder": holder,
                        "generation": generation,
                        "lease_until": effective_lease_until,
                    },
                )
                db.commit()
                return LeaseOutcome(True, role, holder, generation, effective_lease_until, "renewed")
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
    role: Optional[str] = None,
    role_generation: Optional[int] = None,
) -> LockOutcome:
    if holder is not None:
        validate_agent(holder)
    if (role is None) != (role_generation is None):
        raise ValueError("role and role_generation must be provided together")
    if role is not None:
        validate_role(role)
    if role_generation is not None and role_generation <= 0:
        raise ValueError("role_generation must be positive")
    normalized = normalize_resource(project, resource)

    db = connect(project)
    try:
        db.execute("begin immediate")
        apply_schema(db)
        expire_resource_locks_db(db, utc_now_dt())
        row = db.execute(
            """
            select holder, mode, lease_until, status, role, role_generation
            from resource_locks
            where resource = ?
            """,
            (normalized,),
        ).fetchone()
        if not row:
            db.rollback()
            return LockOutcome(False, normalized, holder or "", "", "", "missing", f"{normalized} is not locked")

        current_holder, mode, lease_until, status, current_role, current_generation = row
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
        if role is not None and (current_role != role or current_generation != role_generation):
            db.rollback()
            return LockOutcome(
                False,
                normalized,
                current_holder,
                mode,
                lease_until,
                "stale_role",
                f"{normalized} lock belongs to {current_role or 'no role'} generation {current_generation}",
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


def get_task(project: Path, task_id: int) -> Optional[TaskRecord]:
    with database(project) as db:
        apply_schema(db)
        row = db.execute(
            """
            select id, title, status, payload, claimed_by, claimed_role, claimed_generation
            from tasks
            where id = ?
            """,
            (task_id,),
        ).fetchone()
    return task_from_row(row) if row else None


def requeue_task(project: Path, task_id: int, reason: str = "manual requeue") -> TaskRequeueOutcome:
    db = connect(project)
    try:
        db.execute("begin immediate")
        apply_schema(db)
        row = db.execute(
            """
            select id, title, status, payload, claimed_by, claimed_role, claimed_generation
            from tasks
            where id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            db.rollback()
            return TaskRequeueOutcome(False, task_id, "", "", "task not found")
        task = task_from_row(row)
        if task.status == "pending":
            db.rollback()
            return TaskRequeueOutcome(True, task_id, "pending", "pending", "already pending")
        if task.status not in REQUEUEABLE_TASK_STATUSES:
            db.rollback()
            return TaskRequeueOutcome(
                False,
                task_id,
                task.status,
                "",
                f"cannot requeue task in status {task.status}",
            )

        payload = dict(task.payload)
        now = utc_now()
        payload["requeued_from"] = task.status
        payload["requeued_reason"] = reason
        payload["requeued_at"] = now
        payload["requeue_count"] = int(payload.get("requeue_count", 0) or 0) + 1
        db.execute(
            """
            update tasks
            set status = ?, payload = ?, claimed_by = '', claimed_role = '', claimed_generation = 0,
                claimed_at = '', finished_at = '', updated_at = ?, last_error = ''
            where id = ?
            """,
            ("pending", encode_payload(payload), now, task_id),
        )
        record_event(
            db,
            "task_requeued",
            {"id": task_id, "from": task.status, "status": "pending", "reason": reason},
        )
        db.commit()
        return TaskRequeueOutcome(True, task_id, task.status, "pending")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def claim_task_with_status(
    project: Path,
    agent: str,
    role: str,
    generation: int,
    *,
    source_status: str,
    running_status: str,
) -> Optional[TaskRecord]:
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
            (source_status,),
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
            (running_status, agent, role, generation, now, now, task.id, source_status),
        )
        record_event(
            db,
            "task_claimed",
            {
                "id": task.id,
                "agent": agent,
                "role": role,
                "generation": generation,
                "from": source_status,
                "to": running_status,
            },
        )
        db.commit()
        return TaskRecord(task.id, task.title, running_status, task.payload, agent, role, generation)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def claim_next_task(project: Path, agent: str, role: str, generation: int) -> Optional[TaskRecord]:
    return claim_task_with_status(
        project,
        agent,
        role,
        generation,
        source_status="pending",
        running_status="running",
    )


def claim_next_test_task(project: Path, agent: str, generation: int) -> Optional[TaskRecord]:
    return claim_task_with_status(
        project,
        agent,
        "tester",
        generation,
        source_status="awaiting_test",
        running_status="running_test",
    )


def release_task_claim(project: Path, task_id: int, reason: str) -> None:
    release_task_claim_to(project, task_id, "pending", reason, running_status="running")


def release_task_claim_to(project: Path, task_id: int, target_status: str, reason: str, *, running_status: str) -> None:
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
            (target_status, now, reason, task_id, running_status),
        )
        record_event(db, "task_requeued", {"id": task_id, "status": target_status, "reason": reason})


def move_task_to_status(
    project: Path,
    task_id: int,
    status: str,
    *,
    message: str = "",
    log_file: str = "",
    payload_updates: Optional[dict[str, object]] = None,
) -> None:
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
        last_error = message if status in {"failed", "rejected", "blocked"} else ""
        db.execute(
            """
            update tasks
            set status = ?, payload = ?, claimed_by = '', claimed_role = '', claimed_generation = 0,
                claimed_at = '', updated_at = ?, last_error = ?
            where id = ?
            """,
            (status, encode_payload(payload), now, last_error, task_id),
        )
        record_event(db, "task_status_changed", {"id": task_id, "status": status, "log_file": log_file})


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
    if status not in {"completed", "failed", "no_change", "rejected", "blocked"}:
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
        last_error = message if status in {"failed", "rejected", "blocked"} else ""
        db.execute(
            """
            update tasks
            set status = ?, payload = ?, claimed_by = '', claimed_role = '', claimed_generation = 0,
                claimed_at = '', updated_at = ?, finished_at = ?, last_error = ?
            where id = ?
            """,
            (status, encode_payload(payload), now, now, last_error, task_id),
        )
        record_event(db, "task_finished", {"id": task_id, "status": status, "log_file": log_file})


def write_log(project: Path, message: str) -> None:
    path = log_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{utc_now()} {message}\n")


def record_project_event(project: Path, kind: str, payload: dict[str, object]) -> None:
    with database(project) as db:
        apply_schema(db)
        record_event(db, kind, payload)


def cleanup_runtime_state(project: Path) -> None:
    if not state_path(project).exists():
        return
    with database(project) as db:
        apply_schema(db)
        now = utc_now()
        driver_rows = db.execute("select id from tasks where status = ?", ("running",)).fetchall()
        tester_rows = db.execute("select id from tasks where status = ?", ("running_test",)).fetchall()
        db.execute(
            """
            update tasks
            set status = ?, claimed_by = '', claimed_role = '', claimed_generation = 0,
                claimed_at = '', updated_at = ?, last_error = ?
            where status = ?
            """,
            ("pending", now, "runtime stopped before driver completed", "running"),
        )
        db.execute(
            """
            update tasks
            set status = ?, claimed_by = '', claimed_role = '', claimed_generation = 0,
                claimed_at = '', updated_at = ?, last_error = ?
            where status = ?
            """,
            ("awaiting_test", now, "runtime stopped before tester completed", "running_test"),
        )
        db.execute("update role_leases set status = ? where status = ?", ("released", "active"))
        db.execute("update resource_locks set status = ? where status = ?", ("released", "active"))
        db.execute(
            """
            update worker_heartbeats
            set status = ?, role = null, generation = null, updated_at = ?
            """,
            ("stopped", now),
        )
        for row in driver_rows:
            record_event(
                db,
                "task_requeued",
                {
                    "id": int(row[0]),
                    "status": "pending",
                    "reason": "runtime stopped before driver completed",
                },
            )
        for row in tester_rows:
            record_event(
                db,
                "task_requeued",
                {
                    "id": int(row[0]),
                    "status": "awaiting_test",
                    "reason": "runtime stopped before tester completed",
                },
            )
        record_event(db, "runtime_stopped", {})


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


def resident_root(project: Path) -> Path:
    root = mmux_dir(project) / "resident"
    root.mkdir(parents=True, exist_ok=True)
    return root


def resident_worktree_path(project: Path, agent: str) -> Path:
    validate_agent(agent)
    return resident_root(project) / agent


def resident_context_from_path(path: Path) -> Optional[tuple[Path, str]]:
    resolved = path.expanduser().resolve()
    for ancestor in (resolved, *resolved.parents):
        if ancestor.name not in AGENTS:
            continue
        resident_parent = ancestor.parent
        mmux_parent = resident_parent.parent
        if resident_parent.name != "resident" or mmux_parent.name != ".mmux":
            continue
        project = mmux_parent.parent
        if state_path(project).exists():
            return project, ancestor.name
    return None


def resolve_report_project(path: str) -> tuple[Path, str]:
    candidate = resolve_project(path)
    if state_path(candidate).exists():
        return candidate, ""
    context = resident_context_from_path(candidate)
    if context is not None:
        return context
    return candidate, ""


def prepare_resident_worktree(project: Path, agent: str) -> Path:
    path = resident_worktree_path(project, agent)
    run(["git", "rev-parse", "--show-toplevel"], cwd=project)
    run(["git", "rev-parse", "--verify", "HEAD"], cwd=project)
    if not path.exists():
        run(["git", "worktree", "add", "--detach", str(path), "HEAD"], cwd=project)
        return path
    if not (path / ".git").exists():
        raise RuntimeError(f"resident path exists but is not a git worktree: {path}")
    reset_resident_worktree(path)
    return path


def reset_resident_worktree(worktree: Path) -> None:
    run(["git", "reset", "--hard", "HEAD"], cwd=worktree)
    run(["git", "clean", "-fd"], cwd=worktree)


def create_task_worktree(project: Path, task: TaskRecord, agent: str) -> Path:
    validate_agent(agent)
    run(["git", "rev-parse", "--show-toplevel"], cwd=project)
    run(["git", "rev-parse", "--verify", "HEAD"], cwd=project)
    stamp = utc_now().replace(":", "").replace("+", "Z")
    path = worktree_root(project) / f"task-{task.id}-{agent}-{os.getpid()}-{stamp}"
    run(["git", "worktree", "add", "--detach", str(path), "HEAD"], cwd=project)
    return path


def create_baseline_worktree(project: Path, task: TaskRecord) -> Path:
    run(["git", "rev-parse", "--show-toplevel"], cwd=project)
    run(["git", "rev-parse", "--verify", "HEAD"], cwd=project)
    stamp = utc_now().replace(":", "").replace("+", "Z")
    path = worktree_root(project) / f"baseline-{task.id}-{os.getpid()}-{stamp}"
    run(["git", "worktree", "add", "--detach", str(path), "HEAD"], cwd=project)
    return path


def remove_git_worktree(project: Path, path: Path) -> None:
    run(["git", "worktree", "remove", "--force", str(path)], cwd=project, check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


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


def apply_patch_to_worktree(worktree: Path, patch: str) -> None:
    if patch.strip():
        run_with_input(["git", "apply", "--binary", "-"], cwd=worktree, input_text=patch)


def export_resident_patch(worktree: Path) -> str:
    patch = export_worktree_patch(worktree)
    run(["git", "reset"], cwd=worktree, check=False)
    return patch


def changed_python_files(worktree: Path, changed_files: Iterable[str]) -> list[str]:
    return sorted(
        path
        for path in changed_files
        if path.endswith(".py") and (worktree / path).exists()
    )


def changed_shell_files(worktree: Path, changed_files: Iterable[str]) -> list[str]:
    return sorted(
        path
        for path in changed_files
        if path.endswith(".sh") and (worktree / path).exists()
    )


def changed_json_files(worktree: Path, changed_files: Iterable[str]) -> list[str]:
    return sorted(
        path
        for path in changed_files
        if path.endswith(".json") and (worktree / path).exists()
    )


def has_unittest_tree(worktree: Path) -> bool:
    tests_dir = worktree / "tests"
    return tests_dir.is_dir() and any(path.name.startswith("test") for path in tests_dir.rglob("*.py"))


def looks_like_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = PurePosixPath(path).name.lower()
    return (
        "test" in parts
        or "tests" in parts
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith(".test.js")
        or name.endswith(".test.ts")
        or name.endswith(".spec.js")
        or name.endswith(".spec.ts")
    )


SKIPPED_PROFILE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".mmux",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "vendor",
    "target",
    "dist",
    "build",
    ".next",
    ".cache",
}


SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".swift",
    ".rb",
    ".php",
    ".cs",
}
TEXT_FRONTIER_SUFFIXES = SOURCE_SUFFIXES | {".md", ".rst", ".txt", ".toml", ".yaml", ".yml", ".json", ".sh"}
TODO_MARKERS = ("todo", "fixme", "xxx")


def list_project_files(project: Path, *, limit: int = 5000) -> list[str]:
    result = run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=project, check=False)
    if result.returncode == 0 and result.stdout:
        files = split_nul_paths(result.stdout)
        return sorted(path for path in files if not any(part in SKIPPED_PROFILE_DIRS for part in PurePosixPath(path).parts))[:limit]

    files: list[str] = []
    for path in project.rglob("*"):
        try:
            relative = path.relative_to(project)
        except ValueError:
            continue
        parts = relative.parts
        if any(part in SKIPPED_PROFILE_DIRS for part in parts):
            continue
        if path.is_file():
            files.append(relative.as_posix())
            if len(files) >= limit:
                break
    return sorted(files)


def package_json_scripts(project: Path) -> dict[str, str]:
    path = project / "package.json"
    if not path.exists():
        return {}
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = decoded.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items() if isinstance(key, str)}


def has_real_npm_test_script(project: Path) -> bool:
    script = package_json_scripts(project).get("test", "").strip()
    if not script:
        return False
    lowered = script.lower()
    return "no test specified" not in lowered and "exit 1" not in lowered


def node_test_command(project: Path) -> tuple[str, ...]:
    if (project / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
        return ("pnpm", "test")
    if (project / "yarn.lock").exists() and shutil.which("yarn"):
        return ("yarn", "test")
    if ((project / "bun.lockb").exists() or (project / "bun.lock").exists()) and shutil.which("bun"):
        return ("bun", "test")
    if shutil.which("npm"):
        return ("npm", "test")
    return ()


def makefile_has_target(project: Path, target: str) -> bool:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        path = project / name
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith(f"{target}:"):
                return True
    return False


def composer_has_test_script(project: Path) -> bool:
    path = project / "composer.json"
    if not path.exists():
        return False
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    scripts = decoded.get("scripts")
    return isinstance(scripts, dict) and "test" in scripts


def add_check(checks: list[ProjectCheck], name: str, command: Iterable[str], reason: str) -> None:
    command_tuple = tuple(command)
    if not command_tuple:
        return
    key = (name, command_tuple)
    if any((check.name, check.command) == key for check in checks):
        return
    checks.append(ProjectCheck(name, command_tuple, reason))


def inspect_project(project: Path) -> ProjectProfile:
    files = list_project_files(project)
    file_set = set(files)
    suffixes = {Path(path).suffix for path in files}
    markers: list[str] = []
    ecosystems: set[str] = set()
    languages: set[str] = set()
    active_checks: list[ProjectCheck] = []
    suggested_checks: list[ProjectCheck] = []

    def mark(path: str) -> bool:
        if path in file_set or (project / path).exists():
            markers.append(path)
            return True
        return False

    def mark_any(paths: Iterable[str]) -> bool:
        found = False
        for path in paths:
            if mark(path):
                found = True
        return found

    has_python = mark_any(("pyproject.toml", "setup.py", "requirements.txt", "Pipfile", "poetry.lock", "uv.lock")) or ".py" in suffixes
    if has_python:
        ecosystems.add("python")
        languages.add("python")
        add_check(active_checks, "py-compile", (sys.executable, "-m", "py_compile", "<changed .py files>"), "syntax-check changed Python files")
        if has_unittest_tree(project):
            add_check(active_checks, "unittest", (sys.executable, "-m", "unittest", "discover", "-s", "tests"), "tests/ contains unittest-style tests")
        if mark_any(("pytest.ini", "tox.ini")) or "conftest.py" in file_set:
            add_check(suggested_checks, "pytest", ("pytest",), "pytest markers found; enable when project dependencies are installed")

    if mark("package.json") or suffixes & {".js", ".jsx", ".ts", ".tsx"}:
        ecosystems.add("node")
        languages.update({"javascript"} if suffixes & {".js", ".jsx"} else set())
        languages.update({"typescript"} if suffixes & {".ts", ".tsx"} else set())
        test_cmd = node_test_command(project)
        if has_real_npm_test_script(project):
            if test_cmd and (project / "node_modules").exists():
                add_check(active_checks, "node-test", test_cmd, "package.json test script with local node_modules")
            elif test_cmd:
                add_check(suggested_checks, "node-test", test_cmd, "package.json test script found; install dependencies before enabling")

    if mark("Cargo.toml"):
        ecosystems.add("rust")
        languages.add("rust")
        add_check(suggested_checks, "cargo-test", ("cargo", "test", "--locked", "--offline"), "Cargo project found; offline test avoids network access")

    if mark("go.mod") or ".go" in suffixes:
        ecosystems.add("go")
        languages.add("go")
        add_check(suggested_checks, "go-test", ("env", "GOPROXY=off", "go", "test", "./..."), "Go project found; GOPROXY=off avoids network access")

    if mark("pom.xml"):
        ecosystems.add("java")
        languages.add("java")
        add_check(suggested_checks, "maven-test", ("mvn", "-o", "test"), "Maven project found; offline mode avoids network access")

    if mark_any(("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")):
        ecosystems.add("gradle")
        languages.add("java")
        if (project / "gradlew").exists():
            add_check(suggested_checks, "gradle-test", ("./gradlew", "test", "--offline"), "Gradle wrapper found; offline mode avoids network access")
        else:
            add_check(suggested_checks, "gradle-test", ("gradle", "test", "--offline"), "Gradle project found; offline mode avoids network access")

    if any(path.endswith(".csproj") or path.endswith(".sln") for path in files):
        ecosystems.add("dotnet")
        languages.add("csharp")
        add_check(suggested_checks, "dotnet-test", ("dotnet", "test", "--no-restore"), ".NET project found; --no-restore avoids dependency changes")

    if mark("composer.json"):
        ecosystems.add("php")
        languages.add("php")
        if composer_has_test_script(project):
            add_check(suggested_checks, "composer-test", ("composer", "test"), "composer.json test script found")

    if mark("Gemfile") or ".rb" in suffixes:
        ecosystems.add("ruby")
        languages.add("ruby")
        if mark("Rakefile"):
            add_check(suggested_checks, "rake-test", ("bundle", "exec", "rake", "test"), "Ruby Rakefile found")

    if mark("Package.swift") or ".swift" in suffixes:
        ecosystems.add("swift")
        languages.add("swift")
        add_check(suggested_checks, "swift-test", ("swift", "test"), "Swift package found")

    if ".sh" in suffixes:
        languages.add("shell")
        add_check(active_checks, "shell-check", ("sh", "-n", "<changed .sh files>"), "syntax-check changed shell scripts")

    if ".json" in suffixes:
        languages.add("json")
        add_check(active_checks, "json-syntax", (sys.executable, "-m", "json.tool", "<changed .json files>"), "syntax-check changed JSON files")

    if makefile_has_target(project, "test"):
        ecosystems.add("make")
        add_check(suggested_checks, "make-test", ("make", "test"), "Makefile test target found")

    add_check(active_checks, "diff-check", ("git", "diff", "--check", "HEAD", "--"), "reject whitespace errors in the task diff")

    return ProjectProfile(
        ecosystems=tuple(sorted(ecosystems)),
        languages=tuple(sorted(languages)),
        markers=tuple(sorted(set(markers))),
        active_checks=tuple(active_checks),
        suggested_checks=tuple(suggested_checks),
    )


def build_tester_checks(worktree: Path, changed_files: Iterable[str]) -> list[ProjectCheck]:
    checks: list[ProjectCheck] = [
        ProjectCheck("diff-check", ("git", "diff", "--check", "HEAD", "--"), "reject whitespace errors in the task diff")
    ]

    py_files = changed_python_files(worktree, changed_files)
    if py_files:
        add_check(checks, "py-compile", (sys.executable, "-m", "py_compile", *py_files), "syntax-check changed Python files")

    for shell_file in changed_shell_files(worktree, changed_files):
        add_check(checks, f"shell-check:{shell_file}", ("sh", "-n", shell_file), "syntax-check changed shell script")

    for json_file in changed_json_files(worktree, changed_files):
        add_check(checks, f"json-syntax:{json_file}", (sys.executable, "-m", "json.tool", json_file), "syntax-check changed JSON file")

    if has_unittest_tree(worktree):
        add_check(checks, "unittest", (sys.executable, "-m", "unittest", "discover", "-s", "tests"), "tests/ contains unittest-style tests")

    node_cmd = node_test_command(worktree)
    if has_real_npm_test_script(worktree) and node_cmd and (worktree / "node_modules").exists():
        add_check(checks, "node-test", node_cmd, "package.json test script with local node_modules")

    return checks


def baseline_aware_check(check: ProjectCheck) -> bool:
    return check.name in {"unittest", "node-test"}


def command_text(command: Iterable[str]) -> str:
    return " ".join(sh_quote(part) for part in command)


def profile_to_dict(profile: ProjectProfile) -> dict[str, object]:
    def checks_to_dict(checks: Iterable[ProjectCheck]) -> list[dict[str, object]]:
        return [
            {
                "name": check.name,
                "command": list(check.command),
                "command_text": command_text(check.command),
                "reason": check.reason,
            }
            for check in checks
        ]

    return {
        "ecosystems": list(profile.ecosystems),
        "languages": list(profile.languages),
        "markers": list(profile.markers),
        "active_checks": checks_to_dict(profile.active_checks),
        "suggested_checks": checks_to_dict(profile.suggested_checks),
    }


def compact_text(value: str, limit: int = 90) -> str:
    normalized = " ".join(value.strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def candidate_evidence_text(candidate: FrontierCandidate) -> str:
    payload = dict(candidate.evidence)
    payload["resource"] = candidate.resource
    return json.dumps(payload, sort_keys=True)


def candidate_exists_db(db: sqlite3.Connection, candidate: FrontierCandidate) -> bool:
    row = db.execute(
        "select 1 from frontier_items where title = ? and evidence = ? limit 1",
        (candidate.title, candidate_evidence_text(candidate)),
    ).fetchone()
    return row is not None


def store_frontier_candidates(project: Path, candidates: Iterable[FrontierCandidate]) -> None:
    with database(project) as db:
        apply_schema(db)
        for candidate in candidates:
            if candidate_exists_db(db, candidate):
                continue
            db.execute(
                """
                insert into frontier_items(title, status, evidence, score, created_at)
                values (?, ?, ?, ?, ?)
                """,
                (candidate.title, "candidate", candidate_evidence_text(candidate), candidate.score, utc_now()),
            )
            record_event(
                db,
                "frontier_candidate_added",
                {
                    "title": candidate.title,
                    "resource": candidate.resource,
                    "score": candidate.score,
                    "evidence": candidate.evidence,
                },
            )


def task_title_exists(project: Path, title: str) -> bool:
    with database(project) as db:
        apply_schema(db)
        row = db.execute("select 1 from tasks where title = ? limit 1", (title,)).fetchone()
    return row is not None


def mark_frontier_enqueued(project: Path, candidate: FrontierCandidate, task_id: int) -> None:
    evidence = candidate_evidence_text(candidate)
    with database(project) as db:
        apply_schema(db)
        db.execute(
            "update frontier_items set status = ? where title = ? and evidence = ?",
            ("enqueued", candidate.title, evidence),
        )
        record_event(
            db,
            "frontier_task_added",
            {
                "id": task_id,
                "title": candidate.title,
                "resource": candidate.resource,
                "score": candidate.score,
                "evidence": candidate.evidence,
            },
        )


def read_frontier_text(path: Path, limit: int = 200_000) -> str:
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def discover_todo_frontiers(project: Path, files: Iterable[str]) -> list[FrontierCandidate]:
    candidates: list[FrontierCandidate] = []
    for path_text in files:
        path = PurePosixPath(path_text)
        if path.suffix.lower() not in TEXT_FRONTIER_SUFFIXES:
            continue
        content = read_frontier_text(project / path_text)
        if not content:
            continue
        for line_number, line in enumerate(content.splitlines(), start=1):
            lowered = line.lower()
            marker = next((item for item in TODO_MARKERS if item in lowered), "")
            if not marker:
                continue
            snippet = compact_text(line)
            candidates.append(
                FrontierCandidate(
                    title=f"Resolve {marker.upper()} in {path_text}:{line_number}",
                    resource=path_text,
                    evidence={"kind": "todo", "path": path_text, "line": line_number, "snippet": snippet},
                    score=100,
                )
            )
            break
    return candidates


def discover_test_gap_frontiers(project: Path, files: Iterable[str]) -> list[FrontierCandidate]:
    file_list = list(files)
    test_files = [path for path in file_list if looks_like_test_path(path)]
    tests_dir_exists = (project / "tests").is_dir()
    candidates: list[FrontierCandidate] = []
    for path_text in file_list:
        path = PurePosixPath(path_text)
        suffix = path.suffix.lower()
        if suffix not in SOURCE_SUFFIXES or looks_like_test_path(path_text):
            continue
        stem = path.stem.lower()
        has_named_test = any(stem and stem in PurePosixPath(test_path).stem.lower() for test_path in test_files)
        if has_named_test:
            continue
        resource = "tests" if tests_dir_exists else "."
        candidates.append(
            FrontierCandidate(
                title=f"Add focused coverage for {path_text}",
                resource=resource,
                evidence={"kind": "test_gap", "path": path_text, "tests_dir": tests_dir_exists},
                score=70 if tests_dir_exists else 55,
            )
        )
    return candidates


def discover_frontier_candidates(project: Path, profile: ProjectProfile, *, limit: int = 20) -> list[FrontierCandidate]:
    files = list_project_files(project)
    candidates = discover_todo_frontiers(project, files)
    candidates.extend(discover_test_gap_frontiers(project, files))
    if profile.suggested_checks:
        check = profile.suggested_checks[0]
        candidates.append(
            FrontierCandidate(
                title=f"Document how to enable suggested check: {check.name}",
                resource=".",
                evidence={"kind": "suggested_check", "check": check.name, "reason": check.reason},
                score=40,
            )
        )

    deduped: dict[tuple[str, str], FrontierCandidate] = {}
    for candidate in candidates:
        try:
            resource = normalize_resource(project, candidate.resource)
        except ValueError:
            continue
        normalized = FrontierCandidate(candidate.title, resource, candidate.evidence, candidate.score)
        deduped.setdefault((normalized.title, candidate_evidence_text(normalized)), normalized)
    return sorted(deduped.values(), key=lambda item: (-item.score, item.title))[:limit]


def ensure_frontier_task(project: Path, profile: ProjectProfile) -> Optional[int]:
    candidates = discover_frontier_candidates(project, profile)
    if not candidates:
        return None
    store_frontier_candidates(project, candidates)
    for candidate in candidates:
        if task_title_exists(project, candidate.title):
            continue
        task_id = enqueue_task(project, candidate.title, resource=candidate.resource)
        update_task_payload(
            project,
            task_id,
            {
                "origin": "frontier",
                "frontier_score": candidate.score,
                "frontier_evidence": candidate.evidence,
            },
        )
        mark_frontier_enqueued(project, candidate, task_id)
        return task_id
    return None


def run_logged_test_command(
    handle,
    worktree: Path,
    name: str,
    cmd: list[str],
    *,
    timeout_seconds: int = TEST_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    command_text = " ".join(sh_quote(part) for part in cmd)
    handle.write(f"\n--- {name}: {command_text} ---\n")
    handle.flush()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(worktree),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        handle.write(output)
        message = f"{name} timed out after {timeout_seconds}s"
        handle.write(f"\n{message}\n")
        return False, message

    handle.write(result.stdout or "")
    if result.returncode != 0:
        return False, f"{name} exited {result.returncode}"
    return True, f"{name} passed"


def run_tester_gate(
    project: Path,
    worktree: Path,
    task: TaskRecord,
    changed_files: Iterable[str],
    *,
    timeout_seconds: int = TEST_TIMEOUT_SECONDS,
) -> TestGateResult:
    log_file = run_log_path(project, "tester", task.id)
    changed_file_list = list(changed_files)
    checks = build_tester_checks(worktree, changed_file_list)
    baseline_worktree: Optional[Path] = None
    baseline_failures: list[str] = []

    try:
        with log_file.open("w", encoding="utf-8") as handle:
            handle.write(f"{utc_now()} tester gate for task #{task.id}\n")
            handle.write(f"worktree: {worktree}\n")
            handle.write(f"changed_files: {', '.join(changed_file_list)}\n")
            handle.write(f"timeout_seconds: {timeout_seconds}\n")
            for check in checks:
                if baseline_aware_check(check):
                    if baseline_worktree is None:
                        baseline_worktree = create_baseline_worktree(project, task)
                        handle.write(f"baseline_worktree: {baseline_worktree}\n")
                    baseline_ok, baseline_message = run_logged_test_command(
                        handle,
                        baseline_worktree,
                        f"baseline:{check.name}",
                        list(check.command),
                        timeout_seconds=timeout_seconds,
                    )
                    if not baseline_ok:
                        baseline_failures.append(f"{check.name}: {baseline_message}")
                        handle.write(
                            f"\n{utc_now()} baseline already failing for {check.name}; "
                            "patched result is diagnostic only\n"
                        )
                        patched_ok, patched_message = run_logged_test_command(
                            handle,
                            worktree,
                            check.name,
                            list(check.command),
                            timeout_seconds=timeout_seconds,
                        )
                        if patched_ok:
                            handle.write(f"\n{utc_now()} patched {check.name} passed despite failing baseline\n")
                        else:
                            handle.write(f"\n{utc_now()} patched {check.name} still failing: {patched_message}\n")
                        continue

                ok, message = run_logged_test_command(
                    handle,
                    worktree,
                    check.name,
                    list(check.command),
                    timeout_seconds=timeout_seconds,
                )
                if not ok:
                    handle.write(f"\n{utc_now()} tester failed: {message}\n")
                    return TestGateResult(False, relative_to_project(project, log_file), message)

            if baseline_failures:
                message = "tester passed with pre-existing failures: " + "; ".join(baseline_failures)
            else:
                message = "tester passed"
            handle.write(f"\n{utc_now()} {message}\n")
            return TestGateResult(True, relative_to_project(project, log_file), message, tuple(baseline_failures))
    finally:
        if baseline_worktree is not None:
            remove_git_worktree(project, baseline_worktree)


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
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            prompt,
        ]
    raise ValueError(f"unknown agent: {agent}")


def build_resident_prompt(agent: str, project: Path) -> str:
    peer = "claude" if agent == "codex" else "codex"
    tell_example = module_command(project, "tell", peer, "note", "<message>")
    report_done_example = module_command(project, "report", "done", "--task-id", "N", "<message>")
    report_blocked_example = module_command(project, "report", "blocked", "--task-id", "N", "<reason>")
    return "\n".join(
        [
            f"You are the persistent {agent} agent running under mmux resident mode.",
            "",
            "This tmux session is the shared coordination surface.",
            f"- Your peer is {peer}; talk to them in the tmux session when decisions need discussion.",
            f"- To message your peer from a tool shell, run: {tell_example}",
            "- Treat lines beginning with MMUX_TASK, MMUX_REVIEW, or MMUX_NOTE as control messages.",
            f"- Preferred done channel: {report_done_example}",
            f"- Preferred blocked channel: {report_blocked_example}",
            "- If the report command is unavailable, print a single MMUX_DONE or MMUX_BLOCKED line with the task id.",
            "",
            "Work only in this resident git worktree. Keep changes scoped and testable.",
            "After a done report, mmux may snapshot your diff and reset this resident worktree to HEAD.",
            "Do not edit .mmux, secrets, or files outside the worktree.",
            "A deterministic mmux gate outside the model will inspect diffs and run tests before applying anything.",
            f"Project root: {project}",
        ]
    )


def build_resident_command(agent: str, worktree: Path, prompt: str) -> str:
    if agent == "codex":
        return " ".join(
            [
                "codex",
                "--cd",
                sh_quote(str(worktree)),
                "--sandbox",
                "workspace-write",
                "--ask-for-approval",
                "never",
                "--no-alt-screen",
                sh_quote(prompt),
            ]
        )
    if agent == "claude":
        return " ".join(
            [
                "claude",
                "--permission-mode",
                "acceptEdits",
                sh_quote(prompt),
            ]
        )
    raise ValueError(f"unknown agent: {agent}")


def stop_process_tree(process: subprocess.Popen[str]) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    except OSError:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        process.wait()


def stream_agent_command(
    project: Path,
    cmd: list[str],
    log_file: Path,
    *,
    timeout_seconds: int = AGENT_TIMEOUT_SECONDS,
    no_output_timeout_seconds: int = AGENT_NO_OUTPUT_TIMEOUT_SECONDS,
) -> AdapterResult:
    started = time.monotonic()
    last_output = started
    command_text = " ".join(sh_quote(part) for part in cmd)
    with log_file.open("w", encoding="utf-8") as handle:
        handle.write(f"{utc_now()} command: {command_text}\n\n")
        handle.write(f"timeout_seconds: {timeout_seconds}\n")
        handle.write(f"no_output_timeout_seconds: {no_output_timeout_seconds}\n\n")
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
            start_new_session=True,
        )
        assert process.stdout is not None
        timeout_reason = ""
        while True:
            now = time.monotonic()
            if not timeout_reason and now - started > timeout_seconds:
                timeout_reason = f"agent command timed out after {timeout_seconds}s"
                stop_process_tree(process)
            if not timeout_reason and now - last_output > no_output_timeout_seconds:
                timeout_reason = f"agent command produced no output for {no_output_timeout_seconds}s"
                stop_process_tree(process)
            ready, _write, _error = select.select([process.stdout], [], [], 1)
            if ready:
                chunk = process.stdout.readline()
                if chunk:
                    last_output = time.monotonic()
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
            if timeout_reason:
                break
        process.stdout.close()
        returncode = process.returncode if process.returncode is not None else 124
        if timeout_reason:
            handle.write(f"\n{utc_now()} {timeout_reason}\n")
            return AdapterResult(False, 124, relative_to_project(project, log_file), timeout_reason)
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
    *,
    timeout_seconds: int = AGENT_TIMEOUT_SECONDS,
    no_output_timeout_seconds: int = AGENT_NO_OUTPUT_TIMEOUT_SECONDS,
) -> AdapterResult:
    prompt = build_agent_prompt(agent, task, role_generation, resource)
    log_file = run_log_path(project, agent, task.id)
    output_file = log_file.with_suffix(".last-message.txt")
    cmd = build_agent_command(agent, execution_root, prompt, output_file)
    result = stream_agent_command(
        execution_root,
        cmd,
        log_file,
        timeout_seconds=timeout_seconds,
        no_output_timeout_seconds=no_output_timeout_seconds,
    )
    if output_file.exists():
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write("\n--- last message ---\n")
            handle.write(output_file.read_text(encoding="utf-8", errors="replace"))
            handle.write("\n")
    return result


def is_adapter_health_failure(result: AdapterResult) -> bool:
    return result.returncode == 124 and (
        "produced no output" in result.message or "timed out" in result.message
    )


def tmux_has_session(name: str) -> bool:
    result = run(["tmux", "has-session", "-t", name], check=False)
    return result.returncode == 0


def resident_pane_target(project: Path, agent: str) -> str:
    validate_agent(agent)
    stored = tmux_pane_target_from_meta(project, agent)
    if stored:
        return stored
    return f"{session_name(project)}:0.{RESIDENT_PANE_INDEXES[agent]}"


def format_resident_control_message(kind: str, message: str, *, task_id: int = 0, sender: str = "mmux") -> str:
    if kind not in RESIDENT_MESSAGE_KINDS:
        raise ValueError(f"unknown resident message kind: {kind}")
    normalized = " ".join(message.split())
    parts = [f"MMUX_{kind.upper()}", f"from={sender}"]
    if task_id:
        parts.append(f"task=#{task_id}")
    if normalized:
        parts.append(normalized)
    return " ".join(parts)


def resident_line_hash(agent: str, line: str) -> str:
    return hashlib.sha1(f"{agent}\0{line}".encode("utf-8")).hexdigest()


def normalize_resident_protocol_line(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("`") and stripped.endswith("`"):
        stripped = stripped.strip("`").strip()
    if stripped.startswith("<<MMUX:") and stripped.endswith(">>"):
        inner = stripped[2:-2].strip()
        if inner.startswith("MMUX:DONE"):
            return "MMUX_DONE" + inner[len("MMUX:DONE") :]
        if inner.startswith("MMUX:BLOCKED"):
            return "MMUX_BLOCKED" + inner[len("MMUX:BLOCKED") :]
    return stripped


def format_resident_report_line(kind: str, agent: str, task_id: int, message: str) -> str:
    validate_agent(agent)
    if kind not in ("done", "blocked"):
        raise ValueError(f"unknown resident report kind: {kind}")
    normalized = " ".join(message.split())
    parts = [f"MMUX_{kind.upper()}", f"from={agent}", f"task=#{task_id}"]
    if normalized:
        parts.append(normalized)
    return " ".join(parts)


def task_id_from_token(token: str) -> Optional[int]:
    cleaned = token.strip().rstrip(".,;:")
    if cleaned.startswith("task=#"):
        value = cleaned[6:]
    elif cleaned.startswith("task="):
        value = cleaned[5:].lstrip("#")
    elif cleaned.startswith("#"):
        value = cleaned[1:]
    else:
        return None
    return int(value) if value.isdigit() else None


def parse_resident_protocol_line(agent: str, line: str) -> Optional[ResidentProtocolEvent]:
    validate_agent(agent)
    stripped = normalize_resident_protocol_line(line)
    if not stripped.startswith("MMUX_"):
        return None
    tokens = stripped.split()
    if not tokens:
        return None
    marker = tokens[0]
    if marker == "MMUX_DONE":
        kind = "done"
    elif marker == "MMUX_BLOCKED":
        kind = "blocked"
    else:
        return None

    task_id = 0
    message_tokens = []
    for token in tokens[1:]:
        parsed_task_id = task_id_from_token(token)
        if parsed_task_id is not None:
            task_id = parsed_task_id
            continue
        if token.startswith("from="):
            continue
        message_tokens.append(token)

    message = " ".join(message_tokens)
    canonical = format_resident_report_line(kind, agent, task_id, message)
    return ResidentProtocolEvent(
        agent=agent,
        kind=kind,
        task_id=task_id,
        message=message,
        line=stripped,
        line_hash=resident_line_hash(agent, canonical),
    )


def seen_resident_hashes_db(db: sqlite3.Connection, agent: str) -> list[str]:
    value = meta_json_db(db, resident_seen_key(agent))
    hashes = value.get("hashes")
    if not isinstance(hashes, list):
        return []
    return [str(item) for item in hashes if isinstance(item, str)]


def record_resident_protocol_events(project: Path, events: Iterable[ResidentProtocolEvent]) -> list[ResidentProtocolEvent]:
    grouped = {agent: [] for agent in AGENTS}
    for event in events:
        validate_agent(event.agent)
        grouped[event.agent].append(event)

    recorded: list[ResidentProtocolEvent] = []
    with database(project) as db:
        apply_schema(db)
        for agent, agent_events in grouped.items():
            if not agent_events:
                continue
            seen = seen_resident_hashes_db(db, agent)
            seen_set = set(seen)
            for event in agent_events:
                if event.line_hash in seen_set:
                    continue
                record_event(
                    db,
                    f"resident_agent_{event.kind}",
                    {
                        "agent": event.agent,
                        "task_id": event.task_id,
                        "message": event.message,
                        "line": event.line,
                        "line_hash": event.line_hash,
                    },
                )
                seen.append(event.line_hash)
                seen_set.add(event.line_hash)
                recorded.append(event)
            set_meta_json_db(db, resident_seen_key(agent), {"hashes": seen[-200:]})
    return recorded


def report_resident_protocol_event(
    project: Path,
    *,
    agent: str,
    kind: str,
    task_id: int,
    message: str,
) -> tuple[bool, list[str], ResidentProtocolEvent]:
    line = format_resident_report_line(kind, agent, task_id, message)
    event = parse_resident_protocol_line(agent, line)
    if event is None:
        raise RuntimeError(f"could not parse resident report line: {line}")
    recorded = record_resident_protocol_events(project, [event])
    if not recorded:
        return False, [], event
    return True, process_resident_protocol_events(project, recorded), event


def record_resident_processing_event(project: Path, kind: str, event: ResidentProtocolEvent, payload: dict[str, object]) -> None:
    data = {
        "agent": event.agent,
        "task_id": event.task_id,
        "resident_event": event.line_hash,
    }
    data.update(payload)
    record_project_event(project, kind, data)


def resident_blocked_history(task: TaskRecord) -> list[dict[str, object]]:
    raw_history = task.payload.get("resident_blocked_history")
    if not isinstance(raw_history, list):
        return []
    return [item for item in raw_history if isinstance(item, dict)]


def resident_blocked_agents(history: Iterable[dict[str, object]]) -> list[str]:
    agents = []
    for item in history:
        agent = item.get("agent")
        if isinstance(agent, str) and agent in AGENTS and agent not in agents:
            agents.append(agent)
    return agents


def process_resident_done_event(project: Path, event: ResidentProtocolEvent) -> str:
    if event.task_id <= 0:
        record_resident_processing_event(project, "resident_done_ignored", event, {"reason": "missing task id"})
        return "ignored: missing task id"

    task = get_task(project, event.task_id)
    if task is None:
        record_resident_processing_event(project, "resident_done_ignored", event, {"reason": "unknown task"})
        return f"ignored: unknown task #{event.task_id}"
    if task.status != "pending":
        record_resident_processing_event(
            project,
            "resident_done_ignored",
            event,
            {"reason": f"task status is {task.status}", "status": task.status},
        )
        return f"ignored: task #{task.id} status is {task.status}"

    resident_worktree = resident_worktree_path(project, event.agent)
    if not resident_worktree.exists():
        record_resident_processing_event(project, "resident_done_ignored", event, {"reason": "resident worktree missing"})
        return f"ignored: {event.agent} resident worktree missing"

    resource = task_resource(task)
    policy = check_diff_policy(project, resident_worktree, resource)
    payload_updates = {
        "resident_agent": event.agent,
        "resident_event": event.line_hash,
        "resident_message": event.message,
        "diff_policy": policy.status,
        "changed_files": list(policy.changed_files),
    }

    if policy.status == "no_change":
        finish_task(
            project,
            task.id,
            "no_change",
            message=policy.reason,
            payload_updates=payload_updates,
        )
        reset_resident_worktree(resident_worktree)
        record_resident_processing_event(project, "resident_done_no_change", event, {"reason": policy.reason})
        return f"task #{task.id} no_change from resident {event.agent}"

    if not policy.ok:
        finish_task(
            project,
            task.id,
            "rejected",
            message=policy.reason,
            payload_updates=payload_updates,
        )
        reset_resident_worktree(resident_worktree)
        record_resident_processing_event(project, "resident_done_rejected", event, {"reason": policy.reason})
        return f"task #{task.id} rejected from resident {event.agent}: {policy.reason}"

    snapshot = create_task_worktree(project, task, event.agent)
    try:
        patch = export_resident_patch(resident_worktree)
        apply_patch_to_worktree(snapshot, patch)
    except Exception as exc:
        finish_task(
            project,
            task.id,
            "failed",
            message=f"resident snapshot failed: {exc}",
            payload_updates=payload_updates,
        )
        record_resident_processing_event(project, "resident_done_failed", event, {"reason": str(exc)})
        return f"task #{task.id} failed: resident snapshot failed: {exc}"

    snapshot_policy = check_diff_policy(project, snapshot, resource)
    payload_updates.update(
        {
            "worktree": relative_to_project(project, snapshot),
            "resident_source_worktree": relative_to_project(project, resident_worktree),
            "diff_policy": snapshot_policy.status,
            "changed_files": list(snapshot_policy.changed_files),
        }
    )
    if not snapshot_policy.ok or snapshot_policy.status == "no_change":
        status = "no_change" if snapshot_policy.status == "no_change" else "rejected"
        finish_task(
            project,
            task.id,
            status,
            message=snapshot_policy.reason,
            payload_updates=payload_updates,
        )
        reset_resident_worktree(resident_worktree)
        record_resident_processing_event(
            project,
            f"resident_done_{status}",
            event,
            {"reason": snapshot_policy.reason, "worktree": relative_to_project(project, snapshot)},
        )
        return f"task #{task.id} {status} after resident snapshot"

    move_task_to_status(
        project,
        task.id,
        "awaiting_test",
        message=event.message,
        payload_updates=payload_updates,
    )
    reset_resident_worktree(resident_worktree)
    record_resident_processing_event(
        project,
        "resident_done_awaiting_test",
        event,
        {"worktree": relative_to_project(project, snapshot), "changed_files": list(snapshot_policy.changed_files)},
    )
    return f"task #{task.id} awaiting_test from resident {event.agent}"


def process_resident_blocked_event(project: Path, event: ResidentProtocolEvent) -> str:
    if event.task_id <= 0:
        record_resident_processing_event(project, "resident_blocked_ignored", event, {"reason": "missing task id"})
        return "ignored: missing task id"

    task = get_task(project, event.task_id)
    if task is None:
        record_resident_processing_event(project, "resident_blocked_ignored", event, {"reason": "unknown task"})
        return f"ignored: unknown task #{event.task_id}"
    if task.status != "pending":
        record_resident_processing_event(
            project,
            "resident_blocked_ignored",
            event,
            {"reason": f"task status is {task.status}", "status": task.status},
        )
        return f"ignored: task #{task.id} status is {task.status}"

    peer = peer_agent(event.agent)
    history = resident_blocked_history(task)
    history.append(
        {
            "agent": event.agent,
            "message": event.message,
            "event": event.line_hash,
            "at": utc_now(),
        }
    )
    blocked_agents = resident_blocked_agents(history)
    payload_updates = {
        "resident_blocked_by": event.agent,
        "resident_blocked_reason": event.message,
        "resident_blocked_at": utc_now(),
        "resident_blocked_event": event.line_hash,
        "resident_takeover_agent": peer,
        "resident_blocked_count": len(history),
        "resident_blocked_agents": blocked_agents,
        "resident_blocked_history": history[-10:],
    }

    if len(history) >= RESIDENT_BLOCKED_ESCALATION_EVENTS or len(blocked_agents) >= len(AGENTS):
        reason = f"resident blocked {len(history)} time(s): {event.message}"
        payload_updates.update(
            {
                "resident_escalated_at": utc_now(),
                "resident_escalation_reason": reason,
            }
        )
        finish_task(project, task.id, "blocked", message=reason, payload_updates=payload_updates)
        record_resident_processing_event(
            project,
            "resident_blocked_escalated",
            event,
            {
                "message": event.message,
                "blocked_count": len(history),
                "blocked_agents": blocked_agents,
                "reason": reason,
            },
        )
        return f"task #{task.id} escalated to blocked after {len(history)} resident block(s)"

    update_task_payload(project, task.id, payload_updates)

    delivered = False
    takeover_line = format_resident_control_message(
        "task",
        f"take over task #{task.id}: {task.title}; {event.agent} blocked: {event.message}",
        task_id=task.id,
    )
    if resident_mode_enabled(project) and tmux_has_session(session_name(project)):
        try:
            send_tmux_message(project, peer, takeover_line)
            delivered = True
        except Exception:
            delivered = False

    record_resident_processing_event(
        project,
        "resident_blocked_takeover_requested",
        event,
        {
            "message": event.message,
            "peer": peer,
            "delivered": delivered,
            "takeover_line": takeover_line,
        },
    )
    return f"resident {event.agent} blocked task #{task.id}; takeover requested from {peer}"


def process_resident_protocol_events(project: Path, events: Iterable[ResidentProtocolEvent]) -> list[str]:
    messages = []
    for event in events:
        try:
            if event.kind == "done":
                messages.append(process_resident_done_event(project, event))
            elif event.kind == "blocked":
                messages.append(process_resident_blocked_event(project, event))
        except Exception as exc:
            record_resident_processing_event(project, "resident_event_processing_failed", event, {"reason": str(exc)})
            messages.append(f"resident event failed: {exc}")
    return messages


def capture_resident_pane_output(project: Path, agent: str) -> str:
    result = run(
        ["tmux", "capture-pane", "-p", "-J", "-S", "-", "-t", resident_pane_target(project, agent)],
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def scan_resident_agent_output(project: Path) -> int:
    if not resident_mode_enabled(project):
        return 0
    if not tmux_has_session(session_name(project)):
        return 0

    events = []
    for agent in AGENTS:
        output = capture_resident_pane_output(project, agent)
        for line in output.splitlines():
            event = parse_resident_protocol_line(agent, line)
            if event is not None:
                events.append(event)

    recorded_events = record_resident_protocol_events(project, events)
    if recorded_events:
        messages = process_resident_protocol_events(project, recorded_events)
        write_log(project, f"recorded {len(recorded_events)} resident protocol event(s)")
        for message in messages:
            write_log(project, f"resident event: {message}")
    return len(recorded_events)



def send_tmux_message(project: Path, agent: str, message: str) -> None:
    validate_agent(agent)
    name = session_name(project)
    if not tmux_has_session(name):
        raise RuntimeError(f"tmux session is not running: {name}")
    target = resident_pane_target(project, agent)
    run(["tmux", "send-keys", "-t", target, "-l", message])
    run(["tmux", "send-keys", "-t", target, "Enter"])
    write_log(project, f"sent resident message to {agent}: {message[:200]}")


def tmux_result_target(result: subprocess.CompletedProcess[str], fallback: str) -> str:
    target = result.stdout.strip()
    return target or fallback


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


def format_names(values: Iterable[str]) -> str:
    items = list(values)
    return ", ".join(items) if items else "none"


def format_check_names(checks: Iterable[ProjectCheck]) -> str:
    return ", ".join(check.name for check in checks) or "none"


def cmd_inspect(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    if not project.exists():
        raise SystemExit(f"Project path does not exist: {project}")
    profile = inspect_project(project)
    if args.json:
        print(json.dumps(profile_to_dict(profile), indent=2))
        return 0

    print(f"project: {project}")
    print(f"ecosystems: {format_names(profile.ecosystems)}")
    print(f"languages: {format_names(profile.languages)}")
    print("markers:")
    if profile.markers:
        for marker in profile.markers:
            print(f"  {marker}")
    else:
        print("  none")
    print("active checks:")
    for check in profile.active_checks:
        print(f"  {check.name}: {command_text(check.command)}")
        print(f"    reason: {check.reason}")
    print("suggested checks:")
    if profile.suggested_checks:
        for check in profile.suggested_checks:
            print(f"  {check.name}: {command_text(check.command)}")
            print(f"    reason: {check.reason}")
    else:
        print("  none")
    return 0


def cmd_frontier(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    if not project.exists():
        raise SystemExit(f"Project path does not exist: {project}")
    profile = inspect_project(project)
    candidates = discover_frontier_candidates(project, profile, limit=args.limit)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "title": candidate.title,
                        "resource": candidate.resource,
                        "score": candidate.score,
                        "evidence": candidate.evidence,
                    }
                    for candidate in candidates
                ],
                indent=2,
            )
        )
        return 0

    print(f"project: {project}")
    print("frontier candidates:")
    if not candidates:
        print("  none")
        return 0
    for candidate in candidates:
        evidence_kind = candidate.evidence.get("kind", "")
        print(f"  score={candidate.score} resource={candidate.resource} kind={evidence_kind}")
        print(f"    {candidate.title}")
    return 0


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
        resident_enabled = meta_json_db(db, resident_mode_key()).get("enabled") is True
    cooldowns = list_agent_cooldowns(project)

    print(f"project: {project}")
    print(f"session: {session_name(project)}")
    print(f"resident agents: {'enabled' if resident_enabled else 'disabled'}")
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
    print("agent cooldowns:")
    if cooldowns:
        for agent, until, reason in cooldowns:
            print(f"  {agent}: until={until} reason={reason}")
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


TASK_STATUS_ORDER = (
    "pending",
    "running",
    "awaiting_test",
    "running_test",
    "completed",
    "failed",
    "rejected",
    "no_change",
    "blocked",
)
OPEN_TASK_STATUSES = ("pending", "running", "awaiting_test", "running_test")


def task_status_counts(project: Path) -> dict[str, int]:
    with database(project) as db:
        apply_schema(db)
        rows = db.execute("select status, count(*) from tasks group by status").fetchall()
    return {status: int(count) for status, count in rows}


def ordered_statuses(*counts: dict[str, int]) -> list[str]:
    seen = set(TASK_STATUS_ORDER)
    extras = sorted({status for count in counts for status in count if status not in seen})
    return list(TASK_STATUS_ORDER) + extras


def format_task_counts(counts: dict[str, int]) -> str:
    parts = [f"{status}={counts[status]}" for status in ordered_statuses(counts) if counts.get(status, 0)]
    return ", ".join(parts) if parts else "none"


def format_task_delta(before: dict[str, int], after: dict[str, int]) -> str:
    parts = []
    for status in ordered_statuses(before, after):
        delta = after.get(status, 0) - before.get(status, 0)
        if delta:
            sign = "+" if delta > 0 else ""
            parts.append(f"{status}={sign}{delta}")
    return ", ".join(parts) if parts else "no change"


def has_open_tasks(project: Path) -> bool:
    counts = task_status_counts(project)
    return any(counts.get(status, 0) > 0 for status in OPEN_TASK_STATUSES)


def available_driver_agents(project: Path, now: Optional[dt.datetime] = None) -> tuple[str, ...]:
    ordered_agents = agent_order_for_slot(now)
    with database(project) as db:
        apply_schema(db)
        healthy = tuple(agent for agent in ordered_agents if not agent_is_cooled_down_db(db, agent, now))
    return healthy


def paired_tester_agent(driver: str, ordered_agents: tuple[str, ...]) -> str:
    return next((agent for agent in ordered_agents if agent != driver), driver)


def supervisor_role_plan_for_project(project: Path, now: Optional[dt.datetime] = None) -> tuple[tuple[str, str], ...]:
    counts = task_status_counts(project)
    agents = agent_order_for_slot(now)
    driver_agents = available_driver_agents(project, now)
    if counts.get("awaiting_test", 0) > 0:
        driver = driver_agents[0] if driver_agents else agents[1]
        tester = paired_tester_agent(driver, agents)
        return (("tester", tester), ("driver", driver))
    if counts.get("pending", 0) > 0:
        if not driver_agents:
            return (("scout", agents[0]), ("reviewer", agents[1]))
        driver = driver_agents[0]
        tester = paired_tester_agent(driver, agents)
        return (("driver", driver), ("tester", tester))
    return supervisor_role_plan(now)


def default_task_title(profile: ProjectProfile) -> str:
    ecosystem = ", ".join(profile.ecosystems) if profile.ecosystems else "general"
    return (
        f"Find one small, testable improvement in this {ecosystem} project; "
        "keep the diff minimal and verify it with local checks."
    )


def ensure_default_task(project: Path, profile: ProjectProfile) -> Optional[int]:
    if has_open_tasks(project):
        return None
    frontier_task_id = ensure_frontier_task(project, profile)
    if frontier_task_id is not None:
        return frontier_task_id
    title = default_task_title(profile)
    task_id = enqueue_task(project, title, resource=".")
    update_task_payload(
        project,
        task_id,
        {
            "origin": "auto_default",
            "ecosystems": list(profile.ecosystems),
            "active_checks": [check.name for check in profile.active_checks],
        },
    )
    record_project_event(
        project,
        "default_task_added",
        {"id": task_id, "title": title, "resource": "."},
    )
    return task_id


def maybe_replenish_default_task(
    project: Path,
    profile: ProjectProfile,
    *,
    disabled: bool,
    remaining_seconds: int,
    shutdown_grace_seconds: int,
) -> Optional[int]:
    if disabled:
        return None
    if remaining_seconds < MIN_EXECUTION_BUDGET_SECONDS + shutdown_grace_seconds:
        return None
    return ensure_default_task(project, profile)


def cmd_task_add(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_existing_schema(project)
    task_id = enqueue_task(project, args.title, resource=args.resource)
    print(f"task added: #{task_id} resource={normalize_resource(project, args.resource)}")
    return 0


def cmd_task_requeue(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_existing_schema(project)
    outcome = requeue_task(project, args.task_id, reason=args.reason)
    if not outcome.ok:
        print(f"requeue denied: #{outcome.task_id} {outcome.reason}")
        return 2
    if outcome.from_status == "pending":
        print(f"task already pending: #{outcome.task_id}")
        return 0
    print(f"task requeued: #{outcome.task_id} {outcome.from_status} -> {outcome.to_status}")
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


def positive_task_id(value: str) -> int:
    normalized = value.strip().lstrip("#")
    try:
        parsed = int(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a task id") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive task id")
    return parsed


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
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


def cmd_tell(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    require_project_state(project)
    message = format_resident_control_message(args.kind, args.message, task_id=args.task_id)
    try:
        send_tmux_message(project, args.agent, message)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"sent to {args.agent}: {message}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    project, detected_agent = resolve_report_project(args.project)
    require_project_state(project)
    agent = args.agent or detected_agent
    if not agent:
        raise SystemExit("--agent is required unless running from a resident worktree")
    message = " ".join(args.message).strip()
    try:
        recorded, messages, event = report_resident_protocol_event(
            project,
            agent=agent,
            kind=args.kind,
            task_id=args.task_id,
            message=message,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if not recorded:
        print(f"duplicate report ignored: {event.line}")
        return 0
    print(f"reported: {event.line}")
    for item in messages:
        print(item)
    return 0


def start_tmux_session(
    project: Path,
    *,
    execute_agents: bool = False,
    resident_agents: bool = False,
    run_deadline: str = "",
    agent_timeout_seconds: int = AGENT_TIMEOUT_SECONDS,
    agent_no_output_seconds: int = AGENT_NO_OUTPUT_TIMEOUT_SECONDS,
    test_timeout_seconds: int = TEST_TIMEOUT_SECONDS,
    shutdown_grace_seconds: int = RUN_SHUTDOWN_GRACE_SECONDS,
) -> bool:
    name = session_name(project)
    if tmux_has_session(name):
        return False

    supervisor_cmd = module_command(project, "supervisor")
    worker_flags = ["--execute-agents"] if execute_agents else []
    worker_flags.extend(
        [
            "--agent-timeout-seconds",
            str(agent_timeout_seconds),
            "--agent-no-output-seconds",
            str(agent_no_output_seconds),
            "--test-timeout-seconds",
            str(test_timeout_seconds),
            "--shutdown-grace-seconds",
            str(shutdown_grace_seconds),
        ]
    )
    if run_deadline:
        worker_flags.extend(["--run-deadline", run_deadline])
    codex_worker_cmd = module_command(project, "worker", *worker_flags, "codex")
    claude_worker_cmd = module_command(project, "worker", *worker_flags, "claude")
    log_cmd = f"mkdir -p .mmux/logs; touch .mmux/logs/supervisor.log; tail -f .mmux/logs/supervisor.log"

    if resident_agents:
        codex_worktree = prepare_resident_worktree(project, "codex")
        claude_worktree = prepare_resident_worktree(project, "claude")
        codex_cmd = build_resident_command("codex", codex_worktree, build_resident_prompt("codex", project))
        claude_cmd = build_resident_command("claude", claude_worktree, build_resident_prompt("claude", project))
    else:
        codex_cmd = codex_worker_cmd
        claude_cmd = claude_worker_cmd

    supervisor_result = run(
        ["tmux", "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name, "-c", str(project), supervisor_cmd]
    )
    supervisor_pane = tmux_result_target(supervisor_result, f"{name}:0.0")
    codex_result = run(
        ["tmux", "split-window", "-h", "-P", "-F", "#{pane_id}", "-t", supervisor_pane, "-c", str(project), codex_cmd]
    )
    codex_pane = tmux_result_target(codex_result, f"{name}:0.1")
    log_result = run(
        ["tmux", "split-window", "-v", "-P", "-F", "#{pane_id}", "-t", supervisor_pane, "-c", str(project), log_cmd]
    )
    log_pane = tmux_result_target(log_result, f"{name}:0.2")
    claude_result = run(
        ["tmux", "split-window", "-v", "-P", "-F", "#{pane_id}", "-t", codex_pane, "-c", str(project), claude_cmd]
    )
    claude_pane = tmux_result_target(claude_result, f"{name}:0.3")
    set_tmux_pane_targets(
        project,
        {
            "supervisor": supervisor_pane,
            "codex": codex_pane,
            "log": log_pane,
            "claude": claude_pane,
        },
    )
    run(["tmux", "select-pane", "-t", supervisor_pane, "-T", "supervisor"], check=False)
    run(["tmux", "select-pane", "-t", codex_pane, "-T", "codex"], check=False)
    run(["tmux", "select-pane", "-t", log_pane, "-T", "log"], check=False)
    run(["tmux", "select-pane", "-t", claude_pane, "-T", "claude"], check=False)
    if resident_agents and execute_agents:
        run(["tmux", "new-window", "-t", name, "-n", "automation", "-c", str(project), codex_worker_cmd])
        run(["tmux", "split-window", "-v", "-t", f"{name}:1.0", "-c", str(project), claude_worker_cmd])
        run(["tmux", "select-window", "-t", f"{name}:0"])
    run(["tmux", "select-layout", "-t", name, "tiled"], check=False)

    set_resident_mode(project, resident_agents)
    mode = "resident" if resident_agents else "worker"
    write_log(project, f"started tmux session {name} mode={mode}")
    return True


def stop_tmux_session(project: Path) -> bool:
    name = session_name(project)
    if not shutil.which("tmux"):
        raise SystemExit("tmux is not installed")
    if not tmux_has_session(name):
        cleanup_runtime_state(project)
        if state_path(project).exists():
            set_resident_mode(project, False)
        return False
    run(["tmux", "kill-session", "-t", name])
    cleanup_runtime_state(project)
    if state_path(project).exists():
        set_resident_mode(project, False)
    write_log(project, f"stopped tmux session {name}")
    return True


def cmd_start(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_layout(project, args.task or "")
    name = session_name(project)
    if not start_tmux_session(project, execute_agents=args.execute_agents, resident_agents=args.resident_agents):
        print(f"tmux session already exists: {name}")
        return 0

    print(f"started tmux session: {name}")
    print(f"attach with: tmux attach -t {name}")
    if args.resident_agents:
        print("resident agents: enabled")
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
    if not stop_tmux_session(project):
        print(f"tmux session not running: {name}")
        return 0
    print(f"stopped tmux session: {name}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    if not project.exists():
        raise SystemExit(f"Project path does not exist: {project}")

    ensure_layout(project, args.task or "")
    name = session_name(project)
    if tmux_has_session(name):
        raise SystemExit(f"tmux session already exists: {name}. Stop it before running a timed window.")

    duration_seconds = args.seconds if args.seconds is not None else max(1, int(args.minutes * 60))
    checkpoint_seconds = min(args.checkpoint_seconds, duration_seconds)
    run_deadline = format_utc(utc_now_dt() + dt.timedelta(seconds=duration_seconds))
    profile = inspect_project(project)
    default_task_id = None
    if not args.no_default_task:
        default_task_id = ensure_default_task(project, profile)
    before = task_status_counts(project)
    started_at = utc_now()
    record_project_event(project, "project_inspected", profile_to_dict(profile))
    record_project_event(
        project,
        "run_started",
        {
            "duration_seconds": duration_seconds,
            "execute_agents": args.execute_agents,
            "resident_agents": args.resident_agents,
            "task": args.task or "",
            "default_task_id": default_task_id,
            "run_deadline": run_deadline,
            "agent_timeout_seconds": args.agent_timeout_seconds,
            "agent_no_output_seconds": args.agent_no_output_seconds,
            "test_timeout_seconds": args.test_timeout_seconds,
            "shutdown_grace_seconds": args.shutdown_grace_seconds,
        },
    )

    if not start_tmux_session(
        project,
        execute_agents=args.execute_agents,
        resident_agents=args.resident_agents,
        run_deadline=run_deadline,
        agent_timeout_seconds=args.agent_timeout_seconds,
        agent_no_output_seconds=args.agent_no_output_seconds,
        test_timeout_seconds=args.test_timeout_seconds,
        shutdown_grace_seconds=args.shutdown_grace_seconds,
    ):
        raise SystemExit(f"tmux session already exists: {name}")

    print(f"started timed run: {name}")
    print(f"duration: {duration_seconds}s")
    print(f"deadline: {run_deadline}")
    print(f"agent execution: {'enabled' if args.execute_agents else 'disabled'}")
    print(f"resident agents: {'enabled' if args.resident_agents else 'disabled'}")
    print(f"agent timeout max: {args.agent_timeout_seconds}s")
    print(f"agent no-output timeout: {args.agent_no_output_seconds}s")
    print(f"test timeout max: {args.test_timeout_seconds}s")
    print(f"profile: ecosystems={format_names(profile.ecosystems)} languages={format_names(profile.languages)}")
    print(f"active checks: {format_check_names(profile.active_checks)}")
    if default_task_id is not None:
        print(f"default task: added #{default_task_id}")
    elif args.no_default_task:
        print("default task: disabled")
    print(f"before: {format_task_counts(before)}")
    print(f"attach with: tmux attach -t {name}")

    deadline = time.monotonic() + duration_seconds
    interrupted = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(checkpoint_seconds, remaining))
            remaining_seconds = max(0, int(deadline - time.monotonic()))
            replenished_task_id = maybe_replenish_default_task(
                project,
                profile,
                disabled=args.no_default_task,
                remaining_seconds=remaining_seconds,
                shutdown_grace_seconds=args.shutdown_grace_seconds,
            )
            counts = task_status_counts(project)
            message = f"run checkpoint remaining={remaining_seconds}s tasks={format_task_counts(counts)}"
            if replenished_task_id is not None:
                message += f" default_task_added=#{replenished_task_id}"
            write_log(project, message)
            print(f"{utc_now()} {message}")
            sys.stdout.flush()
    except KeyboardInterrupt:
        interrupted = True
        write_log(project, "timed run interrupted")
    finally:
        stop_tmux_session(project)

    after = task_status_counts(project)
    finished_at = utc_now()
    record_project_event(
        project,
        "run_finished",
        {
            "started_at": started_at,
            "finished_at": finished_at,
            "interrupted": interrupted,
            "before": before,
            "after": after,
            "delta": format_task_delta(before, after),
        },
    )

    print("run summary:")
    print(f"  project: {project}")
    print(f"  session: {name}")
    print(f"  started_at: {started_at}")
    print(f"  finished_at: {finished_at}")
    print(f"  interrupted: {str(interrupted).lower()}")
    print(f"  before: {format_task_counts(before)}")
    print(f"  after: {format_task_counts(after)}")
    print(f"  delta: {format_task_delta(before, after)}")
    print(f"  supervisor_log: {relative_to_project(project, log_path(project))}")
    return 130 if interrupted else 0


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
            plan = supervisor_role_plan_for_project(project, now)
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
            try:
                resident_events = scan_resident_agent_output(project)
            except Exception as exc:
                resident_events = 0
                write_log(project, f"resident scan failed: {exc}")
            resident_text = f" resident_events={resident_events}" if resident_events else ""
            write_log(project, f"heartbeat slot={slot} ttl={ttl} leases={status}{conflict_text}{resident_text}")
            print(f"{utc_now()} slot={slot} ttl={ttl} leases={status}{conflict_text}{resident_text}")
            sys.stdout.flush()
            time.sleep(SUPERVISOR_TICK_SECONDS)
    except KeyboardInterrupt:
        write_log(project, "supervisor interrupted")
    return 0


def task_resource(task: TaskRecord) -> str:
    resource = task.payload.get("resource", ".")
    return str(resource) if isinstance(resource, str) else "."


def task_worktree_path(project: Path, task: TaskRecord) -> Optional[Path]:
    value = task.payload.get("worktree")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else project / path


def seconds_until_deadline(deadline: str) -> Optional[int]:
    if not deadline:
        return None
    remaining = int((parse_utc(deadline) - utc_now_dt()).total_seconds())
    return max(0, remaining)


def execution_budget_seconds(deadline: str, configured_timeout: int, shutdown_grace_seconds: int) -> int:
    remaining = seconds_until_deadline(deadline)
    if remaining is None:
        return configured_timeout
    return max(0, min(configured_timeout, remaining - shutdown_grace_seconds))


def format_budget(deadline: str, timeout_seconds: int, shutdown_grace_seconds: int) -> str:
    remaining = seconds_until_deadline(deadline)
    if remaining is None:
        return f"{timeout_seconds}s"
    return f"{execution_budget_seconds(deadline, timeout_seconds, shutdown_grace_seconds)}s remaining_budget={remaining}s"


def execute_driver_task(
    project: Path,
    agent: str,
    generation: int,
    *,
    timeout_seconds: int = AGENT_TIMEOUT_SECONDS,
    no_output_timeout_seconds: int = AGENT_NO_OUTPUT_TIMEOUT_SECONDS,
) -> str:
    task = claim_next_task(project, agent, "driver", generation)
    if task is None:
        return "no pending task"
    role = acquire_role(project, "driver", agent, timeout_seconds + 60, renew_if_same=True)
    if not role.ok or role.generation != generation:
        release_task_claim(project, task.id, "driver role lease is stale")
        return f"task #{task.id} requeued: driver role lease is stale"

    resource = task_resource(task)
    lock = acquire_resource_lock(
        project,
        resource,
        agent,
        max(RESOURCE_LOCK_TTL_SECONDS, timeout_seconds + 60),
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
        release_resource_lock(
            project,
            lock.resource,
            holder=agent,
            role="driver",
            role_generation=generation,
        )
        release_role(project, "driver", holder=agent, generation=generation)
        return f"task #{task.id} requeued: worktree creation failed: {exc}"

    try:
        update_task_payload(project, task.id, {"worktree": relative_to_project(project, worktree)})
        write_log(project, f"{agent} executing task #{task.id} resource={lock.resource}")
        update_worker_heartbeat(project, agent, "running", role="driver", generation=generation)
        print(f"executing task #{task.id}: {task.title}")
        print(f"resource lock: {lock.resource}")
        print(f"worktree: {worktree}")
        print(f"agent timeout: {timeout_seconds}s")
        print(f"no-output timeout: {no_output_timeout_seconds}s")
        sys.stdout.flush()
        try:
            result = invoke_agent_adapter(
                project,
                worktree,
                agent,
                task,
                generation,
                lock.resource,
                timeout_seconds=timeout_seconds,
                no_output_timeout_seconds=min(no_output_timeout_seconds, timeout_seconds),
            )
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

        if is_adapter_health_failure(result):
            message = f"{agent} adapter unavailable: {result.message}"
            mark_agent_cooldown(project, agent, result.message)
            update_task_payload(
                project,
                task.id,
                {
                    "worktree": relative_to_project(project, worktree),
                    "adapter_log": result.log_file,
                    "adapter_failure": result.message,
                    "adapter_cooldown_agent": agent,
                },
            )
            release_task_claim(project, task.id, message)
            write_log(project, f"{agent} requeued task #{task.id}: {message}")
            return f"task #{task.id} requeued: {message}"

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
            payload_updates["driver_log"] = result.log_file
            move_task_to_status(
                project,
                task.id,
                "awaiting_test",
                message=result.message,
                log_file=result.log_file,
                payload_updates=payload_updates,
            )
            write_log(project, f"{agent} moved task #{task.id} to awaiting_test log={result.log_file}")
            return f"task #{task.id} awaiting_test log={result.log_file}"

        finish_task(
            project,
            task.id,
            "failed",
            message=result.message,
            log_file=result.log_file,
            payload_updates=payload_updates,
        )
        write_log(project, f"{agent} finished task #{task.id} ok={result.ok} log={result.log_file}")
        return f"task #{task.id} {'completed' if result.ok else 'failed'} log={result.log_file}"
    finally:
        release_resource_lock(
            project,
            lock.resource,
            holder=agent,
            role="driver",
            role_generation=generation,
        )
        release_role(project, "driver", holder=agent, generation=generation)


def execute_tester_task(
    project: Path,
    agent: str,
    generation: int,
    *,
    timeout_seconds: int = TEST_TIMEOUT_SECONDS,
) -> str:
    task = claim_next_test_task(project, agent, generation)
    if task is None:
        return "no awaiting_test task"
    role = acquire_role(project, "tester", agent, timeout_seconds + 60, renew_if_same=True)
    if not role.ok or role.generation != generation:
        release_task_claim_to(
            project,
            task.id,
            "awaiting_test",
            "tester role lease is stale",
            running_status="running_test",
        )
        return f"task #{task.id} requeued: tester role lease is stale"

    resource = task_resource(task)
    lock = acquire_resource_lock(
        project,
        resource,
        agent,
        max(RESOURCE_LOCK_TTL_SECONDS, timeout_seconds + 60),
        role="tester",
        role_generation=generation,
        renew_if_same=True,
    )
    if not lock.ok:
        release_task_claim_to(project, task.id, "awaiting_test", lock.reason, running_status="running_test")
        release_role(project, "tester", holder=agent, generation=generation)
        return f"task #{task.id} requeued: {lock.reason}"

    try:
        worktree = task_worktree_path(project, task)
        if worktree is None or not worktree.exists():
            message = "task worktree is missing"
            finish_task(project, task.id, "failed", message=message)
            write_log(project, f"{agent} failed tester task #{task.id}: {message}")
            return f"task #{task.id} failed: {message}"

        update_worker_heartbeat(project, agent, "testing", role="tester", generation=generation)
        print(f"testing task #{task.id}: {task.title}")
        print(f"resource lock: {lock.resource}")
        print(f"worktree: {worktree}")
        print(f"test timeout: {timeout_seconds}s")
        sys.stdout.flush()

        policy = check_diff_policy(project, worktree, lock.resource)
        payload_updates = {
            "worktree": relative_to_project(project, worktree),
            "diff_policy": policy.status,
            "changed_files": list(policy.changed_files),
        }
        if policy.status == "no_change":
            finish_task(
                project,
                task.id,
                "no_change",
                message=policy.reason,
                payload_updates=payload_updates,
            )
            write_log(project, f"{agent} tester found no changes for task #{task.id}")
            return f"task #{task.id} no_change"
        if not policy.ok:
            finish_task(
                project,
                task.id,
                "rejected",
                message=policy.reason,
                payload_updates=payload_updates,
            )
            write_log(project, f"{agent} tester rejected task #{task.id}: {policy.reason}")
            return f"task #{task.id} rejected: {policy.reason}"

        test_result = run_tester_gate(project, worktree, task, policy.changed_files, timeout_seconds=timeout_seconds)
        payload_updates["tester_log"] = test_result.log_file
        if test_result.baseline_failures:
            payload_updates["tester_baseline_failures"] = list(test_result.baseline_failures)
        if not test_result.ok:
            finish_task(
                project,
                task.id,
                "failed",
                message=test_result.message,
                log_file=test_result.log_file,
                payload_updates=payload_updates,
            )
            write_log(project, f"{agent} tester failed task #{task.id}: {test_result.message}")
            return f"task #{task.id} failed: {test_result.message}"

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
                log_file=test_result.log_file,
                payload_updates=payload_updates,
            )
            write_log(project, f"{agent} tester failed to apply task #{task.id}: {exc}")
            return f"task #{task.id} failed: {message}"

        finish_task(
            project,
            task.id,
            "completed",
            message=test_result.message,
            log_file=test_result.log_file,
            payload_updates=payload_updates,
        )
        write_log(project, f"{agent} tester completed task #{task.id} log={test_result.log_file}")
        return f"task #{task.id} completed log={test_result.log_file}"
    finally:
        release_resource_lock(
            project,
            lock.resource,
            holder=agent,
            role="tester",
            role_generation=generation,
        )
        release_role(project, "tester", holder=agent, generation=generation)


def execute_worker_available_task(
    project: Path,
    agent: str,
    roles: list[tuple[str, int, str]],
    *,
    run_deadline: str,
    agent_timeout_seconds: int,
    agent_no_output_seconds: int,
    test_timeout_seconds: int,
    shutdown_grace_seconds: int,
) -> Optional[str]:
    counts = task_status_counts(project)
    tester = next((role for role in roles if role[0] == "tester"), None)
    driver = next((role for role in roles if role[0] == "driver"), None)

    if tester and counts.get("awaiting_test", 0) > 0:
        budget = execution_budget_seconds(run_deadline, test_timeout_seconds, shutdown_grace_seconds)
        if budget < MIN_EXECUTION_BUDGET_SECONDS:
            return f"not enough run time for tester budget={budget}s"
        message = execute_tester_task(project, agent, tester[1], timeout_seconds=budget)
        if message != "no awaiting_test task":
            return message

    if driver and counts.get("pending", 0) > 0:
        budget = execution_budget_seconds(run_deadline, agent_timeout_seconds, shutdown_grace_seconds)
        if budget < MIN_EXECUTION_BUDGET_SECONDS:
            return f"not enough run time for driver budget={budget}s"
        message = execute_driver_task(
            project,
            agent,
            driver[1],
            timeout_seconds=budget,
            no_output_timeout_seconds=min(agent_no_output_seconds, budget),
        )
        if message != "no pending task":
            return message

    return None


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
                    f"run deadline: {args.run_deadline or 'none'}",
                    f"driver budget: {format_budget(args.run_deadline, args.agent_timeout_seconds, args.shutdown_grace_seconds)}",
                    f"tester budget: {format_budget(args.run_deadline, args.test_timeout_seconds, args.shutdown_grace_seconds)}",
                    f"no-output timeout: {args.agent_no_output_seconds}s",
                    "role leases:",
                    *role_lines,
                    "",
                    "adapter: driver writes task worktrees; tester gates and applies patches",
                ]
            )
            if rendered != last_rendered:
                print("\033[2J\033[H" + rendered)
                sys.stdout.flush()
                last_rendered = rendered
            if args.execute_agents:
                message = execute_worker_available_task(
                    project,
                    agent,
                    roles,
                    run_deadline=args.run_deadline,
                    agent_timeout_seconds=args.agent_timeout_seconds,
                    agent_no_output_seconds=args.agent_no_output_seconds,
                    test_timeout_seconds=args.test_timeout_seconds,
                    shutdown_grace_seconds=args.shutdown_grace_seconds,
                )
                if message:
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

    inspect = subparsers.add_parser("inspect", help="inspect project type and inferred checks")
    inspect.add_argument("project", nargs="?", default=".")
    inspect.add_argument("--json", action="store_true", help="print machine-readable project profile")
    inspect.set_defaults(func=cmd_inspect)

    frontier = subparsers.add_parser("frontier", help="show deterministic frontier candidates")
    frontier.add_argument("project", nargs="?", default=".")
    frontier.add_argument("--limit", type=positive_seconds, default=20)
    frontier.add_argument("--json", action="store_true", help="print machine-readable frontier candidates")
    frontier.set_defaults(func=cmd_frontier)

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

    task_requeue = task_subparsers.add_parser("requeue", help="move a blocked or failed task back to pending")
    task_requeue.add_argument("task_id", type=positive_task_id)
    task_requeue.add_argument("--reason", default="manual requeue")
    task_requeue.add_argument("--project", default=".")
    task_requeue.set_defaults(func=cmd_task_requeue)

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

    tell = subparsers.add_parser("tell", help="send a protocol line to a resident agent pane")
    tell.add_argument("agent", choices=AGENTS)
    tell.add_argument("kind", choices=RESIDENT_MESSAGE_KINDS)
    tell.add_argument("message")
    tell.add_argument("--task-id", type=nonnegative_int, default=0)
    tell.add_argument("--project", default=".")
    tell.set_defaults(func=cmd_tell)

    report = subparsers.add_parser("report", help="report resident done/blocked through the state channel")
    report.add_argument("kind", choices=("done", "blocked"))
    report.add_argument("message", nargs="*", help="optional report message")
    report.add_argument("--task-id", "--task", dest="task_id", type=positive_task_id, required=True)
    report.add_argument("--agent", choices=AGENTS)
    report.add_argument("--project", default=".")
    report.set_defaults(func=cmd_report)

    start = subparsers.add_parser("start", help="start the tmux observation workspace")
    start.add_argument("project", nargs="?", default=".")
    start.add_argument("--task", default="")
    start.add_argument(
        "--execute-agents",
        action="store_true",
        help="allow workers to run codex/claude non-interactively when they hold driver",
    )
    start.add_argument(
        "--resident-agents",
        action="store_true",
        help="open persistent interactive Codex/Claude panes with fixed resident worktrees",
    )
    start.set_defaults(func=cmd_start)

    run_parser = subparsers.add_parser("run", help="run a timed tmux workspace and stop automatically")
    run_parser.add_argument("project", nargs="?", default=".")
    run_parser.add_argument("--minutes", type=positive_float, default=30.0)
    run_parser.add_argument("--seconds", type=positive_seconds, help=argparse.SUPPRESS)
    run_parser.add_argument("--checkpoint-seconds", type=positive_seconds, default=60)
    run_parser.add_argument("--agent-timeout-seconds", type=positive_seconds, default=AGENT_TIMEOUT_SECONDS)
    run_parser.add_argument("--agent-no-output-seconds", type=positive_seconds, default=AGENT_NO_OUTPUT_TIMEOUT_SECONDS)
    run_parser.add_argument("--test-timeout-seconds", type=positive_seconds, default=TEST_TIMEOUT_SECONDS)
    run_parser.add_argument("--shutdown-grace-seconds", type=positive_seconds, default=RUN_SHUTDOWN_GRACE_SECONDS)
    run_parser.add_argument("--task", default="")
    run_parser.add_argument(
        "--no-default-task",
        action="store_true",
        help="do not add a conservative default task when the queue is empty",
    )
    run_parser.add_argument(
        "--execute-agents",
        action="store_true",
        help="allow workers to run codex/claude non-interactively inside the timed window",
    )
    run_parser.add_argument(
        "--resident-agents",
        action="store_true",
        help="open persistent interactive Codex/Claude panes with fixed resident worktrees",
    )
    run_parser.set_defaults(func=cmd_run)

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
    worker.add_argument("--run-deadline", default="")
    worker.add_argument("--agent-timeout-seconds", type=positive_seconds, default=AGENT_TIMEOUT_SECONDS)
    worker.add_argument("--agent-no-output-seconds", type=positive_seconds, default=AGENT_NO_OUTPUT_TIMEOUT_SECONDS)
    worker.add_argument("--test-timeout-seconds", type=positive_seconds, default=TEST_TIMEOUT_SECONDS)
    worker.add_argument("--shutdown-grace-seconds", type=positive_seconds, default=RUN_SHUTDOWN_GRACE_SECONDS)
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
