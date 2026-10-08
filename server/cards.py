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
import replace
import views
import work

KINDS = ("end", "open-items", "review", "vendor-page", "ready", "replace")
# cards posted with no delivery callback — a tap's `next` (#1302) or show_view(view="open"):
# seen once posted (review round 1 ruling: the closing open-items card included)
TAP_CARDS = ("review", "vendor-page", "open-items")
CONFIRM_ALL_MAX = 24          # §1: with 25 or more proposals it is left out
CANDIDATE_BUTTONS = 4         # §1: up to four named candidates
PAGE_LINES = 25               # a vendor page's payments, then fitted to BODY_LIMIT
LABEL_MAX = 32                # casa:result_broker.py, a button label
BUCKETS = ("matched", "proposed", "missing", "not_needed", "pending")
TAG_WORST = " · 30 Sep 00:00:00"    # views.tag_now: its longest form


class Undisplayed(RuntimeError):
    """A composer asked to bind a payment (or a candidate) its fitted text does not
    display: a bug in that composer's pagination or trimming, never a deposit."""


# ---- the state the surface is composed from -----------------------------------------------

def named_quarter(conn):
    """Ruling Q2b: the quarter the newest run's operator check named (runs.quarter), or
    None when the newest run named none. Runs are only ever inserted (job.claim's INSERT OR
    IGNORE), so the highest rowid is the newest."""
    r = conn.execute("SELECT quarter FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    return r[0] if r is not None and r[0] else None


def main_quarter(conn, job_id=None) -> str:
    """The run's quarter when an operator check named one (ruling Q2: runs.quarter); with
    no run given (a desk view, a desk get_package), the newest run's named quarter (ruling
    Q2b); else D11: the quarter of the latest in-scope payment (today's when none)."""
    if job_id is not None:
        r = conn.execute("SELECT quarter FROM runs WHERE job_id=?", (job_id,)).fetchone()
        if r is not None and r[0]:
            return r[0]
    elif named_quarter(conn):
        return named_quarter(conn)
    days = [dates.effective_date(row) for _, _, row in loop.in_scope(conn)]
    return dates.quarter_of(max(days) if days else dates.today())


def nothing_to_check(conn, q) -> str:
    """Issue #47: a check with no payment in quarter `q`, in plain words — why, and how to
    change it when the books start at or after it (the binding's watermark)."""
    import binding
    wm = binding.get(conn)["watermark"]
    books, wq, ql = dates.long_day(wm), dates.quarter_of(wm), dates.quarter_label(q)
    if q < wq:
        return before_books(conn, q)
    if q == wq:
        prev = dates.quarter_of(dates.add_months(dates.quarter_bounds(wq)[0], -3))
        return (f"Nothing to check for {ql} yet: the bank has no payment in it (the books "
                f"start {books}). Ask me to do {_qn(prev)} to include it.")
    return f"Nothing to check for {ql} yet: the bank has no payment in it."


def before_the_books(conn, q) -> bool:
    """Issue #55: quarter `q` lies before the books' start (the binding's watermark)."""
    import binding
    b = binding.get(conn)
    return b is not None and q < dates.quarter_of(b["watermark"])


def before_books(conn, q) -> str:
    """Issue #55 (the approved script, 1c): where a quarter before the books stands."""
    import binding
    wm = binding.get(conn)["watermark"]
    ql, first = dates.quarter_label(q), dates.quarter_bounds(q)[0]
    return (f"{ql} isn't in the books yet: they start {dates.long_day(wm)}. Ask me to do "
            f"{_qn(q)} and I'll start the books from {dates.short_day(first)}.")


def checked_line(conn, q) -> str:
    """Issue #47: a run's completion text — what the assistant relays: the main quarter's
    counts, or nothing_to_check."""
    c = state(conn)["counts"].get(q, collections.Counter())
    if not sum(c.values()):
        return nothing_to_check(conn, q)
    line = (f"{dates.quarter_label(q)} checked: {c['matched']} matched, {c['proposed']} to "
            f"confirm, {c['missing']} missing")
    return line + (f", {c['pending']} pending" if c["pending"] else "")


def _line_key(d):
    """Proposal line order (§1): vendor (kb.norm), then date, then pid."""
    return (kb.norm(d["vendor"]), d["date"] or "", d["pid"])


def _bucket(d) -> str:
    """§6.1's partition: pending first, then status. An open payment absent from the latest
    bank read (not fresh: decide refuses it until a later import observes it) is counted
    pending, never missing (Task 6/7 carry): each payment in exactly one bucket (§6.1)."""
    if d["pending"] or (d["status"] == "open" and not d["fresh"]):
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


def _missing_of(conn, vendor) -> list:
    """The vendor's missing invoices (§1): its in-scope open payments that expect a
    document, booked, all quarters (left-missing included). What a vendor card's
    [No invoice needed for these] and [Leave missing] act on — never Never's set."""
    return sorted(pid for pid, p, row in loop.in_scope(conn)
                  if kb.norm(loop.vendor_of(conn, row)) == kb.norm(vendor)
                  and p["status"] == "open" and p["exp_kind"] != "none"
                  and row["status"] == "BOOK")


REHEARSAL_RID = "rehearsal"     # a seen rendering that exists only inside the rehearsal


def _outcomes(conn) -> dict:
    return {pid: (p["status"], p["exp_kind"], p["exp_tier"])
            for pid, p, _ in loop.in_scope(conn)}


def never_set(conn, vendor) -> list:
    """§1 (r10) and d1 (Terra S1, ruled: generalize): every in-scope payment [Never for X]
    changes now, learned by REHEARSAL, never by a filter — the counterparty rule
    (kb.set_expectation_in_tx, kind none) applied under a rehearsal grant inside a savepoint
    that is always rolled back; the set is every payment whose status or expectation it
    moved (a pending payment, a proposal, a matched or exempted one included). That exact
    set is what the vendor card lists and what Never binds. The store is settled first, so
    only the rule's own effect is measured. Runs in the caller's transaction (or its own)."""
    import authority
    if not conn.in_transaction:
        with db.tx(conn):
            return never_set(conn, vendor)
    with authority.rehearsal(conn) as grant:
        lineage.settle_all(conn)
        before = _outcomes(conn)
        # the rule's provenance check wants a seen rendering: one that lives and dies here
        conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                     " membership_json, posted_seq) VALUES (?, 'vendor-page', '{}', ?, '',"
                     " '[]', 0)", (REHEARSAL_RID, db.now()))
        kb.set_expectation_in_tx(conn, scope_type="counterparty", scope=vendor, kind="none",
                                 author="operator", render_id=REHEARSAL_RID, grant=grant)
        after = _outcomes(conn)
    return sorted(pid for pid in set(before) | set(after) if before.get(pid) != after.get(pid))


