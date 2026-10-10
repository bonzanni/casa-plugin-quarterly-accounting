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
BUCKETS = ("matched", "proposed", "missing", "not_needed", "pending", "unclassified")
NUMBER_SHOWN = 16       # #106: a document number at most this long is shown on its line
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
        if r is not None and loop.handover_only(conn, job_id):
            return handover_quarter(conn, job_id)[0]       # #67: never the newest payment
    elif named_quarter(conn):
        return named_quarter(conn)
    days = [dates.effective_date(row) for _, _, row in loop.in_scope(conn)]
    return dates.quarter_of(max(days) if days else dates.today())


def _backed(conn, doc_id) -> list:
    """The payments a handed document backs now: a pairing or proposal holding it, or the
    open question asking to swap it in (rev 18.4 §R18.3)."""
    out = [p for p, _how in matches.holders(conn, doc_id)]
    out += [r[0] for r in conn.execute("SELECT pid FROM replace_questions WHERE new_doc_id=?"
                                       " AND state='open'", (doc_id,))]
    return list(dict.fromkeys(out))


def handover_quarter(conn, job_id, docs=None) -> tuple:
    """#67: the quarter of a run that took only handovers, and whether it came from the
    handed documents themselves — (the earliest quarter of a payment a handed document
    backs, True); else (the quarter in play: the newest run that named one, False); else
    (D11's newest-payment quarter, False), a label only: the card then offers no package."""
    qs = sorted({work.describe(conn, p)["quarter"]
                 for d in (loop.run_handover_docs(conn, job_id) if docs is None else docs)
                 for p in _backed(conn, d)})
    if qs:
        return qs[0], True
    r = conn.execute("SELECT quarter FROM runs WHERE quarter IS NOT NULL ORDER BY rowid DESC"
                     " LIMIT 1").fetchone()
    if r is not None:
        return r[0], False
    days = [dates.effective_date(row) for _, _, row in loop.in_scope(conn)]
    return dates.quarter_of(max(days) if days else dates.today()), False


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
    line += f", {c['unclassified']} not classified yet" if c["unclassified"] else ""
    return line + (f", {c['pending']} pending" if c["pending"] else "")


def _line_key(d):
    """Proposal line order (§1): vendor (kb.norm), then date, then pid."""
    return (kb.norm(d["vendor"]), d["date"] or "", d["pid"])


def unclassified(d) -> bool:
    """#98, #105: an open payment whose classification is not settled — no tag or parked
    (row 4), or tags that conflict (row 5): its expectation's kind is unknown, so it is no
    missing invoice: what it is, is not known yet."""
    return d["status"] == "open" and d["expectation"]["kind"] is None


def _bucket(d) -> str:
    """§6.1's partition: pending first, then status. An open payment absent from the latest
    bank read (not fresh: decide refuses it until a later import observes it) is counted
    pending, never missing (Task 6/7 carry): each payment in exactly one bucket (§6.1)."""
    if views.waiting(d):
        return "pending"
    if unclassified(d):
        return "unclassified"
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
            "pending": by_bucket["pending"], "unclassified": by_bucket["unclassified"],
            "counts": counts, "by_bucket": by_bucket}


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
                  if kb.same_vendor(conn, loop.vendor_of(conn, row), vendor)
                  and p["status"] == "open" and p["exp_kind"] != "none"
                  and p["exp_kind"] is not None          # #98 #105: unsettled, not missing
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
        doc = word          # #99: short names, never a raw document number
        other = c.get("currency") and d["currency"] and c["currency"] != d["currency"]
        if other and c.get("issuer"):
            # #102: the amounts differ, so the line names whose document it is
            doc = f"{views.field(c['issuer'])} {word}"
        if c.get("number") and len(c["number"]) <= NUMBER_SHOWN:
            # #106: a short document number helps find it; a provider's long id does not
            doc += f" {views.field(c['number'])}"
        doc += f" · {_money(c['amount_minor'], c['currency'])}"
        if other:
            doc += " (other currency)"
    return (f"{i}. {views.field(views.shown(d))} · {_day(d['date'])} · "
            f"{_money(d['amount_minor'], d['currency'])} ↔ {doc}")


