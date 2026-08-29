"""The Jinja environment and the filters the pages need.

Server-rendered HTML, no build step and no npm: this is one form and a status page.
The status page refreshes with a `<meta http-equiv=refresh>` rather than JavaScript,
for the same reason halo-workbench does -- a run is watched for hours from a tab that
may be restored long after it was opened, and a meta refresh survives that.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def duration(seconds: float | None) -> str:
    """`2h 41m`. Coarse on purpose: read to answer "is it still moving", not to time it."""
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def filesize(num: int | None) -> str:
    if num is None:
        return "—"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def ago(stamp: float | str | None) -> str:
    """Relative time, for "checked 40s ago" -- an absolute clock answers a question nobody asked."""
    if stamp is None:
        return "—"
    try:
        when = (datetime.fromtimestamp(stamp, timezone.utc) if isinstance(stamp, (int, float))
                else datetime.fromisoformat(stamp))
    except (ValueError, OSError, OverflowError):
        return "—"
    delta = (datetime.now(timezone.utc) - when).total_seconds()
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


templates.env.filters["duration"] = duration
templates.env.filters["filesize"] = filesize
templates.env.filters["ago"] = ago