def _unanswered(conn, vendor, only=None) -> list:
    out = [p for p in _missing_of(conn, vendor)
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
        # d2 (Terra S2): a document set aside (irrelevant) since it was stored is not offered
        gone = conn.execute("SELECT irrelevant FROM documents WHERE doc_id=?",
                            (doc_id,)).fetchone()
        return not (gone and gone[0]) and not matches.taken_elsewhere(conn, doc_id, d["pid"])
    if cur is not None:
        return [{"doc": cur["document"], "match_id": cur["match_id"]}] + [
            {"doc": _doc_summary(conn, a), "match_id": None}
            for a in matches.alternatives(conn, cur["match_id"]) if free(a)]
    return [{"doc": c["document"], "match_id": c["match_id"]} for c in d["candidates"]
            if free(c["document"]["doc_id"])]


def _doc_word(doc) -> str:
    return views.field(doc["number"]) if doc.get("number") else \
        views.KIND_WORD.get(doc["kind"], "document")


def _groups(conn, offered) -> list:
    """Issue #52: the offered documents grouped by purchase (documents.purchase, #48's
    unit: the same issuer and number), in offer order."""
    import documents
    groups: list = []
    for c in offered:
        mine = set(documents.purchase(conn, c["doc"]["doc_id"]))
        g = next((g for g in groups if g["ids"] & mine), None)
        if g is None:
            groups.append({"ids": mine, "cs": [c]})
        else:
            g["ids"] |= mine
            g["cs"].append(c)
    return [g["cs"] for g in groups]


def _purchases(conn, offered) -> int:
    """How many purchases the offered documents are."""
    return len(_groups(conn, offered))


def _one_per_purchase(conn, offered) -> list:
    """The offered documents, one per purchase, in offer order (#52 on the Review card):
    the current document when its purchase holds it, else the purchase's invoice, else its
    first. The others are neither displayed nor bound."""
    return [next((c for c in cs if c["match_id"] is not None), None)
            or next((c for c in cs if c["doc"]["kind"] == "invoice"), cs[0])
            for cs in _groups(conn, offered)]


def _proposal_line(conn, i, d) -> str:
    """§1: "{i}. {vendor} · {day} · {amount} ↔ {doc}" — counting purchases, so an invoice
    and its own receipt read as the one document they are (#52)."""
    offered = _offered(conn, d)
    n = _purchases(conn, offered)
    if d["current"] is None and n != 1:
        doc = f"{n} documents fit"                                   # D3: no chosen one
    elif n > 1:
        chosen = offered[0]["doc"]
        doc = f"{n} documents fit; chose {_doc_word(chosen)} ({_day(chosen['date'])})"
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
    text = "\n".join([lines[0] + TAG_WORST] + list(lines[1:]) + ["x" * LEGEND_MAX])
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
    pre = {"names": {}, "refs": {}, "proposed": [], "offers": [], "next": None,
           "walk": None, "pid": None, "pos": -1, "scheduled": False, **scope}
    key = legend(kind, pre)
    lines = list(lines) + ([key] if key else [])
    out, whole = views.fit_lines(lines, tag=views.tag_now())
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


def _questions_line(qs) -> list:
    """Rev 18.4 §R18.3: the handed-over documents whose payment already has one."""
    if not qs:
        return []
    return [f"{_s(len(qs), 'handed-over document')} to check — Review shows "
            f"{'it' if len(qs) == 1 else 'them'}."]


def _summary(conn, kind, quarter, head, proposals, vendors, tail, states, *, scheduled,
             extra_scope=None, questions=()) -> str:
    """The end-message composer (§1), shared by the end message and the open-items card:
    `head` lines, then "To confirm:" and the numbered proposal lines that fit whole (the
    rest behind one closing line: Review shows them, Confirm all is left out), then `tail`.
    The Review order is every replace question (rev 18.4 §R18.3), every proposal in line
    order, then `vendors`."""
    with views.named(proposals, quarter):
        plines = [_proposal_line(conn, i, d) for i, d in enumerate(proposals, 1)]
        before = list(head) + _questions_line(questions) + (["To confirm:"] if proposals
                                                            else [])

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
                 "walk_counts": {"confirm": len(proposals), "questions": len(questions),
                                 "missing": _walk_missing(conn, vendors, quarter, scheduled)},
                 "order": [{"q": q["question_id"]} for q in questions]
                 + [{"p": d["pid"]} for d in proposals] + list(vendors),
                 **_grammar(listed), **(extra_scope or {})}
        return _store(conn, kind, lines, scope, bound, states, docs=docs)


