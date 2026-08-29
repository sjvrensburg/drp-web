# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository.

## What this is

A FastAPI + Jinja web front end for `../deep-research-podcast`, on `:8095`, reachable
on the tailnet through `tailscale serve`. Server-rendered HTML, no build step, no npm,
four dependencies (`fastapi`, `uvicorn`, `jinja2`, `python-multipart`) and nothing else
— every call to a model, to Open Notebook or to systemd goes through `urllib` or a
subprocess. `README.md` is the user-facing document; this file is the decisions.

It is the sibling of `../halo-workbench` and deliberately shares its skeleton: runs are
directories, pages refresh with `<meta refresh>`, the app binds loopback only, one
`config.py` names every value duplicated outside the repo. Read that repo's CLAUDE.md
for the shared rules; this file records only what is different here, and why.

- `app/runs.py` — the run store and the supervisor.
- `app/progress.py` — turns the pipeline's log into the status page. Read-only, and
  never load-bearing for a run.
- `app/episode.py` — Open Notebook: profiles for the form, job polling, audio.
- `app/suggest.py` — drafts sub-questions. Drafts only.
- `app/health.py` — the form's pre-flight strip.
- `tests/` — 52 checks with no GPU, no models and no network (`tests/acceptance.sh`).

## The rule this app is organised around

**It composes a command line and reads a log. It reimplements nothing.**

Every guard in `deep-research-podcast.py` was added after a real run went wrong — the
grounding gate, the three separate LLM roles, `_strip_reasoning`, `_is_degenerate`, the
honest "no synthesis" fallback, the document-verification gate. A web front end that
decided for itself whether an answer was grounded, or that "helpfully" retried a
sub-question, would be a second implementation of the most safety-critical logic in the
two repos, kept correct by nobody.

Concretely, this app may: build argv, set `DRP_*`, launch, stop, parse the log, poll
Open Notebook, serve files. It may not: call the research or writer models on a run's
behalf, edit a run's results, or decide anything about grounding. The one model call it
makes is `app/suggest.py`, which produces text a person then edits.

## Why runs are units, not threads

halo-workbench runs a job on its own worker thread and reaps anything in flight when it
restarts. That is right for a 20-minute PDF conversion and wrong here: a run is 30
minutes to several hours, and `systemctl --user restart drp-web` — for a one-line
template fix — would cost all of it.

So a run is a transient `drp-web-run-<id>.service`, launched by `systemd-run`, logging
straight into its run directory. Consequences, all of them load-bearing:

- **Recovery reattaches, it does not reap.** `runs.reconcile()` asks, per run left
  `running`: is the unit alive? Then the log's exit sentinel. Then, if neither, it says
  `vanished` — a reboot, an OOM kill, or a `systemctl stop` from a terminal. Not
  `failed`: blaming the pipeline for the box going down sends the next reader to the
  wrong log.
- **The exit code lives in the log.** The unit is `--collect`ed, so systemd garbage-
  collects it seconds after it stops and cannot be asked afterwards. A one-line `bash`
  wrapper appends `[drp-web] exit=N`, which survives a reboot. Without `--collect` a
  failed run would sit in systemd's failed list needing `reset-failed`.
- **`--detach` is not used.** The pipeline's own detach flag makes a unit named
  `deep-research-podcast-<ts>` logging to `DRP_RESULTS_DIR` — a unit this app does not
  own, cannot stop by name, and would have to find by guessing.
- **Stopping is `systemctl stop`, i.e. SIGTERM.** The pipeline turns that into a
  `SystemExit` and releases the on-demand research model on the way out. Killing it
  instead strands a multi-GB model on a box with no swap.

## One run at a time, and one supervisor

The research phase starts `llama-research` — ~30B at a 131k context — on a box whose
resident llama-servers already hold most of the memory, and starting it has been
OOM-killed here. Two runs would also both start and stop that single on-demand unit,
and the second one's release would pull it out from under the first.

The queue enforces it, and a `flock` on the state directory enforces *one supervisor*:
two of them would each see a free slot at the same moment. `scripts/dev.sh` beside the
installed service is all it takes, so the refusal is loud and startup fails.

## Log parsing is a one-way coupling

