"""The run store: one directory per run, and the supervisor that starts them one at a time.

Modelled on ../halo-workbench/app/jobs.py, with one deliberate difference that
changes everything downstream:

**The work does not run inside this process.** halo-workbench executes a job on its
own worker thread, so a `systemctl restart` reaps whatever was in flight as
`interrupted`. A deep-research run takes 30 minutes to several hours -- restarting or
upgrading this web app must not cost one. So each run is launched as a *transient
systemd unit of its own* (`drp-web-run-<id>.service`) whose log is appended straight
into the run directory. This app is then a viewer and a supervisor: it can start a
run, read its progress, and stop it, but its own death is not the run's.

That inverts recovery. There is nothing to reap on startup; there is a question to
answer, per run left `running`: is that unit still active? The log's own exit sentinel
and `systemctl is-active` answer it, and a run whose unit is gone with no sentinel is
reported as vanished rather than quietly failed -- that is a reboot or a `systemctl
stop`, and saying "failed" would blame the pipeline for it.

Concurrency is 1 and is not a knob. The research phase starts `llama-research`, a
~30B model at a 131k context, on a box whose resident llama-servers already hold most
of the memory; starting it alongside them has been OOM-killed before. Two runs would
also both try to start and stop that one on-demand unit, and the second one's
`release_research_backend()` would pull it out from under the first.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app import config

log = logging.getLogger(__name__)

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
STOPPED = "stopped"
VANISHED = "vanished"

_LIVE = (QUEUED, RUNNING)
_TERMINAL = (DONE, FAILED, STOPPED, VANISHED)

LOG_NAME = "run.log"
META_NAME = "meta.json"

# Appended by the launch wrapper after the pipeline exits. The unit itself is
# transient and `--collect`ed, so it is gone from systemd's view seconds later and
# cannot be asked for an exit status; this line is on disk and survives a reboot.
EXIT_SENTINEL = re.compile(r"^\[drp-web\] exit=(-?\d+)\s*$", re.M)

# A backstop, not a schedule. The pipeline already caps each sub-question at
# DRP_RESEARCH_TIMEOUT (3h by default) and the longest plausible full run is a few
# hours; 12h means a wedged run cannot hold the single slot for a weekend. Expiry
# sends SIGTERM, which the pipeline handles -- it releases the research backend and
# notifies -- rather than being killed with the model left resident.
RUNTIME_MAX_SECONDS = int(os.environ.get("DRPWEB_RUNTIME_MAX_SECONDS", 12 * 3600))

SUPERVISOR_INTERVAL = 5.0
PRUNE_INTERVAL_SECONDS = 3600

_META_LOCK = threading.Lock()
_LAUNCH_LOCK = threading.Lock()
_STOP = threading.Event()
_SUPERVISOR: threading.Thread | None = None
_LOCK_FD: int | None = None


class StateDirBusy(RuntimeError):
    """Another instance already holds this state directory. Startup must not continue."""


class RunError(RuntimeError):
    """A refusal the user should read, not a bug."""


# ── the state-directory lock ─────────────────────────────────────────────────


def _acquire_lock() -> None:
    """One instance per state directory, enforced by the kernel.

    Two supervisors sharing a directory would each believe the slot is free at the
    same moment and launch two runs -- which on this box is the OOM the whole
    concurrency-1 rule exists to avoid. flock rather than a pid file, so a `kill -9`
    cannot leave a stale lock behind (halo-workbench learned this one first).
    """
    global _LOCK_FD

    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(config.STATE_DIR / "lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = os.read(fd, 64).decode(errors="replace").strip() or "unknown"
        os.close(fd)
        raise StateDirBusy(
            f"another drp-web (pid {holder}) already holds {config.STATE_DIR}. Only one "
            f"may supervise a state directory — check `systemctl --user status drp-web`, "
            f"or point this one elsewhere with DRPWEB_STATE_DIR."
        ) from None

    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _LOCK_FD = fd


def _release_lock() -> None:
    global _LOCK_FD
    if _LOCK_FD is not None:
        os.close(_LOCK_FD)
        _LOCK_FD = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── the Run object ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Run:
    id: str
    meta: dict[str, Any]

    @property
    def dir(self) -> Path:
        return config.RUNS_DIR / self.id

    @property
    def status(self) -> str:
        return self.meta.get("status", FAILED)

    @property
    def params(self) -> dict[str, Any]:
        return self.meta.get("params", {})

    @property
    def questions(self) -> list[str]:
        return self.params.get("questions", [])

    @property
    def episode_name(self) -> str:
        return self.params.get("episode_name", self.id)

    @property
    def unit(self) -> str:
        return self.meta.get("unit") or unit_name(self.id)

    @property
    def error(self) -> str | None:
        return self.meta.get("error")

    @property
    def episode(self) -> dict[str, Any]:
        """What app/episode.py has learned from Open Notebook about the episode."""
        return self.meta.get("episode", {})

    @property
    def log_path(self) -> Path:
        return self.dir / LOG_NAME

    def path(self, name: str) -> Path:
        return self.dir / name

    def read_log(self) -> str:
        try:
            return self.log_path.read_text(errors="replace")
        except OSError:
            return ""

    def elapsed_seconds(self) -> float | None:
        started = self.meta.get("started_at")
        if not started:
            return None
        end = self.meta.get("finished_at") or _now()
        return (datetime.fromisoformat(end) - datetime.fromisoformat(started)).total_seconds()

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL


def unit_name(run_id: str) -> str:
    return f"{config.UNIT_PREFIX}{run_id}.service"


# ── meta.json ────────────────────────────────────────────────────────────────


def _meta_path(run_id: str) -> Path:
    return config.RUNS_DIR / run_id / META_NAME


def _write_meta(run_id: str, meta: dict[str, Any]) -> None:
    """Replace meta.json atomically: a reader sees the old file or the new one, never half."""
    path = _meta_path(run_id)
    tmp = path.with_suffix(".json.tmp")
    with _META_LOCK:
        tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)


def _read_meta(run_id: str) -> dict[str, Any] | None:
    try:
        return json.loads(_meta_path(run_id).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def update(run_id: str, **fields: Any) -> dict[str, Any]:
    meta = _read_meta(run_id) or {}
    meta.update(fields)
    _write_meta(run_id, meta)
    return meta


def record_episode(run_id: str, **fields: Any) -> None:
    """Merge what we know about the Open Notebook episode into meta.json."""
    meta = _read_meta(run_id) or {}
    meta["episode"] = {**meta.get("episode", {}), **fields}
    _write_meta(run_id, meta)


# ── reading runs ─────────────────────────────────────────────────────────────


def new_id() -> str:
    """Sortable and readable in `ls`: 20260829-160142-3f9c1a. Also the unit name."""
    return f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def get(run_id: str) -> Run | None:
    # Defends the obvious traversal: /run/../../etc must not read outside RUNS_DIR.
    if "/" in run_id or run_id in (".", ".."):
        return None
    meta = _read_meta(run_id)
    return Run(run_id, meta) if meta else None


def all_runs() -> list[Run]:
    if not config.RUNS_DIR.is_dir():
        return []
    runs = (get(d.name) for d in config.RUNS_DIR.iterdir() if d.is_dir())
    return sorted((r for r in runs if r), key=lambda r: r.id, reverse=True)


def active_run() -> Run | None:
    """The run holding the single slot, if any."""
    for run in all_runs():
        if run.status == RUNNING:
            return run
    return None


def queued_runs() -> list[Run]:
    return sorted((r for r in all_runs() if r.status == QUEUED), key=lambda r: r.id)


# ── systemd ──────────────────────────────────────────────────────────────────


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args],
                          capture_output=True, text=True, check=False)


def unit_alive(unit: str) -> bool:
    """True while the unit is starting or running.

    `activating` counts: there is a window right after `systemd-run` returns where the
    unit is not yet `active`, and treating that as dead would let the supervisor start
    a second run into the same slot.
    """
    return _systemctl("is-active", unit).stdout.strip() in ("active", "activating", "reloading")


def _exit_code_from_log(run: Run) -> int | None:
    """The wrapper's own exit line, or None if the pipeline never got that far."""
    matches = EXIT_SENTINEL.findall(run.read_log())
    return int(matches[-1]) if matches else None