def _nonzero(*parts) -> str:
    """The approved script: counts that are zero are not shown."""
    return " · ".join(f"{n} {word}" for n, word in parts if n)


def _walk_missing(conn, vendors, quarter, scheduled) -> int:
    """r5 (Astra S2): the missing payments the walk's vendor cards show and answer — a
    scheduled walk its items' own; an operator walk each vendor's missing payments of the
    quarter, left-missing ones included (cards._vendor_page acts on exactly those)."""
    if scheduled:
        return sum(len(v["pids"]) for v in vendors)
    return sum(1 for v in vendors for p in _missing_of(conn, v["v"])
               if work.describe(conn, p)["quarter"] == quarter)


def _counts_line(c) -> str:
    return _nonzero((c["matched"], "matched"), (c["not_needed"], "need no invoice"),
                    (c["proposed"], "to confirm"), (c["missing"], "missing"),
                    (c["pending"], "waiting on the bank"))


def _ready_scope(conn, quarters) -> dict:
    qs = sorted(set(quarters))
    return {"ready_quarters": qs,
            "ready_sigs": {q: loop.completion_sig(conn, q) for q in qs}}


def _receipts(conn, docs) -> tuple:
    """§2.5, what the handed documents changed: ("Filed." lines — one per document paired
    or fitting no payment yet —, the proposed payments holding one of them)."""
    head, props = [], []
    for doc in dict.fromkeys(docs):
        hs = matches.holders(conn, doc)
        # f2 (Astra S2): "Paired" only while the payment reduces to matched — a machine match
        # whose document's readings then disagree is a proposal (reducer `amount-unknown`)
        status = {p: conn.execute("SELECT status FROM projections WHERE pid=?",
                                  (p,)).fetchone()[0] for p, _ in hs}
        paired = [p for p, how in hs if how == "matched" and status[p] == "matched"]
        held = [p for p, how in hs if p not in paired]
        if paired:
            d = work.describe(conn, paired[0])
            head.append(f"Filed. Matched to {views.field(d['counterparty'])} · "
                        f"{_day(d['date'])} · {_money(d['amount_minor'], d['currency'])}")
            continue
        if conn.execute("SELECT 1 FROM replace_questions WHERE new_doc_id=? AND state='open'",
                        (doc,)).fetchone():
            continue          # rev 18.4 §R18.3: its question is the "to check" line + card
                              # (e2: retired questions are closed first — compose_end)
        d = work.describe(conn, held[0]) if held else None
        if d is not None and d["status"] == "proposed":
            if all(x["pid"] != d["pid"] for x in props):
                d["vendor"] = d["counterparty"]
                props.append(d)
            continue
        head.append("Filed. No payment fits it yet — it's matched when one does.")
    props.sort(key=_line_key)
    return head, props


def _fit_receipts(lines, before, after) -> list:
    """Every handed document accounted for (review round 1): the "Filed." lines that fit
    whole between `before` and `after`, then one count of the rest."""
    def more(left):
        return f"… and {left} more filed."
    k = _fit_count(before, lines, more, after)
    return lines[:k] + ([more(len(lines) - k)] if k < len(lines) else [])


def _confirm_room(props) -> list:
    """The lines a message's proposals need at the least: their heading and closing line."""
    return (["To confirm:", f"… and {len(props)} more to confirm — Review shows them."]
            if props else [])


def _handover(conn, job_id, docs, quarter, tail, ready, sent=None, scheduled=False) -> str:
    """§1 "A missing invoice the operator has" (§2.5), a standalone continuation (d2: a run
    whose only request is the handover): one line per handed document — what the
    continuation changed — and the proposals among them to confirm."""
    head, props = _receipts(conn, docs)
    head = _fit_receipts(head, [], _confirm_room(props) + list(tail))
    return _summary(conn, "end", quarter, head, props, [], tail,
                    {d["pid"]: item_state(d) for d in props}, scheduled=False,
                    extra_scope={"job_id": job_id, **(sent or {}),
                                 **(_ready_scope(conn, ready) if ready else {})},
                    # e5 (Astra S2): a scheduled run offers only its own run's questions
                    questions=[q for q in replace.open_ones(conn)
                               if not scheduled or q["job_id"] == job_id])


