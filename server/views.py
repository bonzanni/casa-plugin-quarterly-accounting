# server/views.py
"""The views (spec §"Pull only", §"What the operator can ask for"). The server
renders; Ellen relays the text verbatim. Every count, sum, date and ordering
is computed here.

Membership is fixed first: every managed, un-ended lineage from the
watermark through the end of the view's quarter, by effective date, whatever
its expectation or state. Only then does a view filter and cap what it
prints. The two coverage dates are printed together or not at all.

Rendering is not showing. build_review persists an UNSHOWN rendering;
mark_rendering_delivered, called after the send succeeded, promotes it and
advances the shown-revision pointers that corrections bind to."""
from __future__ import annotations

import json
import textwrap

import amounts
import binding
import dates
import db
import lineage
import work

WIDTH = 64
CAP = 8
TELEGRAM_LIMIT = 4096
VIEWS = ("status", "missing", "check", "rest", "older", "all", "item", "quarter")
KIND_WORD = {"invoice": "invoice", "sales-invoice": "sales invoice", "credit-note": "credit note",
             "payslip": "payslip", "statement": "statement", "receipt": "receipt",
             "other": "document"}
# Machinery the operator never has to learn (spec §"The reversibility ladder").
FORBIDDEN = ("proposed", "conflicted", "revision", "projection", "CAS", "no-ref",
             "partial-search", "recipient?", "acct::", "pid", "match_id",
             "render_id")


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _wrap(line: str) -> list:
    if len(line) <= WIDTH:
        return [line]
    out, cur = [], ""
    for part in line.split(" · "):
        cand = part if not cur else cur + " · " + part
        if len(cand) <= WIDTH:
            cur = cand
            continue
        if cur:
            out.append(cur)
        pieces = textwrap.wrap(part, WIDTH, break_long_words=False, break_on_hyphens=False) or [""]
        out.extend(pieces[:-1])
        cur = pieces[-1]
    if cur:
        out.append(cur)
    return out


def _day(d):
    return dates.short_day(d) if d else "no date"


def _money(d) -> str:
    return amounts.fmt(d["amount_minor"], d["currency"]) if d.get("amount_minor") is not None else "?"


def headline(d: dict, view_quarter=None) -> str:
    parts = [d["counterparty"], _money(d), _day(d["date"])]
    kind = d["expectation"]["kind"]
    if kind and kind not in ("invoice", "none"):
        parts.append(KIND_WORD[kind])
    if d.get("pending"):
        parts.append("pending")
    if view_quarter and d.get("quarter") and d["quarter"] != view_quarter:
        parts.append(dates.quarter_label(d["quarter"]))
    return " · ".join(parts)


def _a(word: str) -> str:
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


def _docname(doc: dict) -> str:
    w = KIND_WORD.get(doc["kind"], "document")
    return f"{w} {doc['number']}" if doc.get("number") else w


def evidence(d: dict) -> list:
    out = []
    cur = d["current"]
    if cur is not None:
        doc = cur["document"]
        name = _docname(doc)
        if "facts-changed" in d["reasons"]:
            out.append("The bank changed this payment after it was paired — still right?")
        if "kind-mismatch" in d["reasons"]:
            need = KIND_WORD.get(d["expectation"]["kind"] or "", "different document")
            out.append(f"Paired with {_a(KIND_WORD.get(doc['kind'], 'document'))}, but this "
                       f"payment now needs {_a(need)}.")
        elif "kind-changed" in d["reasons"]:
            out.append(f"Its category changed since it was paired — still {name}?")
        labels = cur["labels"]
        if "guessed" not in labels:
            # a line that asks for a verdict names what it is asking about (round p7:
            # a no-ref line never named its invoice, yet "all good" confirmed it)
            out.insert(0, f"Paired with {name} ({_day(doc['date'])}).")
        if "guessed" in labels:
            others = "; ".join(cur["runners_up"])
            out.append(f"Picked {name} ({_day(doc['date'])}); {others} also fits." if others
                       else f"Picked {name} among several that fit.")
        if "no-ref" in labels:
            out.append("Repeating equal charges, and no invoice number on both sides.")
        if "partial-search" in labels:
            out.append("The search was cut short, so this may not be the only fit.")
        if "recipient?" in labels:
            out.append(f"{name[0].upper() + name[1:]} names "
                       f"{doc.get('recipient') or 'someone else'}, not the business.")
        if d["status"] == "proposed" and len(out) == 1:
            out.append("Not sure — say if it's wrong.")
    if d["candidates"]:
        out.append("Could be: " + ", ".join(f"{_docname(c['document'])} "
                                            f"({_day(c['document']['date'])})"
                                            for c in d["candidates"]) + ".")
    return out


