"""drp-web — a browser front end for ../deep-research-podcast, over Tailscale.

Routing, the lifespan, and the two things a page can do that a form cannot: serve an
episode's audio with range support, and hand back a run's log.

The app composes a CLI invocation and reads a log. It does not reimplement any part of
the pipeline, and it must not: every guard in that script exists because a real run
went wrong in a way nobody predicted, and a second implementation of "is this answer
grounded" would be a second thing to keep correct.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, PlainTextResponse,
                               RedirectResponse, Response, StreamingResponse)

from app import config, episode, health, progress, runs, suggest
from app.views import templates

log = logging.getLogger(__name__)

# Cache-control on every HTML page. OBSERVED on halo-workbench from an iPad: a status
# page under a meta refresh kept reading "running, 0s" for minutes after the job had
# finished, because Safari served its own copy back. A page whose entire job is to
# change under a refresh must say it is not cacheable.
NO_STORE = {"cache-control": "no-store"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        runs.start()
    except runs.StateDirBusy as exc:
        # Fail loudly and stop: a second supervisor would launch a second run into a
        # slot that only fits one, which on this box is the OOM kill.
        logging.getLogger("uvicorn.error").error("refusing to start — %s", exc)
        raise
    episode.start()
    try:
        yield
    finally:
        # Stops supervising; deliberately does NOT stop a running pipeline. See
        # runs.stop_supervisor.
        episode.stop()
        runs.stop_supervisor()


app = FastAPI(title="drp-web", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness only. Deliberately touches neither Open Notebook nor a model server:
    "is the app up" must stay answerable while a three-hour run has the GPU."""
    active = runs.active_run()
    return {
        "status": "ok",
        "version": config.VERSION,
        "active_run": active.id if active else None,
        "queued": len(runs.queued_runs()),
    }


# ── the form ─────────────────────────────────────────────────────────────────


def _form_context(request: Request, **extra):
    profiles = episode.profiles()
    return {
        "request": request,
        "health": health.snapshot(),
        "profiles": profiles,
        "defaults": {
            "episode_profile": config.EPISODE_PROFILE,
            "speaker_profile": config.SPEAKER_PROFILE,
            "max_turns": config.DEFAULT_MAX_TURNS,
        },
        "active": runs.active_run(),
        "queued": runs.queued_runs(),
        "recent": runs.all_runs()[:12],
        "max_questions": config.MAX_QUESTIONS,
        **extra,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", _form_context(request),
                                      headers=NO_STORE)


@app.post("/suggest", response_class=HTMLResponse)
def suggest_questions(request: Request, topic: str = Form(""), count: int = Form(4)):
    """Draft sub-questions into the form. Starts nothing -- see app/suggest.py."""
    try:
        drafted = suggest.suggest(topic, count)
        error = None
    except suggest.SuggestError as exc:
        drafted, error = [], str(exc)
    return templates.TemplateResponse(
        request, "index.html",
        _form_context(request, topic=topic, questions_text="\n".join(drafted),
                      suggest_error=error,
                      episode_name_hint=topic.strip()[:80]),
        headers=NO_STORE)


@app.post("/runs")
async def start_run(
    questions: str = Form(...),
    episode_name: str = Form(...),
    notebook_name: str = Form(""),
    briefing_suffix: str = Form(""),
    max_turns: int = Form(config.DEFAULT_MAX_TURNS),
    episode_profile: str = Form(config.EPISODE_PROFILE),
    speaker_profile: str = Form(config.SPEAKER_PROFILE),
    source_document: UploadFile | None = None,
    source_document_title: str = Form(""),
):
    lines = [q.strip() for q in questions.splitlines() if q.strip()]
    if not lines:
        raise HTTPException(400, "give me at least one sub-question, one per line")
    if len(lines) > config.MAX_QUESTIONS:
        raise HTTPException(
            400, f"{len(lines)} sub-questions is more than this box should take on in one "
                 f"run (limit {config.MAX_QUESTIONS}). Each one is 15-40 minutes of research.")
    if not episode_name.strip():
        raise HTTPException(400, "the episode needs a name")
    # Turn budget is clamped, not free-form: below ~80 a comparative or recency
    # sub-question finds good sources and then runs out of budget before it synthesises
    # anything -- measured, on a real run at 50. Trim the question COUNT for a shorter
    # run instead, which is why the count is capped above and this is not.
    max_turns = max(40, min(int(max_turns), 300))

    upload = None
    if source_document is not None and source_document.filename:
        data = await source_document.read(config.MAX_UPLOAD_BYTES + 1)
        if len(data) > config.MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"that document is larger than "
                                     f"{config.MAX_UPLOAD_BYTES // 1024 // 1024} MB")
        if not data.strip():
            raise HTTPException(400, "that document is empty")
        if data[:5] == b"%PDF-":
            # The pipeline does not convert documents and says so; handing it a PDF
            # produces an episode narrating binary. halo-workbench's marker tool is the
            # converter on this box.
            raise HTTPException(
                400, "this takes already-converted text or markdown, not a PDF — run it "
                     "through halo-workbench's marker tool first (http://halo-workbench/marker)")
        upload = (source_document.filename, data)

    params = {
        "questions": lines,
        "episode_name": episode_name.strip(),
        "notebook_name": (notebook_name.strip() or episode_name.strip()),
        "briefing_suffix": briefing_suffix.strip(),
        "max_turns": max_turns,
        "episode_profile": episode_profile,
        "speaker_profile": speaker_profile,
        "deliver_target": config.DELIVER_TARGET,
        "source_document_title": source_document_title.strip(),
    }
    run_id = runs.submit(params, upload=upload)
    # 303 so a reload of the status page cannot resubmit a three-hour run.
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# ── run pages ────────────────────────────────────────────────────────────────


