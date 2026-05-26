from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Iterable, Optional


ROLES = ("driver", "reviewer", "scout", "tester", "summarizer")
AGENTS = ("codex", "claude")
ROLE_LEASE_TTL_SECONDS = 2 * 60
WORKER_REFRESH_SECONDS = 5
SUPERVISOR_TICK_SECONDS = 30
SUPERVISOR_ASSIGNMENT_SECONDS = 5 * 60
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
          updated_at text not null
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
          lease_until text not null
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


def ensure_existing_schema(project: Path) -> None:
    require_project_state(project)
    with connect(project) as db:
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

    with connect(project) as db:
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
            now = utc_now()
            db.execute(
                "insert into tasks(title, status, payload, created_at, updated_at) values (?, ?, ?, ?, ?)",
                (task, "pending", "{}", now, now),
            )
        record_event(db, "init", {"task": task})


def validate_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")


def validate_agent(agent: str) -> None:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent: {agent}")


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
    with connect(project) as db:
        apply_schema(db)
        expire_role_leases_db(db, utc_now_dt())
        return db.execute(
            "select role, holder, generation, lease_until, status from role_leases order by role"
        ).fetchall()


def active_roles_for_agent(project: Path, agent: str) -> list[tuple[str, int, str]]:
    validate_agent(agent)
    with connect(project) as db:
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


def update_worker_heartbeat(
    project: Path,
    agent: str,
    status: str,
    *,
    role: Optional[str] = None,
    generation: Optional[int] = None,
) -> None:
    validate_agent(agent)
    with connect(project) as db:
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
    with connect(project) as db:
        apply_schema(db)
        return db.execute(
            "select agent, pid, status, role, generation, updated_at from worker_heartbeats order by agent"
        ).fetchall()


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
    with connect(project) as db:
        apply_schema(db)
        expire_role_leases_db(db, utc_now_dt())
        tasks = db.execute("select status, count(*) from tasks group by status").fetchall()
        leases = db.execute(
            "select role, holder, generation, lease_until, status from role_leases order by role"
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


def positive_seconds(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
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


def cmd_start(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    ensure_layout(project, args.task or "")
    name = session_name(project)
    if tmux_has_session(name):
        print(f"tmux session already exists: {name}")
        return 0

    supervisor_cmd = module_command(project, "supervisor")
    codex_cmd = module_command(project, "worker", "codex")
    claude_cmd = module_command(project, "worker", "claude")
    log_cmd = f"mkdir -p .mmux/logs; touch .mmux/logs/supervisor.log; tail -f .mmux/logs/supervisor.log"

    run(["tmux", "new-session", "-d", "-s", name, "-c", str(project), supervisor_cmd])
    run(["tmux", "split-window", "-h", "-t", f"{name}:0.0", "-c", str(project), codex_cmd])
    run(["tmux", "split-window", "-v", "-t", f"{name}:0.0", "-c", str(project), log_cmd])
    run(["tmux", "split-window", "-v", "-t", f"{name}:0.1", "-c", str(project), claude_cmd])
    run(["tmux", "select-layout", "-t", name, "tiled"], check=False)

    write_log(project, f"started tmux session {name}")
    print(f"started tmux session: {name}")
    print(f"attach with: tmux attach -t {name}")
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
                    "role leases:",
                    *role_lines,
                    "",
                    "adapter: parked until a real model adapter is enabled",
                ]
            )
            if rendered != last_rendered:
                print("\033[2J\033[H" + rendered)
                sys.stdout.flush()
                last_rendered = rendered
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

    roles = subparsers.add_parser("roles", help="show role leases and worker heartbeats")
    roles.add_argument("project", nargs="?", default=".")
    roles.set_defaults(func=cmd_roles)

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

    start = subparsers.add_parser("start", help="start the tmux observation workspace")
    start.add_argument("project", nargs="?", default=".")
    start.add_argument("--task", default="")
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
