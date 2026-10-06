"""The operator surface (design rev 17 §1): the run's one end message, the open-items card,
the Review cards (one per proposal, then one per vendor with missing invoices, paged), the
"package ready" notice, and the scheduled run's new-state rule.

Every composer runs inside the caller's write transaction (db.next_seq asserts it): a tap
composes its `next` in the tap's own transaction. A rendering binds EXACTLY the payments,
and the candidate documents, whose lines appear whole in its final deposited text (plan
rounds 7–8): each composer sizes its lines on their final form plus the worst-case tag,
and `_store` refuses (Undisplayed) a binding whose line the fit would cut. Every state a
rendering reports — displayed or only counted — is recorded in `render_states`, which the
§1 new-state rule reads."""
from __future__ import annotations

import collections
import json
import re

import amounts
import dates
import db
import kb
import lineage
import loop
import matches
import views
import work

KINDS = ("end", "open-items", "review", "vendor-page", "ready")
# cards posted with no delivery callback — a tap's `next` (#1302) or show_view(view="open"):
# seen once posted (review round 1 ruling: the closing open-items card included)
TAP_CARDS = ("review", "vendor-page", "open-items")
CONFIRM_ALL_MAX = 24          # §1: with 25 or more proposals it is left out
CANDIDATE_BUTTONS = 4         # §1: up to four named candidates
PAGE_LINES = 25               # a vendor page's payments, then fitted to BODY_LIMIT
LABEL_MAX = 32                # casa:result_broker.py, a button label
BUCKETS = ("matched", "proposed", "missing", "not_needed", "pending")
TAG_WORST = " · " + "9" * 18        # views.tag_for: the longest render id (r\d{1,18})


class Undisplayed(RuntimeError):
    """A composer asked to bind a payment (or a candidate) its fitted text does not
    display: a bug in that composer's pagination or trimming, never a deposit."""


# ---- the state the surface is composed from -----------------------------------------------

def main_quarter(conn) -> str:
    """D11: the quarter of the latest in-scope payment (today's quarter when none)."""
    days = [dates.effective_date(row) for _, _, row in loop.in_scope(conn)]
    return dates.quarter_of(max(days) if days else dates.today())


def _line_key(d):
    """Proposal line order (§1): vendor (kb.norm), then date, then pid."""
    return (kb.norm(d["vendor"]), d["date"] or "", d["pid"])


def _bucket(d) -> str:
    """§6.1's partition: pending first, then status."""
    if d["pending"]:
        return "pending"
    return {"matched": "matched", "proposed": "proposed",
            "open": "missing"}.get(d["status"], "not_needed")


def state(conn) -> dict:
    """Every in-scope payment (loop.in_scope), described, in exactly one bucket.
    proposals: the "to confirm" ones in line order; missing: {vendor: [d]} (left-missing
    included), vendors and payments in order; counts: {quarter: Counter(bucket)}."""
    by_bucket = {b: [] for b in BUCKETS}
    counts: dict = {}
    for pid, p, row in loop.in_scope(conn):
        d = work.describe(conn, pid)
        d["vendor"] = loop.vendor_of(conn, row)
        b = _bucket(d)
        by_bucket[b].append(d)
        counts.setdefault(d["quarter"], collections.Counter())[b] += 1
    for b in BUCKETS:
        by_bucket[b].sort(key=_line_key)
    missing: dict = {}
    names: dict = {}
    for d in by_bucket["missing"]:
        v = names.setdefault(kb.norm(d["vendor"]), d["vendor"])
        missing.setdefault(v, []).append(d)
    return {"proposals": by_bucket["proposed"], "missing": missing,
            "pending": by_bucket["pending"], "counts": counts, "by_bucket": by_bucket}


def _answered(d) -> bool:
    return d["search_state"] == "accepted-missing"     # [Leave missing] (§1 "complete")


def item_state(d) -> str:
    if d["status"] == "proposed" and d["current"] is not None:
        return f"proposed:{d['current']['document']['doc_id']}"
    if d["status"] == "proposed":
        return "proposed:joint:" + ",".join(str(c["document"]["doc_id"])
                                            for c in d["candidates"])
    return "missing"