def _tracked(d):
    # the reducer emits `unclassified`/`conflicted` even for lineages it then
    # reports ended or ineligible: those reasons are not the operator's to act on
    return d["status"] not in ("ended", "ineligible")


def _open_required(d):
    return _tracked(d) and d["status"] == "open" and d["expectation"]["kind"] is not None


def _searched(d):
    """Looked for, or no longer to be looked for: an item the operator stopped
    chasing (or a merge carried `accepted-missing` onto) is never searched
    again, and stays "still open, still listed" as missing (spec §"What a pass
    works on"), never "the next pass looks"."""
    return (bool(d["search"].get("last_searched_at")) or d["identity_question"]
            or d["search_state"] != "active")


def _is_missing(d):
    """Missing means searched and not found (spec §Weekly pass: four states stay
    distinct). A required item nobody has looked for yet is `not searched` or
    `not checked`, never `missing`."""
    return _open_required(d) and _searched(d)


def _is_unsearched(d):
    return _open_required(d) and not _searched(d)


def _is_unclassified(d):
    return (_tracked(d) and d["expectation"]["kind"] is None
            and "classification-conflict" not in d["reasons"])


def _is_conflict(d):
    return _tracked(d) and "classification-conflict" in d["reasons"]


def _needs_check(d):
    if not _tracked(d):
        return False
    if d["candidates"] or d["status"] == "proposed":
        return True
    return d["status"] == "matched" and d["current"] is not None and d["current"]["labels"] != ["clean"]


def _missing_detail(d) -> list:
    if d["identity_question"]:
        return ["Who was this payment to?"]
    out = []
    if not d["search"].get("last_searched_at"):
        if d["search_state"] == "active":
            out.append("Not searched yet.")
    elif d["search"].get("incomplete"):
        out.append("Search incomplete — resumes next pass.")
    if d["link"]:
        out.append(d["link"])
    if d["search_state"] == "accepted-missing":
        out.append("No longer chased.")
    return out


def membership(conn, view: str, quarter: str, pid=None) -> list:
    if view == "item":
        return [lineage.resolve_pid(conn, pid)]
    b = binding.get(conn)
    if b is None:
        return []
    end = dates.quarter_bounds(quarter)[1]
    out = []
    for p in lineage.live_pids(conn):
        proj = lineage.projection(conn, p)
        if proj["ended"]:
            continue
        row = lineage.live_row(conn, proj)
        eff = dates.effective_date(row) if row else None
        if eff is not None and b["watermark"] <= eff < end:
            out.append(p)
    return out


def coverage(conn, members) -> str:
    if not members:
        return "No transactions yet."
    snap = conn.execute("SELECT bank_through FROM snapshots ORDER BY snapshot_id DESC"
                        " LIMIT 1").fetchone()
    if snap is None or snap["bank_through"] is None:
        return "Not checked yet."
    obs = [lineage.projection(conn, p)["class_observed_at"] for p in members]
    seen = [o for o in obs if o]
    never = len(obs) - len(seen)
    cls = (f"classification through {dates.short_day(min(seen))}" if seen
           else "classification not checked yet")
    line = f"Bank checked through {dates.short_day(snap['bank_through'])} · {cls}"
    if never and seen:
        line += f" · {never} never checked"
    return line


