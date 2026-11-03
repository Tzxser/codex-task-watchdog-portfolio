"""Independent Windows process for Codex Task Watchdog.

The process only reads local state and writes its own status/history under
LocalAppData.  It never sends input to ChatGPT/Codex and never starts or stops
their processes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .watchdog_core import (
    Deduplicator,
    Event,
    Health,
    JsonStore,
    LogSQLiteSource,
    MonitorConfig,
    Notification,
    Notifier,
    ProcessSource,
    StateMachine,
    TaskState,
    ThreadHistorySource,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "CodexTaskWatchdog"


def load_config() -> MonitorConfig:
    return MonitorConfig.from_file(ROOT / "config.json")


def runtime_dir(config: MonitorConfig) -> Path:
    return Path(config.state_dir).expanduser() if config.state_dir else DEFAULT_RUNTIME


def write_status(store: JsonStore, status: dict) -> None:
    store.save(status)


def build_sources(config: MonitorConfig):
    log_path = Path(config.log_path).expanduser()
    history_path = log_path.with_name("thread_history_1.sqlite")
    return {
        "codex_logs_sqlite": LogSQLiteSource(log_path, tail=config.start_from_tail),
        "thread_history_sqlite": ThreadHistorySource(history_path),
        "process_presence": ProcessSource(),
        "ui_automation": {"health": Health.UNAVAILABLE.value,
                           "note": "UIA window is readable, but Chromium web content/status tree is unavailable; no automatic focus or scan"},
    }


def source_health(sources: dict) -> dict:
    result = {}
    for name, source in sources.items():
        if isinstance(source, dict):
            result[name] = source
        else:
            result[name] = {"health": getattr(source, "health", Health.PARTIAL).value,
                            "note": getattr(source, "note", "")}
    return result


def process_status(process_source: ProcessSource) -> dict:
    snap = process_source.snapshot()
    names = snap.get("process_names", [])
    return {"chatgpt_process": "ChatGPT.exe" in names,
            "codex_process": "codex.exe" in names,
            "process_names": names,
            "note": snap.get("note", "presence only; not progress evidence")}


def safe_event(event: Event) -> dict:
    return {
        "source": event.source, "kind": event.kind, "timestamp": event.timestamp,
        "instance_id": event.instance_id, "session_id": event.session_id,
        "turn_id": event.turn_id, "project": event.project,
        "evidence": event.evidence, "confidence": event.confidence,
    }


def status_snapshot(machine: StateMachine, sources: dict, processes: dict, started_at: float, running: bool) -> dict:
    rows = []
    for rec in machine.tasks.values():
        rows.append({"session_id": rec.session_id, "turn_id": rec.turn_id,
                     "project": rec.project, "state": rec.state, "health": rec.health,
                     "last_source_read": rec.last_source_read,
                     "last_state_event": rec.last_state_event,
                     "last_progress": rec.last_progress,
                     "evidence": rec.evidence, "confidence": rec.confidence,
                     "active": rec.active, "agent_id": rec.agent_id})
    return {
        "schema_version": 1,
        "updated_at": time.time(),
        "started_at": started_at,
        "running": running,
        "mode": "monitor_only_alert_only",
        "control_actions": {"click": False, "input": False, "retry": False, "restart": False,
                             "refresh": False, "model_switch": False, "prompt_resubmit": False},
        "notification": {"history": str(DEFAULT_RUNTIME / "notifications.jsonl"),
                         "sound_enabled": machine.config.sound_enabled,
                         "desktop_toast_enabled": machine.config.desktop_toast_enabled,
                         "self_test_required": True},
        "autostart": False,
        "process_presence": processes,
        "sources": source_health(sources),
        "coverage": {"covered": ["local Codex thread_turns status", "local structured log tail", "ChatGPT/Codex process presence"],
                     "partial": ["task/project alias and child-agent relationship", "notifications visible to user"],
                     "unavailable": ["semantic Chromium web content via UIA", "CDP for current desktop", "remote task state", "Hooks adapter", "App Server subscription"]},
        "tasks": rows,
    }


def send_self_test(notifier: Notifier, now: float) -> dict:
    return notifier.emit(Notification(key="monitor-self-test", severity="info",
        title="Codex 监控已启动（只监控、只提醒）",
        body="本机提醒链路自检已写入通知历史。本工具不会点击、输入、重试、重启或改变 Codex。",
        timestamp=now, project="Codex Task Watchdog", session_id="local", state="RUNNING",
        confidence="high", test=True))


def acquire_lock(state: Path) -> Path | None:
    lock = state / "watchdog.lock"
    state.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "created": time.time()}))
        return lock
    except FileExistsError:
        # Never terminate a prior process.  A live lock is respected.  A stale
        # lock can be removed only after checking its recorded PID is absent.
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
            pid = int(data.get("pid", 0))
            if pid > 0:
                os.kill(pid, 0)
                return None
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        try:
            lock.unlink()
            return acquire_lock(state)
        except OSError:
            return None


def run_once(config: MonitorConfig, *, test_notification: bool = False) -> dict:
    state = runtime_dir(config)
    lock = acquire_lock(state)
    if lock is None and not test_notification:
        raise RuntimeError("已有监控器实例，未触碰现有进程")
    started = time.time()
    status_store = JsonStore(state / "status.json")
    notifier = Notifier(state, config.sound_enabled, config.desktop_toast_enabled)
    dedup = Deduplicator(JsonStore(state / "dedup.json"), config.dedup_repeat_seconds, config.hourly_event_limit)
    clock = type("Clock", (), {"now": staticmethod(time.time)})()
    machine = StateMachine(config, clock, notifier, dedup)
    sources = build_sources(config)
    processes = process_status(sources["process_presence"])
    if test_notification:
        result = send_self_test(notifier, time.time())
        write_status(status_store, status_snapshot(machine, sources, processes, started, False) | {"test_notification": result})
        if lock:
            try: lock.unlink()
            except OSError: pass
        return result
    send_self_test(notifier, time.time())
    stop_file = state / "stop.request"
    try:
        if stop_file.exists():
            stop_file.unlink()
    except OSError:
        pass
    while True:
        now = time.time()
        events: list[Event] = []
        events.extend(sources["codex_logs_sqlite"].poll(now))
        events.extend(sources["thread_history_sqlite"].poll(now))
        for event in events:
            machine.ingest(event)
        notices = machine.evaluate()
        for notice in notices:
            notifier.emit(notice)
        processes = process_status(sources["process_presence"])
        write_status(status_store, status_snapshot(machine, sources, processes, started, True))
        if stop_file.exists():
            break
        time.sleep(max(1.0, config.poll_seconds))
    write_status(status_store, status_snapshot(machine, sources, processes, started, False))
    if lock:
        try: lock.unlink()
        except OSError: pass
    return {"stopped": True, "pid": os.getpid()}


def simulate(path: Path, config: MonitorConfig) -> list[dict]:
    clock = type("Clock", (), {"now": lambda self: self.t, "t": 0.0})()
    state = runtime_dir(config) / "simulation"
    notifier = Notifier(state, False, False)
    machine = StateMachine(config, clock, notifier, Deduplicator(JsonStore(state / "dedup.json")))
    output = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        data = json.loads(raw)
        clock.t = float(data.get("timestamp", clock.t))
        machine.ingest(Event(**data))
        output.extend({"key": n.key, "title": n.title, "body": n.body} for n in machine.evaluate())
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Codex Task Watchdog: read-only alerts")
    parser.add_argument("--test-notification", action="store_true")
    parser.add_argument("--once", action="store_true", help="采集一轮并退出")
    parser.add_argument("--simulate", type=Path)
    args = parser.parse_args(argv)
    config = load_config()
    if args.simulate:
        print(json.dumps(simulate(args.simulate, config), ensure_ascii=False, indent=2))
        return 0
    if args.once:
        # Keep the real source read-only but avoid leaving a long-running process.
        state = runtime_dir(config)
        state.mkdir(parents=True, exist_ok=True)
        sources = build_sources(config)
        now = time.time()
        events = sources["thread_history_sqlite"].poll(now) + sources["codex_logs_sqlite"].poll(now)
        print(json.dumps({"events": [safe_event(e) for e in events], "sources": source_health(sources),
                          "processes": process_status(sources["process_presence"])}, ensure_ascii=False, indent=2))
        return 0
    try:
        run_once(config, test_notification=args.test_notification)
        return 0
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