def seen_state(conn, pid, st) -> bool:
    """§1: shown by a delivered message. A run's own messages (`end`, `open-items`, `ready`)
    count only once delivered (plan round 2, Astra S2: show_view stamps posted_seq BEFORE the
    deposit, so a cut before the receipt left an end message "seen" that never arrived). A
    tap's card has no delivery callback (#1302 posts it after the tap's receipt), so a posted
    one counts — and so does an open-items card, which is only ever posted that way (a
    tap's `next`, or show_view(view="open")): review round 1 ruling."""
    return conn.execute(
        "SELECT 1 FROM render_states i JOIN renders r ON r.render_id=i.render_id WHERE"
        " i.pid=? AND i.item_state=? AND (r.delivered_at IS NOT NULL OR (r.kind IN"
        " (%s) AND r.posted_seq IS NOT NULL)) LIMIT 1" % ",".join("?" * len(TAP_CARDS)),
        (pid, st, *TAP_CARDS)).fetchone() is not None


def never_set(conn, vendor) -> list:
    """§1 (r10): every payment [Never for X] changes now — the vendor's in-scope open
    payments that expect a document, booked, all quarters (accepted-missing included: the
    rule moves them to no-document too)."""
    return sorted(pid for pid, p, row in loop.in_scope(conn)
                  if kb.norm(loop.vendor_of(conn, row)) == kb.norm(vendor)
                  and p["status"] == "open" and p["exp_kind"] != "none"
                  and row["status"] == "BOOK")


def _unanswered(conn, vendor, only=None) -> list:
    out = [p for p in never_set(conn, vendor)
           if lineage.projection(conn, p)["search_state"] != "accepted-missing"]
    return [p for p in out if only is None or p in only]


# ---- text pieces ----------------------------------------------------------------------------

def _qn(q, ref=None) -> str:
    """"Q3" — with its year when it is not `ref`'s year."""
    year, n = dates.parse_quarter(q)
    return f"Q{n}" if ref is None or dates.parse_quarter(ref)[0] == year else f"Q{n} {year}"


