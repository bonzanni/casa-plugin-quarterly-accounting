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

import contextlib
import hashlib
import json
import re
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


# Only unbounded free-text FIELDS are clipped, each to its own bound (fix wave D
# round 3): a payee / bank counterparty text, a document number or recipient, a
# runner-up description, a residue detail — FIELD_MAX; a link — LINK_MAX. What
# identifies an item or a candidate (amount, currency, date, kind, the "Could
# be:" enumeration itself) is never clipped, so a bound item always prints
# everything that identifies it (D3). A whole line is never clipped: that cut
# the amount and date, or a second candidate, off lines that stayed bound.
FIELD_MAX = 60
LINK_MAX = 200


MARK = "\u00b7"             # reserved: only generated text prints it (round 6)
LITERAL_MARK = "\u2022"     # what a literal "·" in free text prints as


class _Names:
    """Per-rendering disambiguation (rounds 5-6): displayed identities are made
    unique BY CONSTRUCTION within one rendering, before anything is composed,
    so the bind-time backstop (_bindable) never decides liveness.

    Generated disambiguators live in a namespace literal text cannot occupy:
    they follow the reserved MARK, and every literal free-text field prints a
    MARK as LITERAL_MARK (field()).
    - digest: hex length of a clipped field's digest ("…·<hex>");
    - docs: doc_id -> level; a document at level k >= 1 prints
      " ·<issuer>" (k = 1), then " ·<issuer>·<sha256 prefix of 4(k-1)>" — the
      stored content hash, so distinct documents always end up distinct;
    - pids: pid -> the generated ref hex a payment's headline adds ("ref <hex>").
    Levels grow to a fixed point over the FINAL identity strings of ALL the
    rendering's entities, not per original collision group."""
    def __init__(self):
        self.digest, self.docs, self.doc_level, self.pids = 4, {}, {}, {}


_NAMES = None


def field(text, units: int = FIELD_MAX) -> str:
    """A literal free-text field: MARK neutralized, clipped to `units` with a
    digest of its FULL value ("…·3f9a"), as long as this rendering needs for
    all its clipped values to print distinct."""
    return _field(text, units, _NAMES.digest if _NAMES is not None else 4)


def _field(text, units, n) -> str:
    if not text:
        return text
    shown = text.replace(MARK, LITERAL_MARK)
    if utf16_len(shown) <= units:
        return shown
    tag = MARK + hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]
    return clip(shown, units - utf16_len(tag)) + tag


def _hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def lineage_ref(pid: int) -> str:
    """The stable hex a payment's generated "ref …" is a prefix of."""
    return _hex(f"lineage:{pid}")


def _fixpoint(ents, render, raise_level, cap=17) -> None:
    """Raise the level of every entity whose FINAL printed identity (render(e),
    normalized as the bind check sees it) is shared with any other entity of
    the rendering, until all are distinct (or `cap`)."""
    for _ in range(cap):
        seen: dict = {}
        for e in ents:
            seen.setdefault(_norm(render(e)), []).append(e)
        dups = [e for es in seen.values() if len(es) > 1 for e in es]
        if not dups:
            return
        for e in dups:
            raise_level(e)


@contextlib.contextmanager
def named(items, view_quarter=None):
    """Compose under the disambiguation for `items` (what build_review does)."""
    global _NAMES
    saved = _NAMES
    _NAMES = names_for(items, view_quarter)
    try:
        yield _NAMES
    finally:
        _NAMES = saved


def _doc_extra(doc, level) -> str:
    if level <= 0:
        return ""
    issuer = field(doc.get("issuer")) or ""
    sha = doc.get("sha256") or _hex(f"doc:{doc.get('doc_id')}")
    parts = ([issuer] if issuer else []) + ([sha[:4 * (level - 1)]] if level > 1 else [])
    if not parts:
        parts = [sha[:4 * level]]
    return " " + MARK + MARK.join(parts)


