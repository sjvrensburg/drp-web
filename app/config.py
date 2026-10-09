"""Every value this app shares with something outside itself.

Same rule as ../halo-workbench/app/config.py: anything here is duplicated in a
systemd unit, another repo, a Docker container or `tailscaled`, and nothing
validates the duplication -- so each one names its other half.

The largest duplication is the block of `DRP_*` variables below. They are the
pipeline's interface (../local-podcast-studio/CLAUDE.md, "Running") and this app
is now a THIRD caller of it, beside the Hermes skill and the operator's shell.
Neither of the other two reads this file.
"""

import os
from pathlib import Path

VERSION = "0.1.0"

# Port. Duplicated in: config/drp-web.service, scripts/dev.sh, README, and -- invisibly,
# outside this repo -- TWO registrations in tailscaled: `tailscale serve --http=8095`
# (http://evo-x3:8095) and the Tailscale Service `svc:drp-web` (http://drp-web/), which
# proxies port 80 here. Nothing here will tell you either is stale; remote clients just
# stop working. 8093 is halo-workbench, 8094 is llama-hymt2, so this took the next free one.
PORT = 8095

# ── The pipeline ─────────────────────────────────────────────────────────────
# Called as a subprocess, never imported. That is not a style choice: the pipeline
# repo's convention is "stdlib only -- do not add a dependency", and importing it
# from a FastAPI app would make FastAPI its dependency by the back door. It also
# calls main() at module scope, so importing it RUNS it.
DRP_REPO = Path(os.environ.get("DRPWEB_REPO", Path.home() / "Projects/local-podcast-studio"))
DRP_SCRIPT = DRP_REPO / "deep-research-podcast.py"

# Run state. Directories, not a database -- see CLAUDE.md "Runs are directories".
# A run directory outlives this process by design: the pipeline runs in a transient
# systemd unit of its own, so restarting this app must not cost a 3-hour run.
STATE_DIR = Path(os.environ.get("DRPWEB_STATE_DIR", Path.home() / ".local/state/drp-web"))
RUNS_DIR = STATE_DIR / "runs"

# A run directory holds the research JSON the pipeline persisted, the full log, and
# any uploaded source document. The research is worth keeping and is small; the
# uploaded document is the sensitive half, and is what this window is really about.
RUN_RETENTION_DAYS = int(os.environ.get("DRPWEB_RETENTION_DAYS", 90))

# systemd unit name for a run. Deliberately NOT the pipeline's own
# `deep-research-podcast-<ts>` prefix: `--detach` uses that, and a run launched from
# the shell must never be mistaken for one this app owns (or reaped as one).
UNIT_PREFIX = "drp-web-run-"

# ── What the pipeline talks to ───────────────────────────────────────────────
# Duplicated in: ../local-podcast-studio/pi-skills/deep-research-podcast/scripts/launch.sh, which
# is the other caller of the same script and must stay in agreement with this.
#
# THREE LLM ROLES, and only one is the research model (pipeline CLAUDE.md). :8085 is
# OpenResearcher-30B-A3B and researches; :8088 is Ornith-1.5-35B-A3B and both
# enriches the question and writes the report. Pointing enrich/synth at :8085 is
# measurably wrong -- it answers questions it was asked to rewrite and emits
# <tool_call> spam instead of prose. Do not "simplify" these to one endpoint.
LLM_URL = os.environ.get("DRP_LLM_URL", "http://127.0.0.1:8085/v1/chat/completions")
LLM_HEALTH_URL = os.environ.get("DRP_LLM_HEALTH_URL", "http://127.0.0.1:8085/health")
ENRICH_LLM_URL = os.environ.get("DRP_ENRICH_LLM_URL", "http://127.0.0.1:8088/v1/chat/completions")
SYNTH_LLM_URL = os.environ.get("DRP_SYNTH_LLM_URL", "http://127.0.0.1:8088/v1/chat/completions")
SEARXNG_URL = os.environ.get("DRP_SEARXNG_URL", "http://127.0.0.1:8888/search")
ON_API = os.environ.get("DRP_OPEN_NOTEBOOK_API", "http://127.0.0.1:5055/api")

