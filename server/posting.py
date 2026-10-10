"""S7: the posting tools' logic. Each composes and stores what it posts (and its tap keys)
in ONE committed transaction, then deposits the body with Casa as the LAST step, and
returns the reference. Nothing here ever returns a key."""
from __future__ import annotations

import json
import re
import sys

import casa_broker
import db
import keys
import views


# #56 (Casa #1362, v0.344.57): a button calling one of these tools leaves its card live —
# the file is sent and the card keeps [Confirm] and its other buttons. An older Casa ignores
# the key and settles the card as it did before.
KEEP_CARD_TOOLS = ("get_document",)


def button_json(label, tool, args) -> dict:
    """One deposited button: its stored call, and `keep_card` for a file it only shows.
    #72: `tool` None is Casa's Close button (v0.344.64, #1375): no call, Casa removes the
    keyboard itself."""
    if tool is None:
        return {"label": label, "close": True}
    out = {"label": label, "call": {"tool": tool, "arguments": args}}
    if tool in KEEP_CARD_TOOLS:
        out["keep_card"] = True
    return out


def _proposal(text, buttons, revision, pages=None) -> str:
    """`pages` (#66, Casa v0.344.67): plain messages Casa posts, in order, before the card."""
    value = {"text": views.deposit_safe(text), "revision": revision,
             "buttons": [button_json(label, tool, args) for label, tool, args in buttons]}
    if pages:
        value["pages"] = [views.deposit_safe(p) for p in pages]
    return json.dumps(value, ensure_ascii=False)


def _keyed(conn, render_id, specs) -> list:
    """The buttons of `specs` (views.buttons_for, cards.buttons), each writing one with a
    fresh key stored under the rendering. A key_spec is (action, pid, doc_id): the key binds
    all three (simple loop §1: a named candidate's document). Inside the caller's
    transaction."""
    out = []
    for label, tool, args, key_spec in specs:
        args = dict(args or {})
        if key_spec is not None:
            key = keys.mint()
            action, pid, doc_id = key_spec
            keys.store_render(conn, render_id, action, pid, key, doc_id=doc_id)
            args["key"] = key
        out.append((label, tool, args))
    return out



def show_view(conn, *, view=None, quarter=None, pid=None, page=None, after=None,
              render_id=None, prev=None, package=False) -> dict:
    """§7.1: render exactly as build_review does (or, with render_id, re-post that stored
    rendering — the job's `view` unit, §5) and deposit it as a proposal: text = the page,
    buttons = §7.2, revision = view:<view>:<quarter>. `prev` is the predecessor page the
    More button (or a typed "more") names (binding V1). Simple loop §1: a cards rendering
    (cards.KINDS) re-posts with its own keyboard (cards.buttons) and the revision of its
    walk; view="open" composes and posts a fresh open-items card (§1 Recovery), with
    [Get package] when `package` (#94). #93: a rendering with nothing to act on is not
    deposited — `post` names it for post_results."""
    import cards
    with db.tx(conn):
        if render_id is not None:
            if any(v is not None for v in (view, quarter, pid, page, after, prev)):
                raise db.Refusal("render_id re-posts a stored rendering: name nothing else")
            r = conn.execute("SELECT * FROM renders WHERE render_id=?",
                             (render_id,)).fetchone() if isinstance(render_id, str) else None
            if r is None or r["kind"] not in views.VIEWS + cards.KINDS:
                raise db.Refusal("there is no view rendering by that id")
            if not views.fits_proposal(r["text"]):
                raise db.Refusal("that rendering is too long for buttons: post it with "
                                 "post_results(render_ids=[…]) instead")
        elif view == "open":
            if any(v is not None for v in (pid, page, after, prev)):
                raise db.Refusal("the open items are one card: name at most its quarter")
            if quarter is not None and cards.before_the_books(conn, quarter):
                # #55 (the approved script, 1c): one line, nothing deposited or stored; the
                # null slot is Casa's no-deposit statement (#1015 addendum, INV-PLUG-028),
                # so the result reaches the desk unchanged
                return {"view": None, "say": cards.before_books(conn, quarter)}
            # #94: [Get package] only when the desk judged the operator wants the package
            rid = cards.compose_open(conn, quarter or cards.main_quarter(conn),
                                     package=package)
            r = conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
        else:
            r = conn.execute("SELECT * FROM renders WHERE render_id=?",
                             (_compose_list(conn, view or "status", quarter, pid, page,
                                            after, prev),)).fetchone()
        shown = _shown_quarter(r, render_id is None and view != "item" and quarter is None)
        if not has_actions(conn, r):
            out = plain_post(conn, r)
            return {"view": None, **out, **shown,
                    "note": " ".join(filter(None, [out["note"], shown.get("note")]))}
        value, scope = _view_value(conn, r)
        # g1 (Astra S1), Casa #1312: re-posting the same rendering after a cut that lost its
        # mark_rendering_delivered sends nothing — Casa answers the original receipt
        key = delivery_key(conn, "view", [r["render_id"]])
    ref = casa_broker.deposit("view", value, key=key)
    # #106: the posted view is the whole answer — no narration of the post
    return {"view": ref, "render_id": r["render_id"], "next": scope.get("next"), **shown,
            "note": " ".join(filter(None, [
                "Casa posts this view itself; never describe it. After its receipt, "
                "mark_rendering_delivered(render_id); your whole reply is <silent/>.",
                shown.get("note")]))}


