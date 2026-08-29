# drp-web

Deep-research podcasts from a browser, on the tailnet.

A front end for [`../deep-research-podcast`](../deep-research-podcast) — the pipeline
that drives OpenResearcher through a multi-turn web-research loop per sub-question and
turns the grounded results into an Open Notebook episode. That pipeline is a CLI meant
to be driven by an agent and left alone for hours. This is the other way to drive it:
type the sub-questions, press start, close the tab, and come back to a page that says
where the run is — and, when it is finished, plays the episode.

**It does not reimplement any part of the pipeline.** It composes the same command line
you would have typed, launches it in a systemd unit of its own, and reads its log.
Every guard in that script — the grounding gate, the three LLM roles, the anti-
degeneration fallbacks — exists because a real run went wrong, and a second
implementation of "is this answer grounded" would be a second thing to keep right.

## What the run page tells you

| | |
|---|---|
| **phase** | preflight → research → notebook → episode → done, from the pipeline's own log markers |
| **per sub-question** | researching / researched / **dropped**, with pages read, turns used, and whether it synthesised |
| **live turn** | `turn 37 — browser.open(...)`, so a 40-minute sub-question is visibly moving |
| **episode** | Open Notebook's job status, polled here rather than by a Signal poller, then the audio itself |

Dropped sub-questions are shown, never hidden. A sub-question whose research read no
page is discarded by the pipeline rather than narrated — an ungrounded but fluent
answer must never pass as research — and a run that shipped two of four questions
should say so on its own status page.

## Run it (development)

```bash
scripts/bootstrap.sh      # create ~/venvs/drpweb
scripts/dev.sh            # uvicorn on :8095 with --reload
curl -s localhost:8095/healthz
tests/acceptance.sh       # 52 checks: no GPU, no models, no Open Notebook, no network
```

The acceptance tests run against `tests/fake-repo`, a stand-in pipeline that emits the
real one's log markers. They cover the log parser (including the case that broke it —
a dropped sub-question shifting every later one), the single-run slot, stop, failure,
the state-directory lock, and HTTP range requests on the audio route.

## Run it (resident) — installed 2026-08-29

```bash
cp config/drp-web.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now drp-web
journalctl --user -u drp-web -f
```

Remote access needs one more command from the operator, whose config lives in
`tailscaled` rather than in this repo — nothing here regenerates it and nothing here
will tell you it is missing:

```bash
tailscale serve --bg --http=8095 http://127.0.0.1:8095
```

Then `http://evo-x3:8095` from any machine on the tailnet — the name, never the `100.x`
address, which returns tailscaled's own 404.

## What it assumes is already there

- `~/Projects/deep-research-podcast` (`DRPWEB_REPO`), run under the system `python3`;
- Open Notebook on `:5055`, with the episode/speaker profiles the form lists;
- the writer model on `:8088` — it enriches questions, writes the reports, and drafts
  sub-question suggestions;
- SearXNG on `:8888`;
- `llama-research` on `:8085` as an **on-demand** unit. The pipeline starts and stops it
  around the research phase, on every exit path including SIGTERM. This app never
  touches it: two owners of one multi-GB on-demand model is how it gets stranded.

One run at a time, and that is not a setting. The research model is ~30B at a 131k
context on a box whose resident servers already hold most of the memory; two runs would
also fight over starting and stopping that one unit.

## A run outlives this app

Each run is a transient `drp-web-run-<id>.service`, logging into its own directory
under `~/.local/state/drp-web/runs/`. So `systemctl --user restart drp-web` does not
cost a three-hour run — on restart the app reconciles what it finds rather than reaping
it. The run directory holds the log, the pipeline's own persisted research JSON, and
any uploaded source document; it is kept for 90 days.

## Related

- `../deep-research-podcast` — the pipeline, its README, and the bugs that shaped it.
- `../halo-workbench` — the sibling app on `:8093`. Same skeleton, same design rules;
  its **marker** tool is where a PDF becomes the markdown this app will accept as a
  source document.
- `../halo-prep` — the runbook for the box: model units, ports, pitfalls.
