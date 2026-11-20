"""Console jobs: run ``python -m synth <command>`` in the background and watch it.

Purpose: let the console start runs, resumes, ingests and smoke tests without
blocking the web server. Each job is one child process (the same CLI a person
would type), its stderr progress lines kept in a ring buffer and its stdout JSON
parsed when it exits. At most one model-using job (run/resume/smoke) runs at a
time, so two runs never fight for the GPU.
Contract: :class:`JobManager` ``start(kind, args, profile)`` / ``get(job_id)`` /
``list()``. Stores written: none directly (the child process writes as usual).
Token caps: none. Spec: P1 §3 J0 (same entry points as the CLI).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synth.config import REPO_ROOT

RUN_ID = re.compile(r"\brun_[0-9A-HJKMNP-TV-Z]{26}\b")
GPU_KINDS = {"run", "resume", "smoke", "speedtest", "load"}
LOG_LINES = 300


class JobBusy(RuntimeError):
    """A model-using job is already running."""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    job_id: str
    kind: str
    args: list[str]
    profile: str | None
    started_at: str = field(default_factory=_now)
    status: str = "running"
    finished_at: str | None = None
    exit_code: int | None = None
    run_id: str | None = None
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    stdout: list[str] = field(default_factory=list)
    result: Any = None

    def as_dict(self, with_log: bool = True) -> dict[str, Any]:
        out = {"job_id": self.job_id, "kind": self.kind, "args": self.args, "profile": self.profile,
               "status": self.status, "started_at": self.started_at, "finished_at": self.finished_at,
               "exit_code": self.exit_code, "run_id": self.run_id, "result": self.result}
        out["log"] = list(self.log) if with_log else list(self.log)[-5:]
        return out


class JobManager:
    def __init__(self, cwd: Path = REPO_ROOT, python: str = sys.executable,
                 env: dict[str, str] | None = None) -> None:
        self.cwd, self.python = Path(cwd), python
        self.base_env = dict(env if env is not None else os.environ)
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def running(self, kinds: set[str] | None = None) -> list[Job]:
        return [j for j in self.jobs.values() if j.status == "running" and (kinds is None or j.kind in kinds)]

    def start(self, kind: str, args: list[str], profile: str | None = None,
              command: list[str] | None = None) -> Job:
        """Run ``python -m synth <args>`` (or ``command`` as given, e.g. ``lms load …``) as a job."""
        with self._lock:
            if kind in GPU_KINDS and self.running(GPU_KINDS):
                busy = self.running(GPU_KINDS)[0]
                raise JobBusy(f"{busy.kind} job {busy.job_id} is still running")
            job = Job(job_id="job_" + uuid.uuid4().hex[:16], kind=kind, args=list(command or args), profile=profile)
            self.jobs[job.job_id] = job
        env = dict(self.base_env, PYTHONUNBUFFERED="1")
        if profile:
            env["SYNTH_PROFILE"] = profile
        proc = subprocess.Popen(command or [self.python, "-m", "synth", *args], cwd=self.cwd, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
        threading.Thread(target=self._pump_stderr, args=(job, proc), daemon=True).start()
        threading.Thread(target=self._wait, args=(job, proc), daemon=True).start()
        return job

    def _pump_stderr(self, job: Job, proc: subprocess.Popen) -> None:
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            job.log.append(line)
            if job.run_id is None:
                m = RUN_ID.search(line)
                if m:
                    job.run_id = m.group(0)

    def _wait(self, job: Job, proc: subprocess.Popen) -> None:
        out = proc.stdout.read()
        job.exit_code = proc.wait()
        job.stdout = out.splitlines()
        text = out.strip()
        start = text.find("{")
        if start >= 0:
            try:
                job.result = json.loads(text[start:])
            except ValueError:
                job.result = {"stdout": text[-4000:]}
        elif text:
            job.result = {"stdout": text[-4000:]}
        if isinstance(job.result, dict) and job.run_id is None:
            job.run_id = job.result.get("run_id")
        job.status = "done" if job.exit_code == 0 else "failed"
        job.finished_at = _now()

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.started_at, reverse=True)