def _shown_quarter(r, defaulted) -> dict:
    """#126: the quarter a view shows, in show_view's answer, so the desk sees which one it
    posted. A call that named none got the store's default (ruling Q2b: the quarter the
    newest check named), which need not be the one the operator is talking about: the note
    says so, for the desk to judge."""
    if r["kind"] == "item":
        return {}
    q = json.loads(r["scope_json"]).get("quarter")
    if not q:
        return {}
    out = {"quarter": q}
    if defaulted:
        out["note"] = (f"No quarter was named, so this is {q}. If the operator named another "
                       "quarter, show_view again with theirs.")
    return out


POST_NOTE = ("Nothing on it to act on, so it goes as a plain message: post_results(render_ids="
             "<each list in `post`>), and after each receipt mark_rendering_delivered on its "
             "render ids. Never retell it; then your whole reply is <silent/>.")


def has_actions(conn, r) -> bool:
    """#93: a posted rendering `r` carries a button to act on (Close goes only beside one)."""
    import cards
    return bool(cards.buttons(conn, r) if r["kind"] in cards.KINDS
                else views.buttons_for(conn, r))


def plain_post(conn, r, lead_rid=None) -> dict:
    """#93: a rendering with nothing to act on is not a card: the desk posts it, with its
    pages (and a rename's line, `lead_rid`) first, as plain messages through post_results —
    `post` holds one render id per call, in order. The null slot is
    Casa's no-deposit statement (#1015 addendum)."""
    scope = json.loads(r["scope_json"])
    ids = ([lead_rid] if lead_rid else []) + list(scope.get("list_pages") or []) \
        + [r["render_id"]]
    # r1 (Astra S2): one rendering per post — Casa pages a joined message at its own
    # boundaries, and a quote of its second message would match no stored rendering
    groups = [[rid] for rid in ids]
    for rid in ids:
        # "a deposit attempted" (as a view's stamp): a reply quoting the plain post binds it
        conn.execute("UPDATE renders SET posted_seq=? WHERE render_id=?",
                     (db.next_seq(conn), rid))
    return {"post": groups, "render_id": r["render_id"], "note": POST_NOTE}


