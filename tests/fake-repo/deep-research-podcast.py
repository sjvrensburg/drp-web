#!/usr/bin/env python3
"""A stand-in for the real pipeline, for tests that must not spend three hours on a GPU.

It emits the same log markers in the same order and the same format -- that format is
the entire interface app/progress.py has -- and nothing else. It talks to no service.

The shape of the run comes from a marker inside a sub-question -- `[[fail]]`,
`[[drop]]`, `[[hang]]` -- and NOT from the environment. The app builds an explicit
env for each run (app/runs._run_env) and forwards nothing else, which is correct and
which silently made every mode fall back to "ok": two tests passed against a run that
was not doing what they thought. A marker in argv cannot be dropped that way.
"""
import os, sys, time

def _mode(argv):
    for arg in argv:
        for name in ("fail", "drop", "hang"):
            if f"[[{name}]]" in arg:
                return name
    return "ok"

MODE = _mode(sys.argv[1:])
questions = [a for a in sys.argv[1:] if not a.startswith("--")]
# argparse-style option values are interleaved; drop anything that followed a flag.
skip = set()
for i, arg in enumerate(sys.argv[1:]):
    if arg.startswith("--") and arg not in ("--fast", "--detach"):
        skip.add(i + 1)
questions = [a for i, a in enumerate(sys.argv[1:], start=1)
             if not a.startswith("--") and i not in skip]


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


log("starting research backend: 'systemctl --user start llama-research'")
time.sleep(0.2)
log("research backend ready")

for index, question in enumerate(questions):
    log(f"researching: {question!r} (max 120 turns)")
    if MODE == "hang":
        time.sleep(3600)
    time.sleep(0.3)
    if MODE == "drop" and index == 1:
        log(f"  warning: research failed for {question[:60]!r}, skipping this "
            f"sub-question and continuing with the rest: no source was read")
        continue
    log(f"  -> {3 + index} sources, natural conclusion after {40 + index} turns, synthesized")

log("stopped research backend: 'systemctl --user stop llama-research'")
log(f"research results persisted to {os.environ.get('DRP_RESULTS_DIR', '/tmp')}/drp-results-1.json")
with open(os.path.join(os.environ.get("DRP_RESULTS_DIR", "/tmp"), "drp-results-1.json"), "w") as fh:
    fh.write('[{"question": "fake", "answer": "fake"}]\n')

if MODE == "fail":
    log("FAILED: RuntimeError: Open Notebook refused the episode")
    sys.exit(1)

log("creating notebook 'Fake notebook'")
log(f"notebook has {len(questions)} research-note sources + 7 link sources")
log("triggering podcast generation (deep_dive profile, content=4211 chars)...")
log("job_id=command:fakejob123")
log("done. job_id=command:fakejob123 -- poll Open Notebook yourself")