def _ready_line(q, ref) -> str:
    return f"{_qn(q, ref)} complete · package ready"


# ---- fitting and storing ---------------------------------------------------------------------

def _fits(lines) -> bool:
    """The COMPLETE final text with the worst-case rendering tag fits a card (plan round 7:
    measured on the real lines, never on an estimate)."""
    if not lines:
        return True
    text = "\n".join([views.title(lines[0]) + TAG_WORST] + list(lines[1:]))
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
    names = {str(d["pid"]): views.field_raw(views.shown(d)) for d in ds}
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
    # #99: line 1 is the card's title; the buttons say what they do (no legend)
    lines = [views.title(lines[0]), *lines[1:]] if lines else []
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


UNCL_SHOWN = 3      # #111: the not-classified payments a status card names, then a count


def _summary(conn, kind, quarter, head, proposals, vendors, tail, states, *, scheduled,
             extra_scope=None, questions=(), unclassified=()) -> str:
    """The end-message composer (§1), shared by the end message and the open-items card:
    `head` lines, then "To confirm:" and the numbered proposal lines that fit whole (the
    rest behind one closing line: Review shows them, Confirm all is left out), then `tail`.
    The Review order is every replace question (rev 18.4 §R18.3), every proposal in line
    order, then `vendors`."""
    with views.named(list(proposals) + list(unclassified), quarter):
        plines = [_proposal_line(conn, i, d) for i, d in enumerate(proposals, 1)]
        shown_uncl = list(unclassified[:UNCL_SHOWN])
        if unclassified:
            # #111: the card names the payments not classified yet and asks what each is
            uncl = [f"{views.headline(d, quarter)} — {views.WHAT_IS_IT}"
                    for d in unclassified[:UNCL_SHOWN]]
            left = len(unclassified) - len(uncl)
            uncl += [f"+{left} more not shown"] if left else []
            tail = [views.title("Not classified yet"), *uncl, *([""] if tail else []), *tail]
        # #99: the title and facts, then the question on its own line above the list its
        # buttons refer to, then the rest — one blank line between the groups
        before = views.groups(head, _questions_line(questions),
                              [""] if proposals else [])
        if proposals:
            before[-1] = views.title(CONFIRM_Q)
        after = views.groups([""], tail)[1:] if tail else []

        def closing(left):
            return f"… and {left} more to confirm — Review shows them."
        k = _fit_count(before, plines, closing, after)
        lines = before + plines[:k] + ([closing(len(plines) - k)] if k < len(plines) else []) \
            + after
        listed = proposals[:k]
        bound = {d["pid"]: len(before) + j for j, d in enumerate(listed)}
        # r1 (Astra S2): a not-classified line is bound, so a reply about it binds as on the
        # views; its lines follow the blank line and the title that open `after`
        at = len(lines) - len(after) + 2
        # r2 (Astra S2): only those the card prints whole (as _store will fit it) — a card
        # crowded by receipts cuts its tail, and a cut bound line would lose the card
        whole = views.fit_lines([views.title(lines[0]), *lines[1:]],
                                tag=views.tag_now())[1] if lines else 0
        shown_uncl = [d for j, d in enumerate(shown_uncl) if at + j < whole]
        bound.update({d["pid"]: at + j for j, d in enumerate(shown_uncl)})
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
                 **_grammar(listed + shown_uncl), **(extra_scope or {})}
        return _store(conn, kind, lines, scope, bound, states, docs=docs)


CONFIRM_Q = "Confirm these matches?"       # #99: the question the Review / Confirm all refer to


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
                    (c["pending"], "waiting on the bank"),
                    (c["unclassified"], "not classified yet"))         # #98


def _ready_scope(conn, quarters) -> dict:
    qs = sorted(set(quarters))
    return {"ready_quarters": qs,
            "ready_sigs": {q: loop.completion_sig(conn, q) for q in qs}}


def _doc_name(d) -> str:
    """#67: a handed document as read — issuer, number, date, amount (what is known); the
    file's own name when nothing was read. Displayed (escaped)."""
    # #99: a short name — the number only when nothing else names it
    who = d["issuer"] or d["counterparty"] or d["document_number"]
    parts = [views.field(who or d["original_name"] or f"document {d['doc_id']}")]
    if d["document_date"]:
        parts.append(_day(d["document_date"]))
    if d["amount_minor"] is not None and d["currency"] and not d["amount_conflict"]:
        parts.append(_money(d["amount_minor"], d["currency"]))
    return " · ".join(parts)