def _view_value(conn, r, lead=None):
    """show_view's deposit of the stored rendering `r` (inside the caller's transaction): the
    proposal value and the rendering's scope. `lead` (#89): one plain line Casa posts before
    the card (its first page), e.g. a rename's "<old> is now called <new>."."""
    import cards
    scope = json.loads(r["scope_json"])
    if r["kind"] in cards.KINDS:
        # keys minted and posted_seq stamped by deposit_of, as for a tap's `next`
        value = json.dumps(cards.deposit_of(conn, r["render_id"]), ensure_ascii=False)
    else:
        buttons = _keyed(conn, r["render_id"], views.buttons_for(conn, r))
        # #111: one payment's card supersedes only an earlier card of the same payment, never
        # another payment's card still open (Casa replaces a live card of the same revision)
        what = scope.get("pid") if r["kind"] == "item" else scope.get("quarter")
        revision = f"view:{r['kind']}:{what or ''}"[:64]
        pages = [conn.execute("SELECT text FROM renders WHERE render_id=?",
                              (p,)).fetchone()[0] for p in scope.get("list_pages") or []]
        value = _proposal(r["text"], buttons, revision, pages)
        for p in scope.get("list_pages") or []:
            # a page is posted with its card: a quote of it binds it (db.seen_render)
            conn.execute("UPDATE renders SET posted_seq=? WHERE render_id=?",
                         (db.next_seq(conn), p))
        # r3 #3: stamped posted before the deposit (which stays last) — a view posted by
        # a tap's stored call is never marked delivered, and a quote of it binds it. The
        # stamp means "a deposit was attempted at seq n": monotone, never restored on a
        # refusal, so a late refusal cannot erase a later post's stamp (binding §3, r4
        # Terra S1). It only makes the row a quote candidate, and recency never picks
        # among those (R1)
        conn.execute("UPDATE renders SET posted_seq=? WHERE render_id=?",
                     (db.next_seq(conn), r["render_id"]))
    if lead is not None:
        v = json.loads(value)
        v["pages"] = [views.deposit_safe(lead), *v.get("pages", [])]
        value = json.dumps(v, ensure_ascii=False)
    return value, scope


def _compose_list(conn, view, quarter, pid, page, after, prev) -> str:
    """#66: the whole list, page after page (each its own rendering, binding what it prints,
    as a More page did), up to views.MAX_PAGES. One page is the card itself; more become
    plain pages before one action card (views.list_card) that acts on all of them. Returns
    the render id to post."""
    page, ids = page or 1, []
    while True:
        out = views.review_in_tx(conn, view, quarter, pid, page, after, prev=prev)
        ids.append(out["render_id"])
        nxt = out["next"]
        if nxt is None or len(ids) == views.MAX_PAGES:
            break
        page, after, prev = nxt["page"], nxt.get("after"), nxt.get("prev")
    if len(ids) == 1 and nxt is None:
        return ids[0]
    for rid in ids:
        # every page is posted: none of them leads anywhere a typed "more" should go
        sc = json.loads(conn.execute("SELECT scope_json FROM renders WHERE render_id=?",
                                     (rid,)).fetchone()[0])
        sc["next"] = None
        conn.execute("UPDATE renders SET scope_json=? WHERE render_id=?",
                     (db.canonical(sc), rid))
    return views.list_card(conn, ids, nxt)


DELIVERY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")    # Casa #1312's `key`


def delivery_key(conn, slot, render_ids) -> str | None:
    """Casa #1312: the deposit's identifier — the slot, this store (db.store_id: new after a
    reset, when render ids restart) and its renderings, so the same message re-posted after
    a cut is the same key and no other message ever is. None when it would not fit (sent
    without a key, as before). Inside the caller's transaction."""
    key = f"{slot}:{db.store_id(conn)}:" + ".".join(render_ids)
    return key if DELIVERY_KEY_RE.fullmatch(key) else None


def post_results(conn, render_ids) -> dict:
    """§5: post stored renderings as ONE operator_message (joined by a blank line) —
    the job's `post` unit, or a desk turn's notice to post. Already delivered ones are
    skipped; with none left, nothing is deposited (the no-post shape)."""
    import job
    if (not isinstance(render_ids, list) or not 1 <= len(render_ids) <= job.POST_MAX
            or not all(isinstance(r, str) for r in render_ids)):
        raise db.Refusal(f"render_ids is a list of 1 to {job.POST_MAX} render ids")
    with db.tx(conn):
        rows = []
        for rid in render_ids:
            r = conn.execute("SELECT render_id, text, delivered_at FROM renders WHERE"
                             " render_id=?", (rid,)).fetchone()
            if r is None:
                raise db.Refusal(f"there is no rendering {rid}")
            if r["delivered_at"] is None:
                rows.append(r)
        if not rows:
            return {"results": None, "render_ids": []}
        body = views.deposit_safe("\n\n".join(r["text"] for r in rows))
        if len(body) > job.POST_CHARS:
            raise db.Refusal("those renderings are too long for one message: post them one "
                             "at a time")
        ids = [r["render_id"] for r in rows]
        key = delivery_key(conn, "results", ids)
    ref = casa_broker.deposit("results", body, key=key)
    return {"results": ref, "render_ids": ids}


