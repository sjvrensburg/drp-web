"""Open Notebook: the episode half of a run, and the audio that comes out of it.

Triggering generation is the pipeline's job and ends the moment it has a `job_id`.
Everything after that happens inside Open Notebook and takes another 20-90 minutes,
so something has to watch it. On this box that something was `poll_and_notify.sh`,
attached through `DRP_POLLER_CMD`, which polls to completion and messages Signal --
and which, because it polls in the foreground, keeps the pipeline process alive for
the whole episode. This app watches instead, from a thread, and shows the answer in
the browser; the Signal handoff stays available (config.POLLER_CMD) but is off.

Endpoint paths here are the ones that actually exist, which is not obvious:
`GET /api/episode-profiles`, with NO `podcasts` segment -- `/api/podcasts/episode-profiles`
404s, and a session once concluded from that the endpoint did not exist at all.
Job status is the other way round: `GET /api/podcasts/jobs/{job_id}`.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from app import config, runs

log = logging.getLogger(__name__)

# Tight early so a bad job id is caught in minutes rather than hours, then relaxed --
# an episode is 20-90 minutes and nothing is gained by asking every 10 seconds.
POLL_INTERVAL_SECONDS = 30
TERMINAL_STATUSES = ("completed", "failed", "error", "cancelled")

RE_JOB_ID = re.compile(r"job_id=(\S+)")

_STOP = threading.Event()
_WATCHER: threading.Thread | None = None


def api_get(path: str, timeout: int = 10) -> Any:
    """GET a JSON endpoint. Returns None rather than raising -- every caller is a page."""
    url = f"{config.ON_API}{path}"
    try:
        request = urllib.request.Request(url, headers={"accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
        log.debug("GET %s failed: %s", url, exc)
        return None


def profiles() -> dict[str, list[str]]:
    """Episode and speaker profile NAMES, for the form's two dropdowns.

    Names rather than ids: POST /api/podcasts/generate accepts either, the pipeline's
    --episode-profile is documented in names, and an id in a form field is unreadable.
    An empty list means Open Notebook did not answer -- the form says so and falls back
    to the configured defaults rather than pretending there is nothing to choose.
    """
    def names(payload: Any) -> list[str]:
        if not isinstance(payload, list):
            return []
        return sorted({p.get("name") for p in payload if isinstance(p, dict) and p.get("name")})

    return {
        "episode": names(api_get("/episode-profiles")),
        "speaker": names(api_get("/speaker-profiles")),
    }


def job_status(job_id: str) -> dict[str, Any] | None:
    return api_get(f"/podcasts/jobs/{job_id}")


def _job_id_from_log(run: runs.Run) -> str | None:
    ids = RE_JOB_ID.findall(run.read_log())
    # `job_id=failed` is the pipeline's sentinel for a failed run that still notified.
    return next((j for j in reversed(ids) if j != "failed"), None)


def audio_path(run: runs.Run) -> Path | None:
    """The finished episode's file on disk, or None.

    `audio_file_path` in the job result is relative to the podcasts directory, which is
    the host side of Open Notebook's `-> /app/data/podcasts` bind mount. A container-
    absolute path is tolerated because the container is what wrote it.

    Deliberately no fallback to "the newest .mp3 under episodes/": that is exactly how
    one episode's audio gets served as another's. If the recorded path is not there,
    the page says so and links Open Notebook.
    """
    rel = run.episode.get("audio_rel")
    if not rel:
        return None
    text = str(rel)
    for prefix in ("/app/data/podcasts/", "/app/data/podcasts"):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    candidate = (config.PODCASTS_DIR / text.lstrip("/")).resolve()
    try:
        candidate.relative_to(config.PODCASTS_DIR.resolve())
    except ValueError:
        # A path outside the podcasts directory is not an episode, whatever wrote it.
        log.warning("run %s: audio path %r escapes the podcasts directory", run.id, rel)
        return None
    return candidate if candidate.is_file() else None


def refresh(run: runs.Run) -> None:
    """Ask Open Notebook where this run's episode is, and record the answer.

    Called from the watcher thread and again whenever someone opens the run page, so a
    page loaded 29 seconds into the poll interval is not stale.
    """
    job_id = run.episode.get("job_id") or _job_id_from_log(run)
    if not job_id:
        return
    if run.episode.get("status") in TERMINAL_STATUSES and run.episode.get("audio_rel"):
        return

    payload = job_status(job_id)
    if payload is None:
        # Not an error state: Open Notebook restarts, and a run whose episode is fine
        # must not be reported as failed because one poll missed.
        runs.record_episode(run.id, job_id=job_id, checked_at=time.time(), reachable=False)
        return

    status = payload.get("status") or "unknown"
    result = payload.get("result") or {}
    fields: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "checked_at": time.time(),
        "reachable": True,
        "error": payload.get("error_message"),
    }
    if result.get("audio_file_path"):
        fields["audio_rel"] = result["audio_file_path"]
    runs.record_episode(run.id, **fields)


def _watch_loop() -> None:
    while not _STOP.is_set():
        try:
            for run in runs.all_runs():
                if _STOP.is_set():
                    return
                # A run still researching has no episode yet; one whose episode is
                # finished has nothing left to learn.
                if run.status == runs.QUEUED:
                    continue
                if run.episode.get("status") in TERMINAL_STATUSES and run.episode.get("audio_rel"):
                    continue
                if not (run.episode.get("job_id") or _job_id_from_log(run)):
                    continue
                refresh(run)
        except Exception:  # a watcher that dies must not take the service with it
            log.exception("episode watch tick failed")
        if _STOP.wait(POLL_INTERVAL_SECONDS):
            return


def start() -> None:
    global _WATCHER
    _STOP.clear()
    _WATCHER = threading.Thread(target=_watch_loop, name="episode-watcher", daemon=True)
    _WATCHER.start()


def stop() -> None:
    _STOP.set()
    if _WATCHER is not None:
        _WATCHER.join(timeout=5)