def _s(n, one, many=None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _money(amount, currency) -> str:
    return amounts.fmt(amount, currency) if amount is not None and currency else "?"


def _day(day) -> str:
    return dates.short_day(day) if day else "no date"


def _doc_summary(conn, doc_id) -> dict:
    d = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    return {"doc_id": d["doc_id"], "kind": d["kind"], "issuer": d["issuer"] or d["counterparty"],
            "number": d["document_number"], "date": d["document_date"],
            "amount_minor": d["amount_minor"], "currency": d["currency"],
            "recipient": d["recipient"], "sha256": d["sha256"],
            "date_read": d["date_read_at"] is not None}


def _offered(conn, d) -> list:
    """The candidates a proposal offers (D3): the chosen document and its alternatives, or
    a legacy joint set's members — each {"doc", "match_id"} (None for an alternative).
    An alternative or set member another payment holds (matches.holders, R5) is not
    offered: a pick of it would only be refused (Task 8 review round 1)."""
    cur = d["current"]

    def free(doc_id):
        return not matches.taken_elsewhere(conn, doc_id, d["pid"])
    if cur is not None:
        return [{"doc": cur["document"], "match_id": cur["match_id"]}] + [
            {"doc": _doc_summary(conn, a), "match_id": None}
            for a in matches.alternatives(conn, cur["match_id"]) if free(a)]
    return [{"doc": c["document"], "match_id": c["match_id"]} for c in d["candidates"]
            if free(c["document"]["doc_id"])]


def _doc_word(doc) -> str:
    return views.field(doc["number"]) if doc.get("number") else \
        views.KIND_WORD.get(doc["kind"], "document")


def _proposal_line(conn, i, d) -> str:
    """§1: "{i}. {vendor} · {day} · {amount} ↔ {doc}"."""
    offered = _offered(conn, d)
    if d["current"] is None:
        doc = f"{len(offered)} invoices fit"                         # D3: no chosen one
    elif len(offered) > 1:
        chosen = offered[0]["doc"]
        doc = f"{len(offered)} invoices fit; chose {_doc_word(chosen)} ({_day(chosen['date'])})"
    else:
        c = offered[0]["doc"]
        word = views.KIND_WORD.get(c["kind"], "document")
        doc = f"{word} {views.field(c['number'])}" if c.get("number") else word
        doc += f" · {_money(c['amount_minor'], c['currency'])}"
        if c.get("currency") and d["currency"] and c["currency"] != d["currency"]:
            doc += " (other currency)"
    return (f"{i}. {views.field(d['vendor'])} · {_day(d['date'])} · "
            f"{_money(d['amount_minor'], d['currency'])} ↔ {doc}")


def _ready_line(q, ref) -> str:
    return f'{_qn(q, ref)} complete · package ready — say "send the {_qn(q, ref)} package"'


# ---- fitting and storing ---------------------------------------------------------------------

def _fits(lines) -> bool:
    """The COMPLETE final text with the worst-case rendering tag fits a card (plan round 7:
    measured on the real lines, never on an estimate)."""
    if not lines:
        return True
    text = "\n".join([lines[0] + TAG_WORST] + list(lines[1:]))
    return views.utf16_len(text) <= views.BODY_LIMIT and views.fits_proposal(text)


def _fit_count(before, items, closing, after=(), total=None) -> int:
    """How many leading `items` fit whole between `before` and `after`, with
    closing(left) after them when some of `total` (default: all the items) are left out."""
    total = len(items) if total is None else total
    for k in range(len(items), -1, -1):
        left = total - k
        if _fits(list(before) + items[:k] + ([closing(left)] if left else []) + list(after)):
            return k
    return 0


def _grammar(ds) -> dict:
    """The S7 grammar fields for the payments a rendering binds (views.FACT_FIELDS): the
    payee each was shown as, and the generated refs printed on them."""
    names = {str(d["pid"]): views.field_raw(d["counterparty"]) for d in ds}
    refs: dict = {}
    for d in sorted(ds, key=lambda x: x["pid"]):
        ref = views.printed_ref(d["pid"])
        if ref:
            refs.setdefault(ref, []).append(d["pid"])
    return {"names": names, "refs": refs}


def _store(conn, kind, lines, scope, bound, states, docs=None) -> str:
    """One rendering (plan rounds 7–8: a rendering binds exactly the payments, and the
    candidate documents, whose lines appear in its final deposited text). `lines` are the
    FINAL lines (escaped fields, suffixes such as "· left missing"); the tag ends line 1
    (binding V2). `bound` maps each payment the rendering binds to the index of its line;
    `docs` maps a bound payment to {match_id or ("alt", doc_id): line index} for the
    pairings it displays — explicitly: a payment it does not name binds no pairing (review
    round 1: no fallback to the current pairing); `states` maps every payment it reports (displayed or counted) to its
    item_state. If the fit (views.fit_lines) would print a bound line less than whole,
    nothing is stored: Undisplayed."""
    rid = f"r{db.next_seq(conn)}"
    out, whole = views.fit_lines(lines, tag=views.tag_for(rid))
    late = [pid for pid, i in bound.items() if i >= whole] + [
        (pid, m) for pid, ms in (docs or {}).items() for m, i in ms.items() if i >= whole]
    if late:
        raise Undisplayed(f"{kind} would bind payments {late} whose lines do not fit")
    text = "\n".join(out)
    full = {"names": {}, "refs": {}, "proposed": [], "offers": [], "next": None,
            "walk": None, "pid": None, "pos": -1, "scheduled": False, **scope,
            "bound_lines": {str(pid): out[i] for pid, i in bound.items()}}
    full.setdefault("review_of", rid)
    conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                 " membership_json) VALUES (?,?,?,?,?,?)",
                 (rid, kind, db.canonical(full), db.now(), text, json.dumps(sorted(bound))))
    for pid in bound:
        rev = conn.execute("SELECT revision FROM projections WHERE pid=?", (pid,)).fetchone()[0]
        shown = {m for m in (docs or {}).get(pid, ()) if isinstance(m, int)}
        mrevs = {str(r[0]): r[1] for r in conn.execute(
            "SELECT match_id, revision FROM match_state WHERE pid=? AND state IN ('matched',"
            " 'proposed', 'conflicted')", (pid,)) if r[0] in shown}
        conn.execute("INSERT INTO render_items(render_id, pid, projection_revision,"
                     " match_revisions_json) VALUES (?,?,?,?)",
                     (rid, pid, rev, db.canonical(mrevs)))
    for pid, st in states.items():
        conn.execute("INSERT INTO render_states(render_id, pid, item_state) VALUES (?,?,?)",
                     (rid, pid, st))
    return rid