CAPTION_MAX = 900          # 1024 − views.LABEL_ALLOWANCE − 1, rounded down (§6.1)
PKG_REFUSED = "the package could not be sent under its name — ask again"
ALREADY_POSTED = "That package was already sent once — nothing was posted again."


def post_package(conn, delivery_id) -> dict:
    """§6.1: deposit a staged package as an operator_file — the staged path (Casa claims and
    consumes it), kind zip, the package's filename as the delivered name (S7a), and one
    caption line. A deposit Casa refuses settles the send `failed` (the staged copy taken
    back) — never a send under the storage name."""
    with db.tx(conn):
        d = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?",
                         (delivery_id,)).fetchone()
        if d is None or d["package_id"] is None:
            raise db.Refusal("post_package posts a staged package: pass its delivery_id")
        if (d["status"] != "staged" or d["revoked_at"] or d["withdrawn_at"]
                or d["channel"] != "telegram"):
            raise db.Refusal("that send is no longer waiting to go out — nothing was posted")
        if d["posted_at"] is not None:
            # at most one deposit per send: Casa consumes the staged copy, so a second
            # deposit's claim fails after a send that went out (Codex r2 S2)
            raise db.Refusal(ALREADY_POSTED)
        pk = conn.execute("SELECT * FROM packages WHERE package_id=?",
                          (d["package_id"],)).fetchone()
        line = pk["caption"].split("\n", 1)[0]
        if d["as_built"]:
            import dates
            line += f" · built {dates.short_day(pk['built_at'])}, as it was then"
    ref = _deposit_package(conn, d, pk, line)
    return {"package": ref, "delivery_id": delivery_id, "filename": pk["filename"]}


def _deposit_package(conn, d, pk, line) -> str:
    """Deposit staged send `d` (package row `pk`) with caption `line`: the tagged
    `package-file` rendering and the post mark commit first, the deposit is the LAST step.
    A deposit Casa refuses settles the send `failed` (its staged copy taken back, its
    caption rendering removed), then refuses with PKG_REFUSED. Returns the reference."""
    import delivery
    delivery_id = d["delivery_id"]
    with db.tx(conn):
        # #44: the caption is a rendering of its own (`package-file`), tagged (binding V2)
        # and offering its package, so a swipe-reply on the file binds it; its text is the
        # caption in the dialect (a plain caption's backslashes doubled: unesc gives back
        # exactly what Telegram shows), seen once deposited (posted_seq)
        rid = f"r{db.next_seq(conn)}"
        tag = views.tag_now()
        caption = views.clip(views.caption_safe(views.esc(line, plain=True)),
                             CAPTION_MAX - views.utf16_len(tag)) + tag
        conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                     " membership_json, posted_seq) VALUES (?, 'package-file', ?, ?, ?, '[]', ?)",
                     (rid, db.canonical({"delivery": delivery_id, "offers": [d["package_id"]]}),
                      db.now(), caption.replace("\\", "\\\\"), db.next_seq(conn)))
        # the post mark commits before the deposit: a turn that dies after it leaves the
        # send staged, recovered `uncertain` at a later claim (delivery.stalled_sends);
        # only a recorded outcome settles it delivered
        conn.execute("UPDATE deliveries SET lease_at=?, posted_at=? WHERE delivery_id=?",
                     (db.now(), db.now(), delivery_id))
    try:
        return casa_broker.deposit("package", d["staged_path"], caption=caption, kind="zip",
                                   filename=pk["filename"])
    except casa_broker.DepositFailed:
        with db.custody_lock():
            with db.tx(conn):
                delivery.withdraw(conn, [dict(d)], refusal="could not take back the staged "
                                                           "package — nothing changed")
                now = db.now()
                conn.execute("UPDATE deliveries SET status='failed', settled_at=?,"
                             " withdrawn_at=? WHERE delivery_id=?", (now, now, delivery_id))
                # Terra r1 S2: the operator never saw this caption — its rendering goes,
                # so no quote can bind a file that did not arrive
                conn.execute("DELETE FROM renders WHERE render_id=? AND kind='package-file'",
                             (rid,))
        raise db.Refusal(PKG_REFUSED)


