"""Safe, read-only Codex task monitoring primitives.

This module deliberately has no UI automation or task-control capability.  It
normalises evidence, tracks each session independently, and emits reminders to
the local notification history only.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional


class TaskState(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    RETRYING_CAPACITY = "RETRYING_CAPACITY"
    RECONNECTING = "RECONNECTING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_INPUT = "WAITING_INPUT"
    QUOTA_BLOCKED = "QUOTA_BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    TURN_COMPLETED = "TURN_COMPLETED"
    TURN_FAILED = "TURN_FAILED"
    INTERRUPTED = "INTERRUPTED"
    EXECUTOR_UNAVAILABLE = "EXECUTOR_UNAVAILABLE"
    SUSPECTED_STALL = "SUSPECTED_STALL"
    UNKNOWN = "UNKNOWN"


class Health(str, Enum):
    HEALTHY = "HEALTHY"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class Event:
    source: str
    kind: str
    timestamp: float
    instance_id: str = "local"
    session_id: str = "unknown"
    turn_id: str = ""
    request_id: str = ""
    project: str = "unknown"
    cwd: str = ""
    parent_session: str = ""
    agent_id: str = ""
    evidence: str = ""
    confidence: str = "low"
    active: bool = True


@dataclass
class TaskRecord:
    key: str
    instance_id: str
    session_id: str
    turn_id: str = ""
    project: str = "unknown"
    state: str = TaskState.UNKNOWN.value
    health: str = Health.PARTIAL.value
    first_seen: float = 0.0
    last_source_read: float = 0.0
    last_state_event: float = 0.0
    last_progress: float = 0.0
    evidence: str = ""
    confidence: str = "low"
    active: bool = False
    parent_session: str = ""
    agent_id: str = ""


@dataclass
class MonitorConfig:
    poll_seconds: float = 5.0
    approval_seconds: float = 10.0
    input_seconds: float = 10.0
    capacity_seconds: float = 90.0
    reconnect_seconds: float = 90.0
    reconnect_escalate_seconds: float = 600.0
    executor_seconds: float = 15.0
    silent_low_seconds: float = 1800.0
    silent_high_seconds: float = 3600.0
    dedup_repeat_seconds: float = 300.0
    hourly_event_limit: int = 6
    sound_enabled: bool = False
    desktop_toast_enabled: bool = False
    log_path: str = str(Path.home() / ".codex" / "logs_2.sqlite")
    state_dir: str = ""
    start_from_tail: int = 0

    @classmethod
    def from_file(cls, path: Path) -> "MonitorConfig":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            allowed = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**allowed)
        except (OSError, ValueError, TypeError):
            return cls()


class ManualClock:
    def __init__(self, value: float = 0.0):
        self.value = float(value)

    def now(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += float(seconds)


class JsonStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self, default: Any) -> Any:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def save(self, value: Any) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.replace(tmp, self.path)
        except OSError:
            # Some Windows encrypted/profile folders reject ReplaceFile with
            # ERROR_NOT_SAME_DEVICE even for two files in one directory.  The
            # fallback still writes only our own state and never touches Codex.
            try:
                if self.path.exists():
                    self.path.unlink()
                os.rename(tmp, self.path)
            except OSError:
                self.path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
                try:
                    tmp.unlink()
                except OSError:
                    pass


@dataclass
class Notification:
    key: str
    severity: str
    title: str
    body: str
    timestamp: float
    project: str = "unknown"
    session_id: str = "unknown"
    state: str = TaskState.UNKNOWN.value
    confidence: str = "low"
    test: bool = False


class Notifier:
    """Persist reminders and optionally attempt a local Windows toast/beep.

    The persistent JSONL history is the authoritative delivery evidence.  Any
    Windows toast failure is recorded and never affects the monitored task.
    """

    def __init__(self, state_dir: Path, sound_enabled: bool = False, desktop_toast_enabled: bool = False):
        self.state_dir = state_dir
        self.history = state_dir / "notifications.jsonl"
        self.errors = state_dir / "notification_errors.jsonl"
        self.sound_enabled = sound_enabled
        self.desktop_toast_enabled = desktop_toast_enabled

    def emit(self, notification: Notification) -> dict[str, Any]:
        record = asdict(notification)
        record["delivery"] = "history"
        self.history.parent.mkdir(parents=True, exist_ok=True)
        with self.history.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.sound_enabled and os.name == "nt":
            try:
                import winsound

                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                record["sound"] = "submitted"
            except Exception as exc:  # pragma: no cover - platform dependent
                record["sound"] = "failed"
                self._error(str(exc))
        if self.desktop_toast_enabled and os.name == "nt":
            # A packaged desktop identity is required for Windows toast APIs;
            # don't claim success when this process has no registered identity.
            record["desktop_toast"] = "not_configured"
        else:
            record["desktop_toast"] = "disabled"
        return record

    def _error(self, message: str) -> None:
        with self.errors.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"timestamp": time.time(), "error": message}, ensure_ascii=False) + "\n")


class Deduplicator:
    def __init__(self, store: JsonStore, repeat_seconds: float = 300.0, hourly_limit: int = 6):
        self.store = store
        self.repeat_seconds = repeat_seconds
        self.hourly_limit = hourly_limit
        self.data = store.load({})

    def allow(self, key: str, now: float) -> bool:
        item = self.data.get(key, {"last": 0.0, "times": []})
        times = [t for t in item.get("times", []) if now - t < 3600]
        if times and now - times[-1] < self.repeat_seconds:
            self.data[key] = {"last": times[-1], "times": times}
            self.store.save(self.data)
            return False
        if len(times) >= self.hourly_limit:
            self.data[key] = {"last": times[-1], "times": times}
            self.store.save(self.data)
            return False
        times.append(now)
        self.data[key] = {"last": now, "times": times}
        self.store.save(self.data)
        return True


class StateMachine:
    def __init__(self, config: MonitorConfig, clock: Any, notifier: Notifier, dedup: Deduplicator):
        self.config = config
        self.clock = clock
        self.notifier = notifier
        self.dedup = dedup
        self.tasks: dict[str, TaskRecord] = {}

    @staticmethod
    def key_for(event: Event) -> str:
        return "|".join((event.instance_id, event.session_id or "unknown", event.turn_id or "", event.agent_id or ""))

    def ingest(self, event: Event) -> TaskRecord:
        now = event.timestamp
        key = self.key_for(event)
        rec = self.tasks.get(key)
        if not rec:
            rec = TaskRecord(key=key, instance_id=event.instance_id, session_id=event.session_id,
                             turn_id=event.turn_id, project=event.project, first_seen=now)
            self.tasks[key] = rec
        rec.last_source_read = now
        rec.last_state_event = now
        rec.health = Health.HEALTHY.value if event.source in {"thread_history_sqlite", "codex_logs_sqlite"} else Health.PARTIAL.value
        rec.project = event.project or rec.project
        rec.parent_session = event.parent_session or rec.parent_session
        rec.agent_id = event.agent_id or rec.agent_id
        rec.evidence = event.evidence[:240]
        rec.confidence = event.confidence
        rec.active = event.active
        state_map = {
            "capacity": TaskState.RETRYING_CAPACITY.value,
            "reconnect": TaskState.RECONNECTING.value,
            "approval": TaskState.WAITING_APPROVAL.value,
            "input": TaskState.WAITING_INPUT.value,
            "quota": TaskState.QUOTA_BLOCKED.value,
            "rate_limit": TaskState.RATE_LIMITED.value,
            "failed": TaskState.TURN_FAILED.value,
            "completed": TaskState.TURN_COMPLETED.value,
            "interrupt": TaskState.INTERRUPTED.value,
            "executor_unavailable": TaskState.EXECUTOR_UNAVAILABLE.value,
            "progress": TaskState.RUNNING.value,
            "active": TaskState.RUNNING.value,
            "idle": TaskState.IDLE.value,
        }
        rec.state = state_map.get(event.kind, rec.state)
        if event.kind in {"progress", "completed", "failed", "interrupt", "idle"}:
            rec.last_progress = now if event.kind == "progress" else rec.last_progress
        return rec

    def evaluate(self) -> list[Notification]:
        now = self.clock.now()
        made: list[Notification] = []
        for rec in self.tasks.values():
            age = max(0.0, now - rec.last_state_event)
            progress_age = max(0.0, now - rec.last_progress) if rec.last_progress else None
            threshold = {
                TaskState.WAITING_APPROVAL.value: self.config.approval_seconds,
                TaskState.WAITING_INPUT.value: self.config.input_seconds,
                TaskState.RETRYING_CAPACITY.value: self.config.capacity_seconds,
                TaskState.RECONNECTING.value: self.config.reconnect_seconds,
                TaskState.QUOTA_BLOCKED.value: self.config.input_seconds,
                TaskState.RATE_LIMITED.value: self.config.input_seconds,
                TaskState.TURN_FAILED.value: 0.0,
                TaskState.EXECUTOR_UNAVAILABLE.value: self.config.executor_seconds,
            }.get(rec.state)
            if threshold is not None and age >= threshold:
                kind = rec.state.lower()
                key = f"{rec.key}|{kind}"
                if self.dedup.allow(key, now):
                    made.append(self._notification(rec, key, f"Codex 需要处理｜{rec.project}",
                        f"状态 {rec.state} 已持续 {int(age)} 秒。远端是否仍在推进：未知。本工具未执行任何 Codex 操作。"))
            if rec.active and rec.last_progress and progress_age is not None:
                if progress_age >= self.config.silent_high_seconds and rec.state == TaskState.RUNNING.value:
                    key = f"{rec.key}|silent-high"
                    if self.dedup.allow(key, now):
                        rec.state = TaskState.SUSPECTED_STALL.value
                        made.append(self._notification(rec, key, f"Codex 长时间无可观察进展｜{rec.project}",
                            f"已 {int(progress_age)} 秒未观察到新进展，仍可能正在推理或执行长命令。请人工查看。"))
                elif progress_age >= self.config.silent_low_seconds and rec.state == TaskState.RUNNING.value:
                    key = f"{rec.key}|silent-low"
                    if self.dedup.allow(key, now):
                        made.append(self._notification(rec, key, f"Codex 进展提醒｜{rec.project}",
                            f"已 {int(progress_age)} 秒未观察到新进展（低优先级）。"))
        return made

    def _notification(self, rec: TaskRecord, key: str, title: str, body: str) -> Notification:
        return Notification(key=key, severity="warning", title=title, body=body,
                             timestamp=self.clock.now(), project=rec.project, session_id=rec.session_id,
                             state=rec.state, confidence=rec.confidence)


class LogSQLiteSource:
    """Read only the new tail of Codex's structured SQLite log."""

    def __init__(self, path: Path, tail: int = 0, limit: int = 500):
        self.path = path
        self.limit = limit
        self.cursor = 0
        self.health = Health.UNKNOWN if False else Health.UNAVAILABLE
        self.note = "not checked"
        if self.path.exists():
            try:
                with self._connect() as con:
                    max_id = int(con.execute("select coalesce(max(id),0) from logs").fetchone()[0])
                self.cursor = max(0, max_id - tail) if tail else max_id
                self.health = Health.HEALTHY
                self.note = f"read-only sqlite tail; cursor={self.cursor}"
            except Exception as exc:
                self.note = f"unavailable: {type(exc).__name__}"

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=1.5)

    def poll(self, now: float) -> list[Event]:
        if self.health == Health.UNAVAILABLE:
            return []
        try:
            with self._connect() as con:
                rows = con.execute(
                    "select id,ts,target,feedback_log_body,thread_id,process_uuid from logs where id>? order by id limit ?",
                    (self.cursor, self.limit)).fetchall()
            events: list[Event] = []
            for row_id, ts, target, body, thread_id, process_uuid in rows:
                self.cursor = max(self.cursor, int(row_id))
                events.extend(self._event_from_row(float(ts), target or "", body or "", thread_id or "unknown", process_uuid or "local"))
            return events
        except Exception as exc:
            self.health = Health.PARTIAL
            self.note = f"read error: {type(exc).__name__}"
            return []

    def _event_from_row(self, ts: float, target: str, body: str, thread_id: str, process_uuid: str) -> list[Event]:
        text = body[:1200]
        lower = text.lower()
        structured = lower.split("app-server event:", 1)[1].strip() if "app-server event:" in lower else ""
        # Only classify explicit structured markers emitted by the app-server.
        # Free-form prompt/chat text is intentionally ignored, even if it
        # contains capacity or reconnect wording.
        kind = None
        evidence = "structured log event"
        if "turn/completed" in structured:
            kind, evidence = "completed", "app-server turn/completed"
        elif re.match(r"(?:error|warning)\b", structured):
            kind, evidence = "failed", "structured error marker"
        elif "remotecontrol/status/changed" in structured and "reconnect" in structured:
            kind, evidence = "reconnect", "structured reconnect marker"
        elif re.match(r"(?:item/(?:started|completed)|turn/(?:started|diff/updated)|thread/tokenUsage/updated)\b", structured):
            kind, evidence = "progress", "app-server item/turn event"
        if not kind:
            return []
        return [Event(source="codex_logs_sqlite", kind=kind, timestamp=ts or time.time(),
                       session_id=thread_id, instance_id=process_uuid, evidence=evidence,
                       confidence="medium" if kind in {"completed", "progress"} else "low")]


