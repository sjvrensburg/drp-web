"""Read a run's log and say where it is.

The pipeline was written for a human reading a journal, not for a parser, so this
reads its log lines rather than any structured output. That is a real coupling and it
is one-way: every marker below is a `log()` call in ../local-podcast-studio, and a
reworded line degrades this page to "running, elapsed 41m" rather than breaking it.
Nothing here raises, nothing here is load-bearing for the run itself.

What this page exists to answer is the question the pipeline is worst at answering:
**is it still moving?** A single sub-question is 15-40 minutes of silence in the log,
because `run_research()` captures the researcher's stderr instead of passing it
through -- so a run that is working perfectly looks identical to one that is wedged.
Two things fix that here: the researcher's own per-turn log (written beside the run
log; see `research log:` below) when the pipeline is new enough to leave one, and a
process check for the researcher child when it is not.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

# ── the markers, each one a log() call in deep-research-podcast.py ───────────
_TS = r"\[\d\d:\d\d:\d\d\]\s*"
RE_LAUNCH = re.compile(r"^\[drp-web\] launching (\S+)", re.M)
RE_BACKEND_START = re.compile(_TS + r"starting research backend:", re.M)
RE_BACKEND_READY = re.compile(_TS + r"research backend ready", re.M)
RE_BACKEND_STOP = re.compile(_TS + r"stopped research backend:", re.M)
RE_RESEARCHING = re.compile(_TS + r"researching: (.+) \(max (\d+) turns\)", re.M)
RE_RESULT = re.compile(
    _TS + r"\s*-> (\d+) sources, (forced|natural) conclusion after (\d+) turns, "
    r"(synthesized|NO SYNTHESIS)", re.M)
RE_SKIPPED = re.compile(_TS + r"\s*warning: research failed for (.+), skipping", re.M)
RE_RESEARCH_LOG = re.compile(_TS + r"\s*research log: (\S+)", re.M)
RE_PERSISTED = re.compile(_TS + r"research results persisted to (\S+)", re.M)
RE_NOTEBOOK = re.compile(_TS + r"creating notebook (.+)", re.M)
RE_NOTEBOOK_DONE = re.compile(_TS + r"notebook has (\d+) research-note sources \+ (\d+) link", re.M)
RE_EXCLUDED = re.compile(_TS + r"\s*(\d+) sub-question\(s\) excluded from the episode", re.M)
RE_TRIGGER = re.compile(_TS + r"triggering podcast generation \((\S+) profile, content=(\d+)", re.M)
RE_JOB_ID = re.compile(_TS + r"job_id=(\S+)", re.M)
RE_FAILED = re.compile(_TS + r"FAILED: (.+)", re.M)
RE_WARNING = re.compile(_TS + r"\s*(warning: .+|WARNING: .+)", re.M)
# The researcher's own per-turn line, on ITS stderr: [turn 37] browser.open({...})
RE_TURN = re.compile(r"^\[turn (\d+)\] (\S+?)\((.*)$", re.M)

PENDING, ACTIVE, RESEARCHED, DROPPED = "pending", "researching", "researched", "dropped"

# Phases, in order. The UI shows them as a strip, so the order is the contract.
PHASES = ("queued", "starting", "research", "notebook", "episode", "finished")


def _dequote(text: str) -> str:
    """The pipeline logs questions with `!r`, so they arrive wearing Python quotes."""
    text = text.strip()
    if len(text) >= 2 and text[0] in "'\"" and text[-1] == text[0]:
        body = text[1:-1]
        return body.replace("\\'", "'").replace('\\"', '"').replace("\\\\", "\\")
    return text


def researcher_alive() -> bool:
    """Is an OpenResearcher child running right now?

    `pgrep openresearcher-run.py` silently matches nothing -- the kernel truncates
    comm at 15 characters -- which is a documented gotcha in the pipeline's own
    progress-monitoring notes. Match the full argument vector instead.
    """
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True,
                             timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "openresearcher-run.py" in out


def _turn_progress(path: Path) -> dict[str, Any] | None:
    """The last turn the researcher logged, from its own stderr log if there is one.

    Read from the end: a 120-turn log is a few hundred KB and this is polled every
    few seconds by every open browser tab.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - 8192))
            tail = fh.read().decode(errors="replace")
    except OSError:
        return None
    matches = RE_TURN.findall(tail)
    if not matches:
        return None
    turn, fn, args = matches[-1]
    return {"turn": int(turn), "tool": fn, "args": args.rstrip(") ")[:120]}