# #56: the kinds Casa sends a document as (its media policies), by the filed extension
SHOWN_AS = {"pdf": "document", "jpg": "photo", "jpeg": "photo", "png": "photo"}


def see_label(ext) -> str | None:
    """The [See …] label for a filed document of extension `ext`, or None when Casa cannot
    send that kind of file."""
    if ext not in SHOWN_AS:
        return None
    return "See PDF" if ext == "pdf" else "See document"


JOB_RUNNING = ("a check is running, so no document is sent to the chat now. Inside the "
               "check, read a document with read_document(doc_id); at the desk, ask again "
               "when the check has finished")


def _tap_key(conn, key, doc_id) -> bool:
    """#68: `key` is a [See PDF] button's key for this document (minted into the deposit
    only, never returned by a tool). Never spent: the button stays usable."""
    if not isinstance(key, str) or not keys.KEY_RE.fullmatch(key):
        return False
    return conn.execute("SELECT 1 FROM render_keys WHERE key=? AND action='see' AND doc_id=?",
                        (key, doc_id)).fetchone() is not None


def get_document(conn, doc_id, key=None) -> dict:
    """#56: a [See PDF] tap's stored call (keep_card: the card stays live). The filed bytes,
    checked against their hash, copied to a fresh outbox path and deposited as one file
    under the document's package name. Nothing is recorded: showing a document is not a
    delivery to the accountant. A deposit Casa refuses takes the copy back.
    #68: while a job pass is open nothing is sent but the operator's own tap (its key) — a
    job reads a document with read_document, never by posting it to the chat."""
    import delivery, documents, job, package
    if not isinstance(doc_id, int) or isinstance(doc_id, bool):
        raise db.Refusal("get_document takes a document's doc_id")
    if job.live_job_pass(conn) is not None and not _tap_key(conn, key, doc_id):
        raise db.Refusal(JOB_RUNNING)
    d = conn.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    if d is None:
        raise db.Refusal(f"there is no document #{doc_id}")
    kind = SHOWN_AS.get(d["ext"])
    if kind is None:
        raise db.Refusal(f"document #{doc_id} is a .{d['ext']} file, which can't be sent "
                         "here")
    try:
        data = documents.path_of(conn, doc_id).read_bytes()
    except FileNotFoundError:
        raise db.Refusal(f"document #{doc_id}'s file is missing — nothing was sent") from None
    import hashlib
    if hashlib.sha256(data).hexdigest() != d["sha256"]:
        raise db.Refusal(f"document #{doc_id}'s file no longer matches what was filed — "
                         "nothing was sent")
    if kind == "document" and not data.startswith(b"%PDF-"):
        # issue #8: a vendor's PDF can carry a prefix (a UTF-8 BOM); the copy shown starts
        # at the header, as the reading copy does
        at = data.find(b"%PDF-", 0, documents.PDF_HEADER_WINDOW)
        if at < 0:
            raise db.Refusal(f"document #{doc_id} is filed as a PDF but has no PDF header — "
                             "it can't be shown")
        data = data[at:]
    name = package.doc_filename(dict(d), set(), (d["ingested_at"] or "")[:10])
    with db.custody_lock():
        path = delivery._to_outbox(delivery._fresh_path(conn, d["ext"]), data)
    try:
        ref = casa_broker.deposit("document", str(path), kind=kind, filename=name)
    except casa_broker.DepositFailed:
        delivery._remove_staged(path)
        raise
    return {"document": ref, "filename": name}