def compose_end(conn, job_id, *, scheduled: bool, handover_docs=(), extra=(), ready=(),
                alerts=(), stopped=None, standalone=True):
    """The run's one end message (kind 'end', §1), or None when a scheduled run has nothing
    new and no extra line (rev 17: "only if it holds an item in a state no delivered
    message showed"). `extra`: failure lines, mirror failures, the partial line — on a
    scheduled run they never make a message alone (§1: the failure line "is otherwise the
    run's one message", the alerts post). `ready`: the quarters whose completion notice is
    owed (D19) — lines of the message when it has items to list, else the message IS the
    completion (compose_ready). `alerts`: the ids of the alerts `extra` prints, marked sent
    when the message is delivered (scope["alerts"], D10). `stopped`: an operator run whose
    pass stopped — its stop line heads the message in place of "checked" (the bank was not
    worked), whatever its alert's state (review round 1). `standalone` (d2, Astra S1): the
    run's only request was the handover — its message shows only what the handed documents
    changed; otherwise (a check, scheduled or the operator's, that a handover joined) the
    full message, the handover's "Filed." receipt lines added after its head."""
    replace.open_ones(conn)        # e2 (Astra S2): stale questions retired before the receipts
    st = state(conn)
    q = main_quarter(conn, job_id)
    open_missing = [d for ds in st["missing"].values() for d in ds if not _answered(d)]
    ready = sorted(set(ready))
    tail = [_ready_line(r, q) for r in ready] + list(extra)
    sent = {"alerts": sorted(alerts)} if alerts else {}
    if stopped and not scheduled:
        tail = [stopped] + tail if handover_docs and standalone else tail
        extra = [stopped] + list(extra)
    if handover_docs and standalone:
        return _handover(conn, job_id, handover_docs, q, tail, ready, sent, scheduled=scheduled)
    receipts = _receipts(conn, handover_docs)[0] if handover_docs else []
    reported = {d["pid"]: item_state(d) for d in st["proposals"]}
    reported.update({d["pid"]: "missing" for ds in st["missing"].values() for d in ds})
    extra_scope = {"job_id": job_id, **sent, **(_ready_scope(conn, ready) if ready else {})}
    if scheduled:
        new_props = [d for d in st["proposals"]
                     if not seen_state(conn, d["pid"], item_state(d))]
        new_miss = [d for d in open_missing if not seen_state(conn, d["pid"], "missing")]
        qs = replace.open_ones(conn)
        new_qs = [x for x in qs if x["job_id"] == job_id]    # asked by this run: new
        if not new_props and not new_miss and not new_qs:
            if ready:
                return compose_ready(conn, ready, extra, alerts=alerts, receipts=receipts)
            if not receipts:
                return None
        earlier = len(st["proposals"]) + len(open_missing) - len(new_props) - len(new_miss)
        head = [f"{_qn(q)} · new: " + _nonzero((len(new_props), "to confirm"),
                                                (len(new_miss), "missing"))]
        if earlier:
            head.append(f"{_s(earlier, 'earlier item')} still open")
        head += _fit_receipts(receipts, head, _confirm_room(new_props) + tail)
        extra_scope["package"] = bool(sum(st["counts"].get(q, collections.Counter()).values()))
        return _summary(conn, "end", q, head, new_props, _vendor_items(new_miss), tail,
                        reported, scheduled=True, extra_scope=extra_scope,
                        questions=new_qs)       # e1 (Astra S2): only this run's new ones
    c = st["counts"].get(q, collections.Counter())
    n = sum(c.values())
    extra_scope["package"] = bool(n)     # r2 (Astra S2): every branch, the early ones too
    all_qs = replace.open_ones(conn)
    # r3 (Astra S2): the card's questions are its quarter's, on every branch
    qs = [x for x in all_qs if work.describe(conn, x["pid"])["quarter"] == q]
    # r4 (Astra S1): the operator's card reports only its quarter's items — another
    # quarter's are one count line, never shown, so never marked seen (the §1 new-state rule
    # still owes them their own card)
    reported = {p: v for p, v in reported.items()
                if work.describe(conn, p)["quarter"] == q}
    if not st["proposals"] and not open_missing:
        if ready and not all_qs:
            return compose_ready(conn, ready, extra, alerts=alerts, receipts=receipts)
        if not c["pending"]:
            # issue #47: no payment at all in the quarter says why, never "all accounted for"
            first = ([stopped] if stopped else
                     [views.esc(nothing_to_check(conn, q))] if not n else
                     [f"{_qn(q)} checked · {_s(n, 'payment')} · all accounted for."])
            rest = list(extra[1:] if stopped else extra)
            first += _fit_receipts(receipts, first, rest)
            return _summary(conn, "end", q, first, [], [], rest, reported,
                            scheduled=False, extra_scope=extra_scope, questions=qs)
    # d1 (Astra S2): the approved script's 2a card is the 1a card — the run's quarter's own
    # items, every other quarter one line
    props = [d for d in st["proposals"] if d["quarter"] == q]
    mine = [d for d in open_missing if d["quarter"] == q]
    earlier = _other_quarters(st, open_missing, q)
    if not n:
        head = [stopped or views.esc(nothing_to_check(conn, q))]
    elif not props and not mine and not qs and not c["pending"]:
        head = [stopped or f"{_qn(q)} checked · {_s(n, 'payment')} · all accounted for."]
    else:
        head = [stopped or f"{_qn(q)} checked · {_s(n, 'payment')}", _counts_line(c)]
    head += _fit_receipts(receipts, head, _confirm_room(props) + earlier + tail)
    return _summary(conn, "end", q, head, props, _vendor_items(mine),
                    earlier + tail, reported, scheduled=False, extra_scope=extra_scope,
                    questions=qs)


