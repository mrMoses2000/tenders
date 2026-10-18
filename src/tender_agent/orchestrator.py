from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .persistence import StateStore

JobHandler = Callable[[dict[str, Any]], dict[str, Any] | None]


@dataclass(frozen=True)
class RunResult:
    job_id: str
    status: str
    error: str = ""


class JobRunner:
    def __init__(
        self, store: StateStore, worker_id: str, handlers: Mapping[str, JobHandler],
        backoff_seconds: int = 0,
    ) -> None:
        self.store = store
        self.worker_id = worker_id
        self.handlers = handlers
        self.backoff_seconds = backoff_seconds

    def run_once(self) -> RunResult | None:
        job = self.store.acquire_job(self.worker_id)
        if job is None:
            return None
        job_id = job["job_id"]
        correlation_id = job["correlation_id"]
        task_id = job["task_id"]
        self.store.transition_task(task_id, "running")
        self.store.audit(
            correlation_id, "job_started", job_id,
            {"kind": job["kind"], "attempt": job["attempts"], "worker_id": self.worker_id},
        )
        handler = self.handlers.get(job["kind"])
        if handler is None:
            status = self.store.fail_job(
                job_id, self.worker_id, f"no handler for {job['kind']}", self.backoff_seconds
            )
            error = f"no handler for {job['kind']}"
            if status == "dead_letter":
                self.store.transition_task(task_id, "failed")
            self.store.audit(correlation_id, "job_failed", job_id, {"status": status, "error": error})
            return RunResult(job_id, status, error)
        try:
            handler(job["payload"])
        except Exception as error:  # worker boundary deliberately catches and records failures
            status = self.store.fail_job(
                job_id, self.worker_id, f"{type(error).__name__}: {error}", self.backoff_seconds
            )
            self.store.audit(
                correlation_id, "job_failed", job_id,
                {"status": status, "error_type": type(error).__name__, "error": str(error)},
            )
            if status == "dead_letter":
                self.store.transition_task(task_id, "failed")
            return RunResult(job_id, status, str(error))
        self.store.complete_job(job_id, self.worker_id)
        if not self.store.task_has_unfinished_jobs(task_id):
            self.store.transition_task(task_id, "completed")
        self.store.audit(correlation_id, "job_completed", job_id, {"worker_id": self.worker_id})
        return RunResult(job_id, "completed")
