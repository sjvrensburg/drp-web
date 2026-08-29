#!/usr/bin/env bash
# Acceptance checks. No GPU, no models, no Open Notebook, no network.
#
#   tests/acceptance.sh
#
# Points the app at a temporary state directory and at tests/fake-repo, whose
# deep-research-podcast.py emits the real pipeline's log markers and nothing else.
# Then starts the app on a spare port and exercises the HTTP surface.
set -uo pipefail

cd "$(dirname "$0")/.."
VENV="${VENV:-$HOME/venvs/drpweb}"
PORT="${TEST_PORT:-8195}"
STATE="$(mktemp -d /tmp/drp-web-test.XXXXXX)"
export DRPWEB_STATE_DIR="$STATE"
export DRPWEB_REPO="$PWD/tests/fake-repo"
export DRPWEB_PODCASTS_DIR="$STATE/podcasts"
trap 'rm -rf "$STATE"' EXIT

[ -x "$VENV/bin/python" ] || { echo "no venv at $VENV -- run scripts/bootstrap.sh" >&2; exit 2; }

echo "== module checks =="
"$VENV/bin/python" tests/driver.py || exit 1

echo
echo "== http surface =="
rm -rf "$STATE"/* 2>/dev/null
"$VENV/bin/uvicorn" app.main:app --host 127.0.0.1 --port "$PORT" >"$STATE/uvicorn.log" 2>&1 &
APP_PID=$!
trap 'kill $APP_PID 2>/dev/null; rm -rf "$STATE"' EXIT

for _ in $(seq 1 40); do
    curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
    sleep 0.25
done

fails=0
expect() {  # expect <label> <condition-output> <needle>
    if printf '%s' "$2" | grep -qF -- "$3"; then echo "  ok   $1"; else
        echo "  FAIL $1 (looking for '$3')"; fails=$((fails + 1)); fi
}

expect "healthz answers" "$(curl -s "http://127.0.0.1:$PORT/healthz")" '"status":"ok"'
expect "the form renders" "$(curl -s "http://127.0.0.1:$PORT/")" "Sub-questions"
expect "the runs page renders" "$(curl -s "http://127.0.0.1:$PORT/runs")" "Runs"

# Submitting a run: a form post, and a 303 so a reload cannot resubmit three hours of GPU.
location="$(curl -s -o /dev/null -w '%{redirect_url} %{http_code}' \
    -X POST "http://127.0.0.1:$PORT/runs" \
    --data-urlencode 'questions=Question one
Question two' \
    --data-urlencode 'episode_name=HTTP test' \
    --data-urlencode 'notebook_name=HTTP test' \
    --data-urlencode 'max_turns=120')"
expect "starting a run redirects (303)" "$location" "303"
run_url="${location%% *}"
run_id="${run_url##*/}"

for _ in $(seq 1 80); do
    page="$(curl -s "$run_url")"
    printf '%s' "$page" | grep -qF 'status done' && break
    sleep 0.5
done
expect "the run page reports done" "$page" "done"
expect "the run page shows what was read" "$page" "source(s) read"
expect "the log is downloadable" "$(curl -s "$run_url/log")" "research backend ready"
expect "the research JSON is downloadable" "$(curl -s "$run_url/results")" "question"

# A PDF must be refused: the pipeline does not convert documents, and an episode will
# narrate whatever it is handed.
pdf_code="$(printf '%%PDF-1.7 fake' > "$STATE/f.pdf"; curl -s -o /dev/null -w '%{http_code}' \
    -X POST "http://127.0.0.1:$PORT/runs" -F 'questions=q' -F 'episode_name=n' \
    -F "source_document=@$STATE/f.pdf")"
expect "a PDF upload is refused" "$pdf_code" "400"

# Range requests. An hour of narration is tens of megabytes and iOS Safari will not
# scrub against a server that ignores Range.
mkdir -p "$STATE/podcasts/episodes/fake/audio"
python3 -c "open('$STATE/podcasts/episodes/fake/audio/fake.mp3','wb').write(bytes(range(256))*64)"
python3 - "$STATE" "$run_id" <<'PY'
import json, sys, pathlib
state, run_id = sys.argv[1], sys.argv[2]
meta_path = pathlib.Path(state) / "runs" / run_id / "meta.json"
meta = json.loads(meta_path.read_text())
meta["episode"] = {"job_id": "command:fake", "status": "completed",
                   "audio_rel": "episodes/fake/audio/fake.mp3", "reachable": True}
meta_path.write_text(json.dumps(meta, indent=2))
PY
headers="$(curl -s -D - -o "$STATE/part.bin" -H 'Range: bytes=10-19' "$run_url/audio")"
expect "a range request answers 206" "$headers" "206"
expect "it names the range it served" "$headers" "content-range: bytes 10-19/16384"
expect "it serves exactly those bytes" "$(wc -c < "$STATE/part.bin")" "10"
expect "an unsatisfiable range is refused" \
    "$(curl -s -o /dev/null -w '%{http_code}' -H 'Range: bytes=99999-' "$run_url/audio")" "416"
expect "a whole-file request still works" \
    "$(curl -s -o /dev/null -w '%{http_code}' "$run_url/audio")" "200"

# Traversal, on both routes that take an id.
expect "a traversal id is refused" \
    "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/runs/..%2F..%2Fetc")" "404"

echo
if [ "$fails" -eq 0 ]; then echo "all http checks passed"; else echo "$fails http check(s) failed"; fi
exit "$fails"
