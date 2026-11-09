"""Small in-process job queue so long backtests never block the web server.

For multi-server deployments swap this for Celery/RQ/Arq behind the same
``submit``/``get`` interface.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

log = logging.getLogger(__name__)


class JobManager:
    def __init__(self, workers: int = 2, keep: int = 100, inline: bool = False):
        # inline=True runs each job inside the request (serverless: nothing may run after the response).
        self.inline = inline
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="job")
        self._jobs: OrderedDict[str, dict] = OrderedDict()
        self._lock = threading.Lock()
        self._keep = keep

    def submit(self, kind: str, fn: Callable[[], Any]) -> str:
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._jobs[job_id] = {"id": job_id, "kind": kind, "status": "queued", "created": time.time(),
                                  "result": None, "error": None}
            while len(self._jobs) > self._keep:
                self._jobs.popitem(last=False)
        if self.inline:
            self._run(job_id, fn)
        else:
            self._pool.submit(self._run, job_id, fn)
        return job_id

    def _run(self, job_id: str, fn: Callable[[], Any]) -> None:
        self._set(job_id, status="running", started=time.time())
        try:
            result = fn()
        except (ValueError, KeyError, LookupError) as exc:
            self._set(job_id, status="failed", error=str(exc), finished=time.time())
        except Exception as exc:  # never let a worker thread die silently
            log.exception("job %s failed", job_id)
            self._set(job_id, status="failed", error=f"Internal error: {type(exc).__name__}", finished=time.time())
        else:
            self._set(job_id, status="done", result=result, finished=time.time())

    def _set(self, job_id: str, **kw) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(kw)

    def get(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"no job {job_id!r}")
            return dict(job)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