def _row(conn, rid):
    r = conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
    if r is None:
        raise db.Refusal(f"there is no rendering {rid}")
    return r


# ---- the end message and the open-items card -------------------------------------------------

def _vendor_items(ds) -> list:
    """One Review item per vendor (§1), sorted by vendor: {"v": vendor, "pids": [...]}."""
    groups: dict = {}
    for d in sorted(ds, key=_line_key):
        groups.setdefault(kb.norm(d["vendor"]), {"v": d["vendor"], "pids": []})["pids"].append(
            d["pid"])
    return [groups[k] for k in sorted(groups)]


def _summary(conn, kind, quarter, head, proposals, vendors, tail, states, *, scheduled,
             extra_scope=None) -> str:
    """The end-message composer (§1), shared by the end message and the open-items card:
    `head` lines, then "To confirm:" and the numbered proposal lines that fit whole (the
    rest behind one closing line: Review shows them, Confirm all is left out), then `tail`.
    The Review order is every proposal in line order, then `vendors`."""
    with views.named(proposals, quarter):
        plines = [_proposal_line(conn, i, d) for i, d in enumerate(proposals, 1)]
        before = list(head) + (["To confirm:"] if proposals else [])

        def closing(left):
            return f"… and {left} more to confirm — Review shows them."
        k = _fit_count(before, plines, closing, tail)
        lines = before + plines[:k] + ([closing(len(plines) - k)] if k < len(plines) else []) \
            + list(tail)
        listed = proposals[:k]
        bound = {d["pid"]: len(before) + j for j, d in enumerate(listed)}
        docs = {d["pid"]: ({d["current"]["match_id"]: bound[d["pid"]]}
                           if d["current"] is not None else {}) for d in listed}
        chosen = [d["pid"] for d in listed if d["current"] is not None]
        confirm_all = (k == len(proposals) and 1 <= len(chosen)
                       and len(proposals) <= CONFIRM_ALL_MAX)
        scope = {"quarter": quarter, "scheduled": scheduled, "proposed": chosen,
                 "confirm_all": len(chosen) if confirm_all else 0,
                 "order": [{"p": d["pid"]} for d in proposals] + list(vendors),
                 **_grammar(listed), **(extra_scope or {})}
        return _store(conn, kind, lines, scope, bound, states, docs=docs)


def _counts_line(c) -> str:
    out = (f"{c['matched']} matched · {c['not_needed']} need no invoice · "
           f"{c['proposed']} to confirm · {c['missing']} missing")
    return out + (f" · {c['pending']} pending" if c["pending"] else "")


def _ready_scope(conn, quarters) -> dict:
    qs = sorted(set(quarters))
    return {"ready_quarters": qs,
            "ready_sigs": {q: loop.completion_sig(conn, q) for q in qs}}


def _handover(conn, job_id, docs, quarter, tail, ready) -> str:
    """§1 "A missing invoice the operator has" (§2.5): one line per handed document — what
    the continuation changed — and the proposals among them to confirm."""
    head, props = [], []
    for doc in dict.fromkeys(docs):
        hs = matches.holders(conn, doc)
        paired = [p for p, how in hs if how == "matched"]
        held = [p for p, how in hs if how != "matched"]
        if paired:
            d = work.describe(conn, paired[0])
            head.append(f"Filed. Paired with {views.field(d['counterparty'])} · "
                        f"{_day(d['date'])} · {_money(d['amount_minor'], d['currency'])}")
            continue
        d = work.describe(conn, held[0]) if held else None
        if d is not None and d["status"] == "proposed":
            if all(x["pid"] != d["pid"] for x in props):
                d["vendor"] = d["counterparty"]
                props.append(d)
            continue
        head.append("Filed. No payment fits it yet — it's matched when one does.")
    props.sort(key=_line_key)
    return _summary(conn, "end", quarter, head, props, [], tail,
                    {d["pid"]: item_state(d) for d in props}, scheduled=False,
                    extra_scope={"job_id": job_id, **(_ready_scope(conn, ready) if ready
                                                      else {})})


