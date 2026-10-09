"""S7: the keyed handlers — the only constructors of authority.OperatorGrant, each after its
key check (§8.1). Every answer is {"receipt": …}: Casa posts that sentence as the tap's
receipt (INV-PROP-002)."""
from __future__ import annotations

import json

import authority
import binding
import db
import keys
import lineage
import matches
import reply
import views
import work

ACTIONS = ("all-good", "right", "wrong", "no-invoice")
# simple loop §1: the cards' taps (cards.buttons), each valid on its own kind of card only
CARD_ACTIONS = ("review", "confirm-all", "links", "confirm", "wrong", "leave", "pick",
                "exempt-these", "leave-missing", "never", "next-page",
                "all-quarters", "this-quarter",              # 0.11.2 §B: the one switch
                "leave-vendor",
                "keep-current", "use-new")             # rev 18.4 §R18.3
_ON_KIND = {"end": ("review", "confirm-all", "links"),
            "open-items": ("review", "confirm-all", "links"),
            "review": ("confirm", "wrong", "leave", "pick"),
            "vendor-page": ("exempt-these", "leave-missing", "never", "next-page",
                            "all-quarters", "this-quarter", "leave-vendor"),
            "replace": ("keep-current", "use-new")}
CARD_CHANGED = "That changed since it was shown — nothing applied. Here it is as it is now."
LIST_CHANGED = "That list changed since it was shown — nothing applied. Here it is as it is now."


def _stale(n) -> str:
    return (f"Nothing was applied: this sheet is out of date — {n} payment"
            f"{'s' if n != 1 else ''} changed since it was shown. Ask me for the list "
            "again to see them as they are now.")


def _item(conn, render_id, pid):
    return conn.execute("SELECT * FROM render_items WHERE render_id=? AND pid=?",
                        (render_id, pid)).fetchone()


def _changed(conn, render_id, pids) -> list:
    """The pids among `pids` whose projection revision, or any match revision the rendering
    recorded for them, differs from now (§7.3: read from the rendering, not from `shown`)."""
    out = []
    for pid in pids:
        it = _item(conn, render_id, pid)
        cur = conn.execute("SELECT revision FROM projections WHERE pid=?", (pid,)).fetchone()
        if it is None or cur is None or cur[0] != it["projection_revision"]:
            out.append(pid)
            continue
        for mid, rev in json.loads(it["match_revisions_json"]).items():
            now = conn.execute("SELECT revision FROM match_state WHERE match_id=?",
                               (int(mid),)).fetchone()
            if now is None or now[0] != rev:
                out.append(pid)
                break
    return out


def _apply_one(conn, grant, render_id, action, d) -> tuple:
    """One payment's verdict, bound to the rendering the tapped button sits on: the
    revisions are those render_items recorded. Returns (result, receipt line)."""
    it = _item(conn, render_id, d["pid"])
    mrevs = json.loads(it["match_revisions_json"])
    cur = d["current"]
    if action in ("all-good", "right"):
        if cur is None:
            raise db.Refusal(keys.NO_LONGER)
        res = matches.confirm_in_tx(conn, grant=grant, match_id=cur["match_id"],
                                    expected_revision=mrevs.get(str(cur["match_id"]), -1),
                                    render_id=render_id, bind="rendered")
        return res, f"Confirmed {views.headline(d)}."
    if action == "wrong" and cur is not None:
        res = matches.reject_in_tx(conn, grant=grant, match_id=cur["match_id"],
                                   expected_revision=mrevs.get(str(cur["match_id"]), -1),
                                   render_id=render_id, bind="rendered")
        return res, (f"Rejected the suggested document for {views.headline(d)}."
                      if d["status"] == "proposed"
                      else f"Removed the match for {views.headline(d)}.")
    if action == "wrong":
        shown = [c["match_id"] for c in d["candidates"] if str(c["match_id"]) in mrevs]
        if not shown:
            raise db.Refusal(keys.NO_LONGER)
        effects = matches.reject_all_in_tx(conn, d["pid"], [(m, render_id) for m in shown],
                                           grant=grant)
        n = len(shown)
        return ({"set_aside": shown, "effects": effects},
                f"Ruled out {'both' if n == 2 else n} invoice{'s' if n != 1 else ''} for "
                f"{views.headline(d)}.")
    res = matches.set_exemption_in_tx(conn, grant=grant, pid=d["pid"], exempt=True,
                                      expected_revision=it["projection_revision"],
                                      render_id=render_id, bind="rendered")
    dropped = [e for e in res["effects"] if e.startswith("unpaired")]
    return res, (f"{views.headline(d)}: needs no document"
                 + ("; removed its match." if dropped else "."))