def _lead(conn):
    """(stop lines or None, gmail unavailable, the interrupted pass's report or None)."""
    setup = binding.check_setup(conn)
    if not setup["can_run"]:
        return [setup["header"], *setup["conditions"], "Nothing else to do until then."], False, None
    gmail = setup["probes"].get("gmail")
    gmail_down = gmail is not None and not gmail["ok"]
    last = conn.execute("SELECT * FROM passes WHERE ended_at IS NOT NULL ORDER BY ended_at DESC,"
                        " generation DESC LIMIT 1").fetchone()
    interrupted = None
    if last is not None and last["outcome"] == "interrupted":
        rep = json.loads(last["report_json"] or "{}")
        interrupted = (int(rep.get("checked", 0)), int(rep.get("total", 0)))
    return None, gmail_down, interrupted


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _degraded_block(gmail_down, interrupted, missing, unsearched) -> list:
    """A degraded pass leads with its own condition, ahead of any accounting
    result, and keeps `missing` apart from `not searched` / `not checked`
    (spec §Weekly pass, "A broken pass must not read as deficient books")."""
    out = []
    if gmail_down:
        out.append("Review incomplete - Gmail unavailable.")
        if missing:
            kinds = {d["expectation"]["kind"] for d in missing}
            word = KIND_WORD.get(kinds.pop(), "document") if len(kinds) == 1 else "document"
            out.append(f"{_plural(len(missing), word)} already missing.")
        if unsearched:
            out.append(f"{_plural(len(unsearched), 'new payment')} not searched.")
        out.append("No reply needed; I'll retry next pass.")
    if interrupted is not None:
        checked, total = interrupted
        out += ["Review interrupted.", f"{checked} of {total} new payments checked.",
                f"{max(total - checked, 0)} not checked yet. Saved."]
    return out


class _Block:
    """One printed item: its lines, and what printing it binds on delivery."""
    __slots__ = ("lines", "pid", "pairings", "residue", "amount", "order", "name", "guessed",
                 "offer")

    def __init__(self, lines, *, pid=None, pairings=(), residue=None, amount=0, order=("", 0),
                 name=None, guessed=False, offer=None):
        self.lines, self.pid, self.pairings, self.residue = list(lines), pid, set(pairings), residue
        self.offer = offer              # a package offered for "send it again"
        self.amount, self.order, self.name, self.guessed = amount or 0, order, name, guessed


class _Section:
    def __init__(self, title, blocks, *, gap=False, empty=None):
        self.title, self.blocks, self.gap, self.empty = title, blocks, gap, empty


def _residue_blocks(conn) -> tuple:
    """Unshown residue as blocks, and the ids of residue rows that print
    nothing (no amount known, a merge, a per-pairing retirement the `ended`
    line already tells). Those are marked shown with the rendering that
    carries the residue, so they cannot accumulate unseen."""
    blocks, silent = [], []
    for r in conn.execute("SELECT * FROM residue WHERE shown_render IS NULL ORDER BY id"):
        d = work.describe(conn, r["pid"]) if r["pid"] else None
        if d is None or d.get("amount_minor") is None:
            silent.append(r["id"])
            continue
        head = headline(d)
        if r["reason"] == "ended":
            freed = conn.execute("SELECT COUNT(*) FROM log WHERE pid=? AND kind='retire' AND"
                                 " cause='row-ended'", (r["pid"],)).fetchone()[0]
            tail = "its document is free again" if freed else "nothing was paired to it"
            line = f"{head} left the bank ledger ({r['detail']}) — {tail}"
        elif r["reason"] == "occupied":
            line = f"{head} — that document is already on another payment"
        elif r["reason"] == "kind-mismatch":
            line = f"{head} — its category changed; its document no longer fits"
        elif r["reason"] == "exempt-doc":
            line = f"{head} — a document turned up for a payment you said needs none"
        elif r["reason"] == "broken-floor":
            line = f"{head} — bank-feed's history for it is broken; left as it was"
        else:
            silent.append(r["id"])
            continue
        blocks.append(_Block([line], residue=r["id"], amount=abs(d["amount_minor"]),
                             order=(d["date"] or "", r["id"])))
    return blocks, silent


def _shown_pairings(d) -> set:
    ids = {c["match_id"] for c in d["candidates"]}
    if d["current"] is not None:
        ids.add(d["current"]["match_id"])
    return ids


def _item_blocks(ds, detail, q, shows_pairings=False, guessed=False) -> list:
    """`pairings` are the match ids whose proposition the text displays. Only
    those are bound for a later correction (round p1, Astra S1: a missing view
    that bound candidates it never showed let "Adobe is wrong" reject them)."""
    return [_Block([headline(d, q), *detail(d)], pid=d["pid"],
                   pairings=_shown_pairings(d) if shows_pairings else (),
                   amount=abs(d["amount_minor"] or 0), order=(d["date"] or "", d["pid"]),
                   name=d["counterparty"], guessed=guessed)
            for d in ds]


