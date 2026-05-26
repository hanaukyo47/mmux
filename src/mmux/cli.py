from __future__ import annotations

import argparse
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


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


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
    return sqlite3.connect(state_path(project))


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
        db.execute(
            "insert into events(kind, payload, created_at) values (?, ?, ?)",
            ("init", json.dumps({"task": task}), utc_now()),
        )


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
    require_project_state(project)
    with connect(project) as db:
        tasks = db.execute("select status, count(*) from tasks group by status").fetchall()
        leases = db.execute("select role, holder, generation, lease_until, status from role_leases").fetchall()
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
    print("recent events:")
    for kind, created_at in events:
        print(f"  {created_at} {kind}")
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
    write_log(project, "supervisor online")
    try:
        while True:
            write_log(project, "heartbeat")
            time.sleep(30)
    except KeyboardInterrupt:
        write_log(project, "supervisor interrupted")
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    project = resolve_project(args.project)
    agent = args.agent
    require_project_state(project)
    print(f"mmux {agent} worker")
    print("status: parked")
    print("role lease: none")
    print("This pane is reserved for the future agent adapter.")
    write_log(project, f"{agent} worker online")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        write_log(project, f"{agent} worker interrupted")
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