def verdict(conn, render_id, action, pid, key, doc_id=None) -> dict:
    """§7.3: the only button that writes. After the key check (§7.5) and in ONE transaction:
    every affected payment must stand exactly as the tapped rendering recorded it, or
    nothing commits; `all-good` confirms exactly the pairings that rendering proposed.
    Simple loop §1 (#1302): a tap on a cards rendering (cards.KINDS) answers
    {"receipt", "next"} — the next card, composed in the tap's own transaction. The key
    binds (render, action, pid, doc_id); a key, rendering or action that does not hold is
    a refusal — {"receipt"} only, no card."""
    import cards
    if action not in ACTIONS + CARD_ACTIONS:
        raise db.Refusal(keys.NO_LONGER)
    with db.tx(conn):
        keys.spend_render(conn, key, render_id, action, pid, doc_id)
        grant = authority.OperatorGrant("verdict", key)
        r = conn.execute("SELECT * FROM renders WHERE render_id=?", (render_id,)).fetchone()
        if r is not None and r["kind"] in cards.KINDS:
            if action not in _ON_KIND.get(r["kind"], ()):
                raise db.Refusal(keys.NO_LONGER)
            return _card_tap(conn, r, action, pid, doc_id, grant)
        if (r is None or r["kind"] not in views.SHEET_VIEWS + ("item",)
                or action not in ACTIONS or (action == "all-good") != (pid is None)):
            raise db.Refusal(keys.NO_LONGER)
        scope = json.loads(r["scope_json"])
        affected = (scope.get("proposed") or []) if action == "all-good" else [pid]
        if not affected or any(_item(conn, render_id, p) is None for p in affected):
            raise db.Refusal(keys.NO_LONGER)
        changed = _changed(conn, render_id, affected)
        if changed:
            raise db.Refusal(_stale(len(changed)))
        lines, applied, quarters = [], [], set()
        for p in affected:
            d = work.describe(conn, p)
            res, line = _apply_one(conn, grant, render_id, action, d)
            applied.append({"pid": p, **res})
            lines.append(line)
            if d["quarter"]:
                quarters.add(d["quarter"])
        lines += reply.package_lines(conn, quarters)
    return {"receipt": views.fit_message(lines), "applied": applied}


def _answer(conn, receipt, next_rid, in_place=False) -> dict:
    """#1302: the receipt (a non-blank sentence), then the card Casa posts after it, if
    any (#80: after a walk's last card the receipt is the whole answer). `in_place` (Casa
    #1339, v0.344.48): a tap that only changes the card's own view — a page turn, the
    switch, the invoice links — asks Casa to edit the tapped card into the next one; a
    landed edit sends no receipt, anything else is the receipt and the card."""
    import cards
    if not receipt.strip():
        raise RuntimeError("#1302: every card answer is a receipt")
    out = {"receipt": views.fit_message(receipt)}
    if next_rid is not None:
        out["next"] = cards.deposit_of(conn, next_rid)
        if in_place:
            out["in_place"] = True
    return out


