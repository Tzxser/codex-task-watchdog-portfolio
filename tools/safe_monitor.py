"""Safe launcher and runner for the read-only Codex Task Watchdog.

This entry point intentionally does not call ``src.watchdog_app.run_once``.
The historical runner owns a file lock whose stale-lock recovery probes a PID;
this runner has its own Windows named mutex and its own runtime directory.
It imports only the read-only sources/state machine and writes only files below
``%LOCALAPPDATA%\\CodexTaskWatchdog\\safe-monitor`` by default.

Commands::

    python tools/safe_monitor.py check
    python tools/safe_monitor.py start
    python tools/safe_monitor.py status
    python tools/safe_monitor.py stop
    python tools/safe_monitor.py mute|unmute

``start`` creates a detached child.  ``run`` is the child command and is kept
as a separate command so a caller can audit exactly what is launched.  The
monitor never sends input to ChatGPT/Codex and never starts, stops, refreshes,
retries, or switches those applications.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.watchdog_core import (  # noqa: E402
    Deduplicator,
    Health,
    JsonStore,
    LogSQLiteSource,
    MonitorConfig,
    Notifier,
    ProcessSource,
    StateMachine,
    ThreadHistorySource,
)


DEFAULT_RUNTIME = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "CodexTaskWatchdog"
MUTEX_NAME = r"Local\CodexTaskWatchdog.SafeMonitor.v1"
POLL_MIN_SECONDS = 1.0
STATUS_SCHEMA = 1


def build_sources(config: MonitorConfig) -> dict:
    """Build only the read-only source adapters used by this runner."""

    log_path = Path(config.log_path).expanduser()
    history_path = log_path.with_name("thread_history_1.sqlite")
    return {
        "codex_logs_sqlite": LogSQLiteSource(log_path, tail=config.start_from_tail),
        "thread_history_sqlite": ThreadHistorySource(history_path),
        "process_presence": ProcessSource(),
        "ui_automation": {
            "health": Health.UNAVAILABLE.value,
            "note": "UIA semantic web content unavailable; no focus, scan, or control",
        },
    }


def source_health(sources: dict) -> dict:
    result = {}
    for name, source in sources.items():
        if isinstance(source, dict):
            result[name] = source
        else:
            result[name] = {
                "health": getattr(source, "health", Health.PARTIAL).value,
                "note": getattr(source, "note", ""),
            }
    return result


def process_status(process_source: ProcessSource | None) -> dict:
    if process_source is None:
        return {"chatgpt_process": False, "codex_process": False,
                "process_names": [], "note": "presence probe unavailable"}
    snap = process_source.snapshot()
    names = snap.get("process_names", [])
    return {
        "chatgpt_process": "ChatGPT.exe" in names,
        "codex_process": "codex.exe" in names,
        "process_names": names,
        "note": snap.get("note", "presence only; not progress evidence"),
    }


def status_snapshot(machine: StateMachine, sources: dict, processes: dict,
                    started_at: float, running: bool) -> dict:
    rows = []
    for rec in machine.tasks.values():
        rows.append({
            "session_id": rec.session_id,
            "turn_id": rec.turn_id,
            "project": rec.project,
            "state": rec.state,
            "health": rec.health,
            "last_source_read": rec.last_source_read,
            "last_state_event": rec.last_state_event,
            "last_progress": rec.last_progress,
            "evidence": rec.evidence,
            "confidence": rec.confidence,
            "active": rec.active,
            "agent_id": rec.agent_id,
        })
    return {
        "schema_version": STATUS_SCHEMA,
        "updated_at": time.time(),
        "started_at": started_at,
        "running": running,
        "mode": "monitor_only_alert_only",
        "control_actions": {
            "click": False, "input": False, "retry": False,
            "restart": False, "refresh": False, "model_switch": False,
            "prompt_resubmit": False,
        },
        "autostart": False,
        "process_presence": processes,
        "sources": source_health(sources),
        "coverage": {
            "covered": ["local Codex thread_turns status", "local structured log tail",
                        "ChatGPT/Codex process presence"],
            "partial": ["task/project alias and child-agent relationship",
                        "notifications visible to user"],
            "unavailable": ["semantic Chromium web content via UIA", "CDP for current desktop",
                            "remote task state", "Hooks adapter", "App Server subscription"],
        },
        "tasks": rows,
    }


def utc_iso(seconds: float | None = None) -> str:
    """Return a stable, human-readable UTC timestamp."""

    value = time.time() if seconds is None else float(seconds)
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write_json(path: Path, value: Any) -> None:
    """Write a JSON control/status file without exposing a partial document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    try:
        last_error: OSError | None = None
        for _ in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last_error = exc
                time.sleep(0.05)
        # A reader or endpoint scanner can briefly hold the destination on
        # Windows.  Fall back to writing our own file directly after retries;
        # failure is still reported to the caller rather than misreported as
        # a healthy heartbeat.
        if last_error is not None:
            path.write_text(payload, encoding="utf-8")
        else:
            raise OSError("status replace failed")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


