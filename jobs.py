#!/usr/bin/env python3
"""
jobs.py — background-job runner, state persisted in Postgres (db.jobs table).

A "job" wraps one long async task (crawl, GSC check, nudge batch) so the UI can
start it, poll a progress bar, and switch away while it runs. State lives in Postgres
(not memory) because on a serverless deploy the request that starts a job and the
requests that poll it can land on different instances.

The work itself still runs via `asyncio.create_task` in the process that started it —
correct on a persistent server (uvicorn), and best-effort on Vercel (relies on the
instance staying warm for the job's duration; see README's deploy notes).

A job's work function is ``async def work(progress) -> dict`` where ``progress`` is
``progress(phase=..., done=..., total=..., message=...)``.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable

import db


class Job:
    def __init__(self, job_id: str, kind: str, client_id: int | None) -> None:
        self.id = job_id
        self.kind = kind
        self.client_id = client_id

    def update(self, **kw: Any) -> None:
        db.update_job_progress(self.id, {k: v for k, v in kw.items() if v is not None})


def start_job(
    kind: str, client_id: int | None,
    work: Callable[[Callable[..., None]], Awaitable[dict[str, Any]]],
) -> Job:
    job_id = uuid.uuid4().hex
    db.create_job(job_id, kind, client_id)
    job = Job(job_id, kind, client_id)

    async def runner() -> None:
        try:
            result = await work(job.update)
            db.finish_job(job_id, status="done", result=result)
        except Exception as exc:  # noqa: BLE001
            db.finish_job(job_id, status="error", error=str(exc))

    asyncio.create_task(runner())
    return job


def get(job_id: str) -> dict[str, Any] | None:
    return db.get_job(job_id)


def active_for(kind: str, client_id: int | None) -> dict[str, Any] | None:
    return db.get_active_job(kind, client_id)