`app/progress.py` reads `log()` calls in another repo. That is a real coupling and it
is deliberately one-way and lossy: a reworded line degrades the page to "running,
elapsed 41m" rather than breaking a run. Nothing there raises; nothing there decides
anything.

**The join is sequential, not positional.** Sub-questions and their results were once
joined by index, which is wrong precisely when it matters: a dropped sub-question logs
a warning and no result line, so every later question inherited its neighbour's sources
and the dropped one showed as researched. Found by `tests/driver.py`, which now checks
it against a log a real (fake) run produced. Walk the markers in log order and keep a
cursor.

**Dropped sub-questions are surfaced, never hidden.** The pipeline discards a
sub-question whose research read no page — an ungrounded but fluent answer must never
pass as research — and a run that shipped two of four questions must say so on its own
status page. Same for a `NO SYNTHESIS` answer and for questions excluded from the
episode.

## The change made to the pipeline (2026-08-29)

`run_research()` used `capture_output=True`, so the researcher's own per-turn lines
(`[turn 37] browser.open(...)`) existed nowhere until the sub-question ended, 15–40
minutes later. A healthy run and a wedged one were indistinguishable for that whole
time — the `hermes-skills` progress-monitoring notes are a workaround for exactly this
absence, and a status page cannot work around it at all. Its stderr now goes to a file
in `DRP_RESULTS_DIR` (this app points that at the run directory), which the run page
tails. `stdout` was not touched: it is the JSON interface between the two scripts, and
one stray line on it breaks the pipeline.

If that file is missing — an older pipeline, or an unwritable directory — the page falls
back to a process check for the researcher child, matched on the full argument vector
because `pgrep openresearcher-run.py` silently matches nothing (comm truncates at 15
characters).

## Smaller decisions worth not undoing

- **No Signal poller by default.** `DRP_POLLER_CMD` runs `poll_and_notify.sh`, which
  polls to completion — it keeps the pipeline process alive for the whole episode. This
  app polls the same endpoint from a thread and shows the answer in the browser, which
  is the point of having a browser. `DRPWEB_POLLER_CMD` turns the Signal handoff back on.
- **Audio is served with real range support.** An hour of narration is tens of
  megabytes, and iOS Safari will not scrub — in places will not play — against a server
  that answers a `Range` request with the whole file.
- **No fallback to "the newest mp3 under episodes/".** If the path Open Notebook
  recorded is not on disk, the page says so. Guessing is how one episode's audio gets
  served as another's, which is the same failure the poller script hit with container
  log lines that carry no job id.
- **A PDF upload is refused at the form.** The pipeline does not convert documents and
  says so; handing it a PDF produces an episode narrating binary. halo-workbench's
  marker tool is the converter on this box, and `DRPWEB_DOC_VERIFY_CMD` gates the
  upload if a verifier is configured.
- **Suggesting sub-questions and starting a run are two separate forms posting to two
  separate routes.** Drafting must never be one mistaken keypress away from committing
  three hours of GPU, and decomposition stays a human decision — the pipeline's own
  CLAUDE.md keeps it out of the pipeline for the same reason.
- **The turn budget is clamped (40–300), the question count is capped (8).** Below ~80
  turns a comparative or recency question finds good sources and runs out of budget
  before synthesising; the way to shorten a run is fewer questions, not fewer turns.

## Verification

`tests/acceptance.sh` — 52 checks, no GPU, no models, no Open Notebook, no network. It
runs `tests/driver.py` (log parsing, then real runs against `tests/fake-repo`, whose
stand-in pipeline emits the real log markers) and then exercises the HTTP surface on a
spare port. Two of its checks caught bugs on first run and stay because of it: the
sequential join, and the fact that the fake pipeline's mode must travel in argv — it
was read from the environment, which `runs._run_env` deliberately does not forward, so
two tests were passing against a run that was not doing what they claimed.

A green harness is not a verified change. The pipeline repo's rule applies here too:
**verification is a real run** — one sub-question at a low `--max-turns` through the
form exercises launch, env, the research backend's lifecycle, the research log, the
parser, the notebook, the episode trigger, the watcher and the audio route.

## Conventions

Match the sibling repos: non-obvious decisions are dated code comments naming the run
or measurement that motivated them, and anything user-facing is mirrored into
`README.md`. Values duplicated outside the repo live in `app/config.py` with their
other half named.