class SafeInstanceLease:
    """A non-stale single-instance lease.

    Windows gets a kernel named mutex.  A mutex is released by Windows when
    the owning process exits, so there is no PID probing or stale-lock
    deletion.  The non-Windows fallback deliberately refuses an existing file
    and never guesses that it is stale; this favors safety over recovery.
    """

    def __init__(self, name: str = MUTEX_NAME, fallback_path: Path | None = None):
        self.name = name
        self.fallback_path = fallback_path
        self.handle: Any = None
        self.fd: int | None = None
        self._owns = False

    def acquire(self) -> bool:
        if self._owns:
            return True
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            ctypes.set_last_error(0)
            handle = kernel32.CreateMutexW(None, 1, self.name)
            if not handle:
                raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                kernel32.CloseHandle(handle)
                return False
            self.handle = (kernel32, handle)
            self._owns = True
            return True

        # This branch is only for development/test hosts.  An existing file
        # is always respected; there is intentionally no stale-PID recovery.
        path = self.fallback_path or (Path.cwd() / ".safe-monitor.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, json.dumps({"pid": os.getpid(), "created": time.time()}).encode("ascii"))
        except FileExistsError:
            return False
        self.fallback_path = path
        self._owns = True
        return True

    def release(self) -> None:
        if not self._owns:
            return
        if os.name == "nt":
            kernel32, handle = self.handle
            kernel32.CloseHandle(handle)
            self.handle = None
        else:
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
            # The fallback lock is ours because it was created with O_EXCL.
            try:
                self.fallback_path.unlink()
            except (OSError, AttributeError):
                pass
        self._owns = False

    def __enter__(self) -> "SafeInstanceLease":
        if not self.acquire():
            raise RuntimeError("safe monitor is already running; existing instance was left untouched")
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()


@dataclass(frozen=True)
class RuntimePaths:
    root: Path

    @property
    def status(self) -> Path:
        return self.root / "safe-monitor.status.json"

    @property
    def heartbeat(self) -> Path:
        return self.root / "safe-monitor.heartbeat.json"

    @property
    def log(self) -> Path:
        return self.root / "safe-monitor.log.jsonl"

    @property
    def stop(self) -> Path:
        return self.root / "safe-monitor.stop.json"

    @property
    def control(self) -> Path:
        return self.root / "safe-monitor.control.json"

    @property
    def dedup(self) -> Path:
        return self.root / "safe-monitor.dedup.json"

    @property
    def notifications(self) -> Path:
        return self.root / "notifications.jsonl"


def config_path_from_arg(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else ROOT / "config.json"


def load_config(path: Path) -> MonitorConfig:
    return MonitorConfig.from_file(path)


def runtime_paths(config: MonitorConfig, override: str | None = None) -> RuntimePaths:
    if override:
        root = Path(override).expanduser()
    elif config.state_dir:
        root = Path(config.state_dir).expanduser()
    else:
        # Keep this runner independent from the historical runner's files.
        root = DEFAULT_RUNTIME / "safe-monitor"
    resolved = root.resolve()
    # A caller may select a test subdirectory, but production state is kept
    # inside the dedicated LocalAppData tree.  This prevents an accidental
    # ``--state-dir`` from writing into the repository or a Codex data folder.
    allowed = DEFAULT_RUNTIME.resolve()
    try:
        resolved.relative_to(allowed)
    except ValueError as exc:
        raise ValueError(f"safe monitor state must stay under {allowed}") from exc
    return RuntimePaths(resolved)


def append_log(paths: RuntimePaths, event: str, **fields: Any) -> None:
    """Append a bounded-schema local diagnostic record.

    Values are deliberately selected by callers.  Exception messages and raw
    source bodies are never written here.
    """

    record: dict[str, Any] = {"timestamp": time.time(), "timestamp_utc": utc_iso(), "event": event}
    for key, value in fields.items():
        if key in {"run_id", "pid", "sample_count", "source_count", "notice_count", "reason", "error_type"}:
            record[key] = value
    paths.root.mkdir(parents=True, exist_ok=True)
    try:
        with paths.log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        # A diagnostic log failure must not stop the read-only monitor.
        return


def run_id_from_status(paths: RuntimePaths) -> str | None:
    data = read_json(paths.status, {})
    if not isinstance(data, dict):
        return None
    watchdog = data.get("safe_monitor", {})
    if not isinstance(watchdog, dict):
        return None
    # Controls are accepted only while the status file proves this run is
    # currently active.  A stopped run's ID is never enough to authorize a
    # later command.
    if not bool(data.get("running")) or not bool(watchdog.get("running", data.get("running"))):
        return None
    value = watchdog.get("run_id")
    return value if isinstance(value, str) and value else None


def write_heartbeat(paths: RuntimePaths, run_id: str, started: float, sample_count: int,
                    *, running: bool, stop_requested: bool = False) -> None:
    atomic_write_json(paths.heartbeat, {
        "schema_version": STATUS_SCHEMA,
        "run_id": run_id,
        "pid": os.getpid(),
        "started_at": started,
        "started_at_utc": utc_iso(started),
        "last_heartbeat_at": time.time(),
        "last_heartbeat_at_utc": utc_iso(),
        "sample_count": sample_count,
        "running": running,
        "stop_requested": stop_requested,
    })


def control_action(paths: RuntimePaths, run_id: str) -> str | None:
    data = read_json(paths.control, {})
    if not isinstance(data, dict) or data.get("run_id") != run_id:
        return None
    action = data.get("action")
    return action if action in {"mute", "unmute"} else None


def stop_matches(paths: RuntimePaths, run_id: str) -> bool:
    data = read_json(paths.stop, {})
    if not isinstance(data, dict):
        return False
    return data.get("run_id") == run_id


def safe_status(machine: StateMachine, sources: dict, processes: dict, started: float,
                run_id: str, paths: RuntimePaths, running: bool, sample_count: int,
                muted: bool, stop_requested: bool = False, stop_reason: str = "") -> dict:
    snapshot = status_snapshot(machine, sources, processes, started, running)
    snapshot["schema_version"] = STATUS_SCHEMA
    snapshot["safe_monitor"] = {
        "run_id": run_id,
        "pid": os.getpid(),
        "running": running,
        "started_at": started,
        "started_at_utc": utc_iso(started),
        "updated_at_utc": utc_iso(),
        "sample_count": sample_count,
        "runtime_dir": str(paths.root),
        "heartbeat": str(paths.heartbeat),
        "stop_request": str(paths.stop),
        "control_file": str(paths.control),
        "muted": muted,
        "stop_requested": stop_requested,
        "stop_reason": stop_reason,
    }
    snapshot["notification"] = {
        "history": str(paths.notifications),
        "sound_enabled": bool(machine.config.sound_enabled) and not muted,
        "desktop_toast_enabled": bool(machine.config.desktop_toast_enabled) and not muted,
        "self_test": False,
        "delivery_evidence": "history_only",
    }
    return snapshot


def one_readonly_sample(config: MonitorConfig) -> tuple[dict, dict, int]:
    """Read current sources once, without creating runtime state or alerts."""

    sources = build_sources(config)
    now = time.time()
    events = list(sources["codex_logs_sqlite"].poll(now))
    events.extend(sources["thread_history_sqlite"].poll(now))
    processes = process_status(sources["process_presence"])
    return source_health(sources), processes, len(events)


def run_monitor(config: MonitorConfig, paths: RuntimePaths, *, poll_seconds: float | None = None) -> int:
    paths.root.mkdir(parents=True, exist_ok=True)
    lease = SafeInstanceLease(fallback_path=paths.root / "safe-monitor.fallback.lock")
    if not lease.acquire():
        append_log(paths, "already_running")
        return 2

    run_id = uuid.uuid4().hex
    started = time.time()
    notifier = Notifier(paths.root, config.sound_enabled, config.desktop_toast_enabled)
    dedup = Deduplicator(JsonStore(paths.dedup), config.dedup_repeat_seconds, config.hourly_event_limit)
    clock = type("Clock", (), {"now": staticmethod(time.time)})()
    machine = StateMachine(config, clock, notifier, dedup)
    sources: dict = {}
    processes: dict = {}
    sample_count = 0
    muted = False
    stop_reason = ""
    interval = max(POLL_MIN_SECONDS, float(poll_seconds if poll_seconds is not None else config.poll_seconds))

    # Construct each read-only source once.  Their cursors are intentionally
    # process-local and must advance across samples; rebuilding a source every
    # loop would reset the log tail to the current maximum ID and miss events.
    try:
        sources = build_sources(config)
    except Exception as exc:
        append_log(paths, "source_init_error", run_id=run_id, error_type=type(exc).__name__)
        sources = {}

    # A request belongs to a run ID.  Old requests cannot stop a new run.
    try:
        paths.stop.unlink()
    except OSError:
        pass
    append_log(paths, "started", run_id=run_id, pid=os.getpid())
    try:
        while True:
            action = control_action(paths, run_id)
            if action == "mute":
                muted = True
                notifier.sound_enabled = False
                notifier.desktop_toast_enabled = False
            elif action == "unmute":
                muted = False
                notifier.sound_enabled = config.sound_enabled
                notifier.desktop_toast_enabled = config.desktop_toast_enabled

            notices_count = 0
            try:
                events = []
                if sources:
                    events.extend(sources["codex_logs_sqlite"].poll(time.time()))
                    events.extend(sources["thread_history_sqlite"].poll(time.time()))
                for event in events:
                    machine.ingest(event)
                notices = machine.evaluate()
                for notice in notices:
                    notifier.emit(notice)
                notices_count = len(notices)
                if sources:
                    processes = process_status(sources["process_presence"])
            except Exception as exc:  # source failures must not drop heartbeat
                append_log(paths, "sample_error", run_id=run_id, sample_count=sample_count,
                           error_type=type(exc).__name__)

            sample_count += 1
            requested = stop_matches(paths, run_id)
            status = safe_status(machine, sources, processes, started, run_id, paths,
                                 True, sample_count, muted, requested)
            try:
                atomic_write_json(paths.status, status)
            except OSError as exc:
                append_log(paths, "status_write_error", run_id=run_id,
                           sample_count=sample_count, error_type=type(exc).__name__)
            try:
                write_heartbeat(paths, run_id, started, sample_count, running=True,
                                stop_requested=requested)
            except OSError as exc:
                append_log(paths, "heartbeat_write_error", run_id=run_id,
                           sample_count=sample_count, error_type=type(exc).__name__)
            append_log(paths, "sample", run_id=run_id, sample_count=sample_count,
                       source_count=len(machine.tasks), notice_count=notices_count)
            if requested:
                stop_reason = "stop_request"
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        stop_reason = "keyboard_interrupt"
    except Exception as exc:
        stop_reason = "runner_error"
        append_log(paths, "runner_error", run_id=run_id, error_type=type(exc).__name__)
        raise
    finally:
        try:
            if sources:
                processes = process_status(sources.get("process_presence")) if sources.get("process_presence") else processes
            final = safe_status(machine, sources, processes, started, run_id, paths,
                                False, sample_count, muted, stop_reason == "stop_request", stop_reason)
            try:
                atomic_write_json(paths.status, final)
            except OSError:
                pass
            try:
                write_heartbeat(paths, run_id, started, sample_count, running=False,
                                stop_requested=stop_reason == "stop_request")
            except OSError:
                pass
            append_log(paths, "stopped", run_id=run_id, sample_count=sample_count)
        finally:
            lease.release()
    return 0


def detached_start(config_path: Path, paths: RuntimePaths, *, dry_run: bool = False) -> int:
    command = [sys.executable, str(Path(__file__).resolve()), "run",
               "--config", str(config_path), "--state-dir", str(paths.root)]
    if dry_run:
        print(json.dumps({"command": command, "runtime_dir": str(paths.root),
                          "mutex": MUTEX_NAME}, ensure_ascii=False, indent=2))
        return 0
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    child = subprocess.Popen(command, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, creationflags=flags)
    deadline = time.time() + 5.0
    verified = False
    while time.time() < deadline:
        status = read_json(paths.status, {})
        safe = status.get("safe_monitor", {}) if isinstance(status, dict) else {}
        if (isinstance(status, dict) and status.get("running") is True
                and isinstance(safe, dict) and safe.get("pid") == child.pid):
            verified = True
            break
        if child.poll() is not None:
            break
        time.sleep(0.1)
    print(json.dumps({"launch_requested": True, "launch_verified": verified,
                      "pid": child.pid, "runtime_dir": str(paths.root),
                      "status": str(paths.status), "heartbeat": str(paths.heartbeat)}, ensure_ascii=False))
    return 0 if verified else 1


def request_stop(paths: RuntimePaths) -> int:
    run_id = run_id_from_status(paths)
    status = read_json(paths.status, {})
    if not run_id or not isinstance(status, dict) or not status.get("safe_monitor", {}).get("running", status.get("running", False)):
        print(json.dumps({"requested": False, "reason": "no_running_safe_monitor",
                          "status": str(paths.status)}, ensure_ascii=False))
        return 1
    atomic_write_json(paths.stop, {"schema_version": STATUS_SCHEMA, "run_id": run_id,
                                   "requested_at": time.time(), "requested_at_utc": utc_iso()})
    print(json.dumps({"requested": True, "run_id": run_id, "stop": str(paths.stop)}, ensure_ascii=False))
    return 0


def request_control(paths: RuntimePaths, action: str) -> int:
    run_id = run_id_from_status(paths)
    status = read_json(paths.status, {})
    safe = status.get("safe_monitor", {}) if isinstance(status, dict) else {}
    running = bool(status.get("running") is True) if isinstance(status, dict) else False
    if isinstance(safe, dict) and "running" in safe:
        running = bool(safe.get("running"))
    if not run_id or not running:
        print(json.dumps({"requested": False, "reason": "no_safe_monitor_run"}, ensure_ascii=False))
        return 1
    atomic_write_json(paths.control, {"schema_version": STATUS_SCHEMA, "run_id": run_id,
                                      "action": action, "requested_at": time.time(),
                                      "requested_at_utc": utc_iso()})
    print(json.dumps({"requested": True, "action": action, "run_id": run_id}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="独立、安全的只监控只提醒启动入口")
    parser.add_argument("command", choices=["check", "start", "run", "status", "stop", "mute", "unmute"])
    parser.add_argument("--config", default=None, help="配置文件路径，默认使用项目 config.json")
    parser.add_argument("--state-dir", default=None, help="独立运行目录")
    parser.add_argument("--poll-seconds", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="start 只显示将执行的子进程命令")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg_path = config_path_from_arg(args.config)
    config = load_config(cfg_path)
    paths = runtime_paths(config, args.state_dir)
    if args.command == "check":
        sources, processes, event_count = one_readonly_sample(config)
        print(json.dumps({"config": str(cfg_path), "runtime_dir": str(paths.root),
                          "sources": sources, "processes": processes,
                          "event_count": event_count, "mutex": MUTEX_NAME,
                          "writes": False}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "start":
        return detached_start(cfg_path, paths, dry_run=args.dry_run)
    if args.command == "run":
        return run_monitor(config, paths, poll_seconds=args.poll_seconds)
    if args.command == "status":
        print(json.dumps({"status": read_json(paths.status, None),
                          "heartbeat": read_json(paths.heartbeat, None),
                          "runtime_dir": str(paths.root)}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "stop":
        return request_stop(paths)
    return request_control(paths, args.command)


if __name__ == "__main__":
    raise SystemExit(main())