def _pending_of(conn, doc):
    """#72: the earliest in-scope payment still pending at the bank of exactly the document's
    amount and currency, inside its window (loop.in_window) — or None."""
    rows = [row for _p, _proj, row in loop.in_scope(conn)
            if row["status"] != "BOOK" and row["currency"] == doc["currency"]
            and row["amount_minor"] == doc["amount_minor"]
            and loop.in_window(row, doc["document_date"])]
    return min(rows, key=lambda r: dates.effective_date(r) or "") if rows else None


def _receipts(conn, docs, job_id=None) -> tuple:
    """§2.5, what the handed documents changed (#67: one line per document, naming it as
    read, with what the run computed for it — never a result it did not compute): matched
    to a payment, a copy of an earlier document, not read,
    or the payments it was checked against; a proposed one is only its "To confirm" line
    (#96). Returns (those lines, the proposed payments holding one of them)."""
    import documents
    head, props = [], []
    for doc in dict.fromkeys(docs):
        row = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc,)).fetchone()
        name = _doc_name(row)
        dup = documents.duplicate_of(conn, doc)
        if dup is not None:
            head.append(f"{name}: already filed as {views.esc(f'#{dup}')}.")
            continue
        hs = matches.holders(conn, doc)
        # f2 (Astra S2): "Paired" only while the payment reduces to matched — a machine match
        # whose document's readings then disagree is a proposal (reducer `amount-unknown`)
        status = {p: conn.execute("SELECT status FROM projections WHERE pid=?",
                                  (p,)).fetchone()[0] for p, _ in hs}
        paired = [p for p, how in hs if how == "matched" and status[p] == "matched"]
        held = [p for p, how in hs if p not in paired]
        if paired:
            d = work.describe(conn, paired[0])
            # operator ruling 2026-10-09: an automatic choice is unmistakable on the card
            auto = conn.execute("SELECT 1 FROM match_state WHERE pid=? AND doc_id=? AND"
                                " state='matched' AND author<>'operator'",
                                (paired[0], doc)).fetchone() is not None
            head.append(f"{name}: matched {'automatically ' if auto else ''}to the "
                        f"{_day(d['date'])} {_money(d['amount_minor'], d['currency'])} payment "
                        f"({views.field(views.shown(d))}).")
            continue
        if conn.execute("SELECT 1 FROM replace_questions WHERE new_doc_id=? AND state='open'",
                        (doc,)).fetchone():
            # rev 18.4 §R18.3: its question is the "to check" line + card (e2: retired
            # questions are closed first — compose_end)
            head.append(f"{name}: its payment already has a document — Review asks which "
                        "to keep.")
            continue
        d = work.describe(conn, held[0]) if held else None
        if d is not None and d["status"] == "proposed":
            # #96: said once — its line in the numbered "To confirm" list, which Review and
            # Confirm all refer to, names the document and the payment
            if all(x["pid"] != d["pid"] for x in props):
                d["vendor"] = d["counterparty"]
                props.append(d)
            continue
        if row["read_at"] is None:
            head.append(f"{name}: could not be read, so it was not matched — send it again "
                        "to retry.")
            continue
        if documents.amount_unknown(row):
            head.append(f"{name}: its amount could not be read, so it was not matched.")
            continue
        r = conn.execute("SELECT fits FROM run_docs WHERE job_id=? AND doc_id=?",
                         (job_id, doc)).fetchone() if job_id is not None else None
        n = r[0] if r is not None else None
        money = _money(row["amount_minor"], row["currency"])
        if n:
            head.append(f"{name}: checked against {_s(n, 'payment')} it could fit — not "
                        "matched.")
        elif n == 0:
            waiting = _pending_of(conn, row)
            if waiting is not None:
                # #72 (d2, Astra S2): a payment of that amount the bank has not booked yet —
                # named as a possibility, never as the document's own
                head.append(f"{name}: not matched yet — a payment of {money} to "
                            f"{views.field(loop.vendor_of(conn, waiting))} on "
                            f"{_day(dates.effective_date(waiting))} is still pending at the bank.")
            else:
                head.append(f"{name}: no payment of {money} in the books yet — it's matched "
                            "when one comes in.")
        else:
            head.append(f"{name}: not matched yet.")
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
    """The lines a message's proposals need at the least: a blank line, the question and the
    closing line."""
    return (["", views.title(CONFIRM_Q), f"… and {len(props)} more to confirm — Review shows "
             "them."] if props else [])


