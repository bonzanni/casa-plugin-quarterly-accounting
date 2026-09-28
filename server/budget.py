# server/budget.py
"""How much one tool answer may hold (issue #3). Claude Code hands an agent at
most 25,000 tokens of an MCP answer (10,000 draws a warning); above that it
saves the answer to a file the agent may not be able to read. So every answer
that lists records is bounded by a character budget, whatever the records hold:
lists are paged by what they will render to, not only by a count."""
from __future__ import annotations

import json

RESULT_LIMIT = 20_000      # a whole answer: about 8 K tokens of this JSON
PAGE_BUDGET = 16_000       # one page's items, leaving room for the answer's own fields


def render(out) -> str:
    """The one rendering of a tool answer: compact JSON."""
    if isinstance(out, str):
        return out
    return json.dumps(out, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def size(out) -> int:
    return len(render(out))


def clip(text, n: int):
    """Free text shortened to at most n characters, marked with an ellipsis."""
    if not isinstance(text, str) or len(text) <= n:
        return text
    return text[:n - 1] + "…"


def page(items: list, limit: int, budget: int = PAGE_BUDGET) -> tuple[list, int]:
    """The leading items that fit both `limit` and `budget` (as rendered, with the
    separating commas), and how many were left out. At least one item, so a page
    always makes progress."""
    shown, used = [], 2
    for item in items:
        if len(shown) >= limit:
            break
        cost = size(item) + 1
        if shown and used + cost > budget:
            break
        shown.append(item)
        used += cost
    return shown, len(items) - len(shown)
