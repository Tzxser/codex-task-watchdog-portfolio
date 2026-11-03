import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.watchdog_core import (
    Deduplicator,
    Event,
    JsonStore,
    LogSQLiteSource,
    ManualClock,
    MonitorConfig,
    Notifier,
    StateMachine,
    TaskState,
    ThreadHistorySource,
)


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = ManualClock(0)
        self.cfg = MonitorConfig(capacity_seconds=90, reconnect_seconds=90,
                                 approval_seconds=10, input_seconds=10,
                                 silent_low_seconds=30, silent_high_seconds=60)
        self.notifier = Notifier(self.root)
        self.machine = StateMachine(self.cfg, self.clock, self.notifier,
                                     Deduplicator(JsonStore(self.root / "dedup.json"), 300, 6))

    def tearDown(self):
        self.tmp.cleanup()

    def ev(self, kind, session="s1", turn="t1", ts=None, **kw):
        return Event(source="fixture", kind=kind, timestamp=self.clock.now() if ts is None else ts,
                     session_id=session, turn_id=turn, project=kw.pop("project", "fixture"),
                     evidence=kw.pop("evidence", "fixture"), confidence="high", **kw)

    def test_taskbook_text_is_not_a_structured_log_event(self):
        p = self.root / "logs.sqlite"
        con = sqlite3.connect(p)
        con.execute("create table logs(id integer primary key, ts integer, target text, feedback_log_body text, thread_id text, process_uuid text)")
        con.execute("insert into logs values(1,0,'codex_core::stream_events_utils','taskbook says Selected model is at capacity. Reconnecting... waiting for network','s','p')")
        con.commit(); con.close()
        source = LogSQLiteSource(p, tail=10)
        self.assertEqual(source.poll(0), [])

    def test_short_capacity_recovers_without_alert(self):
        self.machine.ingest(self.ev("capacity"))
        self.clock.advance(20)
        self.machine.ingest(self.ev("progress", ts=20))
        self.assertEqual(self.machine.evaluate(), [])
        self.assertEqual(self.machine.tasks["local|s1|t1|"].state, TaskState.RUNNING.value)

    def test_capacity_threshold_notifies_once_and_dedups(self):
        self.machine.ingest(self.ev("capacity"))
        self.clock.advance(91)
        notices = self.machine.evaluate()
        self.assertEqual(len(notices), 1)
        self.clock.advance(20)
        self.assertEqual(self.machine.evaluate(), [])

    def test_failed_turn_does_not_wait_for_capacity_timeout(self):
        self.machine.ingest(self.ev("capacity"))
        self.clock.advance(2)
        self.machine.ingest(self.ev("failed", ts=2))
        notices = self.machine.evaluate()
        self.assertEqual(len(notices), 1)
        self.assertIn("TURN_FAILED", notices[0].body)

    def test_reconnect_is_separate_and_remote_unknown(self):
        self.machine.ingest(self.ev("reconnect"))
        self.clock.advance(91)
        notices = self.machine.evaluate()
        self.assertEqual(len(notices), 1)
        self.assertIn("RECONNECTING", notices[0].body)
        self.assertIn("未知", notices[0].body)

    def test_quick_approval_resolution_leaves_no_alert(self):
        self.machine.ingest(self.ev("approval"))
        self.clock.advance(5)
        self.machine.ingest(self.ev("progress", ts=5))
        self.clock.advance(20)
        self.assertEqual(self.machine.evaluate(), [])

    def test_normal_question_is_not_waiting_input_without_event(self):
        self.machine.ingest(self.ev("progress", evidence="ordinary chat question"))
        self.clock.advance(100)
        self.assertEqual(self.machine.evaluate(), [])

    def test_sessions_do_not_mask_each_other(self):
        self.machine.ingest(self.ev("capacity", session="a"))
        self.machine.ingest(self.ev("progress", session="b"))
        self.clock.advance(91)
        notices = self.machine.evaluate()
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].session_id, "a")

    def test_repeated_retry_does_not_refresh_progress(self):
        self.machine.ingest(self.ev("progress"))
        self.clock.advance(10)
        self.machine.ingest(self.ev("capacity", ts=10))
        self.clock.advance(10)
        self.machine.ingest(self.ev("capacity", ts=20))
        rec = self.machine.tasks["local|s1|t1|"]
        self.assertEqual(rec.last_progress, 0)

    def test_thread_history_reads_status_without_item_content(self):
        p = self.root / "thread_history_1.sqlite"
        con = sqlite3.connect(p)
        con.execute("create table thread_turns(thread_id text, turn_id text, rollout_ordinal integer, status text, error_json text, started_at integer, completed_at integer, duration_ms integer, first_user_item_id text, final_agent_item_id text, rollout_byte_offset integer, rollout_end_ordinal integer, rollout_end_byte_offset integer, primary key(thread_id,turn_id))")
        con.execute("insert into thread_turns values('s','t',1,'inProgress',null,10,null,null,null,null,0,null,null)")
        con.commit(); con.close()
        src = ThreadHistorySource(p)
        rows = src.poll(20)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, "active")
        self.assertEqual(rows[0].session_id, "s")


if __name__ == "__main__":
    unittest.main()