def _first_review(conn) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM renders WHERE delivered_at IS NOT NULL AND kind IN ('status','all')"
        " AND coalesce(json_extract(scope_json, '$.stop'), 0) = 0").fetchone()[0] == 0


def _compose(conn, view, q, items, members, lead):
    """The parts of a view, before any capping or paging: a head printed on
    every page, the announcement printed once, the sections, and a tail built
    from what was actually printed."""
    stop, gmail_down, interrupted = lead
    cur = [d for d in items if d["quarter"] == q]
    older_missing = [d for d in items if d["quarter"] and d["quarter"] < q and _is_missing(d)]
    missing = [d for d in cur if _is_missing(d)]
    unsearched = [d for d in items if _is_unsearched(d)]
    guessed = [d for d in items if _needs_check(d)]
    nice = [d for d in cur if _tracked(d) and d["status"] == "optional"]
    uncl = [d for d in cur if _is_unclassified(d)]
    conflicts = [d for d in cur if _is_conflict(d)]
    matched_clean = [d for d in cur if d["status"] == "matched" and not _needs_check(d)]
    b = binding.get(conn)
    parts = {"view": view, "head": [], "announce": [], "sections": [], "silent": [],
             "tail": None}
    if view == "item":
        d = items[0]
        lines = [headline(d), _item_sentence(d), *evidence(d)]
        if _open_required(d):
            lines += _missing_detail(d)
        parts["sections"].append(_Section(None, [_Block(lines, pid=d["pid"],
                                                        pairings=_shown_pairings(d))]))
        parts["tail"] = lambda printed_guessed: []
        return parts

    titles = {"status": f"Accounting · {dates.quarter_label(q)}",
              "all": f"Accounting · {dates.quarter_label(q)}",
              "missing": f"Missing · {dates.quarter_label(q)}",
              "check": f"To check · {dates.quarter_label(q)}",
              "rest": f"Nice to have · {dates.quarter_label(q)}",
              "older": "Older, still missing",
              "quarter": f"Accounting · {dates.quarter_label(q)}"}
    # the same quarter figure the coverage line prints; older ones have their own line
    parts["head"] = _degraded_block(gmail_down, interrupted, missing, unsearched)
    if parts["head"]:
        parts["head"].append("")
    parts["head"].append(titles[view])
    cov = coverage(conn, members)
    if view in ("status", "all", "quarter") and members:
        cov += f" · {_plural(len(cur), 'transaction')}, {len(missing)} missing a document."
    if view in ("status", "all") and _first_review(conn):
        # "Same sheet, preceded by `First review · bank checked through 20 Sep`"
        cov = "First review · " + cov[0].lower() + cov[1:]
    parts["head"].append(cov)
    if b is not None and not b["watermark_announced"] and view in ("status", "all", "missing"):
        start_q = dates.quarter_of(b["watermark"])
        n = dates.parse_quarter(start_q)[1]
        before = f"Q{n - 1}" if n > 1 else "Q4"
        parts["announce"].append(f"Starting from {dates.quarter_label(start_q)} — say \"start "
                                 f"from {before}\" to go further back")
    secs = parts["sections"]
    if view in ("status", "all"):
        res, parts["silent"] = _residue_blocks(conn)
        secs.append(_Section("", res))
        # a send that may not have arrived is offered, never resent by itself
        # (spec §Packaging, "Delivery"); a block like any other, so the cap,
        # the paging and the final fit all apply to it. The rendering records
        # the packages it printed: "send it again" resolves against those only
        # (delivery.resend_target).
        import delivery
        secs.append(_Section("", [_Block([f"{fname} may not have arrived —",
                                          'say "send it again".'], order=("", pkg_id),
                                         offer=pkg_id)
                                  for pkg_id, fname in delivery.uncertain(conn, q)]))
    if view in ("status", "all", "missing", "quarter"):
        secs.append(_Section("MISSING", _item_blocks(missing, _missing_detail, q), gap=True))
    if view in ("status", "all"):
        secs.append(_Section("WHAT IS THIS?", _item_blocks(
            conflicts, lambda d: ["The categories on it disagree — which is it?"], q)))
        secs.append(_Section("I GUESSED THESE", _item_blocks(guessed, evidence, q, True, True)))
    if view == "check":
        secs.append(_Section("", _item_blocks(guessed, evidence, q, True, True),
                             empty="Nothing to check."))
    if view == "rest":
        secs.append(_Section("", _item_blocks(nice, lambda d: [], q),
                             empty="Nothing else is missing."))
    if view == "older":
        secs.append(_Section("", _item_blocks(older_missing, _missing_detail, q),
                             empty="Nothing older is missing."))

    packages = []
    if view == "quarter":
        for pk in conn.execute("SELECT p.filename, d.settled_at FROM packages p JOIN deliveries d"
                               " ON d.package_id=p.package_id WHERE p.quarter=? AND"
                               " d.status='delivered' ORDER BY d.settled_at", (q,)):
            packages.append(f"Sent {pk['filename']} on {_day(pk['settled_at'])}.")
        packages.append(f'Say "rebuild {dates.quarter_label(q).split()[0]}" for a fresh package.')

    def tail(printed_guessed):
        out = list(packages)
        if view in ("status", "all", "missing"):
            counts = []
            if uncl:
                counts.append(f"{len(uncl)} not yet classified — the categories aren't in yet.")
            # one line, one source: an interrupted pass's lead already says how
            # many it did not reach, from the run record
            if unsearched and not gmail_down and interrupted is None:
                counts.append(f"{_plural(len(unsearched), 'new payment')} not checked yet"
                              " — the next pass looks.")
            if counts:
                out += ["", *counts]
            if nice:
                out.append(f'+{len(nice)} nice-to-have — say "show the rest"')
            if older_missing:
                qs = sorted({dates.quarter_label(d["quarter"]).split()[0] for d in older_missing})
                out.append(f'+{len(older_missing)} older still missing ({", ".join(qs)}) — '
                           'say "show older"')
        if view in ("status", "all"):
            if printed_guessed or matched_clean:
                out.append("")
            if matched_clean:
                out.append("Everything else matched cleanly." if (guessed or missing)
                           else "Everything matched cleanly.")
            if printed_guessed:
                # the example names an item this very text shows
                out.append(f'Tell me if one is wrong — "the {printed_guessed[0]} one is wrong".')
        if view in ("status", "all", "missing") and missing:
            out.append("Download the PDFs and email them to yourself, then")
            out.append('say "check emailed invoices" to file them now.')
        return out
    parts["tail"] = tail
    return parts


