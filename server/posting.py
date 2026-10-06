"""S7: the posting tools' logic. Each composes and stores what it posts (and its tap keys)
in ONE committed transaction, then deposits the body with Casa as the LAST step, and
returns the reference. Nothing here ever returns a key."""
from __future__ import annotations

import json
import sys

import casa_broker
import db
import keys
import views


def _proposal(text, buttons, revision) -> str:
    return json.dumps({"text": views.deposit_safe(text), "revision": revision,
                       "buttons": [{"label": label, "call": {"tool": tool, "arguments": args}}
                                   for label, tool, args in buttons]},
                      ensure_ascii=False)


def _keyed(conn, render_id, specs) -> list:
    """The buttons of `specs` (views.buttons_for, cards.buttons), each writing one with a
    fresh key stored under the rendering. A key_spec is (action, pid, doc_id): the key binds
    all three (simple loop §1: a named candidate's document). Inside the caller's
    transaction."""
    out = []
    for label, tool, args, key_spec in specs:
        args = dict(args)
        if key_spec is not None:
            key = keys.mint()
            action, pid, doc_id = key_spec
            keys.store_render(conn, render_id, action, pid, key, doc_id=doc_id)
            args["key"] = key
        out.append((label, tool, args))
    return out


def show_view(conn, *, view=None, quarter=None, pid=None, page=None, after=None,
              render_id=None, prev=None) -> dict:
    """§7.1: render exactly as build_review does (or, with render_id, re-post that stored
    rendering — the job's `view` unit, §5) and deposit it as a proposal: text = the page,
    buttons = §7.2, revision = view:<view>:<quarter>. `prev` is the predecessor page the
    More button (or a typed "more") names (binding V1). Simple loop §1: a cards rendering
    (cards.KINDS) re-posts with its own keyboard (cards.buttons) and the revision of its
    walk; view="open" composes and posts a fresh open-items card (§1 Recovery)."""
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
            rid = cards.compose_open(conn, quarter or cards.main_quarter(conn))
            r = conn.execute("SELECT * FROM renders WHERE render_id=?", (rid,)).fetchone()
        else:
            out = views.review_in_tx(conn, view or "status", quarter, pid, page, after,
                                     prev=prev)
            r = conn.execute("SELECT * FROM renders WHERE render_id=?",
                             (out["render_id"],)).fetchone()
        scope = json.loads(r["scope_json"])
        if r["kind"] in cards.KINDS:
            # keys minted and posted_seq stamped by deposit_of, as for a tap's `next`
            value = json.dumps(cards.deposit_of(conn, r["render_id"]), ensure_ascii=False)
        else:
            buttons = _keyed(conn, r["render_id"], views.buttons_for(conn, r))
            revision = f"view:{r['kind']}:{scope.get('quarter') or ''}"[:64]
            value = _proposal(r["text"], buttons, revision)
            # r3 #3: stamped posted before the deposit (which stays last) — a view posted by
            # a tap's stored call is never marked delivered, and a quote of it binds it. The
            # stamp means "a deposit was attempted at seq n": monotone, never restored on a
            # refusal, so a late refusal cannot erase a later post's stamp (binding §3, r4
            # Terra S1). It only makes the row a quote candidate, and recency never picks
            # among those (R1)
            conn.execute("UPDATE renders SET posted_seq=? WHERE render_id=?",
                         (db.next_seq(conn), r["render_id"]))
    ref = casa_broker.deposit("view", value)
    return {"view": ref, "render_id": r["render_id"], "next": scope.get("next")}


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
    ref = casa_broker.deposit("results", body)
    return {"results": ref, "render_ids": [r["render_id"] for r in rows]}


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
        tag = views.tag_for(rid)
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


READING_TOO_LONG = ("That is more than I can show for one Apply — nothing was read. Send it "
                    "in shorter parts.")


def propose_reading(conn, text, quoted=None) -> dict:
    """§8: read the operator's words; with a write, post them as a reading to Apply."""
    import reply
    if not isinstance(text, str) or not text.strip():
        raise db.Refusal("text is the operator's words, verbatim")
    if quoted is not None and not isinstance(quoted, str):
        raise db.Refusal("quoted is the quoted post's text, as the desk context gave it")
    with db.tx(conn):
        out = reply.reading_in_tx(conn, text, quoted)
        base = {k: out[k] for k in ("instructions", "reshow", "understood", "not_a_reply")}
        if not out["plan"]:
            say = "\n".join(out["receipt"])
            return {"reading": None,
                    "say": views.fit_message(say, views.FIT_CLOSING) if say else "", **base}
        body = ["I read this as:"] + [f"· {x}" for x in out["propose"]]
        if out["unresolved"]:
            body += ["", "Not included:"] + [f"· {x}" for x in out["unresolved"]]
        body += [x for x in out["receipt"] if x.startswith("Not rebuilding yet")]
        text_ = "\n".join(body)
        if not views.fits_proposal(text_):
            raise db.Refusal(READING_TOO_LONG)
        now = db.now()
        # a newer reading supersedes an unanswered older one (§8: `↻ replaced`)
        conn.execute("UPDATE readings SET state='stale', settled_at=? WHERE state='open'", (now,))
        key = keys.mint()
        rid = conn.execute("INSERT INTO readings(key, text, quoted, render_id, plan_json,"
                           " created_seq, created_at, state) VALUES (?,?,?,?,?,?,?, 'open')",
                           (key, text, quoted, out["render_id"], db.canonical(out["plan"]),
                            db.next_seq(conn), now)).lastrowid
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
    return {"reading": ref, "reading_id": rid, **base}


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
        lines, buttons = [ACCOUNT_Q], []
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