def _other_quarters(st, open_missing, q) -> list:
    """One line per OTHER quarter with open items, non-zero parts only (the approved
    script): "Q2 still open: …" before `q`, "Q4 so far: …" after it."""
    out = []
    for eq in sorted(st["counts"]):
        if eq == q:
            continue
        a = sum(1 for d in st["proposals"] if d["quarter"] == eq)
        b = sum(1 for d in open_missing if d["quarter"] == eq)
        if a or b:
            out.append(f"{_qn(eq, q)} {'still open' if eq < q else 'so far'}: "
                       + _nonzero((a, "to confirm"), (b, "missing")))
    return out


def compose_open(conn, quarter, *, scheduled=False) -> str:
    """The quarter status card (#55, the approved script 1a/1b; the open-items card of §1
    r11): where `quarter` stands — its payments and non-zero counts, ITS proposals and
    missing invoices (Review walks only those), one line per other quarter with open items,
    and [Get package] only when the quarter has a payment in the books. `b` counts the
    unanswered missing payments only ([Leave missing] is an answer)."""
    st = state(conn)
    open_missing = [d for ds in st["missing"].values() for d in ds if not _answered(d)]
    props = [d for d in st["proposals"] if d["quarter"] == quarter]
    mine = [d for d in open_missing if d["quarter"] == quarter]
    reported = {d["pid"]: item_state(d) for d in props + mine}
    qs = [x for x in replace.open_ones(conn)
          if work.describe(conn, x["pid"])["quarter"] == quarter]
    c = st["counts"].get(quarter, collections.Counter())
    n = sum(c.values())
    if not n:
        head = [views.esc(nothing_to_check(conn, quarter))]
    elif not props and not mine and not qs and not c["pending"]:
        head = [f"{_qn(quarter)} · {_s(n, 'payment')} · all accounted for"]
    else:
        head = [f"{_qn(quarter)} · {_s(n, 'payment')}", _counts_line(c)]
    return _summary(conn, "open-items", quarter, head, props, _vendor_items(mine),
                    _other_quarters(st, open_missing, quarter), reported,
                    scheduled=scheduled, questions=qs, extra_scope={"package": bool(n)})