def names_for(items, view_quarter=None) -> _Names:
    """The disambiguation for a rendering over `items` (every payment it may
    print: a superset of what it prints, so what it prints is distinct too)."""
    global _NAMES
    names, saved = _Names(), _NAMES
    try:
        values = set()
        docs = {}
        for d in items:
            values.add(d["counterparty"] or "")
            for c in ([d["current"]] if d["current"] else []) + d["candidates"]:
                doc = c["document"]
                docs[doc["doc_id"]] = doc
                values.update(x for x in (doc.get("number"), doc.get("issuer")) if x)
        values = {v for v in values if utf16_len(v) > FIELD_MAX}
        for n in range(4, 65, 4):
            if len({_field(v, FIELD_MAX, n) for v in values}) == len(values):
                names.digest = n
                break
        _NAMES = names

        def raise_doc(i):
            names.doc_level[i] = names.doc_level.get(i, 0) + 1
            names.docs[i] = _doc_extra(docs[i], names.doc_level[i])
        _fixpoint(list(docs), lambda i: ident(docs[i]), raise_doc)

        by_pid = {d["pid"]: d for d in items}
        level: dict = {}

        def raise_pid(p):
            level[p] = level.get(p, 0) + 1
            names.pids[p] = lineage_ref(p)[:4 * level[p]]
        _fixpoint(list(by_pid), lambda p: headline(by_pid[p], view_quarter), raise_pid)
    finally:
        _NAMES = saved
    return names


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
    parts = [field(d["counterparty"]), _money(d), _day(d["date"])]
    kind = d["expectation"]["kind"]
    if kind and kind not in ("invoice", "none"):
        parts.append(KIND_WORD[kind])
    if d.get("pending"):
        parts.append("pending")
    if _NAMES is not None and _NAMES.pids.get(d["pid"]):
        parts.append("ref " + _NAMES.pids[d["pid"]])
    if view_quarter and d.get("quarter") and d["quarter"] != view_quarter:
        parts.append(dates.quarter_label(d["quarter"]))
    return " · ".join(parts)


def _a(word: str) -> str:
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


def _docname(doc: dict) -> str:
    w = KIND_WORD.get(doc["kind"], "document")
    return f"{w} {field(doc['number'])}" if doc.get("number") else w


# Evidence enumerations are bounded (round 4). Runner-ups are context and never
# bind: at most RUNNERS_MAX, then "and N others". Candidates DO bind: a view
# prints at most CANDIDATES_MAX and binds only those; the rest are one phrase
# away — an item view that pages every candidate, each page binding what it prints.
RUNNERS_MAX = 3
CANDIDATES_MAX = 3


def ident(doc: dict) -> str:
    """How a pairing is named in the text: document and date. The binding
    check (_bindable) requires this exact string to be visible, and unique
    within its payment."""
    extra = _NAMES.docs.get(doc.get("doc_id"), "") if _NAMES is not None else ""
    return f"{_docname(doc)}{extra} ({_day(doc['date'])})"


def _cands(d, cands):
    return d["candidates"][:CANDIDATES_MAX] if cands is None else cands


def _amount_word(d) -> str:
    return f"{abs(d['amount_minor'] or 0) / 100:.2f}"


def candidates_phrase(d) -> str:
    return f"candidates for {_amount_word(d)} {_day(d['date'])}"


def evidence(d: dict, cands=None) -> list:
    """`cands` are the candidates to print (default: the first
    CANDIDATES_MAX, with the phrase that shows the rest)."""
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
            out.insert(0, f"Paired with {ident(doc)}.")
        if "guessed" in labels:
            rs = cur["runners_up"]
            others = "; ".join(field(x) for x in rs[:RUNNERS_MAX])
            if len(rs) > RUNNERS_MAX:
                others += f"; and {len(rs) - RUNNERS_MAX} others"
            out.append(f"Picked {ident(doc)}; {others} also fits." if others
                       else f"Picked {ident(doc)} among several that fit.")
        if "no-ref" in labels:
            out.append("Repeating equal charges, and no invoice number on both sides.")
        if "partial-search" in labels:
            out.append("The search was cut short, so this may not be the only fit.")
        if "recipient?" in labels:
            out.append(f"{name[0].upper() + name[1:]} names "
                       f"{field(doc.get('recipient')) or 'someone else'}, not the business.")
        if d["status"] == "proposed" and len(out) == 1:
            out.append("Not sure — say if it's wrong.")
    shown = _cands(d, cands)
    if shown:
        out.append("Could be: " + ", ".join(ident(c["document"]) for c in shown) + ".")
    if cands is None and len(d["candidates"]) > len(shown):
        out.append(f"{len(d['candidates']) - len(shown)} more could fit — say "
                   f"\"{candidates_phrase(d)}\".")
    return out