def compose_end(conn, job_id, *, scheduled: bool, handover_docs=(), extra=(), ready=()):
    """The run's one end message (kind 'end', §1), or None when a scheduled run has nothing
    new and no extra line (rev 17: "only if it holds an item in a state no delivered
    message showed"). `extra`: failure lines, mirror failures, the partial line. `ready`:
    the quarters whose completion notice is owed (D19) — lines of the message when it has
    items to list, else the message IS the completion (compose_ready)."""
    st = state(conn)
    q = main_quarter(conn)
    open_missing = [d for ds in st["missing"].values() for d in ds if not _answered(d)]
    ready = sorted(set(ready))
    tail = [_ready_line(r, q) for r in ready] + list(extra)
    if handover_docs:
        return _handover(conn, job_id, handover_docs, q, tail, ready)
    reported = {d["pid"]: item_state(d) for d in st["proposals"]}
    reported.update({d["pid"]: "missing" for ds in st["missing"].values() for d in ds})
    extra_scope = {"job_id": job_id, **(_ready_scope(conn, ready) if ready else {})}
    if scheduled:
        new_props = [d for d in st["proposals"]
                     if not seen_state(conn, d["pid"], item_state(d))]
        new_miss = [d for d in open_missing if not seen_state(conn, d["pid"], "missing")]
        if not new_props and not new_miss:
            if ready:
                return compose_ready(conn, ready, extra)
            if not extra:
                return None
        earlier = len(st["proposals"]) + len(open_missing) - len(new_props) - len(new_miss)
        head = [f"{_qn(q)} · new: {len(new_props)} to confirm · {len(new_miss)} missing"]
        if earlier:
            head.append(f"{_s(earlier, 'earlier item')} still open")
        return _summary(conn, "end", q, head, new_props, _vendor_items(new_miss), tail,
                        reported, scheduled=True, extra_scope=extra_scope)
    c = st["counts"].get(q, collections.Counter())
    n = sum(c.values())
    if not st["proposals"] and not open_missing:
        if ready:
            return compose_ready(conn, ready, extra)
        if not c["pending"]:
            return _summary(conn, "end", q,
                            [f"{_qn(q)} checked · {_s(n, 'payment')} · all accounted for."],
                            [], [], list(extra), reported, scheduled=False,
                            extra_scope=extra_scope)
    earlier = []
    for eq in sorted(st["counts"]):
        if eq == q:
            continue
        a = sum(1 for d in st["proposals"] if d["quarter"] == eq)
        b = sum(1 for d in open_missing if d["quarter"] == eq)
        if a or b:
            earlier.append(f"{_qn(eq, q)} · still open: {a} to confirm · {b} missing")
    head = [f"{_qn(q)} checked · {_s(n, 'payment')}", _counts_line(c)]
    return _summary(conn, "end", q, head, st["proposals"], _vendor_items(open_missing),
                    earlier + tail, reported, scheduled=False, extra_scope=extra_scope)


def compose_open(conn, quarter, *, scheduled=False) -> str:
    """The open-items card (§1, r11): the end-message composer over the current full state.
    `b` counts the unanswered missing payments only ([Leave missing] is an answer)."""
    st = state(conn)
    open_missing = [d for ds in st["missing"].values() for d in ds if not _answered(d)]
    reported = {d["pid"]: item_state(d) for d in st["proposals"] + open_missing}
    if not st["proposals"] and not open_missing:
        head = [f"{_qn(quarter)} · all answered"]
    else:
        head = [f"{_qn(quarter)} · still open: {len(st['proposals'])} to confirm · "
                f"{len(open_missing)} missing"]
    return _summary(conn, "open-items", quarter, head, st["proposals"],
                    _vendor_items(open_missing), [], reported, scheduled=scheduled)