def _text(lines) -> str:
    return "\n".join(w for line in lines for w in (_wrap(line) if line else [""]))


def _emit(parts, picks, *, announce, more=None, cap=None, all_sections_empty_msgs=True):
    """Lines for the chosen blocks. `picks` maps a section index to the blocks
    to print (in order); `cap`, when set, adds the `+N more` line to every
    section with more blocks than it printed; `more` is a continuation line."""
    out = list(parts["head"]) + (list(parts["announce"]) if announce else [])
    chosen, printed_guessed = [], []
    for i, sec in enumerate(parts["sections"]):
        blocks = picks.get(i, [])
        if not sec.blocks:
            if sec.empty and all_sections_empty_msgs:
                out.append(sec.empty)
            continue
        if not blocks and cap is None:
            continue
        if sec.title is not None:
            out.append("")
            if sec.title:
                out.append(sec.title)
        for j, blk in enumerate(blocks):
            if j and sec.gap:
                out.append("")
            out.extend(blk.lines)
            chosen.append(blk)
            if blk.guessed:
                printed_guessed.append(blk.name)
        if cap is not None and len(sec.blocks) > len(blocks):
            out.append(f'+{len(sec.blocks) - len(blocks)} more — say "all of them"')
    if more is not None:
        out += ["", more]
    else:
        out += parts["tail"](printed_guessed)
    return out, chosen


def _capped(parts, cap):
    picks = {}
    for i, sec in enumerate(parts["sections"]):
        if len(sec.blocks) > cap:
            picks[i] = sorted(sec.blocks, key=lambda b: (-b.amount, b.order))[:cap]
        else:
            picks[i] = sorted(sec.blocks, key=lambda b: b.order)
    return _emit(parts, picks, announce=True, cap=cap)


MORE_LINE = 'There is more — say "more".'


def _key(i, blk) -> list:
    return [i, blk.order[0], blk.order[1]]


