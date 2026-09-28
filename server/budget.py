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


def _rendered(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False)) - 2


def clip(text, n: int):
    """Free text shortened so it renders to at most n characters (a control
    character renders as six), marked with an ellipsis."""
    if not isinstance(text, str) or _rendered(text) <= n:
        return text
    t = text[:n - 1]
    while _rendered(t) + 1 > n:
        t = t[:len(t) - max(1, (_rendered(t) + 1 - n) // 6)]
    return t + "…"


def bounded(obj, n: int = 200, *, longer: dict | None = None):
    """Every string in `obj`, however deep, clipped to n characters (a key named in
    `longer` to its own length): what makes a listed item's size bounded by
    construction, whatever the store holds (issue #3, revision 2)."""
    longer = longer or {}
    if isinstance(obj, str):
        return clip(obj, n)
    if isinstance(obj, dict):
        return {k: bounded(v, longer.get(k, n), longer=longer) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [bounded(v, n, longer=longer) for v in obj]
    return obj


class Oversized(RuntimeError):
    """A listed item over the page budget on its own. A bug: every item is bounded by
    construction (the one stated way to reach it is ~1,000 candidates on a payment),
    so it is reported, never let through as an answer no agent can read."""


def page(items: list, limit: int, budget: int | None = None, ident=None) -> tuple[list, int]:
    """The leading items that fit both `limit` and `budget` (default PAGE_BUDGET; as
    rendered, with the separating commas), and how many were left out. An item over
    the budget on its own raises: a page never carries it."""
    budget = PAGE_BUDGET if budget is None else budget
    shown, used = [], 2
    for item in items:
        if len(shown) >= limit:
            break
        cost = size(item) + 1
        if cost + 2 > budget:
            name = ident(item) if ident else "an item"
            raise Oversized(f"{name} renders to {cost} characters, over one page's {budget}")
        if used + cost > budget:
            break
        shown.append(item)
        used += cost
    return shown, len(items) - len(shown)
