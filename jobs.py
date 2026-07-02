#!/usr/bin/env python3
"""
jobs.py — tiny in-memory background-job runner.

A "job" wraps one long async task (crawl, GSC check, nudge batch) so the UI can
start it, poll a progress bar, and switch away while it runs. In-memory is fine
for this single-user local tool (jobs don't need to survive a restart).

A job's work function is ``async def work(progress) -> dict`` where ``progress`` is
``progress(phase=..., done=..., total=..., message=...)``.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable

_jobs: dict[str, "Job"] = {}


class Job:
    def __init__(self, kind: str, client_id: int | None) -> None:
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.client_id = client_id
        self.status = "running"          # running | done | error
        self.progress: dict[str, Any] = {"phase": "starting", "done": 0, "total": 0, "message": ""}
        self.result: dict[str, Any] | None = None
        self.error: str | None = None

    def update(self, **kw: Any) -> None:
        self.progress.update({k: v for k, v in kw.items() if v is not None})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "client_id": self.client_id,
            "status": self.status,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
        }


def start_job(
    kind: str, client_id: int | None,
    work: Callable[[Callable[..., None]], Awaitable[dict[str, Any]]],
) -> Job:
    job = Job(kind, client_id)
    _jobs[job.id] = job

    async def runner() -> None:
        try:
            job.result = await work(job.update)
            job.status = "done"
            job.update(phase="done", message="Complete.")
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = str(exc)
            job.update(phase="error", message=str(exc))

    asyncio.create_task(runner())
    return job


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def active_for(kind: str, client_id: int | None) -> Job | None:
    for job in reversed(list(_jobs.values())):
        if job.kind == kind and job.client_id == client_id and job.status == "running":
            return job
    return None