def compose_ready(conn, quarters: list, extra=()) -> str:
    """The "package ready" notice (§1, D19): the latest owed quarter heads it with [Get
    package]; each earlier one is a line; then `extra`. Delivery records each completion
    (`ready_sigs`, views.mark_rendering_delivered)."""
    qs = sorted(set(quarters))
    latest = qs[-1]
    n = conn.execute("SELECT times FROM quarter_notices WHERE quarter=?", (latest,)).fetchone()
    if n is not None and n["times"] > 0:
        head = f"{_qn(latest)} complete · updated · package ready"
    else:
        k = sum(1 for _, _, row in loop.in_scope(conn)
                if dates.quarter_of(dates.effective_date(row)) == latest)
        head = f"{_qn(latest)} complete · {k} of {k} accounted for · package ready"
    lines = [head] + [_ready_line(q, latest) for q in qs[:-1]] + list(extra)
    return _store(conn, "ready", lines, {"quarter": latest, "order": [],
                                         **_ready_scope(conn, qs)}, {}, {})


# ---- the Review cards ------------------------------------------------------------------------

_LINKISH = re.compile(r"(?i)www\.|://")


def _label(text) -> str:
    """A button label Casa accepts (casa:result_broker.py `_text_ok`): one printable line
    of at most LABEL_MAX characters that cannot read as a link — a payee such as
    "WWW.EXAMPLE.COM" would otherwise get the whole card refused."""
    text = _LINKISH.sub(lambda m: m.group(0)[:-1] + " ", views.caption_safe(text))
    return views.clip(text, LABEL_MAX)


def _proposal_card(conn, review_of, pos, n, quarter, scheduled, pid):
    """§1: "Card i of n · to confirm", the headline, one line per offered candidate (at most
    CANDIDATE_BUTTONS, each bound only when its line is displayed whole; the rest counted),
    then the rest of the evidence. None when the payment is no longer to confirm."""
    d = work.describe(conn, pid)
    if d["status"] != "proposed":
        return None
    offered = _offered(conn, d)
    with views.named([{**d, "candidates": [{"document": c["doc"]} for c in offered]}],
                     quarter):
        head = [f"Card {pos + 1} of {n} · to confirm", views.headline(d, quarter)]
        clines = [f"{k}. {views.ident(c['doc'])} · "
                  f"{_money(c['doc']['amount_minor'], c['doc']['currency'])}"
                  for k, c in enumerate(offered[:CANDIDATE_BUTTONS], 1)]

        def closing(left):
            return f"{left} more could fit — say \"{views.candidates_phrase(d)}\""
        k = _fit_count(head, clines, closing, total=len(offered))
        shown = offered[:k]
        left = len(offered) - k
        lines = head + clines[:k] + ([closing(left)] if left else []) \
            + views.evidence(d, cands=[])
        docs = {(c["match_id"] if c["match_id"] is not None else ("alt", c["doc"]["doc_id"])):
                len(head) + j for j, c in enumerate(shown)}
        picks = len(shown) >= 2 or d["current"] is None
        scope = {"quarter": quarter, "scheduled": scheduled, "review_of": review_of,
                 "pos": pos, "pid": pid,
                 "alternatives": [c["doc"]["doc_id"] for c in shown if c["match_id"] is None],
                 "picks": [[c["doc"]["doc_id"],
                            _label(f"{c['doc'].get('number') or views.KIND_WORD.get(c['doc']['kind'], 'document')}"
                                   f" · {_day(c['doc']['date'])}")]
                           for c in shown] if picks else [],
                 "proposed": [pid] if d["current"] is not None and shown else [],
                 **_grammar([d])}
        return _store(conn, "review", lines, scope, {pid: 1}, {pid: item_state(d)},
                      docs={pid: docs})


def _page_lines(vendor, ds, i, n, p, pages, link, quarter) -> list:
    """A vendor page's FINAL lines — what _store fits and what _pages measures (one
    function, so the measure is the text)."""
    head = f"Card {i} of {n} · missing invoices · {views.field(vendor)}"
    if pages > 1:
        head += f" · page {p} of {pages}"
    body = [views.headline(d, quarter) + (" · left missing" if _answered(d) else "")
            for d in ds]
    return [head] + body + ([views.field(link, views.LINK_MAX)] if link else [])