def _handover(conn, job_id, docs, quarter, tail, ready, sent=None, scheduled=False) -> str:
    """§1 "A missing invoice the operator has" (§2.5), a standalone continuation (d2: a run
    whose only request is the handover): one line per handed document — what the
    continuation changed — and the proposals among them to confirm."""
    head, props = _receipts(conn, docs, job_id)
    n = len(dict.fromkeys(docs))
    top = [f"{_s(n, 'document')} you sent"]          # #99: the card's title
    head = top + _fit_receipts(head, top, _confirm_room(props) + list(tail))
    return _summary(conn, "end", quarter, head, props, [], tail,
                    {d["pid"]: item_state(d) for d in props}, scheduled=False,
                    # #94: a job's end card never offers the package
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
    # issue #57: [Invoice links] while the quarter has missing invoices
    offer = {"links_offer": any(d["quarter"] == q for d in open_missing)}
    if stopped and not scheduled:
        tail = [stopped] + tail if handover_docs and standalone else tail
        extra = [stopped] + list(extra)
    if handover_docs and standalone:
        return _handover(conn, job_id, handover_docs, q, tail, ready, sent, scheduled=scheduled)
    receipts = _receipts(conn, handover_docs, job_id)[0] if handover_docs else []
    reported = {d["pid"]: item_state(d) for d in st["proposals"]}
    reported.update({d["pid"]: "missing" for ds in st["missing"].values() for d in ds})
    reported.update({d["pid"]: "unclassified" for d in st["unclassified"]})        # #98
    extra_scope = {"job_id": job_id, **sent, **offer,
                   **(_ready_scope(conn, ready) if ready else {})}
    if scheduled:
        new_props = [d for d in st["proposals"]
                     if not seen_state(conn, d["pid"], item_state(d))]
        new_miss = [d for d in open_missing if not seen_state(conn, d["pid"], "missing")]
        new_uncl = [d for d in st["unclassified"]
                    if not seen_state(conn, d["pid"], "unclassified")]           # #98
        qs = replace.open_ones(conn)
        new_qs = [x for x in qs if x["job_id"] == job_id]    # asked by this run: new
        if not new_props and not new_miss and not new_qs and not new_uncl:
            if ready:
                return compose_ready(conn, ready, extra, alerts=alerts, receipts=receipts)
            if not receipts:
                return None
        earlier = len(st["proposals"]) + len(open_missing) + len(st["unclassified"]) \
            - len(new_props) - len(new_miss) - len(new_uncl)
        head = [f"{_qn(q)} · new: " + _nonzero((len(new_props), "to confirm"),
                                                (len(new_miss), "missing"),
                                                (len(new_uncl), "not classified yet"))]
        if earlier:
            head.append(f"{_s(earlier, 'earlier item')} still open")
        head += _fit_receipts(receipts, head, _confirm_room(new_props) + tail)
        return _summary(conn, "end", q, head, new_props, _vendor_items(new_miss), tail,
                        reported, scheduled=True, extra_scope=extra_scope,
                        questions=new_qs,       # e1 (Astra S2): only this run's new ones
                        unclassified=new_uncl)  # r1 (Terra S2): named, as on the other cards
    c = st["counts"].get(q, collections.Counter())
    n = sum(c.values())
    all_qs = replace.open_ones(conn)
    # r3 (Astra S2): the card's questions are its quarter's, on every branch
    qs = [x for x in all_qs if work.describe(conn, x["pid"])["quarter"] == q]
    # r4 (Astra S1): the operator's card reports only its quarter's items — another
    # quarter's are one count line, never shown, so never marked seen (the §1 new-state rule
    # still owes them their own card)
    reported = {p: v for p, v in reported.items()
                if work.describe(conn, p)["quarter"] == q}
    if not st["proposals"] and not open_missing and not st["unclassified"]:
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
    elif not props and not mine and not qs and not c["pending"] and not c["unclassified"]:
        head = [stopped or f"{_qn(q)} checked · {_s(n, 'payment')} · all accounted for."]
    else:
        head = [stopped or f"{_qn(q)} checked · {_s(n, 'payment')}", _counts_line(c)]
    head += _fit_receipts(receipts, head, _confirm_room(props) + earlier + tail)
    return _summary(conn, "end", q, head, props, _vendor_items(mine),
                    earlier + tail, reported, scheduled=False, extra_scope=extra_scope,
                    questions=qs,
                    unclassified=[d for d in st["unclassified"] if d["quarter"] == q])


def _other_quarters(st, open_missing, q) -> list:
    """One line per OTHER quarter with open items, non-zero parts only (the approved
    script): "Q2 still open: …" before `q`, "Q4 so far: …" after it."""
    out = []
    for eq in sorted(st["counts"]):
        if eq == q:
            continue
        a = sum(1 for d in st["proposals"] if d["quarter"] == eq)
        b = sum(1 for d in open_missing if d["quarter"] == eq)
        u = sum(1 for d in st["unclassified"] if d["quarter"] == eq)        # r1 (Astra)
        if a or b or u:
            out.append(f"{_qn(eq, q)} {'still open' if eq < q else 'so far'}: "
                       + _nonzero((a, "to confirm"), (b, "missing"),
                                  (u, "not classified yet")))
    return out


LINKS_HEAD = "Where to download the missing invoices:"


def _links_lines(conn, mine, before, after, quarter) -> list:
    """Issue #57: one line per vendor of the quarter's missing invoices (`mine`), with
    where its invoices can be downloaded (the KB's document link and link note), or "no
    link known"; as many as fit whole between `before` and `after`, the rest counted."""
    if not mine:
        return [f"Nothing is missing in {_qn(quarter)} any more."]
    firsts: dict = {}
    for d in mine:
        firsts.setdefault(kb.norm(d["vendor"]), d)
    ds = sorted(firsts.values(), key=lambda d: (kb.norm(views.shown(d)), d["pid"]))
    lines = []
    for d in ds:
        where = ([views.field(d["link"], views.LINK_MAX)] if d["link"] else []) \
            + ([views.field(d["link_note"])] if d.get("link_note") else [])
        if where:            # #99: a vendor with no known link adds nothing
            lines.append(f"{views.field(views.shown(d))}: " + " — ".join(where))
    if not lines:
        return ["", "No download links known for these vendors."]

    def more(left):
        return f"… and {_s(left, 'more vendor')}"
    k = _fit_count(list(before) + [LINKS_HEAD], lines, more, after)
    return ["", views.title(LINKS_HEAD)] + lines[:k] \
        + ([more(len(lines) - k)] if k < len(lines) else [])


def compose_open(conn, quarter, *, scheduled=False, links=False, package=False) -> str:
    """The quarter status card (#55, the approved script 1a/1b; the open-items card of §1
    r11): where `quarter` stands — its payments and non-zero counts, ITS proposals and
    missing invoices (Review walks only those), one line per other quarter with open items,
    and [Get package] only with `package` (#94: the desk asked for it, the operator's intent to
    get the package being clear) and a payment in the books. `b` counts the
    unanswered missing payments only ([Leave missing] is an answer). `links` (issue #57,
    the [Invoice links] tap): the same card with where to download each missing invoice."""
    st = state(conn)
    open_missing = [d for ds in st["missing"].values() for d in ds if not _answered(d)]
    props = [d for d in st["proposals"] if d["quarter"] == quarter]
    mine = [d for d in open_missing if d["quarter"] == quarter]
    reported = {d["pid"]: item_state(d) for d in props + mine}
    reported.update({d["pid"]: "unclassified" for d in st["unclassified"]
                     if d["quarter"] == quarter})                              # #98
    qs = [x for x in replace.open_ones(conn)
          if work.describe(conn, x["pid"])["quarter"] == quarter]
    c = st["counts"].get(quarter, collections.Counter())
    n = sum(c.values())
    if not n:
        head = [views.esc(nothing_to_check(conn, quarter))]
    elif not props and not mine and not qs and not c["pending"] and not c["unclassified"]:
        head = [f"{_qn(quarter)} · {_s(n, 'payment')} · all accounted for"]
    else:
        head = [f"{_qn(quarter)} · {_s(n, 'payment')}", _counts_line(c)]
    if package and n:
        # #102: the operator asked whether the quarter is ready for the accountant — the card
        # answers it in one line after its title
        head.insert(1, f"{_qn(quarter)} is ready for your accountant."
                    if loop.complete(conn, quarter) else "Not ready yet.")
    tail = _other_quarters(st, open_missing, quarter)
    if links:
        tail += _links_lines(conn, mine, head + _confirm_room(props), tail, quarter)
    return _summary(conn, "open-items", quarter, head, props, _vendor_items(mine),
                    tail, reported, scheduled=scheduled, questions=qs,
                    unclassified=[d for d in st["unclassified"] if d["quarter"] == quarter],
                    extra_scope={"package": bool(package and n), "links_offer": bool(mine),
                                 "links": links})


def compose_ready(conn, quarters: list, extra=(), alerts=(), receipts=()) -> str:
    """The "package ready" notice (§1, D19): the latest owed quarter heads it (no button:
    #94); `receipts` (d2: the "Filed." lines of a handover the completing check took);
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
    # d1 (Astra, Terra): a job's completion notice is no package request (#94): no button,
    # so it goes as a plain message; "send the Qn package" in words gets it
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
            return f"{left} more could fit — ask me to show them"
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
                 # #56: [See PDF] for the one document a Confirm card proposes
                 **_see(conn, d, picks, shown),
                 # PLAY 0.11.2: the Confirm legend names the document's own kind
                 "doc_word": views.KIND_WORD.get(
                     (d["current"]["document"] if d["current"] is not None
                      else shown[0]["doc"] if shown else {}).get("kind"), "document"),
                 **_grammar([d])}
        return _store(conn, "review", lines, scope, {pid: 1}, {pid: item_state(d)},
                      docs={pid: docs})


def _see(conn, d, picks, shown) -> dict:
    """#56: `{"see": [doc_id, label]}` when the card proposes one document (a Confirm card)
    Casa can send as a file; else nothing."""
    import posting
    if picks or d["current"] is None or not shown:
        return {}
    doc_id = d["current"]["document"]["doc_id"]
    row = conn.execute("SELECT ext FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    label = posting.see_label(row["ext"]) if row is not None else None
    return {"see": [doc_id, label]} if label else {}


def live_question_card(conn, review_of, pos, pid):
    """e2 (Astra S2): the payment's live question's card, in place of a superseded one —
    None when it has none."""
    live = [q for q in replace.open_ones(conn) if q["pid"] == pid]
    if not live:
        return None
    src = json.loads(_row(conn, review_of)["scope_json"])
    return _replace_card(conn, review_of, pos, len(src.get("order") or []) or 1,
                         src["quarter"], bool(src.get("scheduled")), live[-1]["question_id"])


DIFF_FIELDS = (("issuer", "issuer"), ("recipient", "recipient"),
               ("document_number", "number"), ("document_date", "date"),
               ("amount_minor", "amount"), ("currency", "currency"), ("kind", "kind"))


def _differs(conn, old, new) -> str:
    """#72: what the two readings say differently, so Keep current / Use new is a choice the
    operator can see (a reissue often differs only in its recipient)."""
    a, b = (conn.execute("SELECT * FROM documents WHERE doc_id=?", (x,)).fetchone()
            for x in (old, new))
    out = []
    for col, word in DIFF_FIELDS:
        if a[col] != b[col]:
            def show(v):
                if v is None or v == "":
                    return "none"
                if col == "amount_minor":
                    return _money(v, b["currency"] or a["currency"])
                return views.field(str(v), 60)
            out.append(f"{word} {show(a[col])} → {show(b[col])}")
    if not out:
        return "Same reading as the current one."
    text = "Differs: " + "; ".join(out)
    return text if text.endswith(".") else text + "."


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
             f"New: {doc_line(qn['new_doc_id'])} (from you).",
             _differs(conn, old, qn["new_doc_id"])]
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
    "BELASTINGDIENST"). Composed under views.named(..., payee=False), so a generated ref
    tells apart two payments that read the same without it (r8 Astra S2)."""
    return views.headline(d, quarter, payee=False)


def wanted(ds) -> str:
    """Issue #59 (3): what a vendor card's missing payments lack — "invoice" when they
    expect invoices, the one other kind they all expect ("credit note"), else
    "document"."""
    kinds = {d["expectation"]["kind"] or "invoice" for d in ds if _bucket(d) == "missing"}
    if kinds <= {"invoice"}:
        return "invoice"
    return views.KIND_WORD.get(kinds.pop(), "document") if len(kinds) == 1 else "document"


def _page_lines(vendor, ds, i, n, p, pages, link, quarter, noun="invoice") -> list:
    """A vendor page's FINAL lines — what _store fits and what _pages measures (one
    function, so the measure is the text). `vendor` is the name the operator reads."""
    head = f"Card {i} of {n} · missing {noun}s · {views.field(vendor)}"
    if pages > 1:
        head += f" · page {p} of {pages}"
    body = [_payee_free(d, quarter) + _mark(d) for d in ds]
    return [head] + body + ([views.link_line(link)] if link else [])


def _pages(vendor, ds, i, n, link, quarter, extra=(), noun="invoice") -> list:
    """Greedy pages of ≤ PAGE_LINES payments, each measured on its COMPLETE final text: the
    page's real lines (_page_lines: escaped vendor name, "· left missing" suffixes, the
    link) with the worst-case page numbers and the worst-case rendering tag (plan round 7,
    Astra S1: a 60-character punctuated vendor name and 24 suffixes overflowed a page
    measured without them, and its 25th payment was bound but cut)."""
    worst = max(len(ds), 2)

    def fits(trial):
        # r5 (Astra S2): with the lines a page carries after its payments (§B's count line,
        # in its longest, switched-on form), so the fit never drops a frozen page's payment
        return _fits(_page_lines(vendor, trial, i, n, worst, worst, link, quarter, noun)
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


def _others_line(others, others_missing, quarter, on, vendor="") -> list:
    """§B: the vendor's payments of OTHER quarters a quarter's card counts (operator ruling
    2026-10-08), stated plainly — every one [Never] would change (d2 Astra + Terra S1), and
    how many are missing; with the switch on, that the answers cover the missing ones.
    Issue #59 (4): when they are not all missing, the line says why it is there."""
    if not others:
        return []
    qs = ", ".join(_qn(q, quarter) for q in sorted(set(others.values())))
    n, m = len(others), len(others_missing)
    line = (f"Also missing in other quarters: {m} ({qs})" if m == n
            else f"Also in other quarters: {_s(n, 'payment')}"
                 + (f", {m} missing" if m else "") + f" ({qs}) · Never for "
                 f"{views.field(vendor)} would also change "
                 + ("it" if n == 1 else "them"))
    if on and m:
        line += " · answers will cover them" if m == n else f" · answers will cover the {m} missing"
    return [line]


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
    # issue #59 (1, 3): the name the operator reads, and what the payments lack — fixed on
    # page 1 like the pages, so every page and every answer reads the same
    name = src["vendor_name"] if src is not None and src.get("vendor_name") \
        else views.shown(order[0])
    noun = src["noun"] if src is not None and src.get("noun") else wanted(order)
    extra = [] if scheduled else (_also_line(name, also)
                                  + _others_line(others, others_missing, quarter, on, name))
    with views.named(order, quarter, payee=False):      # r8: the lines it prints
        if pages is None:
            worst = [] if scheduled else (_also_line(name, also)
                                          + _others_line(others, others_missing, quarter, True,
                                                         name))
            pages = _pages(name, order, pos + 1, n, link, quarter, worst, noun)
        page = max(1, min(page, len(pages)))
        mine = [ds[p] for p in pages[page - 1] if p in ds]
        if not mine:
            return None
        lines = _page_lines(name, mine, pos + 1, n, page, len(pages), link, quarter, noun)
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
                 "vendor_name": name, "noun": noun, **_grammar(shown)}
        bound = {d["pid"]: 1 + j for j, d in enumerate(shown)}
        also_line = _also_line(name, also)
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


def next_after(conn, review_of, pos):
    """§1: every answer posts its successor — the next item of the stored Review order
    still unanswered, else None: after the walk's last card the receipt alone answers
    (#80: no quarter card the operator did not ask for) — unless the walk started from a
    card answering the operator's package request (#99 r3): then its quarter's card, with
    [Get package]."""
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
    if scope.get("package") and scope.get("quarter"):
        # #99 r3 (Terra, defended): the walk started from a card that answered the operator's
        # package request; a tap cleared that card's keyboard, so the walk ends on the
        # quarter's card with [Get package] — asked for, so no card unasked (#80)
        return compose_open(conn, scope["quarter"], package=True)
    return None


# ---- buttons and the deposit -------------------------------------------------------------

def buttons(conn, r) -> list:
    """The stored calls of a cards rendering, in order, as (label, tool, args, key_spec);
    key_spec is (action, pid, doc_id) (D16: every tap is a keyed `verdict`); [Get package]
    — the one unkeyed call (#1303) — is the last action, then Close."""
    out = _buttons(r["render_id"], r["kind"], json.loads(r["scope_json"]))
    # #93: Close beside the actions (room allowing: Casa takes six), so a card can be
    # dismissed without acting; never alone — a card with nothing to tap is posted plain
    return out + [views.CLOSE] if out and len(out) < 6 else out


def _buttons(rid, kind, scope) -> list:
    """buttons() from a rendering's id, kind and scope, Close aside."""

    def v(label, action, pid=None, doc_id=None):
        args = {"render_id": rid, "action": action}
        if pid is not None:
            args["pid"] = pid
        if doc_id is not None:
            args["doc_id"] = doc_id
        return (label, "verdict", args, (action, pid, doc_id))
    get = [("Get package", "get_package", {"quarter": scope["quarter"]}, None)] \
        if scope.get("package") else []
    if kind in ("end", "open-items"):
        out = []
        if scope.get("order"):
            out.append(v("Review", "review"))
        if scope.get("confirm_all"):
            out.append(v("Confirm all", "confirm-all"))
        if scope.get("links_offer") and not scope.get("links"):
            out.append(v("Invoice links", "links"))
        return out + get
    if kind == "ready":
        return []
    if kind == "replace":
        pid, doc = scope["pid"], scope["new_doc_id"]
        return [v("Keep current", "keep-current", pid, doc), v("Use new", "use-new", pid, doc)]
    if kind == "review":
        pid = scope["pid"]
        if scope.get("picks"):
            out = [v(label, "pick", pid, doc) for doc, label in scope["picks"]]
        else:
            out = [v("Confirm", "confirm", pid)]
            if scope.get("see"):
                # #56: the file only; Casa leaves the card live (#1362 keep_card)
                doc_id, label = scope["see"]
                # #68: keyed — the operator's tap is the one get_document a running job
                # pass lets through (posting.get_document); the key is never spent
                out.append((label, "get_document", {"doc_id": doc_id}, ("see", pid, doc_id)))
        return (out + [v("Wrong", "wrong", pid), v("Leave for now", "leave", pid)])[:6]
    if kind == "vendor-page":
        last = scope["page"] == len(scope["pages"])
        acts = scope.get("missing", True)       # a page with no missing line: no exemption
        out = [v(exempt_label(scope), "exempt-these")] if acts else []
        if last and not scope.get("scheduled"):
            out.append(v(_label(f"Never for {vendor_name(scope)}"), "never"))
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


def vendor_name(scope) -> str:
    """Issue #59 (1): a vendor card's name as the operator reads it (renderings stored
    before 0.11.3 carry only the vendor)."""
    return scope.get("vendor_name") or scope.get("vendor") or ""


def exempt_label(scope) -> str:
    """Issue #59 (3): [No invoice needed for these] names what is missing."""
    return ("No invoice needed for these" if scope.get("noun", "invoice") == "invoice"
            else "No document needed for these")


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
            "buttons": [posting.button_json(label, tool, args) for label, tool, args in out],
            "revision": ("walk:" + scope["review_of"])[:64]}
