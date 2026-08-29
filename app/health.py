"""What the form needs to tell you before you commit three hours to a run.

Not a supervisor's health check -- `/healthz` is that, and deliberately touches
nothing. This is the pre-flight a person wants: the four services this run will use,
and whether the box has room to start a ~30B research model beside its resident ones.

Every check answers "reachable" or "not reachable" and nothing stronger. None of them
blocks a run: the operator knows things this app does not, and a form that refuses on
a failed probe is worse than one that says what it saw.
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from app import config


def _reachable(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except urllib.error.HTTPError:
        # An HTTP error is still an answer: something is listening and speaking HTTP.
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def mem_available_gib() -> float | None:
    """MemAvailable from /proc/meminfo, in GiB.

    An imperfect proxy and knowingly so: what actually OOM-kills a run on this box is
    GTT pressure from the resident llama-servers, which this number does not see. It
    is here because "3 GiB free" is still worth knowing before starting a 30B model.
    """
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None
    return None


def unit_state(unit: str) -> str:
    try:
        return subprocess.run(["systemctl", "--user", "is-active", unit],
                              capture_output=True, text=True, timeout=5,
                              check=False).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def snapshot() -> dict[str, Any]:
    """Everything the form's status strip shows."""
    memory = mem_available_gib()
    return {
        "open_notebook": _reachable(f"{config.ON_API}/episode-profiles"),
        "writer": _reachable(config.SYNTH_LLM_URL.replace("/v1/chat/completions", "/health")),
        "searxng": _reachable(config.SEARXNG_URL.split("/search")[0]),
        # Expected to be inactive: it is on-demand, and the PIPELINE starts and stops
        # it around the research phase. "inactive" here is health, not a problem.
        "research_unit": unit_state(config.RESEARCH_UNIT),
        "memory_gib": memory,
        "memory_low": memory is not None and memory < config.MEMORY_WARN_GIB,
        "pipeline_present": config.DRP_SCRIPT.is_file(),
    }