def _pages(vendor, ds, i, n, link, quarter) -> list:
    """Greedy pages of ≤ PAGE_LINES payments, each measured on its COMPLETE final text: the
    page's real lines (_page_lines: escaped vendor name, "· left missing" suffixes, the
    link) with the worst-case page numbers and the worst-case rendering tag (plan round 7,
    Astra S1: a 60-character punctuated vendor name and 24 suffixes overflowed a page
    measured without them, and its 25th payment was bound but cut)."""
    worst = max(len(ds), 2)

    def fits(trial):
        return _fits(_page_lines(vendor, trial, i, n, worst, worst, link, quarter))
    pages, cur = [], []
    for d in ds:
        trial = cur + [d]
        if cur and (len(trial) > PAGE_LINES or not fits(trial)):
            pages.append([x["pid"] for x in cur])
            cur = [d]
        else:
            cur = trial
    if cur:
        pages.append([x["pid"] for x in cur])
    return pages


def _vendor_pages_of(conn, review_of, pos, page):
    """The vendor-page renderings of this walk item: the latest first page, and the latest
    rendering of each later page composed after it (rid order is the store sequence)."""
    rows = [r for r in conn.execute(
        "SELECT render_id, scope_json FROM renders WHERE kind='vendor-page' AND"
        " json_extract(scope_json, '$.review_of')=? AND json_extract(scope_json, '$.pos')=?",
        (review_of, pos))]
    rows.sort(key=lambda r: int(r["render_id"][1:]))
    first = [r for r in rows if json.loads(r["scope_json"])["page"] == 1]
    if not first:
        return None, []
    first = first[-1]
    since = int(first["render_id"][1:])
    prior = []
    for p in range(2, page):
        later = [r["render_id"] for r in rows if int(r["render_id"][1:]) > since
                 and json.loads(r["scope_json"])["page"] == p]
        if later:
            prior.append(later[-1])
    return first, [first["render_id"]] + prior


def _vendor_page(conn, review_of, pos, n, quarter, scheduled, item, page):
    """§1 (r11): one vendor's missing invoices, paged. An operator walk lists the vendor's
    whole never_set (left-missing ones marked), so the pages' union is what [Never for X]
    changes; a scheduled walk lists the item's new payments only, with no Never (rev 17).
    Page 1 freezes the pages; later pages copy them. None when nothing is left to list."""
    vendor = item["v"]
    first, prior = (None, [])
    if page > 1:
        first, prior = _vendor_pages_of(conn, review_of, pos, page)
    now = set(_unanswered(conn, vendor, item["pids"]) if scheduled
              else never_set(conn, vendor))
    if first is not None:
        # a later page copies page 1's frozen pages, but lists only the payments still in
        # the walk's set now (review round 1: a page-2 payment matched meanwhile is no
        # missing invoice — never printed under that header, never bound for an exemption);
        # Never's union then differs from the vendor's set, so Never refuses with page 1
        pages = json.loads(first["scope_json"])["pages"]
        pids = [p for pg in pages for p in pg if p in now]
    else:
        pids = sorted(now)
        pages = None
    if not pids:
        return None
    ds = {p: work.describe(conn, p) for p in pids}
    order = sorted(ds.values(), key=lambda d: (d["date"] or "", d["pid"]))
    link = next((d["link"] for d in order if d["link"]), None)
    with views.named(order, quarter):
        if pages is None:
            pages = _pages(vendor, order, pos + 1, n, link, quarter)
        page = max(1, min(page, len(pages)))
        mine = [ds[p] for p in pages[page - 1] if p in ds]
        if not mine:
            return None
        lines = _page_lines(vendor, mine, pos + 1, n, page, len(pages), link, quarter)
        # a later page whose payments changed since page 1 froze it may no longer fit: it
        # binds what it displays whole, and Never then sees a changed union (fresh page 1)
        k = len(mine)
        while k and not _fits(lines[:1 + k] + lines[1 + len(mine):]):
            k -= 1
        lines = lines[:1 + k] + lines[1 + len(mine):]
        shown = mine[:k]
        scope = {"quarter": quarter, "scheduled": scheduled, "review_of": review_of,
                 "pos": pos, "vendor": vendor, "page": page, "pages": pages,
                 "prior": prior if page > 1 else [], **_grammar(shown)}
        return _store(conn, "vendor-page", lines, scope,
                      {d["pid"]: 1 + j for j, d in enumerate(shown)},
                      {d["pid"]: "missing" for d in shown})