def pairings(d: dict, cands=None) -> dict:
    """The pairings a block that shows evidence(d, cands) displays, by match id,
    with the identity string it prints for each."""
    out = {c["match_id"]: ident(c["document"]) for c in _cands(d, cands)}
    if d["current"] is not None:
        out[d["current"]["match_id"]] = ident(d["current"]["document"])
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
        out.append(field(d["link"], LINK_MAX))
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
    # A package snapshot or a document handover is not a review pass: it must not
    # erase an interrupted review's block (Task 22 review, item 5).
    last = conn.execute("SELECT * FROM passes WHERE ended_at IS NOT NULL"
                        " AND trigger NOT IN ('package', 'handover')"
                        " ORDER BY ended_at DESC, generation DESC LIMIT 1").fetchone()
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
                 "offer", "ident")

    def __init__(self, lines, *, pid=None, pairings=None, residue=None, amount=0, order=("", 0),
                 name=None, guessed=False, offer=None, ident=None):
        # `pairings`: match id -> the identity string printed for it; `ident`: the
        # payment's printed identity (its headline). Both are checked at bind time.
        self.lines, self.pid, self.residue = list(lines), pid, residue
        self.pairings, self.ident = dict(pairings or {}), ident
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
            line = f"{head} left the bank ledger ({field(r['detail'])}) — {tail}"
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