def _page(parts, after, first):
    """The next page of the whole list: every block after the cursor `after`,
    in section and date order, as many as fit one message with the
    continuation line. A cursor rather than a page number, so blocks that
    delivering an earlier page removed (residue) do not shift later pages.
    The continuation phrase is never the one that asked for the whole list
    (spec: "the rest stays one word away")."""
    flat = [(i, blk) for i, sec in enumerate(parts["sections"])
            for blk in sorted(sec.blocks, key=lambda b: b.order)]
    if after is not None:
        flat = [e for e in flat if _key(*e) > list(after)]

    def emit(page, last):
        picks = {}
        for i, blk in page:
            picks.setdefault(i, []).append(blk)
        return _emit(parts, picks, announce=first, more=None if last else MORE_LINE,
                     all_sections_empty_msgs=first)

    def fits(page, last):
        return utf16_len(_text(emit(page, last)[0])) <= TELEGRAM_LIMIT

    if not flat and not first:
        return list(parts["head"]) + ["", "That is everything."], [], None
    if fits(flat, True):
        lines, chosen = emit(flat, True)
        return lines, chosen, None
    page = flat[:1]
    for e in flat[1:]:
        if not fits(page + [e], False):
            break
        page.append(e)
    lines, chosen = emit(page, False)
    return lines, chosen, _key(*page[-1])


FIT_CLOSING = "The rest did not fit in one message."


def _fit(lines, closing=FIT_CLOSING) -> str:
    """The last resort: whole lines up to the limit, and a closing line (which
    carries the continuation phrase when there is one). Never returns text over
    TELEGRAM_LIMIT."""
    text = _text(lines)
    if utf16_len(text) <= TELEGRAM_LIMIT:
        return text
    kept = []
    for w in text.split("\n"):
        if utf16_len("\n".join(kept + [w, closing])) > TELEGRAM_LIMIT:
            break
        kept.append(w)
    return "\n".join(kept + [closing])


def _item_sentence(d) -> str:
    cur, kind = d["current"], d["expectation"]["kind"]
    word = KIND_WORD.get(kind or "", "document")
    if d["ended"]:
        return "It has left the bank ledger."
    if d["status"] == "matched":
        return f"{_docname(cur['document'])[0].upper() + _docname(cur['document'])[1:]} is filed with it."
    if d["status"] == "proposed":
        return f"Paired with {_docname(cur['document'])}, not confirmed."
    if d["status"] in ("exempt", "no-document"):
        return "Needs no document."
    if d["status"] == "optional":
        return f"No {word} found (nice to have)."
    if d["status"] == "ineligible":
        return "Before the start date; not tracked."
    if kind is None:
        return "Not yet classified, so nothing was searched."
    n = len(d["search"].get("queries", []))
    return f"No {word} yet." + (f" Searched {n} ways." if n else "")


