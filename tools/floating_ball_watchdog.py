"""Small, independent Windows floating ball for the local watchdog.

The ball is deliberately a *viewer and request writer*.  It reads the
watchdog's own status and notification files and writes only a small set of
ball-owned control files in that same runtime directory:

* ``ball.mute`` records this ball's local mute preference;
* ``safe-monitor.control.json`` asks the safe runner to mute/unmute itself;
* ``safe-monitor.stop.json`` requests a cooperative watchdog stop.

The control and stop records include the current safe-monitor ``run_id`` so a
stale record cannot stop or mute a later run.  When an explicitly supplied
legacy runtime is used, the bridge falls back to its old ``stop.request``
marker and keeps mute local to the ball.

It never imports ``watchdog_app``, starts or kills a process, sends input to
ChatGPT/Codex, changes a Codex task, or invokes any of the old batch/PowerShell
entry points.  The notification history remains the watchdog's authoritative
record; this process only provides a convenient local sound and UI.

This file intentionally uses only the Python standard library.  On Windows a
normal Python installation provides ``tkinter`` and ``winsound``.  Use
``--self-test`` for a headless, isolated test without creating a window.  The
``--runtime`` option is an explicit local directory override; the ball writes
only its own control files below that directory, and it should point to the
safe-monitor directory rather than a Codex data directory.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


DEFAULT_RUNTIME = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
    / "CodexTaskWatchdog"
    / "safe-monitor"
)


def _now() -> float:
    return time.time()


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically write a small control record owned by the ball."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_pid_alive(pid: int) -> bool:
    """Return process presence using a read-only tasklist query.

    ``os.kill(pid, 0)`` is intentionally avoided because the production
    watchdog's old lock implementation uses it and that path must not be
    touched by the ball.  A failed or unsupported query simply means
    "unknown/not alive" for display purposes.
    """

    if pid <= 0:
        return False
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return False
    lowered = "\n".join(lines).lower()
    if "no tasks are running" in lowered or "no instance" in lowered:
        return False
    # tasklist CSV rows begin with the image name and include the requested PID.
    return any(f'","{pid}",' in line or line.endswith(f'","{pid}"') for line in lines)


@dataclass
class DisplayState:
    label: str = "未运行"
    color: str = "#6b7280"
    muted: bool = False
    age_seconds: Optional[float] = None
    alert_count: int = 0


class RuntimeBridge:
    """Read status/alerts and write only ball-owned control records."""

    def __init__(self, runtime: Path, stale_after: float = 30.0):
        self.runtime = runtime
        # The safe launcher uses an isolated ``safe-monitor`` runtime and a
        # run-scoped control protocol.  Keep a read-only compatibility path
        # for an explicitly supplied legacy runtime, but never mix the two.
        self.safe_layout = runtime.name.lower() == "safe-monitor" or (runtime / "safe-monitor.status.json").exists()
        self.status_path = runtime / ("safe-monitor.status.json" if self.safe_layout else "status.json")
        self.heartbeat_path = runtime / ("safe-monitor.heartbeat.json" if self.safe_layout else "heartbeat.json")
        self.notice_path = runtime / "notifications.jsonl"
        self.mute_path = runtime / "ball.mute"
        self.stop_path = runtime / ("safe-monitor.stop.json" if self.safe_layout else "stop.request")
        self.control_path = runtime / ("safe-monitor.control.json" if self.safe_layout else "ball.control.json")
        self.stale_after = float(stale_after)
        self._notice_offset = 0
        self._muted = bool(_read_json(self.mute_path).get("muted", False))
        self._last_run_id: Optional[str] = None

    @property
    def muted(self) -> bool:
        return self._muted

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        run_id = self._run_id()
        running = bool(_read_json(self.status_path).get("running", False))
        if self.safe_layout and run_id and running:
            # The safe runner consumes this run-scoped action and changes its
            # own Notifier settings.  It is still only a local control file;
            # no host task or ChatGPT/Codex process is touched.
            _write_json(
                self.control_path,
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "action": "mute" if self._muted else "unmute",
                    "requested_at": _now(),
                    "source": "floating_ball",
                },
            )
        _write_json(
            self.mute_path,
            {
                "muted": self._muted,
                "updated_at": _now(),
                "source": "floating_ball",
                "scope": "floating_ball_sound_only",
            },
        )

    def request_stop(self) -> None:
        # The watchdog already polls this cooperative marker.  Writing it is
        # reversible and does not terminate a process or touch the host task.
        if self.safe_layout:
            run_id = self._run_id()
            running = bool(_read_json(self.status_path).get("running", False))
            if not run_id or not running:
                return
            _write_json(
                self.stop_path,
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "requested_at": _now(),
                    "source": "floating_ball",
                },
            )
            return
        _write_json(
            self.stop_path,
            {
                "requested_at": _now(),
                "source": "floating_ball",
                "scope": "watchdog_only_cooperative_stop",
            },
        )

    def _run_id(self) -> Optional[str]:
        nested = _read_json(self.status_path).get("safe_monitor", {})
        if not isinstance(nested, dict):
            return None
        value = nested.get("run_id")
        return value if isinstance(value, str) and value else None

    def status(self) -> DisplayState:
        data = _read_json(self.status_path)
        if not data:
            return DisplayState("未运行", "#6b7280", self._muted)
        self._sync_persisted_mute(data)
        updated = data.get("updated_at")
        try:
            age = max(0.0, _now() - float(updated))
        except (TypeError, ValueError):
            age = None
        running = data.get("running") is True
        # A stale status must never be shown as a healthy running monitor.
        if running and age is not None and age <= self.stale_after:
            return DisplayState("监测中", "#198754", self._muted, age)
        if running:
            return DisplayState("状态陈旧", "#d97706", self._muted, age)
        return DisplayState("已停止", "#6b7280", self._muted, age)

    def _sync_persisted_mute(self, data: dict[str, Any]) -> None:
        """Apply a persisted ball mute to each newly observed safe run."""

        if not self.safe_layout or not self._muted:
            return
        nested = data.get("safe_monitor", {})
        if not isinstance(nested, dict):
            return
        run_id = nested.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id == self._last_run_id:
            return
        self._last_run_id = run_id
        if data.get("running") is True:
            _write_json(
                self.control_path,
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "action": "mute",
                    "requested_at": _now(),
                    "source": "floating_ball_persisted_preference",
                },
            )

    def new_notifications(self) -> list[dict[str, Any]]:
        """Return only notifications appended since the ball was opened."""

        try:
            size = self.notice_path.stat().st_size
            if size < self._notice_offset:
                self._notice_offset = 0
            with self.notice_path.open("r", encoding="utf-8") as stream:
                stream.seek(self._notice_offset)
                text = stream.read()
                self._notice_offset = stream.tell()
        except OSError:
            return []
        found: list[dict[str, Any]] = []
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(item, dict):
                found.append(item)
        return found

    def prime_notifications(self) -> None:
        """Do not replay old alarms on startup."""

        try:
            self._notice_offset = self.notice_path.stat().st_size
        except OSError:
            self._notice_offset = 0

    def monitor_process_present(self) -> Optional[bool]:
        """Best-effort display-only check of the PID in watchdog.lock."""

        source = self.heartbeat_path if self.safe_layout else self.runtime / "watchdog.lock"
        lock = _read_json(source)
        try:
            pid = int(lock.get("pid", 0))
        except (TypeError, ValueError):
            return None
        if pid <= 0:
            return None
        return _safe_pid_alive(pid)


def _start_alert_loop() -> str:
    """Start the local Windows warning sound asynchronously and repeatedly."""

    if os.name != "nt":
        return "unsupported"
    try:
        import winsound

        winsound.PlaySound(
            "SystemExclamation",
            winsound.SND_ALIAS | winsound.SND_LOOP | winsound.SND_ASYNC,
        )
        return "submitted"
    except Exception:
        # A sound failure must never affect the monitored task or its history.
        return "failed"


def _stop_alert_loop() -> str:
    """Stop only the sound started by this floating-ball process."""

    if os.name != "nt":
        return "unsupported"
    try:
        import winsound

        winsound.PlaySound(None, 0)
        return "stopped"
    except Exception:
        return "failed"


class FloatingBall:
    """Tkinter UI.  Import/create this only when a desktop is available."""

    def __init__(self, bridge: RuntimeBridge, poll_ms: int = 1000):
        import tkinter as tk

        self.tk = tk
        self.bridge = bridge
        self.poll_ms = max(250, int(poll_ms))
        self._alerting = False
        self._sound_loop_active = False
        self.root = tk.Tk()
        self.root.title("Codex Watchdog")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", 0.94)
        except tk.TclError:
            pass
        width = height = 88
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.root.geometry(f"{width}x{height}+{max(0, screen_w - width - 24)}+{max(0, screen_h - height - 80)}")
        self.canvas = tk.Canvas(self.root, width=width, height=height, highlightthickness=0, bg="#111827")
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self._toggle_mute)
        self.canvas.bind("<Button-3>", self._menu)
        self.canvas.bind("<Double-Button-1>", self._show_details)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.menu = tk.Menu(self.root, tearoff=False)
        self._draw(DisplayState("未运行", "#6b7280", bridge.muted))
        self.bridge.prime_notifications()
        self._tick()

    def _draw(self, state: DisplayState) -> None:
        self.canvas.delete("all")
        self.canvas.create_oval(5, 5, 83, 83, fill=state.color, outline="#e5e7eb", width=2)
        mute_text = "静音" if state.muted else "有声"
        self.canvas.create_text(44, 32, text="WD", fill="white", font=("Segoe UI", 16, "bold"))
        self.canvas.create_text(44, 55, text=f"{state.label}\n{mute_text}", fill="white", font=("Segoe UI", 8), justify="center")
        self.canvas.configure(cursor="hand2")

    def _toggle_mute(self, _event: Any = None) -> None:
        self.bridge.set_muted(not self.bridge.muted)
        if self.bridge.muted:
            self._stop_sound()
        elif self._alerting:
            self._start_sound()
        state = self.bridge.status()
        state.muted = self.bridge.muted
        self._draw(state)

    def _menu(self, event: Any) -> None:
        self.menu.delete(0, "end")
        self.menu.add_command(label=("开启提示音" if self.bridge.muted else "静音提示音"), command=self._toggle_mute)
        self.menu.add_command(label="暂停当前循环提示音", command=self._pause_sound)
        self.menu.add_separator()
        self.menu.add_command(label="请求停止 watchdog", command=self._request_stop)
        self.menu.add_command(label="关闭悬浮球", command=self.root.destroy)
        self.menu.tk_popup(event.x_root, event.y_root)

    def _request_stop(self) -> None:
        self._stop_sound()
        self.bridge.request_stop()
        self._draw(DisplayState("停止请求", "#d97706", self.bridge.muted))

    def _start_sound(self) -> None:
        if self.bridge.muted or self._sound_loop_active:
            return
        if _start_alert_loop() == "submitted":
            self._sound_loop_active = True

    def _stop_sound(self) -> None:
        if self._sound_loop_active:
            _stop_alert_loop()
            self._sound_loop_active = False

    def _pause_sound(self) -> None:
        if not self.bridge.muted:
            self.bridge.set_muted(True)
        self._stop_sound()
        state = self.bridge.status()
        state.muted = self.bridge.muted
        self._draw(state)

    def _close(self) -> None:
        self._stop_sound()
        self.root.destroy()

    def _show_details(self, _event: Any = None) -> None:
        state = self.bridge.status()
        process = self.bridge.monitor_process_present()
        detail = f"状态：{state.label}\n"
        if state.age_seconds is not None:
            detail += f"状态年龄：{int(state.age_seconds)} 秒\n"
        detail += f"watchdog 进程：{'存在' if process else '未确认'}\n"
        detail += "左键：静音/有声；右键：菜单"
        try:
            from tkinter import messagebox

            messagebox.showinfo("Codex Watchdog", detail, parent=self.root)
        except Exception:
            pass

    def _tick(self) -> None:
        state = self.bridge.status()
        notices = self.bridge.new_notifications()
        if notices:
            self._alerting = True
        if self._alerting and self.bridge.muted:
            self._stop_sound()
        elif self._alerting:
            self._start_sound()
        state.muted = self.bridge.muted
        if notices:
            state.color = "#b91c1c"
            state.alert_count = len(notices)
        self._draw(state)
        self.root.after(self.poll_ms, self._tick)

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            self._stop_sound()


def run_sound_self_test() -> dict[str, Any]:
    """Verify loop and stop flags without playing a real sound."""

    calls: list[tuple[Any, int]] = []
    fake = types.SimpleNamespace(
        SND_ALIAS=0x00010000,
        SND_LOOP=0x00000008,
        SND_ASYNC=0x00000001,
        PlaySound=lambda sound, flags: calls.append((sound, flags)),
    )
    previous = sys.modules.get("winsound")
    sys.modules["winsound"] = fake
    try:
        started = _start_alert_loop()
        stopped = _stop_alert_loop()
    finally:
        if previous is None:
            sys.modules.pop("winsound", None)
        else:
            sys.modules["winsound"] = previous
    if started != "submitted" or stopped != "stopped":
        raise AssertionError("sound loop control calls failed")
    if calls != [("SystemExclamation", fake.SND_ALIAS | fake.SND_LOOP | fake.SND_ASYNC), (None, 0)]:
        raise AssertionError(f"unexpected sound calls: {calls!r}")
    return {"loop_flag_verified": True, "stop_call_verified": True, "real_sound_played": False}


def run_self_test() -> dict[str, Any]:
    """Exercise bridge controls and incremental notification parsing in temp."""

    with tempfile.TemporaryDirectory(prefix="codex-watchdog-ball-") as folder:
        runtime = Path(folder) / "safe-monitor"
        runtime.mkdir(parents=True, exist_ok=True)
        status = {
            "schema_version": 1,
            "updated_at": _now(),
            "running": True,
            "mode": "monitor_only_alert_only",
            "safe_monitor": {"run_id": "self-test-run", "running": True},
        }
        (runtime / "safe-monitor.status.json").write_text(json.dumps(status), encoding="utf-8")
        notice = {"key": "self-test", "severity": "warning", "delivery": "history"}
        (runtime / "notifications.jsonl").write_text(json.dumps(notice) + "\n", encoding="utf-8")
        bridge = RuntimeBridge(runtime, stale_after=30)
        bridge.prime_notifications()
        if bridge.new_notifications():
            raise AssertionError("startup must not replay old notification")
        with (runtime / "notifications.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"key": "new-alert"}) + "\n")
        if [item.get("key") for item in bridge.new_notifications()] != ["new-alert"]:
            raise AssertionError("incremental notification read failed")
        bridge.set_muted(True)
        if not _read_json(runtime / "ball.mute").get("muted"):
            raise AssertionError("mute marker missing")
        control = _read_json(runtime / "safe-monitor.control.json")
        if control.get("action") != "mute" or control.get("run_id") != "self-test-run":
            raise AssertionError("safe monitor mute control missing")
        bridge.request_stop()
        stop = _read_json(runtime / "safe-monitor.stop.json")
        if stop.get("source") != "floating_ball":
            raise AssertionError("stop marker source missing")
        if stop.get("run_id") != "self-test-run":
            raise AssertionError("stop marker run id missing")
        state = bridge.status()
        if state.label != "监测中":
            raise AssertionError(f"unexpected status label: {state.label}")
        return {
            "ok": True,
            "runtime_is_temporary": True,
            "old_notification_replayed": False,
            "new_notification_seen": True,
            "mute_marker_written": True,
            "cooperative_stop_marker_written": True,
            "production_started": False,
            "host_task_controlled": False,
            "sound_loop": run_sound_self_test(),
        }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Independent Codex watchdog floating ball")
    parser.add_argument("--runtime", type=Path, default=DEFAULT_RUNTIME,
                        help="safe-monitor runtime directory (default: %%LOCALAPPDATA%%\\CodexTaskWatchdog\\safe-monitor)")
    parser.add_argument("--poll-ms", type=int, default=1000)
    parser.add_argument("--stale-after", type=float, default=30.0)
    parser.add_argument("--self-test", action="store_true", help="run isolated bridge checks without opening a window")
    args = parser.parse_args(argv)
    if args.self_test:
        print(json.dumps(run_self_test(), ensure_ascii=False, indent=2))
        return 0
    try:
        runtime = args.runtime.expanduser().resolve()
        allowed = DEFAULT_RUNTIME.resolve()
        try:
            runtime.relative_to(allowed)
        except ValueError as exc:
            raise ValueError(f"runtime must stay under {allowed}") from exc
        bridge = RuntimeBridge(runtime, stale_after=args.stale_after)
        FloatingBall(bridge, poll_ms=args.poll_ms).run()
        return 0
    except Exception as exc:
        # A missing desktop/Tk must be reported without starting anything else.
        print(f"floating ball unavailable: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