def _plural(n, word) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _card_tap(conn, r, action, pid, doc_id, grant) -> dict:
    import cards
    scope = json.loads(r["scope_json"])
    rid, review_of, pos = r["render_id"], scope["review_of"], scope.get("pos", -1)
    page, pages = scope.get("page", 1), scope.get("pages") or []

    def later_page():
        """The first later page of this vendor that still has something to show (r7 Astra
        S1: a page answered meanwhile is skipped, never the end of the vendor), or None."""
        for p in range(page + 1, len(pages) + 1):
            nxt = cards.card(conn, review_of, pos, p)
            if nxt is not None:
                return nxt
        return None

    def onward():
        """The next page of this vendor that has something left, else the next item
        (cards.card is None when a later page has nothing left: Task 7 carry)."""
        return later_page() or cards.next_after(conn, review_of, pos)

    def fresh():
        """The same item as it is now (a fresh first page for a vendor, r10/r11), else the
        next item when it is no longer open."""
        return cards.card(conn, review_of, pos, 1) or cards.next_after(conn, review_of, pos)

    if action == "review":
        # PLAY 0.11.2: the counts line's own numbers, never a count of cards
        return _answer(conn, f"Reviewing {cards.walk_words(scope, ', then ')}.",
                       cards.next_after(conn, rid, -1)
                       or cards.compose_open(conn, scope["quarter"]))
    if action == "confirm-all":
        return _confirm_all(conn, r, scope, grant)
    if action == "links":
        # issue #57: the quarter's card again, with where to download each missing invoice
        return _answer(conn, "Invoice links: on the card below.",
                       cards.compose_open(conn, scope["quarter"], links=True), in_place=True)
    if action == "next-page":
        nxt = later_page()
        if nxt is not None:
            at = json.loads(_row_scope(conn, nxt))["page"]
            return _answer(conn, f"Page {at} of {len(pages)}.", nxt, in_place=True)
        # r1 Terra S1: page 1 may still hold payments; fresh() re-posts it, else the next item
        return _answer(conn, "Nothing is left on the later pages: answered meanwhile.",
                       fresh())
    if action in ("all-quarters", "this-quarter"):
        # §B: a switch writes nothing; the same card comes back switched (Casa #1302: the
        # receipt is required, so it is the shortest plain phrase)
        on = action == "all-quarters"
        nxt = cards.switched(conn, rid, on)
        if nxt is None:
            # r2 Astra S1: as for an emptied later page, the vendor's page 1 may still be owed
            return _answer(conn, "Nothing is left on this card: answered meanwhile.", fresh())
        return _answer(conn, "All quarters on." if on else "This quarter only.", nxt,
                       in_place=True)
    if action == "leave-vendor":
        # PLAY 0.11.2: a vendor card's non-answer, as on a to-confirm card
        return _answer(conn, f"Left for now: {views.field(cards.vendor_name(scope))}.",
                       cards.next_after(conn, review_of, pos))
    if action == "leave":
        return _answer(conn, f"Left for now: {views.headline(work.describe(conn, pid))}.",
                       cards.next_after(conn, review_of, pos))
    # the decision in a savepoint: a refusal (or a changed binding) commits nothing of it,
    # and the answer is still a receipt plus the item as it is now (§1: every answer's
    # `next` re-offers what is still open); the next card is composed after it
    then = "next"
    try:
        with db.savepoint(conn, "card_answer"):
            if action in ("exempt-these", "leave-missing", "never"):
                out = _vendor_answer(conn, rid, scope, action, grant)
                receipt, then = out if out is not None else (LIST_CHANGED, "fresh")
            elif action in ("keep-current", "use-new"):
                # rev 18.4 §R18.3: bound to the payment and the pairing the card displayed
                import replace
                if _changed(conn, rid, [pid]):
                    receipt, then = CARD_CHANGED, "fresh"
                else:
                    receipt = replace.answer_in_tx(
                        conn, grant, replace.get(conn, scope["question_id"]), action, rid,
                        shown_fp=scope["new_doc_fp"])
            elif _changed(conn, rid, [pid]):
                receipt, then = CARD_CHANGED, "fresh"
            else:
                receipt = _proposal_answer(conn, rid, scope, action, pid, doc_id, grant)
    except db.Refusal as exc:
        receipt, then = f"Nothing was applied: {str(exc).rstrip('.')}.", "fresh"
    if then == "fresh" and action in ("keep-current", "use-new"):
        # e2 (Astra S2): a superseded or stale replace card is answered with the payment's
        # live question, when it has one
        newer = cards.live_question_card(conn, review_of, pos, pid)
        if newer is not None and newer != rid:
            return _answer(conn, receipt, newer)
    nxt = {"fresh": fresh, "onward": onward,
           "next": lambda: cards.next_after(conn, review_of, pos)}[then]()
    return _answer(conn, receipt, nxt)


def _row_scope(conn, rid) -> str:
    return conn.execute("SELECT scope_json FROM renders WHERE render_id=?", (rid,)).fetchone()[0]


def _proposal_answer(conn, rid, scope, action, pid, doc_id, grant) -> str:
    """confirm | wrong | pick on one proposal card, bound to what it displayed."""
    d = work.describe(conn, pid)
    shown_alts = scope.get("alternatives") or []
    if action == "pick":
        mrevs = json.loads(_item(conn, rid, pid)["match_revisions_json"])
        matches.pick_in_tx(conn, grant=grant, pid=pid, doc_id=doc_id, render_id=rid,
                           mrevs=mrevs, alternatives_shown=shown_alts)
        return f"Matched {views.headline(d)}."
    if action == "confirm":
        return _apply_one(conn, grant, rid, "right", d)[1]
    # D3 / plan round 2 (Astra S1): Wrong answers every candidate the card displayed — the
    # chosen document and its displayed alternatives, or a set's displayed members — and
    # nothing it did not display (plan round 8)
    _, line = _apply_one(conn, grant, rid, "wrong", d)
    matches.reject_alternatives_in_tx(conn, grant=grant, pid=pid, doc_ids=shown_alts,
                                      render_id=rid)
    if shown_alts:
        line = (f"Rejected the suggested document for {views.headline(d)} and ruled out its "
                f"{_plural(len(shown_alts), 'other document')}.")
    return line


