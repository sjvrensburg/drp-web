"""Acceptance checks that need no GPU, no models and no Open Notebook.

Run through tests/acceptance.sh, which points the app at a temporary state directory
and at tests/fake-repo -- a stand-in pipeline that emits the real one's log markers.

What is worth checking here is exactly what is easy to get wrong and invisible in a
green import: the log parser's join between sub-questions and results (a dropped
question shifts every later one), the single-slot rule, and the fact that a run
outlives this process.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, progress, runs  # noqa: E402

PASS, FAIL = 0, 0


def check(label, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def wait_for(predicate, timeout=30, interval=0.4):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ── 1. the log parser ────────────────────────────────────────────────────────
print("\nlog parsing")

LOG_DONE = """\
[drp-web] launching drp-web-run-x.service at 2026-08-29T10:00:00+00:00
[10:00:01] starting research backend: 'systemctl --user start llama-research'
[10:00:40] research backend ready
[10:00:40] researching: 'What changed in X since 2025?' (max 120 turns)
[10:22:11]   -> 9 sources, natural conclusion after 63 turns, synthesized
[10:22:11] researching: 'How does X compare to Y?' (max 120 turns)
[10:48:02]   -> 14 sources, forced conclusion after 120 turns, NO SYNTHESIS
[10:48:03] stopped research backend: 'systemctl --user stop llama-research'
[10:48:03] research results persisted to /run/drp-results-1.json
[10:48:04] creating notebook 'X vs Y'
[10:48:30] notebook has 2 research-note sources + 21 link sources
[10:48:31] triggering podcast generation (deep_dive profile, content=8123 chars)...
[10:48:33] job_id=command:abc123
[drp-web] exit=0
"""

state = progress.parse(LOG_DONE, ["What changed in X since 2025?", "How does X compare to Y?"])
check("phase is episode once a job id exists", state["phase"] == "episode", state["phase"])
check("job id is read from the log", state["job_id"] == "command:abc123", state["job_id"])
check("both questions researched", state["researched"] == 2)
check("sources are summed", state["sources_total"] == 23, state["sources_total"])
check("a forced, unsynthesised answer is flagged", len(state["unsynthesized"]) == 1)
check("notebook counts are read", state["notebook"] == {"notes": 2, "links": 21}, state["notebook"])
check("backend is reported released", state["backend_up"] is False)

LOG_DROP = """\
[10:00:40] researching: 'Question one, which works' (max 120 turns)
[10:22:11]   -> 9 sources, natural conclusion after 63 turns, synthesized
[10:22:11] researching: 'Question two, which reads nothing at all' (max 120 turns)
[10:30:00]   warning: research failed for 'Question two, which reads nothing at all', skipping this sub-question and continuing: no source was read
[10:30:01] researching: 'Question three, which works' (max 120 turns)
[10:44:00]   -> 4 sources, natural conclusion after 51 turns, synthesized
"""
qs = ["Question one, which works", "Question two, which reads nothing at all",
      "Question three, which works"]
state = progress.parse(LOG_DROP, qs)
statuses = [q["status"] for q in state["questions"]]
# The join is positional, and a dropped question produces no result line -- so without
# the re-alignment, question three would inherit question two's absent result.
check("a dropped question is marked dropped", statuses[1] == progress.DROPPED, statuses)
check("the question after a drop is not shifted",
      statuses[0] == progress.RESEARCHED and statuses[2] != progress.PENDING, statuses)
check("dropped is counted", state["dropped"] == 1)

state = progress.parse("[10:00:00] FAILED: RuntimeError: nothing worked\n", ["q"])
check("a failure is a phase", state["phase"] == "failed")
check("the failure reason is kept", "nothing worked" in (state["failure"] or ""))

state = progress.parse("[10:00:00] handing off...\n[10:00:00] job_id=failed\n", ["q"])
check("job_id=failed is not treated as an episode", state["job_id"] is None)

check("an empty log parses", progress.parse("", ["q"])["phase"] == "queued")
check("a truncated line does not raise",
      progress.parse("[10:00] researching: 'x' (max\n", ["x"])["phase"] == "queued")


# ── 2. a run, end to end, against the fake pipeline ──────────────────────────
print("\nrunning (fake pipeline)")

check("the state directory is the temporary one", "/tmp" in str(config.STATE_DIR),
      str(config.STATE_DIR))
check("the pipeline path points at the fake repo", "fake-repo" in str(config.DRP_SCRIPT),
      str(config.DRP_SCRIPT))

runs.start()
try:
    run_id = runs.submit({
        "questions": ["Question one", "Question two"],
        "episode_name": "Test episode",
        "notebook_name": "Test notebook",
        "max_turns": 120,
        "episode_profile": "deep_dive",
        "speaker_profile": "tech_experts",
    })
    check("a submitted run is queued", runs.get(run_id).status == runs.QUEUED)

    check("the supervisor launches it", wait_for(lambda: runs.get(run_id).status != runs.QUEUED))
    check("it runs in a unit of its own", runs.get(run_id).unit.startswith(config.UNIT_PREFIX))

    check("it reaches done", wait_for(lambda: runs.get(run_id).status == runs.DONE, timeout=60),
          runs.get(run_id).status)

    run = runs.get(run_id)
    state = progress.parse(run.read_log(), run.questions, run_dir=run.dir)
    check("progress reads the real log it produced", state["researched"] == 2, state)
    check("the exit sentinel was written", "[drp-web] exit=0" in run.read_log())
    check("the pipeline's results JSON landed in the run directory",
          list(run.dir.glob("drp-results-*.json")) != [])

    # Single slot. Checked by SAMPLING rather than by looking once: the fake pipeline
    # finishes in about a second, so a single look after the first run starts can
    # easily land after the second one has already been and gone -- which is what the
    # first version of this check did, and it reported a failure that was not one.
    a = runs.submit({"questions": ["a"], "episode_name": "A", "notebook_name": "A"})
    b = runs.submit({"questions": ["b"], "episode_name": "B", "notebook_name": "B"})
    overlaps = 0
    deadline = time.time() + 90
    while time.time() < deadline:
        running = [r for r in runs.all_runs() if r.status == runs.RUNNING]
        overlaps = max(overlaps, len(running))
        if runs.get(a).status == runs.DONE and runs.get(b).status == runs.DONE:
            break
        time.sleep(0.1)
    check("both finish", runs.get(a).status == runs.DONE and runs.get(b).status == runs.DONE,
          f"{runs.get(a).status}/{runs.get(b).status}")
    check("never two runs at once", overlaps <= 1, f"saw {overlaps} running together")

    # Stop: a hanging run must come back as `stopped`, and its unit must be gone.
    hung = runs.submit({"questions": ["forever [[hang]]"], "episode_name": "H",
                        "notebook_name": "H"})
    check("the hanging run starts", wait_for(lambda: runs.get(hung).status == runs.RUNNING))
    runs.stop(hung)
    check("stopping marks it stopped", runs.get(hung).status == runs.STOPPED)
    check("the unit is gone", wait_for(lambda: not runs.unit_alive(runs.get(hung).unit)))

    # A failing pipeline is `failed`, with the reason from its own log.
    bad = runs.submit({"questions": ["x [[fail]]"], "episode_name": "F", "notebook_name": "F"})
    check("the failing run finishes", wait_for(
        lambda: runs.get(bad).status in runs._TERMINAL, timeout=60), runs.get(bad).status)
    check("it is failed, not done", runs.get(bad).status == runs.FAILED, runs.get(bad).status)
    check("the failure text is in the parsed state",
          "Open Notebook refused" in (progress.parse(runs.get(bad).read_log(),
                                                     ["x [[fail]]"])["failure"] or ""))

    # And a run that drops a sub-question must say so, on a log it really produced.
    dropped_run = runs.submit({"questions": ["kept one", "lost one [[drop]]"],
                               "episode_name": "D", "notebook_name": "D"})
    check("the dropping run finishes", wait_for(
        lambda: runs.get(dropped_run).status in runs._TERMINAL, timeout=60))
    dropped = runs.get(dropped_run)
    dstate = progress.parse(dropped.read_log(), dropped.questions, run_dir=dropped.dir)
    check("a real dropped sub-question is counted", dstate["dropped"] == 1, dstate["dropped"])
    check("the kept sub-question keeps its own numbers",
          dstate["questions"][0]["sources"] == 3, dstate["questions"][0])

    # A second supervisor on the same state directory must refuse, loudly.
    second = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r);\n"
         "from app import runs\n"
         "try:\n"
         "    runs.start(); print('STARTED')\n"
         "except runs.StateDirBusy as exc:\n"
         "    print('REFUSED')\n" % str(Path(__file__).resolve().parent.parent)],
        capture_output=True, text=True, timeout=30)
    check("a second instance refuses the state directory", "REFUSED" in second.stdout,
          second.stdout + second.stderr)
finally:
    runs.stop_supervisor()

# A run outlives the supervisor: stopping it must not have killed anything.
check("stopping the supervisor stops no run", True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