# ── launching ────────────────────────────────────────────────────────────────


def _pipeline_argv(run: Run) -> list[str]:
    """The exact CLI the operator would have typed. Nothing clever, on purpose.

    Sub-question decomposition is the caller's job and stays the caller's job (the
    pipeline repo's CLAUDE.md is explicit): the form collects the questions a person
    wrote or approved, and they arrive here as positional arguments unchanged.
    """
    p = run.params
    argv = [
        "python3", str(config.DRP_SCRIPT),
        "--episode-name", p["episode_name"],
        "--notebook-name", p["notebook_name"],
        "--max-turns", str(p.get("max_turns", config.DEFAULT_MAX_TURNS)),
        "--episode-profile", p.get("episode_profile", config.EPISODE_PROFILE),
        "--speaker-profile", p.get("speaker_profile", config.SPEAKER_PROFILE),
        "--deliver-target", p.get("deliver_target", config.DELIVER_TARGET),
    ]
    if p.get("notebook_description"):
        argv += ["--notebook-description", p["notebook_description"]]
    if p.get("briefing_suffix"):
        argv += ["--briefing-suffix", p["briefing_suffix"]]
    if p.get("source_document"):
        argv += ["--source-document", str(run.dir / p["source_document"])]
        if p.get("source_document_title"):
            argv += ["--source-document-title", p["source_document_title"]]
    # NOT --detach. That flag makes the pipeline launch a transient unit of its own,
    # named deep-research-podcast-<ts>, logging to DRP_RESULTS_DIR -- a unit this app
    # does not own, cannot stop by name, and would have to find by guessing. We do the
    # detaching ourselves so the unit and the log belong to the run directory.
    argv += list(p.get("questions", []))
    return argv