def _vendor_answer(conn, rid, scope, action, grant):
    """A vendor page's writing buttons. [No invoice needed for these] and [Leave missing]
    bind only this page's own payments (plan round 2, Astra S2); [Never for X] binds the
    union of the pages the walk displayed, by membership (review round 1 ruling): it
    applies when every payment the rule changes now was on one of those pages, so the
    operator's own earlier-page answers ([Leave missing], [No invoice needed for these])
    never block it, and a payment that arrived since (r10) refuses it. Returns
    (receipt, "onward" | "next"), or None when the binding changed (nothing written).
    Never's set is cards.never_set — the rule rehearsed (d1 ruling) — and the real write
    that follows in the same transaction changes exactly that set."""
    import cards
    import kb
    listed = views.render_items(conn, rid)
    vendor = views.field(cards.vendor_name(scope))
    if not listed:
        return None
    if action == "never":
        if _changed(conn, rid, listed):          # Never binds every line it displayed
            return None
        union = set(listed) | {int(p) for p in scope.get("others") or {}} \
            | {int(p) for p in scope.get("also") or []}      # §B, PLAY 0.11.2: stated
        for p in scope.get("prior") or []:
            union |= set(views.render_items(conn, p))
        changes = cards.never_set(conn, scope["vendor"])
        if not set(changes) <= union:
            return None
        kb.set_expectation_in_tx(conn, scope_type="counterparty", scope=scope["vendor"],
                                 kind="none", author="operator", render_id=rid, grant=grant)
        return (f"{vendor} never needs {views._a(scope.get('noun', 'invoice'))}: "
                f"{_plural(len(changes), 'payment')} "
                "changed.", "next")
    # the page's missing lines only: a pending, proposed, matched or exempted line is
    # listed because Never would change it (d1 ruling), never to be exempted or left
    acts = [p for p in listed if p in set(scope.get("missing", listed))]
    if not acts or _changed(conn, rid, acts):   # bound to the missing lines it acts on
        return None
    revs = {p: _item(conn, rid, p)["projection_revision"] for p in acts}
    if scope.get("all_quarters"):
        # §B: the switch on — plus the other quarters' missing payments the card stated
        # (bound to its count line), at the revisions frozen on page 1 and recorded by
        # this card: one that moved since refuses the whole answer
        frozen = scope.get("others_rev") or {}
        more = list(scope.get("others_missing") or [])
        if _changed(conn, rid, more) or any(
                lineage.projection(conn, p)["revision"] != frozen.get(str(p)) for p in more):
            return None
        revs.update({p: _item(conn, rid, p)["projection_revision"] for p in more})
    # PLAY 0.11.2: the receipt names the payments it acted on, as the card showed them
    # (r8 Astra S2: the vendor once, then the card's own compact lines, so every payment
    # fits whole); the other quarters' are counted
    shown = scope.get("bound_lines") or {}
    named = "; ".join(shown.get(str(p)) or views.headline(work.describe(conn, p)) for p in acts)
    more = len(revs) - len(acts)
    named += f"; and {_plural(more, 'payment')} in other quarters" if more else ""
    if action == "exempt-these":
        for p, rev in revs.items():
            matches.set_exemption_in_tx(conn, grant=grant, pid=p, exempt=True,
                                        expected_revision=rev, render_id=rid,
                                        bind="rendered")
        return f"No {scope.get('noun', 'invoice')} needed ({vendor}): {named}.", "onward"
    work.leave_missing_in_tx(conn, list(revs), grant=grant)
    return f"Left missing ({vendor}): {named}.", "onward"