NO_CHECK = "Nothing to package yet — ask me to check the bank first."


def get_package(conn, quarter) -> dict:
    """#1303: a [Get package] tap's stored call, and the desk's typed "send the package".
    Pure code: builds the zip synchronously from the store's latest state (no bank read, no
    model), stages it, deposits it as post_package does — the landed file IS the receipt —
    and records the send delivered (D15). Never called by the job (R6)."""
    import dates, delivery, package
    dates.parse_quarter(quarter)
    if conn.in_transaction:
        raise RuntimeError("get_package opens its own transactions")
    if conn.execute("SELECT 1 FROM snapshots").fetchone() is None:
        raise db.Refusal(NO_CHECK)
    built = package.build_quarterly_package(conn, quarter)
    if built["oversize"]:
        raise db.Refusal(f"the {dates.quarter_label(quarter)} package is "
                         f"{built['size'] / 1e6:.1f} MB, over Telegram's 20 MB limit; it is "
                         "kept here, and notes.md names the largest files")
    with db.custody_lock():
        st = delivery._stage(conn, built["package_id"], None, None)
    d = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?",
                     (st["delivery_id"],)).fetchone()
    pk = conn.execute("SELECT * FROM packages WHERE package_id=?",
                      (built["package_id"],)).fetchone()
    ref = _deposit_package(conn, d, pk, pk["caption"])    # raises Refusal on a refusal
    # Nothing may raise past this point: the file has landed. Any failure to record the
    # send delivered rolls its transaction back and leaves the send staged and posted, and
    # the next claim's recovery (delivery.posted_unrecorded) settles it `uncertain` (D15).
    alerts_ = []
    try:
        with db.tx(conn):
            if conn.execute("UPDATE deliveries SET status='delivered', settled_at=? WHERE"
                            " delivery_id=? AND status='staged'",
                            (db.now(), d["delivery_id"])).rowcount:
                alerts_ = delivery.settle_delivered(conn, d)
    except Exception as exc:  # noqa: BLE001 — the file landed; recovery settles the send
        alerts_ = []
        print(f"get_package: delivery {d['delivery_id']} posted but not recorded "
              f"({type(exc).__name__}); the next claim settles it", file=sys.stderr,
              flush=True)
    # `alerts`: what settle_delivered raised (the delivered copy compared with the bank, as
    # record_delivery's `speak` reports). A tap has no text beside the file (the landed
    # file IS the receipt), so they are not spoken here: they stay undelivered alerts, and
    # the next run's post — or a desk turn's post_results — tells them.
    return {"package": ref, "filename": pk["filename"], "delivery_id": d["delivery_id"],
            "alerts": alerts_}


READING_TOO_LONG = ("That is more than I can show for one Apply — nothing was read. Ask "
                    "for it in smaller parts.")


def reading_context(conn, quoted=None) -> dict:
    """#121: what the operator's words are about (reply.context); posts nothing."""
    import reply
    if quoted is not None and not isinstance(quoted, str):
        raise db.Refusal("quoted is the quoted post's text, as the desk context gave it")
    with db.tx(conn):
        return reply.context(conn, quoted)