def compose_ready(conn, quarters: list, extra=(), alerts=(), receipts=()) -> str:
    """The "package ready" notice (§1, D19): the latest owed quarter heads it with [Get
    package]; `receipts` (d2: the "Filed." lines of a handover the completing check took);
    each earlier quarter is a line; then `extra`. Delivery records each completion
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
    after = [_ready_line(q, latest) for q in qs[:-1]] + list(extra)
    lines = [head] + _fit_receipts(list(receipts), [head], after) + after
    return _store(conn, "ready", lines, {"quarter": latest, "order": [],
                                         **({"alerts": sorted(alerts)} if alerts else {}),
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
    offered = _one_per_purchase(conn, _offered(conn, d))
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
                                   f" ({_day(c['doc']['date'])})")]
                           for c in shown] if picks else [],
                 "proposed": [pid] if d["current"] is not None and shown else [],
                 # PLAY 0.11.2: the Confirm legend names the document's own kind
                 "doc_word": views.KIND_WORD.get(
                     (d["current"]["document"] if d["current"] is not None
                      else shown[0]["doc"] if shown else {}).get("kind"), "document"),
                 **_grammar([d])}
        return _store(conn, "review", lines, scope, {pid: 1}, {pid: item_state(d)},
                      docs={pid: docs})


def live_question_card(conn, review_of, pos, pid):
    """e2 (Astra S2): the payment's live question's card, in place of a superseded one —
    None when it has none."""
    live = [q for q in replace.open_ones(conn) if q["pid"] == pid]
    if not live:
        return None
    src = json.loads(_row(conn, review_of)["scope_json"])
    return _replace_card(conn, review_of, pos, len(src.get("order") or []) or 1,
                         src["quarter"], bool(src.get("scheduled")), live[-1]["question_id"])


def _replace_card(conn, review_of, pos, n, quarter, scheduled, qid):
    """Rev 18.4 §R18.3: "<payment> already has an invoice. Current: … (matched by the job |
    confirmed by you | suggested by the job). New: … (from you)." [Keep current] [Use new].
    Bound to the payment and the displayed pairing (render_items: 18.4). None when the
    question is answered, superseded, or its payment holds another pairing now."""
    qn = replace.get(conn, qid)
    if qn is None or qn["state"] != "open":
        return None
    cur = replace.current(conn, qn["pid"])
    if cur is None or cur[0] != qn["match_id"]:
        return None
    d = work.describe(conn, qn["pid"])
    old = conn.execute("SELECT doc_id FROM match_state WHERE match_id=?",
                       (cur[0],)).fetchone()[0]
    how = ("confirmed by you" if cur[2] == "operator" else
           "matched by the job" if cur[1] == "matched" and d["status"] == "matched"
           else "suggested by the job")           # f2: a demoted match is a suggestion

    def doc_line(doc_id):
        x = _doc_summary(conn, doc_id)
        number = f" {views.field(x['number'])}" if x.get("number") else ""
        return (f"{views.KIND_WORD.get(x['kind'], 'document')}{number} · {_day(x['date'])}"
                f" · {_money(x['amount_minor'], x['currency'])}")
    lines = [f"Card {pos + 1} of {n} · to check",
             f"{views.headline(d, quarter)} already has an invoice.",
             f"Current: {doc_line(old)} ({how}).",
             f"New: {doc_line(qn['new_doc_id'])} (from you)."]
    scope = {"quarter": quarter, "scheduled": scheduled, "review_of": review_of, "pos": pos,
             "pid": qn["pid"], "question_id": qid, "new_doc_id": qn["new_doc_id"],
             "new_doc_fp": replace.new_doc_fp(conn, qn["new_doc_id"])}
    return _store(conn, "replace", lines, scope, {qn["pid"]: 1},
                  {qn["pid"]: item_state(d)}, docs={qn["pid"]: {cur[0]: 2}})


def _mark(d) -> str:
    """A vendor page line's state (d1 ruling: the card lists every payment Never changes,
    and says which of them are not missing invoices)."""
    b = _bucket(d)
    if b == "missing":
        return " · left missing" if _answered(d) else ""
    return {"pending": " · waiting on the bank", "proposed": " · to confirm",
            "matched": " · matched",
            "not_needed": " · no invoice needed"}.get(b, "")


def _payee_free(d, quarter) -> str:
    """PLAY 0.11.2: a vendor card's line without the bank's payee text — the card's head
    names the vendor once (the bank writes it in several forms: "Belastingdienst",
    "BELASTINGDIENST"); the rest of the headline (amount, date, kind, ref) identifies it."""
    h, payee = views.headline(d, quarter), views.field(d["counterparty"])
    return h[len(payee) + 3:] if payee and h.startswith(payee + " · ") else h


def _page_lines(vendor, ds, i, n, p, pages, link, quarter) -> list:
    """A vendor page's FINAL lines — what _store fits and what _pages measures (one
    function, so the measure is the text)."""
    head = f"Card {i} of {n} · missing invoices · {views.field(vendor)}"
    if pages > 1:
        head += f" · page {p} of {pages}"
    body = [_payee_free(d, quarter) + _mark(d) for d in ds]
    return [head] + body + ([views.field(link, views.LINK_MAX)] if link else [])


def _pages(vendor, ds, i, n, link, quarter, extra=()) -> list:
    """Greedy pages of ≤ PAGE_LINES payments, each measured on its COMPLETE final text: the
    page's real lines (_page_lines: escaped vendor name, "· left missing" suffixes, the
    link) with the worst-case page numbers and the worst-case rendering tag (plan round 7,
    Astra S1: a 60-character punctuated vendor name and 24 suffixes overflowed a page
    measured without them, and its 25th payment was bound but cut)."""
    worst = max(len(ds), 2)

    def fits(trial):
        # r5 (Astra S2): with the lines a page carries after its payments (§B's count line,
        # in its longest, switched-on form), so the fit never drops a frozen page's payment
        return _fits(_page_lines(vendor, trial, i, n, worst, worst, link, quarter)
                     + list(extra))
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


def _others_line(others, others_missing, quarter, on) -> list:
    """§B: the vendor's payments of OTHER quarters a quarter's card counts (operator ruling
    2026-10-08), stated plainly — every one [Never] would change (d2 Astra + Terra S1), and
    how many are missing; with the switch on, that the answers cover the missing ones."""
    if not others:
        return []
    qs = ", ".join(_qn(q, quarter) for q in sorted(set(others.values())))
    n, m = len(others), len(others_missing)
    line = (f"Also missing in other quarters: {m} ({qs})" if m == n
            else f"Also in other quarters: {_s(n, 'payment')}"
                 + (f", {m} missing" if m else "") + f" ({qs})")
    return [line + (" · answers will cover them" if on and m else "")]


def _also_line(vendor, also) -> list:
    """PLAY 0.11.2: the quarter's other payments [Never] would change, as one line."""
    if not also:
        return []
    n = len(also)
    return [f"Never for {views.field(vendor)} would also change {n} more "
            f"payment{'s' if n != 1 else ''} of this quarter."]