def _confirm_all(conn, r, scope, grant) -> dict:
    """§1 (S7 §7.3 extended): over the proposals the message listed, in order — one the
    operator answered after this rendering (Review, or an applied reading: an operator log
    entry past its seq) is skipped; one changed by anything else gets a refusal line and
    commits nothing; the rest are confirmed, each in its own savepoint."""
    import cards
    rid, since = r["render_id"], int(r["render_id"][1:])
    listed = scope.get("proposed") or []
    lines, done, skipped = [], 0, 0
    for pid in listed:
        if conn.execute("SELECT 1 FROM log WHERE pid=? AND author='operator' AND seq>?",
                        (pid, since)).fetchone():
            skipped += 1
            continue
        d = work.describe(conn, pid)
        if _changed(conn, rid, [pid]):
            lines.append(f"{views.headline(d)}: changed since it was shown — nothing applied.")
            continue
        try:
            with db.savepoint(conn, "confirm_one"):
                _apply_one(conn, grant, rid, "right", d)
        except db.Refusal as exc:
            lines.append(f"{views.headline(d)}: nothing applied — {str(exc).rstrip('.')}.")
            continue
        done += 1
    head = [f"Confirmed {done} of {len(listed)}."]
    if skipped:
        head.append(f"{_plural(skipped, 'proposal')} already answered — left as answered.")
    return _answer(conn, "\n".join(head + lines), cards.compose_open(conn, scope["quarter"]))


CHANGED = ("Something changed since I read your message — nothing was applied. Say it again.")
DONE = {"applied": "That was applied already.", "cancelled": "That was cancelled — nothing "
        "was applied.", "stale": keys.NO_LONGER}


def _reading(conn, reading_id, key):
    import hmac
    row = conn.execute("SELECT * FROM readings WHERE reading_id=?", (reading_id,)).fetchone() \
        if isinstance(reading_id, int) and not isinstance(reading_id, bool) else None
    if (row is None or not isinstance(key, str) or not keys.KEY_RE.fullmatch(key)
            or not hmac.compare_digest(row["key"], key)):
        raise db.Refusal(keys.NO_LONGER)
    if row["state"] != "open":
        raise db.Refusal(DONE[row["state"]])
    return row


class _Changed(Exception):
    pass


def apply_reading(conn, reading_id, key) -> dict:
    """§8: Apply on a reading. After the key check, the stored words are read again under
    the operator's grant, bound to the same rendering, in ONE transaction; they commit only
    when that plan is exactly the stored one — every step, every revision it read, the
    binding row. Anything else: nothing applies, and the reading is stale."""
    with db.tx(conn):
        row = _reading(conn, reading_id, key)
        grant = authority.OperatorGrant("apply_reading", key)
        try:
            with db.savepoint(conn, "replay"):
                out = reply.replay(conn, row, grant)
                if db.canonical(out["plan"]) != row["plan_json"]:
                    raise _Changed
        except _Changed:
            conn.execute("UPDATE readings SET state='stale', settled_at=? WHERE reading_id=?",
                         (db.now(), reading_id))
            return {"receipt": CHANGED}
        conn.execute("UPDATE readings SET state='applied', settled_at=? WHERE reading_id=?",
                     (db.now(), reading_id))
        lines = [x for x in out["receipt"] if not x.startswith("Not rebuilding yet")]
        lines += reply.package_lines(conn, out["quarters"])
    return {"receipt": views.fit_message(lines, views.FIT_CLOSING)}


def cancel_reading(conn, reading_id, key) -> dict:
    """§8: Cancel on a reading — nothing is applied, and the key is spent. It constructs no
    grant: it writes no operator authority."""
    with db.tx(conn):
        _reading(conn, reading_id, key)
        conn.execute("UPDATE readings SET state='cancelled', settled_at=? WHERE reading_id=?",
                     (db.now(), reading_id))
    return {"receipt": "Cancelled — nothing was applied."}


def bind_account(conn, choice, key) -> dict:
    """§11: a tap on `Account n` binds the account frozen under the key at n (§7.6). The
    key is spent with the binding, in ONE transaction: a refusal from binding.bind_in_tx
    (an account already bound) rolls the spend back too."""
    if isinstance(choice, bool) or not isinstance(choice, int) or not 1 <= choice <= 5:
        raise db.Refusal(keys.NO_LONGER)
    with db.tx(conn):
        rows = conn.execute("SELECT * FROM account_choices WHERE key=?", (key,)).fetchall() \
            if isinstance(key, str) and keys.KEY_RE.fullmatch(key) else []
        pick = next((r for r in rows if r["n"] == choice), None)
        if pick is None or any(r["spent_at"] for r in rows):
            raise db.Refusal(keys.NO_LONGER)
        conn.execute("UPDATE account_choices SET spent_at=? WHERE key=?", (db.now(), key))
        grant = authority.OperatorGrant("bind_account", key)
        binding.bind_in_tx(conn, pick["account_id"], pick["label"], grant=grant)
    return {"receipt": f"The business account is {views.field(pick['label']) or 'set'} "
                       f"(…{views.esc(pick['account_id'][-4:])}). Ask me to check when you "
                       "want the first check."}