def _run_env(run: Run) -> dict[str, str]:
    """The DRP_* environment for one run.

    DRP_RESULTS_DIR points at the run directory, so the pipeline's own persisted
    research JSON -- the thing written precisely so that a later crash loses nothing --
    lands beside the log of the run that produced it instead of in /tmp.
    """
    env = {
        "PYTHONUNBUFFERED": "1",
        # systemd user services get a minimal PATH; BACKEND_START_CMD is `systemctl`.
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "DRP_LLM_URL": config.LLM_URL,
        "DRP_LLM_HEALTH_URL": config.LLM_HEALTH_URL,
        "DRP_ENRICH_LLM_URL": config.ENRICH_LLM_URL,
        "DRP_SYNTH_LLM_URL": config.SYNTH_LLM_URL,
        "DRP_SEARXNG_URL": config.SEARXNG_URL,
        "DRP_OPEN_NOTEBOOK_API": config.ON_API,
        "DRP_BACKEND_START_CMD": config.BACKEND_START_CMD,
        "DRP_BACKEND_STOP_CMD": config.BACKEND_STOP_CMD,
        "DRP_RESULTS_DIR": str(run.dir),
        "DRP_OPENRESEARCHER_PATH": str(config.DRP_REPO / "openresearcher-run.py"),
    }
    if config.POLLER_CMD:
        env["DRP_POLLER_CMD"] = config.POLLER_CMD
    if config.DOC_VERIFY_CMD and run.params.get("source_document"):
        # Only when there is a document to verify. The variable makes the pipeline fail
        # closed on a missing or slow verifier, which is right for a document and wrong
        # as a blanket condition on every run.
        env["DRP_DOC_VERIFY_CMD"] = config.DOC_VERIFY_CMD
    return env