def _vendor_page(conn, review_of, pos, n, quarter, scheduled, item, page, frozen=None):
    """§1 (r11), 0.11.2 §B: one vendor's missing invoices, paged. An operator walk lists the
    vendor's never_set (the rehearsed set Never changes, d1 ruling) OF THE WALK'S QUARTER,
    each marked; its payments of other quarters are frozen on page 1 as `others` (pid ->
    quarter) with `others_rev` (their projection revisions) and `others_missing` (the
    unanswered missing ones), and stated in one line. [Never for X] binds the pages' union
    plus `others`; with the switch on (`all_quarters`, the [Apply to all quarters] tap) [No
    invoice needed for these] and [Leave missing] act on the page's missing lines plus
    `others_missing`. A scheduled walk lists the item's new missing payments only, with no
    Never and no switch (rev 17). Page 1 freezes the pages; later pages and a switched
    card (`frozen`, the tapped card's scope) copy them. None when the vendor has no missing
    invoice left to list."""
    vendor = item["v"]
    first, prior = (None, [])
    if page > 1:
        first, prior = _vendor_pages_of(conn, review_of, pos, page)
    missing = set(_missing_of(conn, vendor))
    allset = set(_unanswered(conn, vendor, item["pids"]) if scheduled
                 else never_set(conn, vendor))
    quarter_of = {p: work.describe(conn, p)["quarter"] for p in allset}
    here = {p for p in allset if scheduled or quarter_of[p] == quarter}
    # PLAY 0.11.2: the card's subject is its missing payments; the quarter's other payments
    # Never would change are one summary line (`also`, bound by membership like `others`)
    now = {p for p in here if p in missing}
    src = frozen or (json.loads(first["scope_json"]) if first is not None else None)
    if src is not None:
        # a later page (or the switched card) copies page 1's frozen pages and others, but
        # lists only the payments still in the walk's set now; Never's union then differs
        # from the vendor's set when the set changed, so Never refuses with page 1
        pages = src["pages"]
        pids = [p for pg in pages for p in pg if p in now]
        others = {int(k): v for k, v in (src.get("others") or {}).items()}
        others_rev = {int(k): v for k, v in (src.get("others_rev") or {}).items()}
        others_missing = list(src.get("others_missing") or [])
        also = [int(p) for p in src.get("also") or []]
    else:
        pids = sorted(now)
        pages = None
        others = {p: quarter_of[p] for p in sorted(allset - here)}
        others_rev = {p: lineage.projection(conn, p)["revision"] for p in others}
        others_missing = [p for p in others if p in missing and
                          lineage.projection(conn, p)["search_state"] != "accepted-missing"]
        also = sorted(here - now)
    on = bool(frozen and frozen.get("all_quarters"))
    if not pids or not (now & missing):
        return None
    ds = {p: work.describe(conn, p) for p in pids}
    order = sorted(ds.values(), key=lambda d: (d["date"] or "", d["pid"]))
    link = next((d["link"] for d in order if d["link"]), None)
    extra = [] if scheduled else (_also_line(vendor, also)
                                  + _others_line(others, others_missing, quarter, on))
    with views.named(order, quarter):
        if pages is None:
            worst = [] if scheduled else (_also_line(vendor, also)
                                          + _others_line(others, others_missing, quarter, True))
            pages = _pages(vendor, order, pos + 1, n, link, quarter, worst)
        page = max(1, min(page, len(pages)))
        mine = [ds[p] for p in pages[page - 1] if p in ds]
        if not mine:
            return None
        lines = _page_lines(vendor, mine, pos + 1, n, page, len(pages), link, quarter)
        # a later page whose payments changed since page 1 froze it may no longer fit: it
        # binds what it displays whole, and Never then sees a changed union (fresh page 1)
        k = len(mine)
        while k and not _fits(lines[:1 + k] + lines[1 + len(mine):] + extra):
            k -= 1
        lines = lines[:1 + k] + lines[1 + len(mine):] + extra
        shown = mine[:k]
        acts = [d["pid"] for d in shown if d["pid"] in missing]
        scope = {"quarter": quarter, "scheduled": scheduled, "review_of": review_of,
                 "pos": pos, "vendor": vendor, "page": page, "pages": pages,
                 "missing": acts, "prior": prior if page > 1 else [],
                 "others": {str(p): q for p, q in others.items()},
                 "others_rev": {str(p): r for p, r in others_rev.items()},
                 "others_missing": others_missing, "all_quarters": on, "also": also,
                 **_grammar(shown)}
        bound = {d["pid"]: 1 + j for j, d in enumerate(shown)}
        also_line = _also_line(vendor, also)
        if also_line and also_line[0] in lines:
            # PLAY 0.11.2: the quarter's other payments Never would change are stated on one
            # line and bound to it at their revision now, so Never refuses one that changed
            # (as when it was a listed line)
            bound.update({p: lines.index(also_line[0]) for p in also})
        if others_missing and not scheduled:
            # §B: the stated other-quarter missing payments are bound to their count line
            # (displayed whole), at their revisions now; one that moved since page 1 froze
            # them is refused at the tap (taps._vendor_answer)
            bound.update({p: len(lines) - 1 for p in others_missing})
        return _store(conn, "vendor-page", lines, scope, bound,
                      {p: "missing" for p in acts})


def switched(conn, rid, on: bool):
    """§B: the vendor card `rid` again, its switch set to `on` — the same page, the same
    frozen pages and others (the [Apply to all quarters] / [✓ All quarters] tap)."""
    r = _row(conn, rid)
    sc = json.loads(r["scope_json"])
    src = json.loads(_row(conn, sc["review_of"])["scope_json"])
    item = (src.get("order") or [])[sc["pos"]]
    return _vendor_page(conn, sc["review_of"], sc["pos"], len(src["order"]), sc["quarter"],
                        bool(sc.get("scheduled")), item, sc["page"],
                        frozen={**sc, "all_quarters": on})


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
    if "q" in o:
        return _replace_card(conn, review_of, pos, len(order), q, scheduled, o["q"])
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
        if "q" in o:
            qn = replace.get(conn, o["q"])
            if qn is not None and qn["state"] == "open":
                rid = card(conn, review_of, k)
                if rid is not None:
                    return rid
        elif "p" in o:
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
    return _buttons(r["render_id"], r["kind"], json.loads(r["scope_json"]))