def _run_or_404(run_id: str) -> runs.Run:
    run = runs.get(run_id)
    if run is None:
        raise HTTPException(404, "no such run")
    return run


@app.get("/runs", response_class=HTMLResponse)
def run_list(request: Request):
    return templates.TemplateResponse(
        request, "runs.html",
        {"runs": runs.all_runs(), "active": runs.active_run()},
        headers=NO_STORE)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str):
    run = _run_or_404(run_id)
    # Refresh the episode on view as well as on the watcher's 30-second tick, so a page
    # opened just after a poll is not showing a half-minute-old answer.
    if not run.episode.get("audio_rel"):
        episode.refresh(run)
        run = _run_or_404(run_id)

    state = progress.parse(run.read_log(), run.questions, run_dir=run.dir)
    return templates.TemplateResponse(
        request, "run.html",
        {
            "run": run,
            "state": state,
            "researcher_alive": (progress.researcher_alive()
                                 if run.status == runs.RUNNING else False),
            "audio": episode.audio_path(run),
            "results_file": _results_file(run),
            "on_ui": config.ON_UI,
            "log_tail": "\n".join(run.read_log().splitlines()[-60:]),
        },
        headers=NO_STORE)


def _results_file(run: runs.Run) -> Path | None:
    """The pipeline's own persisted research JSON, written into the run directory.

    It exists so that a crash after research loses nothing; offering it here means the
    research survives even a run whose episode failed outright.
    """
    matches = sorted(run.dir.glob("drp-results-*.json"))
    return matches[-1] if matches else None


@app.post("/runs/{run_id}/stop")
def stop_run(run_id: str):
    run = _run_or_404(run_id)
    try:
        runs.stop(run.id)
    except runs.RunError as exc:
        raise HTTPException(409, str(exc))
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}/log", response_class=PlainTextResponse)
def run_log(run_id: str):
    run = _run_or_404(run_id)
    return PlainTextResponse(run.read_log() or "(no log yet)", headers=NO_STORE)


@app.get("/runs/{run_id}/results")
def run_results(run_id: str):
    run = _run_or_404(run_id)
    path = _results_file(run)
    if path is None:
        raise HTTPException(404, "this run has not persisted its research yet")
    return FileResponse(path, media_type="application/json",
                        filename=f"{run.id}-research.json")


# ── audio ────────────────────────────────────────────────────────────────────

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
CHUNK = 256 * 1024


@app.get("/runs/{run_id}/audio")
def run_audio(request: Request, run_id: str):
    """Serve the finished episode, with range support.

    Range matters here rather than being a nicety: an hour of VibeVoice narration is
    tens of megabytes, and Safari on iOS will not scrub -- in places will not play at
    all -- against a response that answers a Range request with the whole file.
    """
    run = _run_or_404(run_id)
    path = episode.audio_path(run)
    if path is None:
        raise HTTPException(404, "no audio for this run yet")

    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "audio/mpeg"
    filename = f"{run.episode_name[:80] or run.id}{path.suffix}"
    disposition = f'inline; filename="{filename}"'

    header = request.headers.get("range")
    match = RANGE_RE.fullmatch(header.strip()) if header else None
    if not match:
        return FileResponse(path, media_type=media_type,
                            headers={"accept-ranges": "bytes",
                                     "content-disposition": disposition})

    start_text, end_text = match.groups()
    if start_text:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    else:
        # A suffix range ("bytes=-500") asks for the LAST n bytes, not the first.
        length = int(end_text or 0)
        start, end = max(0, size - length), size - 1
    end = min(end, size - 1)
    if start > end or start >= size:
        return Response(status_code=416, headers={"content-range": f"bytes */{size}"})

    def stream():
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = fh.read(min(CHUNK, remaining))
                if not data:
                    return
                remaining -= len(data)
                yield data

    return StreamingResponse(
        stream(), status_code=206, media_type=media_type,
        headers={
            "content-range": f"bytes {start}-{end}/{size}",
            "content-length": str(end - start + 1),
            "accept-ranges": "bytes",
            "content-disposition": disposition,
        })