class ThreadHistorySource:
    """Read whitelisted turn status fields from the local history projection."""

    def __init__(self, path: Path, limit: int = 100):
        self.path = path
        self.limit = limit
        self.health = Health.UNAVAILABLE
        self.note = "not checked"
        self.cursor = 0
        if path.exists():
            try:
                with self._connect() as con:
                    self.cursor = int(con.execute("select coalesce(max(coalesce(completed_at,started_at)),0) from thread_turns").fetchone()[0] or 0)
                self.health = Health.HEALTHY
                self.note = f"read-only thread_turns; cursor={self.cursor}"
            except Exception as exc:
                self.note = f"unavailable: {type(exc).__name__}"

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=1.5)

    def poll(self, now: float) -> list[Event]:
        if self.health == Health.UNAVAILABLE:
            return []
        try:
            with self._connect() as con:
                # Always include active turns, plus newly completed/failed ones.
                rows = con.execute(
                    "select thread_id,turn_id,status,started_at,completed_at from thread_turns "
                    "where status='inProgress' or coalesce(completed_at,started_at)>? "
                    "order by coalesce(completed_at,started_at) desc limit ?", (self.cursor, self.limit)).fetchall()
            events: list[Event] = []
            for thread_id, turn_id, status, started_at, completed_at in rows:
                stamp = float(completed_at or started_at or now)
                self.cursor = max(self.cursor, int(stamp))
                kind = {"inProgress": "active", "completed": "completed", "failed": "failed",
                        "interrupted": "interrupt"}.get(status)
                if not kind:
                    continue
                events.append(Event(source="thread_history_sqlite", kind=kind, timestamp=now if kind == "active" else stamp,
                                    session_id=thread_id, turn_id=turn_id,
                                    evidence=f"thread_turns status={status}", confidence="high",
                                    active=status == "inProgress"))
            return events
        except Exception as exc:
            self.health = Health.PARTIAL
            self.note = f"read error: {type(exc).__name__}"
            return []


class ProcessSource:
    """Auxiliary process presence probe; never treats presence as progress."""

    def __init__(self):
        self.health = Health.HEALTHY
        self.last = ""

    def snapshot(self) -> dict[str, Any]:
        names: list[str] = []
        try:
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=2,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in out.stdout.splitlines():
                if line.lower().startswith('"chatgpt.exe"') or line.lower().startswith('"codex.exe"'):
                    names.append(line.split(",", 1)[0].strip('"'))
            self.last = ",".join(sorted(set(names)))
        except Exception as exc:
            self.health = Health.PARTIAL
            self.last = f"probe_error:{type(exc).__name__}"
        return {"process_names": sorted(set(names)), "healthy": self.health.value, "note": "presence only; not progress evidence"}


def redact_path(value: str) -> str:
    if not value:
        return ""
    return re.sub(r"(?i)([A-Z]:\\Users\\)[^\\]+", r"\1<user>", value)


__all__ = ["Event", "TaskRecord", "TaskState", "Health", "MonitorConfig", "ManualClock",
           "JsonStore", "Notification", "Notifier", "Deduplicator", "StateMachine",
           "LogSQLiteSource", "ThreadHistorySource", "ProcessSource", "redact_path"]