def _buttons(rid, kind, scope) -> list:
    """buttons() from a rendering's id, kind and scope: _store reads the labels before the
    row exists, for the legend line (the approved script)."""

    def v(label, action, pid=None, doc_id=None):
        args = {"render_id": rid, "action": action}
        if pid is not None:
            args["pid"] = pid
        if doc_id is not None:
            args["doc_id"] = doc_id
        return (label, "verdict", args, (action, pid, doc_id))
    get = [("Get package", "get_package", {"quarter": scope["quarter"]}, None)] \
        if scope.get("package", True) else []
    if kind in ("end", "open-items"):
        out = []
        if scope.get("order"):
            out.append(v("Review", "review"))
        if scope.get("confirm_all"):
            out.append(v("Confirm all", "confirm-all"))
        return out + get
    if kind == "ready":
        return get
    if kind == "replace":
        pid, doc = scope["pid"], scope["new_doc_id"]
        return [v("Keep current", "keep-current", pid, doc), v("Use new", "use-new", pid, doc)]
    if kind == "review":
        pid = scope["pid"]
        if scope.get("picks"):
            out = [v(label, "pick", pid, doc) for doc, label in scope["picks"]]
        else:
            out = [v("Confirm", "confirm", pid)]
        return (out + [v("Wrong", "wrong", pid), v("Leave for now", "leave", pid)])[:6]
    if kind == "vendor-page":
        last = scope["page"] == len(scope["pages"])
        acts = scope.get("missing", True)       # a page with no missing line: no exemption
        out = [v("No invoice needed for these", "exempt-these")] if acts else []
        if last and not scope.get("scheduled"):
            out.append(v(_label(f"Never for {scope['vendor']}"), "never"))
        if acts:
            out.append(v("Leave missing", "leave-missing"))
        if not last:
            out.append(v("Next page", "next-page"))
        elif acts and scope.get("others_missing") and not scope.get("scheduled"):
            # §B: the one switch (operator ruling 2026-10-08)
            # PLAY 0.11.2: no label starts with a mark (Casa adds "☑ <label>" to a tapped card)
            out.append(v("Only this quarter", "this-quarter") if scope.get("all_quarters")
                       else v("Apply to all quarters", "all-quarters"))
        return out + [v("Leave for now", "leave-vendor")]
    raise ValueError(kind)


LEGEND = {"review": "Review: go through {walk}, one at a time",
          "confirm-all": "Confirm all: accept the invoices listed above",
          "pick": "{label}: use this document",
          "confirm": "Confirm: this {kind} is right",
          "wrong": "Wrong: not this one, keep looking",
          "leave": "Leave for now: decide later",
          "exempt-these": "No invoice needed: these need none",
          "exempt-these-all": "No invoice needed: these and the {others} in other quarters "
                              "need none",
          "never": "{label}: {vendor} never sends one, in any quarter",
          "leave-missing": "Leave missing: stop looking, keep them missing",
          "leave-missing-all": "Leave missing: these and the {others} in other quarters",
          "all-quarters": "Apply to all quarters: your next answer here also covers the "
                          "{others} in other quarters",
          "this-quarter": "Only this quarter: switch back",
          "leave-vendor": "Leave for now: decide later",
          "next-page": "Next page: the rest of this vendor",
          "keep-current": "Keep current: keep the filed invoice",
          "use-new": "Use new: use the new one"}


def legend(kind, scope) -> str:
    """The approved script: a card that carries buttons ends with ONE short plain line
    saying what each button shown does — the buttons actually shown, in order, from the
    same function that makes them. Picks share one entry."""
    parts, picked = [], False
    for label, tool, args, _ in _buttons("r0", kind, scope):
        if tool == "get_package":
            parts.append(f"Get package: the {_qn(scope['quarter'])} zip for your accountant")
            continue
        action = args["action"]
        if action == "pick":
            # PLAY 0.11.2: one plain entry for every pick button
            if not picked:
                parts.append("A document button: use that document")
            picked = True
            continue
        if action in ("exempt-these", "leave-missing") and scope.get("all_quarters"):
            action += "-all"
        parts.append(LEGEND[action].format(label=label, walk=walk_words(scope),
                                           kind=scope.get("doc_word") or "document",
                                           others=len(scope.get("others_missing") or []),
                                           vendor=views.field(scope.get("vendor") or "")))
    return " · ".join(parts)


def walk_words(scope, sep=" and ") -> str:
    """BRAIN/operator 2026-10-08: a count on a button must be a number the card shows, so
    [Review] carries none; PLAY 0.11.2: one thing, one number — the legend and the Review
    receipt print only the counts line's own number ("the 8 to confirm") and name the
    missing invoices without one (the walk's vendor cards may cover fewer or more payments
    than the counts line's "missing")."""
    w = scope.get("walk_counts") or {}
    q = w.get("questions", 0)
    parts = ([f"the {w['confirm']} to confirm"] if w.get("confirm") else []) \
        + ([f"the {q} handed-over document{'s' if q != 1 else ''}"] if q else []) \
        + (["the missing invoices"] if w.get("missing") else [])
    if not parts:
        return "each open item"
    return sep.join(parts) if len(parts) <= 2 else ", ".join(parts[:-1]) + sep + parts[-1]


LEGEND_MAX = 400          # the longest legend line: the vendor page's, its name clipped


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