def card(conn, review_of, pos, page=1):
    """The Review card for item `pos` of rendering `review_of`'s stored order: a proposal
    card ('review') or a vendor page ('vendor-page'). None when the item has nothing left
    to show."""
    src = json.loads(_row(conn, review_of)["scope_json"])
    order = src.get("order") or []
    if not isinstance(pos, int) or not 0 <= pos < len(order):
        return None
    o = order[pos]
    q, scheduled = src["quarter"], bool(src.get("scheduled"))
    if "p" in o:
        return _proposal_card(conn, review_of, pos, len(order), q, scheduled, o["p"])
    return _vendor_page(conn, review_of, pos, len(order), q, scheduled, o, page)


def next_after(conn, review_of, pos) -> str:
    """§1: every answer posts its successor — the next item of the stored Review order
    still unanswered, else the open-items card ("all answered" when nothing is open)."""
    scope = json.loads(_row(conn, review_of)["scope_json"])
    order = scope.get("order") or []
    for k in range(pos + 1, len(order)):
        o = order[k]
        if "p" in o:
            if work.describe(conn, o["p"])["status"] == "proposed":
                rid = card(conn, review_of, k)
                if rid is not None:
                    return rid
        elif _unanswered(conn, o["v"], o["pids"] if scope.get("scheduled") else None):
            rid = card(conn, review_of, k)
            if rid is not None:
                return rid
    return compose_open(conn, scope["quarter"])


# ---- buttons and the deposit -------------------------------------------------------------

def buttons(conn, r) -> list:
    """The stored calls of a cards rendering, in order, as (label, tool, args, key_spec);
    key_spec is (action, pid, doc_id) (D16: every tap is a keyed `verdict`), and [Get
    package] — the one unkeyed call (#1303) — is always last."""
    scope = json.loads(r["scope_json"])
    rid, kind = r["render_id"], r["kind"]

    def v(label, action, pid=None, doc_id=None):
        args = {"render_id": rid, "action": action}
        if pid is not None:
            args["pid"] = pid
        if doc_id is not None:
            args["doc_id"] = doc_id
        return (label, "verdict", args, (action, pid, doc_id))
    get = ("Get package", "get_package", {"quarter": scope["quarter"]}, None)
    if kind in ("end", "open-items"):
        out = []
        if scope.get("order"):
            out.append(v(f"Review {len(scope['order'])}", "review"))
        if scope.get("confirm_all"):
            out.append(v(f"Confirm all {scope['confirm_all']}", "confirm-all"))
        return out + [get]
    if kind == "ready":
        return [get]
    if kind == "review":
        pid = scope["pid"]
        if scope.get("picks"):
            out = [v(label, "pick", pid, doc) for doc, label in scope["picks"]]
        else:
            out = [v("Confirm", "confirm", pid)]
        return (out + [v("Wrong", "wrong", pid), v("Leave for now", "leave", pid)])[:6]
    if kind == "vendor-page":
        last = scope["page"] == len(scope["pages"])
        out = [v("No invoice needed for these", "exempt-these")]
        if last and not scope.get("scheduled"):
            out.append(v(_label(f"Never for {scope['vendor']}"), "never"))
        out.append(v("Leave missing", "leave-missing"))
        if not last:
            out.append(v("Next page", "next-page"))
        return out
    raise ValueError(kind)


def deposit_of(conn, rid) -> dict:
    """A cards rendering as an operator_proposal deposit (#1302's `next`, or show_view's
    value): its keys minted and stored, posted_seq stamped ("a deposit attempted")."""
    import posting
    assert conn.in_transaction
    r = _row(conn, rid)
    if r["kind"] not in KINDS:
        raise db.Refusal(f"{rid} is not a card")
    scope = json.loads(r["scope_json"])
    out = posting._keyed(conn, rid, buttons(conn, r))
    conn.execute("UPDATE renders SET posted_seq=? WHERE render_id=?", (db.next_seq(conn), rid))
    return {"text": views.deposit_safe(r["text"]),
            "buttons": [{"label": label, "call": {"tool": tool, "arguments": args}}
                        for label, tool, args in out],
            "revision": ("walk:" + scope["review_of"])[:64]}