def propose_reading(conn, ops, quoted=None) -> dict:
    """§8, #121: the operations the desk read in the operator's words, run under a rehearsal;
    with a write, posted as a reading to Apply. `reshow`: payments changed since the post
    (each re-shown by the desk, beside the reading or alone)."""
    import reply
    ops = reply.check_ops(ops)
    if quoted is not None and not isinstance(quoted, str):
        raise db.Refusal("quoted is the quoted post's text, as the desk context gave it")
    with db.tx(conn):
        try:
            out = reply.reading_in_tx(conn, ops, quoted)
        except views.QuoteRefusal as exc:
            return {"reading": None, "say": exc.line, "reshow": [], "show_view": exc.view}
        if not out["plan"]:
            say = "\n".join(out["receipt"])
            return {"reading": None, "reshow": out["reshow"],
                    "say": views.fit_message(say, views.FIT_CLOSING) if say else ""}
        body = [views.title("I read this as:")] + [f"· {x}" for x in out["propose"]]
        if out["unresolved"]:
            body += ["", "Not included:"] + [f"· {x}" for x in out["unresolved"]]
        text_ = "\n".join(body)
        if not views.fits_proposal(text_):
            raise db.Refusal(READING_TOO_LONG)
        now = db.now()
        # a newer reading supersedes an unanswered older one (§8: `↻ replaced`)
        conn.execute("UPDATE readings SET state='stale', settled_at=? WHERE state='open'", (now,))
        key = keys.mint()
        # #121: `text` holds the operations (canonical JSON), replayed at Apply
        rid = conn.execute("INSERT INTO readings(key, text, quoted, render_id, plan_json,"
                           " created_seq, created_at, state) VALUES (?,?,?,?,?,?,?, 'open')",
                           (key, db.canonical(ops), quoted, out["render_id"],
                            db.canonical(out["plan"]), db.next_seq(conn), now)).lastrowid
        value = _proposal(text_, [("Apply", "apply_reading", {"reading_id": rid, "key": key}),
                                  ("Cancel", "cancel_reading", {"reading_id": rid, "key": key})],
                          "reading")
    try:
        ref = casa_broker.deposit("reading", value)
    except casa_broker.DepositFailed:
        # no tap can carry a key Casa never took: the reading can never be applied (§7.5)
        with db.tx(conn):
            conn.execute("UPDATE readings SET state='stale', settled_at=? WHERE reading_id=?"
                         " AND state='open'", (db.now(), rid))
        raise
    return {"reading": ref, "reading_id": rid, "reshow": out["reshow"]}


ACCOUNTS_PER_PAGE = 5
ACCOUNT_Q = "Which one is the business account? Tap it below."


def _company_accounts(conn) -> list:
    p = conn.execute("SELECT data_json FROM probes WHERE kind='bank_accounts'").fetchone()
    accounts = (json.loads(p["data_json"] or "{}").get("accounts") or []) if p else []
    return [a for a in accounts if isinstance(a, dict) and a.get("category") == "company"
            and isinstance(a.get("account_id"), str)]


def propose_account(conn, after=0) -> dict:
    """§11: ask which company account is the business account — at most five choices a
    page, listed in the body; each button carries only (choice, key), and the account id
    and label are frozen under the key (§7.6). `after` is the page number already shown
    (an int: the More button's only argument)."""
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise db.Refusal("after is the page number the More button carried")
    with db.tx(conn):
        if conn.execute("SELECT 1 FROM binding WHERE id=1").fetchone() is not None:
            raise db.Refusal("the business account is already set")
        company = _company_accounts(conn)
        if len(company) < 2:
            raise db.Refusal("there is no choice to make: one company account is bound "
                             "automatically at the next check")
        page = company[after * ACCOUNTS_PER_PAGE:(after + 1) * ACCOUNTS_PER_PAGE]
        if not page:
            raise db.Refusal("there are no more accounts")
        key, now = keys.mint(), db.now()
        lines, buttons = [views.title(ACCOUNT_Q)], []
        for n, a in enumerate(page, 1):
            label = a.get("label") or ""
            if not isinstance(label, str):
                label = ""
            conn.execute("INSERT INTO account_choices(key, n, account_id, label, created_at)"
                         " VALUES (?,?,?,?,?)", (key, n, a["account_id"], label, now))
            lines.append(f"Account {n}: {views.field(label) or '(no name)'} "
                         f"(…{views.esc(a['account_id'][-4:])})")
            buttons.append((f"Account {n}", "bind_account", {"choice": n, "key": key}))
        more = len(company) > (after + 1) * ACCOUNTS_PER_PAGE
        if more:
            buttons.append(("More", "propose_account", {"after": after + 1}))
        text = "\n".join(lines)
        assert views.fits_proposal(text)   # 6 lines of ≤ 2×60+20 units: far within budget
        value = _proposal(text, buttons, "accounts")
    ref = casa_broker.deposit("accounts", value)
    return {"accounts": ref, "page": after + 1, "more": more}