# :8085 is an ON-DEMAND unit (`llama-research`, --ctx-size 131072) and the box is near
# its memory ceiling with the resident servers up -- starting it alongside them has
# been OOM-killed. The pipeline runs these itself, around the research phase only, on
# every exit path including SIGTERM. This app must not start or stop it separately:
# two owners of one on-demand multi-GB unit is how it gets stranded.
BACKEND_START_CMD = os.environ.get("DRP_BACKEND_START_CMD", "systemctl --user start llama-research")
BACKEND_STOP_CMD = os.environ.get("DRP_BACKEND_STOP_CMD", "systemctl --user stop llama-research")
RESEARCH_UNIT = "llama-research"

# Notification handoff. UNSET BY DEFAULT HERE, unlike the Hermes skill, and that is a
# decision rather than an omission: `poll_and_notify.sh` polls Open Notebook to
# completion, so setting it keeps the pipeline process alive for the whole episode
# generation. This app polls the same endpoint itself (app/episode.py) and shows the
# result in the browser, which is the point of having a web interface. Set
# DRPWEB_POLLER_CMD if you also want the Signal message.
POLLER_CMD = os.environ.get("DRPWEB_POLLER_CMD", "")
DELIVER_TARGET = os.environ.get("DRPWEB_DELIVER_TARGET", "signal")

# Gate for --source-document. Fails CLOSED in the pipeline once set, on purpose: three
# document-substitution incidents in two days (pipeline CLAUDE.md,
# verify_source_document). Unset by default because this box's converter and its
# verifier live in halo-prep, not here; set it and uploads are verified before the
# episode can narrate them.
DOC_VERIFY_CMD = os.environ.get("DRPWEB_DOC_VERIFY_CMD", "")

# ── Episode output ───────────────────────────────────────────────────────────
# Open Notebook is a container; this is the host side of its `-> /app/data/podcasts`
# bind mount (`docker inspect open-notebook`). The job result's `audio_file_path` is
# relative to it. Final episode audio lands at episodes/<uuid>/audio/<uuid>.mp3.
PODCASTS_DIR = Path(os.environ.get("DRPWEB_PODCASTS_DIR", Path.home() / "Music/open-notebook-podcasts"))
ON_UI = os.environ.get("DRPWEB_ON_UI", "http://127.0.0.1:8502")

# ── Defaults for the form ────────────────────────────────────────────────────
# Profile names, not ids: POST /api/podcasts/generate takes either, and the names are
# what the skill file and the README use. The list itself is read live from
# GET /api/episode-profiles (NOT /api/podcasts/episode-profiles -- that 404s).
EPISODE_PROFILE = os.environ.get("DRP_EPISODE_PROFILE", "deep_dive")
SPEAKER_PROFILE = os.environ.get("DRP_SPEAKER_PROFILE", "tech_experts")

# 120 is the pipeline's own default and should not go below ~80 for comparative or
# recency questions -- a real run at 50 found good sources and never synthesised.
# Trim question COUNT for a shorter run, not the turn budget.
DEFAULT_MAX_TURNS = int(os.environ.get("DRPWEB_MAX_TURNS", 120))
MAX_QUESTIONS = 8

# Uploads are read into memory before being written into the run directory. A
# converted paper is well under this; the ceiling is here so a mistaken upload
# cannot fill a box with no swap.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# The suggest-sub-questions helper writes nothing and runs nothing: it drafts, you
# edit, you press Start. Same endpoint as the pipeline's writer role, because this is
# prose work and the research model is measurably bad at meta-work about a question.
SUGGEST_LLM_URL = os.environ.get("DRPWEB_SUGGEST_LLM_URL", ENRICH_LLM_URL)
SUGGEST_TIMEOUT = int(os.environ.get("DRPWEB_SUGGEST_TIMEOUT", 180))

# How much free memory to want before starting a run. The research model is ~30B at a
# 131k context on a box whose resident servers already hold most of the GPU/GTT pool,
# and it HAS been OOM-killed here. This is a warning on the form, never a refusal:
# the operator knows what else is running, and MemAvailable does not see the GTT
# pressure that actually kills it.
MEMORY_WARN_GIB = float(os.environ.get("DRPWEB_MEMORY_WARN_GIB", 24))