def _item_blocks(ds, detail, q, shows_pairings=False, guessed=False) -> list:
    """`pairings` are the match ids whose proposition the text displays. Only
    those are bound for a later correction (round p1, Astra S1: a missing view
    that bound candidates it never showed let "Adobe is wrong" reject them)."""
    return [_Block([headline(d, q), *detail(d)], pid=d["pid"], ident=headline(d, q),
                   pairings=pairings(d) if shows_pairings else {},
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
        parts["tail"] = lambda printed_guessed: []
        return parts                    # composed page by page in _item_page

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
        secs.append(_Section("", [_Block(delivery.offer_lines(fname, status),
                                         order=("", pkg_id), offer=pkg_id)
                                  for pkg_id, fname, status in delivery.offerable(conn, q)]))
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
                out.append(f'Tell me if one is wrong — "the {field(printed_guessed[0])} one is '
                           'wrong".')
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
CLIP_MARK = "\u2026"


def clip(text: str, units: int) -> str:
    """`text` cut to at most `units` UTF-16 units, ending in CLIP_MARK when it
    was cut — for displayed detail with no natural bound (a probe's diagnostic,
    a bank counterparty text). Cuts between code points, so a non-BMP
    character is never split into half a surrogate pair."""
    if utf16_len(text) <= units:
        return text
    out, used = [], 0
    budget = units - utf16_len(CLIP_MARK)
    for ch in text:
        n = utf16_len(ch)
        if used + n > budget:
            break
        out.append(ch)
        used += n
    return "".join(out) + CLIP_MARK


def fit_lines(lines, closing=None, always_close=False) -> tuple:
    """THE fit (fix wave D round 2, generalized after the same shape recurred in
    views, alerts and receipts): every operator-facing message is produced
    through here, and what it returns joins with "\n" to at most
    TELEGRAM_LIMIT UTF-16 units — every separator and the closing line
    included, whatever the inputs.

    Returns (out_lines, whole): `whole` is how many leading input lines are
    printed IN FULL — the only ones a caller may bind (D3). When everything fits,
    out_lines is the input (plus `closing` if always_close). Otherwise whole
    lines while they fit beside the closing line; when not even the first line
    fits, it is clipped with CLIP_MARK so the message still says something (and
    `whole` is 0). The closing line is never cut away (it is clipped only if it
    alone exceeds the limit)."""
    lines = list(lines)
    tail = [clip(closing, TELEGRAM_LIMIT)] if closing else []
    if utf16_len("\n".join(lines + (tail if always_close else []))) <= TELEGRAM_LIMIT:
        return lines + (tail if always_close else []), len(lines)
    budget = TELEGRAM_LIMIT - (utf16_len(tail[0]) + 1 if tail else 0)
    kept, used = [], 0
    for ln in lines:
        need = utf16_len(ln) + (1 if kept else 0)
        if used + need > budget:
            break
        kept.append(ln)
        used += need
    whole = len(kept)
    if whole == 0 and lines and budget > utf16_len(CLIP_MARK):
        kept = [clip(lines[0], budget)]
    if not kept and tail:
        return tail, 0
    return kept + tail, whole


def fit_message(lines, closing=None, always_close=False) -> str:
    """fit_lines, joined: a text that is always deliverable. `lines` may be
    a str (split at newlines)."""
    if isinstance(lines, str):
        lines = lines.split("\n")
    return "\n".join(fit_lines(lines, closing, always_close)[0])


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


def _item_block(d, cands, more) -> _Block:
    lines = [headline(d), _item_sentence(d), *evidence(d, cands=cands)]
    if _open_required(d):
        lines += _missing_detail(d)
    if more:
        lines += ["", MORE_LINE]
    return _Block(lines, pid=d["pid"], ident=headline(d), pairings=pairings(d, cands))


def _item_page(d, after):
    """The item view pages EVERY candidate (round 4): as many as fit one
    message after the cursor (the last match id printed), each page binding
    the candidates it prints. Returns (block, cursor or None)."""
    rest = [c for c in d["candidates"] if after is None or c["match_id"] > after[0]]
    n = 1 if rest else 0
    while n < len(rest) and utf16_len(_text(_item_block(d, rest[:n + 1],
                                                         n + 1 < len(rest)).lines)) \
            <= TELEGRAM_LIMIT:
        n += 1
    more = n < len(rest)
    return _item_block(d, rest[:n], more), ([rest[n - 1]["match_id"]] if more else None)


def build_review(conn, view="status", quarter=None, pid=None, page=None, after=None) -> dict:
    """See _build_review. The per-rendering disambiguation (_NAMES) lives only
    for the duration of one call."""
    global _NAMES
    try:
        return _build_review(conn, view, quarter, pid, page, after)
    finally:
        _NAMES = None


def _build_review(conn, view="status", quarter=None, pid=None, page=None, after=None) -> dict:
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
        items = []
        if lead[0] is not None:
            text = fit_message(_text(lead[0]), FIT_CLOSING)
            scope["stop"] = True
        else:
            members = membership(conn, view, q, pid)
            items = [work.describe(conn, p) for p in members]
            # every identity this rendering prints is made distinct before composing
            global _NAMES
            _NAMES = names_for(items, None if view == "item" else q)
            parts = _compose(conn, view, q, items, members, lead)
            if view == "item":
                blk, cursor = _item_page(items[0], after)
                lines, chosen = blk.lines, [blk]
                text = _text(lines)
                if cursor is not None:
                    nxt = {"view": "item", "pid": pid, "page": (page or 1) + 1, "after": cursor}
                scope["page"], scope["after"] = page, after
                # a later page adds to what the earlier pages of this item bound
                scope["continues"] = bool(page and page > 1)
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
            # The final text goes through the one fit. If it cut anything, nothing it
            # prints is bound: the text may not show every block chosen. The closing
            # line carries the phrase `next` answers, so it is never cut.
            closing = FIT_CLOSING
            if nxt is not None:
                closing = (MORE_LINE if "after" in nxt
                           else 'The rest did not fit — say "all of them".')
            body = text.split("\n")
            out, whole = fit_lines(body, closing)
            text, cut = "\n".join(out), whole < len(body)
            if cut:
                chosen = []
            if page in (None, 1) and parts["announce"] and not cut \
                    and parts["announce"][0] in lines:
                scope["announce_watermark"] = True
            if view in ("status", "all"):
                scope["residue"] = [c.residue for c in chosen if c.residue is not None]
                scope["offers"] = [c.offer for c in chosen if c.offer is not None]
                if page in (None, 1):
                    scope["residue_silent"] = parts["silent"]
        printed = _bindable(chosen, text)
        if _NAMES is not None:
            # the generated refs this rendering printed on payments it binds: a reply's
            # "ref <hex>" is honoured only against these (round 6)
            # hex -> EVERY payment printed with that ref (round 9: refs are distinct only
            # within a payee-collision group, so two groups can print the same hex)
            refs: dict = {}
            for p in sorted(printed):
                if _NAMES.pids.get(p):
                    refs.setdefault(_NAMES.pids[p], []).append(p)
            if refs:
                scope["refs"] = refs
            # the payee name each bound payment was SHOWN as: a reply resolves names
            # against what the operator saw, as well as the stored names (round 7)
            by_pid = {d["pid"]: d for d in items}
            seen = {str(p): field(by_pid[p]["counterparty"]) for p in printed if p in by_pid}
            if seen:
                scope["names"] = seen
        rid = f"r{db.next_seq(conn)}"
        conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                     " membership_json) VALUES (?,?,?,?,?,?)",
                     (rid, view, db.canonical(scope), db.now(), text, json.dumps(members)))
        for p, shown_ids in printed.items():
            prev = conn.execute("SELECT revision FROM projections WHERE pid=?", (p,)).fetchone()[0]
            mrevs = {str(r[0]): r[1] for r in conn.execute(
                "SELECT match_id, revision FROM match_state WHERE pid=?", (p,))
                if r[0] in shown_ids}
            conn.execute("INSERT INTO render_items(render_id, pid, projection_revision,"
                         " match_revisions_json) VALUES (?,?,?,?)",
                         (rid, p, prev, db.canonical(mrevs)))
    return {"render_id": rid, "text": text, "printed": len(printed), "next": nxt}


def _norm(s: str) -> str:
    return re.sub(r"[\s\u00b7]+", "", s)


def _bindable(chosen, text) -> dict:
    """THE bind-time check (round 4): a payment is bound only if its printed
    identity (headline) is visible in the text and no other bound payment in
    this rendering prints the same identity; a pairing only if its identity
    string is visible and unique within its payment. Whatever the formatting,
    every entity a rendering binds is uniquely identified by text visibly in it.
    Returns pid -> the match ids bound."""
    flat = _norm(text)
    by_pid: dict = {}
    for c in chosen:
        if c.pid is None:
            continue
        e = by_pid.setdefault(c.pid, {"ident": c.ident, "pairings": {}})
        e["pairings"].update(c.pairings)
    seen: dict = {}
    for e in by_pid.values():
        k = _norm(e["ident"] or "")
        seen[k] = seen.get(k, 0) + 1
    out = {}
    for p, e in by_pid.items():
        k = _norm(e["ident"] or "")
        if not k or seen[k] > 1 or k not in flat:
            continue
        names = [_norm(v) for v in e["pairings"].values()]
        out[p] = {m for m, v in e["pairings"].items()
                  if names.count(_norm(v)) == 1 and _norm(v) in flat}
    return out


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
        # the delivery ORDER is the store sequence, not the one-second timestamp (db.last_delivered)
        conn.execute("UPDATE renders SET delivered_at=?, delivered_seq=? WHERE render_id=?",
                     (now, db.next_seq(conn), render_id))
        scope = json.loads(r["scope_json"])
        for it in conn.execute("SELECT * FROM render_items WHERE render_id=?",
                               (render_id,)).fetchall():
            mrevs = it["match_revisions_json"]
            if scope.get("continues"):
                # a later page of one item view: the operator has now been shown the
                # earlier pages' candidates too, at the same revision of the payment
                prev = conn.execute("SELECT s.*, r.kind FROM shown s JOIN renders r ON"
                                    " r.render_id=s.render_id WHERE s.pid=?",
                                    (it["pid"],)).fetchone()
                if prev is not None and prev["kind"] == "item" \
                        and prev["projection_revision"] == it["projection_revision"]:
                    merged = json.loads(prev["match_revisions_json"])
                    merged.update(json.loads(mrevs))
                    mrevs = db.canonical(merged)
            conn.execute("INSERT OR REPLACE INTO shown(pid, render_id, projection_revision,"
                         " match_revisions_json, delivered_at) VALUES (?,?,?,?,?)",
                         (it["pid"], render_id, it["projection_revision"], mrevs, now))
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