def parse(log_text: str, questions: list[str], run_dir: Path | None = None) -> dict[str, Any]:
    """Turn a run log into what the status page renders. Never raises."""
    # Walk the log's markers IN THE ORDER THEY APPEAR, rather than joining
    # question[i] to result[i]. That positional join was wrong in the one case that
    # matters: a dropped sub-question writes a warning and NO result line, so every
    # later question inherited its neighbour's result -- the run's third question
    # showed the second's sources, and the dropped one showed as researched. The
    # pipeline researches strictly in order and logs one `researching:` line per
    # attempt, so the cursor below is the join.
    events: list[tuple[int, str, object]] = []
    events += [(m.start(), "start", m.groups()) for m in RE_RESEARCHING.finditer(log_text)]
    events += [(m.start(), "result", m.groups()) for m in RE_RESULT.finditer(log_text)]
    events += [(m.start(), "skip", m.group(1)) for m in RE_SKIPPED.finditer(log_text)]
    events.sort(key=lambda e: e[0])

    items: list[dict[str, Any]] = [
        {"question": q, "status": PENDING, "sources": None, "turns": None,
         "synthesized": None, "conclusion": None, "max_turns": None}
        for q in questions
    ]

    cursor = -1
    started = 0
    for _, kind, payload in events:
        if kind == "start":
            cursor += 1
            started += 1
            if cursor < len(items):
                items[cursor]["status"] = ACTIVE
                items[cursor]["max_turns"] = int(payload[1])
                # The log carries the question the pipeline actually researched. It
                # should equal the one submitted, and if it does not -- the caller
                # edited the run directory, or a marker was misread -- the log wins.
                items[cursor]["logged_question"] = _dequote(payload[0])
        elif cursor < 0 or cursor >= len(items):
            continue
        elif kind == "result":
            sources, conclusion, turns, synth = payload
            items[cursor].update(status=RESEARCHED, sources=int(sources), turns=int(turns),
                                 conclusion=conclusion, synthesized=(synth == "synthesized"))
        else:  # skip
            items[cursor]["status"] = DROPPED

    job_ids = RE_JOB_ID.findall(log_text)
    # `job_id=failed` is the pipeline's own sentinel for "this run failed but somebody
    # is still being notified" -- never an Open Notebook job to poll.
    job_id = next((j for j in reversed(job_ids) if j != "failed"), None)

    failure = RE_FAILED.findall(log_text)
    trigger = RE_TRIGGER.findall(log_text)
    notebook_done = RE_NOTEBOOK_DONE.findall(log_text)
    excluded = RE_EXCLUDED.findall(log_text)

    # `reached` is how far the run got; `phase` is where it is. They differ only for a
    # failure, and the difference is what the status strip needs: a run that died while
    # generating the episode must not draw the same empty strip as one that died in
    # preflight -- that is the first thing anyone reads to know which log to open.
    if job_id or trigger:
        reached = "episode"
    elif notebook_done or RE_NOTEBOOK.search(log_text) or RE_PERSISTED.search(log_text):
        reached = "notebook"
    elif started:
        reached = "research"
    elif RE_BACKEND_START.search(log_text) or RE_LAUNCH.search(log_text):
        reached = "starting"
    else:
        reached = "queued"
    phase = "failed" if failure else reached

    turn = None
    research_logs = RE_RESEARCH_LOG.findall(log_text)
    if research_logs and run_dir is not None:
        # The pipeline names its own file; resolve it inside the run directory rather
        # than trusting an absolute path from a log line.
        candidate = run_dir / Path(research_logs[-1]).name
        if candidate.is_file():
            turn = _turn_progress(candidate)

    return {
        "phase": phase,
        "reached": reached,
        "questions": items,
        "researched": sum(1 for i in items if i["status"] == RESEARCHED),
        "dropped": sum(1 for i in items if i["status"] == DROPPED),
        "sources_total": sum(i["sources"] or 0 for i in items),
        "unsynthesized": [i for i in items if i["status"] == RESEARCHED and not i["synthesized"]],
        "job_id": job_id,
        "excluded_from_episode": int(excluded[-1]) if excluded else 0,
        "notebook": {"notes": int(notebook_done[-1][0]), "links": int(notebook_done[-1][1])}
                    if notebook_done else None,
        "episode_content_chars": int(trigger[-1][1]) if trigger else None,
        "backend_up": bool(RE_BACKEND_READY.search(log_text)) and not RE_BACKEND_STOP.search(log_text),
        "failure": failure[-1] if failure else None,
        "turn": turn,
        "warnings": RE_WARNING.findall(log_text)[-12:],
    }