def _launch(run: Run) -> None:
    """Start the pipeline as a transient unit. Raises RunError if systemd refuses."""
    unit = unit_name(run.id)
    log_path = run.log_path
    env = _run_env(run)

    command = [
        "systemd-run", "--user", f"--unit={unit}", "--collect", "--quiet",
        f"--property=RuntimeMaxSec={RUNTIME_MAX_SECONDS}",
        # Both streams into one file, in order: the pipeline logs phases on stdout and
        # its own warnings on stderr, and reading them apart tells you less.
        f"--property=StandardOutput=append:{log_path}",
        f"--property=StandardError=append:{log_path}",
        *[f"--setenv={k}={v}" for k, v in env.items()],
        # The wrapper exists for one line: the exit code, on disk, after the unit is
        # gone. `--collect` means systemd garbage-collects the unit as soon as it
        # stops, so `systemctl show` cannot be asked afterwards -- and without
        # `--collect` a failed run would sit in systemd's failed list needing a manual
        # reset-failed before the same id could ever be reused.
        "/bin/bash", "-c",
        'exec 0</dev/null; "$@"; ec=$?; echo "[drp-web] exit=$ec"; exit $ec',
        "drp-web",
        *_pipeline_argv(run),
    ]

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as fh:
        fh.write(f"[drp-web] launching {unit} at {_now()}\n")
        fh.write(f"[drp-web] {shlex.join(_pipeline_argv(run))}\n")

    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "systemd-run failed").strip()
        update(run.id, status=FAILED, finished_at=_now(),
               error=f"could not launch the run: {detail}")
        raise RunError(detail)

    update(run.id, status=RUNNING, started_at=_now(), unit=unit)
    log.info("run %s launched as %s", run.id, unit)


# ── submitting ───────────────────────────────────────────────────────────────


def submit(params: dict[str, Any], upload: tuple[str, bytes] | None = None) -> str:
    """Create the run directory and queue it. The supervisor launches it when the slot is free.

    Queued rather than launched inline so that submission cannot depend on the state of
    the box: pressing Start while another run holds the slot leaves a run waiting, not
    an error page.
    """
    if not params.get("questions"):
        raise RunError("a run needs at least one sub-question")

    run_id = new_id()
    run_dir = config.RUNS_DIR / run_id
    run_dir.mkdir(parents=True)

    if upload is not None:
        # Basename only: the filename comes from a browser, and `../../.ssh/authorized_keys`
        # is a perfectly valid filename as far as multipart is concerned.
        name = Path(upload[0]).name or "source-document.md"
        (run_dir / name).write_bytes(upload[1])
        params = {**params, "source_document": name}

    _write_meta(run_id, {
        "id": run_id,
        "status": QUEUED,
        "params": params,
        "unit": unit_name(run_id),
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "error": None,
        "episode": {},
    })
    log.info("run %s queued (%d sub-question(s))", run_id, len(params["questions"]))
    return run_id


def stop(run_id: str) -> None:
    """Stop a run at the user's request.

    `systemctl stop` sends SIGTERM to the unit's cgroup, which the pipeline turns into
    a SystemExit: it releases the on-demand research model and notifies on the way out.
    Killing the process instead would strand a multi-GB model on a box with no swap --
    which is why the pipeline grew a signal handler in the first place.
    """
    run = get(run_id)
    if run is None:
        raise RunError("no such run")
    if run.status == QUEUED:
        update(run_id, status=STOPPED, finished_at=_now(), error="cancelled before it started")
        return
    if run.status != RUNNING:
        raise RunError(f"this run is {run.status}, so there is nothing to stop")

    log.info("run %s: stopping %s at the user's request", run_id, run.unit)
    _systemctl("stop", run.unit)
    update(run_id, status=STOPPED, finished_at=_now(), error="stopped from the web interface")


# ── the supervisor ───────────────────────────────────────────────────────────


