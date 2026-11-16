"""Background jobs: test runs, locator scans, health checks, recordings.

Each job is a separate process started with a fixed argument list (no shell). Output is
masked line by line before it is written to studio_data/jobs/<id>/output.log, and the job
record (who started it, command, status, timings) survives Studio restarts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from qaforge.compliance.logger import utc_now_iso
from qaforge.compliance.masker import masker

FINAL = {"passed", "failed", "error", "cancelled"}


@dataclass
class Job:
    id: str
    kind: str  # run | scan | pick | check | record | collect
    title: str
    user: str
    args: list[str]
    status: str = "queued"
    created: str = field(default_factory=utc_now_iso)
    started: str | None = None
    finished: str | None = None
    returncode: int | None = None
    run_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class JobManager:
    def __init__(self, root: Path, data_dir: Path, max_parallel: int = 2) -> None:
        self.root = root
        self.dir = data_dir / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._sem = threading.Semaphore(max_parallel)
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()
        for d in self.dir.iterdir():  # jobs interrupted by a restart
            j = self.get(d.name)
            if j and j.status not in FINAL:
                j.status, j.finished = "error", utc_now_iso()
                self._write(j)

    def job_dir(self, job_id: str) -> Path:
        if not job_id.isalnum():
            raise KeyError(job_id)
        return self.dir / job_id

    def _write(self, job: Job) -> None:
        d = self.job_dir(job.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "job.json").write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")

    def get(self, job_id: str) -> Job | None:
        try:
            f = self.job_dir(job_id) / "job.json"
        except KeyError:
            return None
        if not f.exists():
            return None
        return Job(**json.loads(f.read_text(encoding="utf-8")))

    def list(self, limit: int = 50, kind: str | None = None) -> list[Job]:
        jobs = [j for d in self.dir.iterdir() if (j := self.get(d.name))]
        if kind:
            jobs = [j for j in jobs if j.kind == kind]
        return sorted(jobs, key=lambda j: j.created, reverse=True)[:limit]

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def start(self, kind: str, title: str, user: str, args: list[str], env: dict[str, str] | None = None,
              run_id: str | None = None, meta: dict[str, Any] | None = None, job_id: str | None = None) -> Job:
        job = Job(id=job_id or self.new_id(), kind=kind, title=title, user=user, args=args, run_id=run_id,
                  meta=meta or {})
        self._write(job)
        threading.Thread(target=self._run, args=(job, env or {}), daemon=True, name=f"job-{job.id}").start()
        return job

    def _run(self, job: Job, extra_env: dict[str, str]) -> None:
        with self._sem:
            job.status, job.started = "running", utc_now_iso()
            self._write(job)
            env = {**os.environ, **extra_env, "PYTHONUNBUFFERED": "1", "QA_TRIGGERED_BY": job.user}
            log = self.job_dir(job.id) / "output.log"
            try:
                with log.open("w", encoding="utf-8") as out:
                    proc = subprocess.Popen(  # noqa: S603 - argument list built by Studio, no shell
                        [sys.executable, *job.args], cwd=self.root, env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
                    with self._lock:
                        self._procs[job.id] = proc
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        out.write(masker.mask(line.rstrip("\n")) + "\n")
                        out.flush()
                    job.returncode = proc.wait()
                if job.status != "cancelled":
                    job.status = "passed" if job.returncode == 0 else "failed"
            except Exception as exc:  # noqa: BLE001
                job.status = "error"
                with log.open("a", encoding="utf-8") as out:
                    out.write(f"Studio could not run this job: {exc}\n")
            finally:
                with self._lock:
                    self._procs.pop(job.id, None)
                job.finished = utc_now_iso()
                self._write(job)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(job_id)
        job = self.get(job_id)
        if proc is None or job is None:
            return False
        job.status = "cancelled"
        self._write(job)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        return True

    def read_log(self, job_id: str, offset: int = 0, max_bytes: int = 200_000) -> tuple[str, int]:
        f = self.job_dir(job_id) / "output.log"
        if not f.exists():
            return "", offset
        with f.open("rb") as fh:
            fh.seek(offset)
            data = fh.read(max_bytes)
        return data.decode("utf-8", errors="replace"), offset + len(data)

    def result(self, job_id: str) -> dict[str, Any] | None:
        f = self.job_dir(job_id) / "result.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None

    def wait(self, job_id: str, timeout: float = 600) -> Job | None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            j = self.get(job_id)
            if j and j.status in FINAL:
                return j
            time.sleep(0.3)
        return self.get(job_id)
