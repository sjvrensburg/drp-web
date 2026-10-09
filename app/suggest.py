"""Draft sub-questions for a topic. Drafts only -- nothing here starts a run.

The pipeline deliberately does not decompose a topic: "sub-question decomposition is
the caller's job, not the pipeline's" (../local-podcast-studio/CLAUDE.md). Until now
the caller was an agent. A person at a form is a worse decomposer than an agent only
because they have no draft to react to, so this offers one -- and then makes them
approve it. The suggestions land in the textarea, editable, and the run does not start
until Start is pressed.

That matters more here than the usual "human in the loop" reasons: a bad split is not
discovered for three hours, and each sub-question is 15-40 minutes of GPU.

The writer endpoint (:8088, Ornith-1.5-35B-A3B), not the research model. This is
meta-work about a research question, which is exactly what OpenResearcher was measured
to be bad at: it answers the question instead of rewriting it, or emits <tool_call>
spam instead of prose.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

from app import config

log = logging.getLogger(__name__)

MAX_TOKENS = 900

PROMPT = """You are helping plan a deep-research podcast episode about this topic:

{topic}

Break it into {count} research sub-questions. Each one will be researched separately by \
a web-research agent that reads real pages, so each must be answerable from public \
sources and worth 15-40 minutes of searching on its own.

Rules:
- One sub-question per line. No numbering, no bullets, no preamble, no commentary.
- Each is a complete question, specific enough that two different researchers would \
look for the same kind of evidence.
- Together they should cover the topic without overlapping.
- Prefer questions that ask for specifics: numbers, dates, named systems, documented \
comparisons, stated positions.
- If recency matters, say so inside the question itself.

Write only the {count} lines."""

# Numbering, bullets and the occasional "Sub-question 3:" the model adds despite being
# told not to. Stripped rather than rejected: a good question wearing a bullet is still
# a good question, and the user is about to edit these anyway.
RE_LEAD = re.compile(r"^\s*(?:[-*•]|\d+[.)]|(?:sub-?)?question\s*\d*\s*[:.)])\s*", re.I)


class SuggestError(RuntimeError):
    """Something the form should show the user, not a traceback."""


def _strip_reasoning(content: str) -> str:
    """Drop a leading <think> block.

    The writer model is a reasoning model and its chat template can leave the block in
    `content`. Handling this wrong is how the pipeline once silently discarded real
    answers -- the same shape of bug, in a much less important place.
    """
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    return content.strip()


def suggest(topic: str, count: int = 4) -> list[str]:
    """Ask the writer model for `count` sub-questions. Raises SuggestError with a reason."""
    topic = topic.strip()
    if not topic:
        raise SuggestError("give me a topic first")
    count = max(2, min(count, config.MAX_QUESTIONS))

    body = json.dumps({
        "messages": [{"role": "user", "content": PROMPT.format(topic=topic, count=count)}],
        "temperature": 0.4,
        "max_tokens": MAX_TOKENS,
    }).encode()
    request = urllib.request.Request(
        config.SUGGEST_LLM_URL, data=body,
        headers={"content-type": "application/json"}, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=config.SUGGEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise SuggestError(f"the writer model answered HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SuggestError(
            f"could not reach the writer model at {config.SUGGEST_LLM_URL} ({exc}). "
            f"Write the sub-questions yourself — this is a convenience, not a dependency."
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise SuggestError("the writer model returned something that was not JSON") from exc

    try:
        content = payload["choices"][0]["message"].get("content") or ""
        # Some builds put the whole answer out of band and leave content empty.
        if not content.strip():
            content = payload["choices"][0]["message"].get("reasoning_content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise SuggestError("the writer model returned no message") from exc

    lines = []
    for raw in _strip_reasoning(content).splitlines():
        line = RE_LEAD.sub("", raw).strip().strip("*_ ")
        # A heading, a stray "Here are four sub-questions:", or a fragment. A research
        # sub-question is a sentence; anything shorter is not one.
        if len(line) < 20 or line.endswith(":"):
            continue
        lines.append(line)

    if not lines:
        raise SuggestError("the writer model did not produce anything usable — try again, "
                           "or write the sub-questions yourself")
    return lines[:count]