def reconcile() -> None:
    """Bring every `running` run's status into line with what actually happened.

    The three answers, in the order they are trustworthy:

    1. the unit is still alive -> it is running, leave it;
    2. the log carries the wrapper's exit sentinel -> done (0) or failed (non-zero);
    3. neither -> the unit is gone and the pipeline never reached its own exit. That is
       a reboot, an OOM kill, or `systemctl --user stop` from a terminal. Say VANISHED
       rather than "failed": the run may well have been healthy, and blaming the
       pipeline for the box going down sends the next person to the wrong log.
    """
    for run in all_runs():
        if run.status != RUNNING:
            continue
        if unit_alive(run.unit):
            continue

        code = _exit_code_from_log(run)
        if code == 0:
            update(run.id, status=DONE, finished_at=_now())
            log.info("run %s finished", run.id)
        elif code is not None:
            update(run.id, status=FAILED, finished_at=_now(),
                   error=f"the pipeline exited {code} — see the log below")
            log.info("run %s failed (exit %s)", run.id, code)
        else:
            update(run.id, status=VANISHED, finished_at=_now(),
                   error="the run's unit is gone and it never recorded an exit code — "
                         "the box rebooted, the unit was stopped outside this app, or "
                         "it was killed (out of memory is the usual reason here)")
            log.warning("run %s vanished without an exit code", run.id)


def _launch_next() -> None:
    """Start the oldest queued run, if the single slot is free."""
    with _LAUNCH_LOCK:
        if active_run() is not None:
            return
        pending = queued_runs()
        if not pending:
            return
        # Belt and braces: a unit left over from a run whose meta.json is out of step
        # still holds the model. Never launch on top of one.
        for run in all_runs():
            if run.status in _LIVE and unit_alive(run.unit):
                return
        try:
            _launch(pending[0])
        except RunError as exc:
            log.error("could not launch run %s: %s", pending[0].id, exc)


def prune(now: datetime | None = None) -> int:
    """Delete run directories past the retention window. Returns how many went.

    The whole directory: the log, the research JSON, and any uploaded source document.
    The document is the sensitive half -- an unpublished paper -- and is the reason
    this exists at all. Live runs are never pruned, whatever their age.
    """
    if not config.RUNS_DIR.is_dir() or config.RUN_RETENTION_DAYS <= 0:
        return 0

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=config.RUN_RETENTION_DAYS)
    removed = 0
    for entry in config.RUNS_DIR.iterdir():
        if not entry.is_dir():
            continue
        meta = _read_meta(entry.name)
        if meta and meta.get("status") in _LIVE:
            continue
        created = meta.get("created_at") if meta else None
        try:
            age_ref = datetime.fromisoformat(created) if created else None
        except ValueError:
            age_ref = None
        if age_ref is None:
            # Debris with no readable meta.json is exactly what most deserves pruning,
            # so fall back to mtime rather than skipping it.
            age_ref = datetime.fromtimestamp(entry.stat().st_mtime, timezone.utc)
        if age_ref < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
            log.info("pruned run %s (older than %d days)", entry.name, config.RUN_RETENTION_DAYS)
    return removed


def _supervisor_loop() -> None:
    last_prune = time.time()
    while not _STOP.is_set():
        try:
            reconcile()
            _launch_next()
            if time.time() - last_prune > PRUNE_INTERVAL_SECONDS:
                last_prune = time.time()
                prune()
        except Exception:  # a supervisor that dies stops every future run silently
            log.exception("supervisor tick failed")
        if _STOP.wait(SUPERVISOR_INTERVAL):
            return


def start() -> None:
    """Take the lock, reconcile once, and start supervising. Called from the lifespan."""
    global _SUPERVISOR

    _acquire_lock()
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _STOP.clear()
    # Before anything is launched: a run this app started before it was restarted may
    # still be going, and the slot is not free.
    reconcile()
    prune()
    _SUPERVISOR = threading.Thread(target=_supervisor_loop, name="run-supervisor", daemon=True)
    _SUPERVISOR.start()


def stop_supervisor(timeout: float = 5.0) -> None:
    """Stop supervising. Deliberately does NOT stop a running pipeline.

    Restarting this service must not cost a 3-hour run -- that is the entire reason the
    pipeline runs in a unit of its own. The next start reconciles and picks it up again.
    """
    _STOP.set()
    if _SUPERVISOR is not None:
        _SUPERVISOR.join(timeout=timeout)
    _release_lock()
