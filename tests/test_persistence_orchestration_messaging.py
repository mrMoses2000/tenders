import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from tender_agent.messaging import ApprovedMessageDispatcher, MessageDraft
from tender_agent.orchestrator import JobRunner
from tender_agent.persistence import StateStore


class FakeAdapter:
    def __init__(self) -> None:
        self.sent = []

    def send(self, recipient: str, body: str, idempotency_key: str) -> str:
        self.sent.append((recipient, body, idempotency_key))
        return "provider-1"


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = StateStore()
        self.store.migrate()

    def tearDown(self) -> None:
        self.store.close()

    def test_fact_is_idempotent_but_cannot_change(self) -> None:
        self.assertTrue(self.store.append_fact("r", "r:1", "f", {"value": 1}))
        self.assertFalse(self.store.append_fact("r", "r:1", "f", {"value": 1}))
        with self.assertRaises(ValueError):
            self.store.append_fact("r", "r:1", "f", {"value": 2})

    def test_job_retry_is_durable_and_dead_letters(self) -> None:
        self.store.create_task("t", "r", "corr", {})
        self.store.enqueue_job("j", "t", "explode", {}, max_attempts=2)
        runner = JobRunner(self.store, "w", {"explode": lambda _: (_ for _ in ()).throw(ValueError("boom"))})
        self.assertEqual(runner.run_once().status, "retry")
        self.assertEqual(runner.run_once().status, "dead_letter")
        self.assertEqual(self.store.job_counts(), {"dead_letter": 1})

    def test_completed_job_is_not_acquired_twice(self) -> None:
        calls = []
        self.store.create_task("t", "r", "corr", {})
        self.store.enqueue_job("j", "t", "work", {"x": 1})
        runner = JobRunner(self.store, "w", {"work": lambda payload: calls.append(payload)})
        self.assertEqual(runner.run_once().status, "completed")
        self.assertIsNone(runner.run_once())
        self.assertEqual(calls, [{"x": 1}])
        self.assertEqual(self.store.task_status("t"), "completed")
        events = self.store.audit_events("corr")
        self.assertEqual([event["event_type"] for event in events], ["job_started", "job_completed"])

    def test_expired_running_lease_is_recovered(self) -> None:
        self.store.create_task("t", "r", "corr", {})
        self.store.enqueue_job("j", "t", "work", {})
        first = self.store.acquire_job("crashed-worker")
        self.assertEqual(first["status"], "running")
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.store.connection.execute("UPDATE jobs SET lease_until=? WHERE job_id='j'", (expired,))
        self.store.connection.commit()
        recovered = self.store.acquire_job("recovery-worker")
        self.assertEqual(recovered["job_id"], "j")
        self.assertEqual(recovered["attempts"], 2)

    def test_invalid_task_transition_is_rejected(self) -> None:
        self.store.create_task("t", "r", "corr", {})
        with self.assertRaisesRegex(ValueError, "invalid task transition"):
            self.store.transition_task("t", "completed")

    def test_message_needs_exact_approval_and_is_idempotent(self) -> None:
        adapter = FakeAdapter()
        dispatcher = ApprovedMessageDispatcher(self.store, adapter)
        draft = MessageDraft("d", "whatsapp", "+7700", "Цена и наличие?", "r")
        with self.assertRaises(PermissionError):
            dispatcher.dispatch(draft, "a", "send:1")
        self.store.approve("a", "send_message", draft.payload_hash, "operator")
        first = dispatcher.dispatch(draft, "a", "send:1")
        second = dispatcher.dispatch(draft, "a", "send:1")
        self.assertEqual(first, second)
        self.assertEqual(len(adapter.sent), 1)

    def test_uncertain_send_is_not_retried_blindly(self) -> None:
        class FailingAdapter:
            def send(self, recipient, body, idempotency_key):
                raise TimeoutError("provider outcome unknown")

        draft = MessageDraft("d", "whatsapp", "+7700", "Цена?", "r")
        self.store.approve("a", "send_message", draft.payload_hash, "operator")
        dispatcher = ApprovedMessageDispatcher(self.store, FailingAdapter())
        with self.assertRaises(TimeoutError):
            dispatcher.dispatch(draft, "a", "send:uncertain")
        with self.assertRaisesRegex(RuntimeError, "outcome is uncertain"):
            dispatcher.dispatch(draft, "a", "send:uncertain")

    def test_sqlite_survives_reopen(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as handle:
            with StateStore(handle.name) as store:
                store.migrate()
                store.create_task("t", "r", "corr", {})
            with StateStore(handle.name) as reopened:
                reopened.migrate()
                reopened.enqueue_job("j", "t", "work", {})
                self.assertEqual(reopened.job_counts(), {"queued": 1})


if __name__ == "__main__":
    unittest.main()