def build_review(conn, view="status", quarter=None, pid=None, page=None, after=None) -> dict:
    """`page` (1, 2, ...) renders the view uncapped, one message per page, and
    `after` is the cursor the previous page's `next` returned; the `all` view
    is the status view paged. The answer's `next` is the call that the phrase
    the text ends with asks for (`all of them`, `more`), or None."""
    if view not in VIEWS:
        raise db.Refusal(f"view is one of {', '.join(VIEWS)}")
    if view == "item" and pid is None:
        raise db.Refusal("an item view names one transaction")
    if page is not None and (not isinstance(page, int) or page < 1):
        raise db.Refusal("page is 1, 2, 3, ...")
    if view == "all" and page is None:
        page = 1
    if after is not None and (page is None or page < 2 or not isinstance(after, list)):
        raise db.Refusal("after is the cursor a previous page's `next` returned")
    q = quarter or dates.quarter_of(db.now()[:10])
    dates.parse_quarter(q)
    lead = _lead(conn)                  # may record the pass's gate: outside the read below
    # Compose and persist under ONE write lock, so the revisions recorded are
    # exactly those of the facts the text shows (round p1, Astra S1: a write
    # between composing and recording bound the operator to an unseen document).
    with db.tx(conn):
        members, chosen, scope, nxt = [], [], {"quarter": q, "pid": pid}, None
        if lead[0] is not None:
            text = _fit(lead[0])
            scope["stop"] = True
        else:
            members = membership(conn, view, q, pid)
            items = [work.describe(conn, p) for p in members]
            parts = _compose(conn, view, q, items, members, lead)
            if view == "item":
                page = None
                lines, chosen = _emit(parts, {0: parts["sections"][0].blocks}, announce=False)
                text = _text(lines)
            elif page is not None:
                lines, chosen, cursor = _page(parts, after, page == 1)
                text = _text(lines)
                if cursor is not None:
                    nxt = {"view": view, "quarter": q, "page": page + 1, "after": cursor}
                scope["page"], scope["after"] = page, after
            else:
                for cap in range(CAP, -1, -1):
                    lines, chosen = _capped(parts, cap)
                    text = _text(lines)
                    if utf16_len(text) <= TELEGRAM_LIMIT:
                        break
                if any(len(s.blocks) > sum(1 for c in chosen if c in s.blocks)
                       for s in parts["sections"]):
                    nxt = {"view": "all" if view == "status" else view, "quarter": q, "page": 1}
            cut = utf16_len(text) > TELEGRAM_LIMIT
            if cut:
                # nothing it prints is bound: the text may not show every block chosen.
                # The closing line carries the phrase `next` answers, so it is never cut.
                closing = FIT_CLOSING
                if nxt is not None:
                    closing = (MORE_LINE if "after" in nxt
                               else 'The rest did not fit — say "all of them".')
                text, chosen = _fit(lines, closing), []
            if page in (None, 1) and parts["announce"] and not cut \
                    and parts["announce"][0] in lines:
                scope["announce_watermark"] = True
            if view in ("status", "all"):
                scope["residue"] = [c.residue for c in chosen if c.residue is not None]
                scope["offers"] = [c.offer for c in chosen if c.offer is not None]
                if page in (None, 1):
                    scope["residue_silent"] = parts["silent"]
        rid = f"r{db.next_seq(conn)}"
        conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                     " membership_json) VALUES (?,?,?,?,?,?)",
                     (rid, view, db.canonical(scope), db.now(), text, json.dumps(members)))
        printed = {}
        for c in chosen:
            if c.pid is not None:
                printed.setdefault(c.pid, set()).update(c.pairings)
        for p, shown_ids in printed.items():
            prev = conn.execute("SELECT revision FROM projections WHERE pid=?", (p,)).fetchone()[0]
            mrevs = {str(r[0]): r[1] for r in conn.execute(
                "SELECT match_id, revision FROM match_state WHERE pid=?", (p,))
                if r[0] in shown_ids}
            conn.execute("INSERT INTO render_items(render_id, pid, projection_revision,"
                         " match_revisions_json) VALUES (?,?,?,?)",
                         (rid, p, prev, db.canonical(mrevs)))
    return {"render_id": rid, "text": text, "printed": len(printed), "next": nxt}


def render_items(conn, render_id) -> list:
    return [r[0] for r in conn.execute("SELECT pid FROM render_items WHERE render_id=?",
                                       (render_id,))]


def mark_rendering_delivered(conn, render_id: str) -> dict:
    with db.tx(conn):
        r = conn.execute("SELECT * FROM renders WHERE render_id=?", (render_id,)).fetchone()
        if r is None:
            raise db.Refusal(f"there is no rendering {render_id}")
        if r["delivered_at"] is not None:
            return {"render_id": render_id, "already": True}
        now = db.now()
        conn.execute("UPDATE renders SET delivered_at=? WHERE render_id=?", (now, render_id))
        for it in conn.execute("SELECT * FROM render_items WHERE render_id=?", (render_id,)):
            conn.execute("INSERT OR REPLACE INTO shown(pid, render_id, projection_revision,"
                         " match_revisions_json, delivered_at) VALUES (?,?,?,?,?)",
                         (it["pid"], render_id, it["projection_revision"],
                          it["match_revisions_json"], now))
        scope = json.loads(r["scope_json"])
        for rid_ in scope.get("residue", []) + scope.get("residue_silent", []):
            conn.execute("UPDATE residue SET shown_render=? WHERE id=?", (render_id, rid_))
        if scope.get("announce_watermark"):
            conn.execute("UPDATE binding SET watermark_announced=1 WHERE id=1")
        for a in scope.get("alerts", []):
            conn.execute("UPDATE alerts SET sent_at=?, render_id=? WHERE alert_id=?",
                         (now, render_id, a))
        if scope.get("announce_package_name"):
            conn.execute("UPDATE binding SET package_name_announced=1 WHERE id=1")
        return {"render_id": render_id, "delivered_at": now}
