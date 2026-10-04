"""S7: the posting tools' logic. Each composes and stores what it posts (and its tap keys)
in ONE committed transaction, then deposits the body with Casa as the LAST step, and
returns the reference. Nothing here ever returns a key."""
from __future__ import annotations

import json
import re

import casa_broker
import db
import keys
import views

RENDER_ID = re.compile(r"^r\d{1,18}$")


def _proposal(text, buttons, revision) -> str:
    return json.dumps({"text": views.deposit_safe(text), "revision": revision,
                       "buttons": [{"label": label, "call": {"tool": tool, "arguments": args}}
                                   for label, tool, args in buttons]},
                      ensure_ascii=False)


def _keyed(conn, render_id, specs) -> list:
    """The buttons of `specs` (views.buttons_for), each writing one with a fresh key stored
    under the rendering. Inside the caller's transaction."""
    out = []
    for label, tool, args, key_spec in specs:
        args = dict(args)
        if key_spec is not None:
            key = keys.mint()
            keys.store_render(conn, render_id, key_spec[0], key_spec[1], key)
            args["key"] = key
        out.append((label, tool, args))
    return out


def show_view(conn, *, view=None, quarter=None, pid=None, page=None, after=None, walk=None,
              render_id=None) -> dict:
    """§7.1: render exactly as build_review does (or, with render_id, re-post that stored
    rendering — the job's `view` unit, §5) and deposit it as a proposal: text = the page,
    buttons = §7.2, revision = view:<view>:<quarter>."""
    if walk is not None and (not isinstance(walk, str) or not RENDER_ID.fullmatch(walk)):
        raise db.Refusal("walk is the render id the One by one button carried")
    with db.tx(conn):
        if render_id is not None:
            if any(v is not None for v in (view, quarter, pid, page, after)):
                raise db.Refusal("render_id re-posts a stored rendering: name nothing else")
            r = conn.execute("SELECT * FROM renders WHERE render_id=?",
                             (render_id,)).fetchone() if isinstance(render_id, str) else None
            if r is None or r["kind"] not in views.VIEWS:
                raise db.Refusal("there is no view rendering by that id")
            if not views.fits_proposal(r["text"]):
                raise db.Refusal("that rendering is too long for buttons: post it with "
                                 "post_results(render_ids=[…]) instead")
        else:
            out = views.review_in_tx(conn, view or "status", quarter, pid, page, after)
            r = conn.execute("SELECT * FROM renders WHERE render_id=?",
                             (out["render_id"],)).fetchone()
        scope = json.loads(r["scope_json"])
        buttons = _keyed(conn, r["render_id"], views.buttons_for(conn, r, walk))
        revision = f"view:{r['kind']}:{scope.get('quarter') or ''}"[:64]
        value = _proposal(r["text"], buttons, revision)
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
PKG_REFUSED = "the package could not be sent under its name ({code}) — ask again"


def post_package(conn, delivery_id, package_token=None) -> dict:
    """§6.1: deposit a staged package as an operator_file — the staged path (Casa claims and
    consumes it), kind zip, the package's filename as the delivered name (S7a), and one
    caption line. A deposit Casa refuses settles the send `failed` (the staged copy taken
    back) and closes its request with a package-stopped notice — never a send under the
    storage name."""
    import alerts, dates, delivery, passes
    with db.tx(conn):
        d = conn.execute("SELECT * FROM deliveries WHERE delivery_id=?",
                         (delivery_id,)).fetchone()
        if d is None or d["package_id"] is None:
            raise db.Refusal("post_package posts a staged package: pass its delivery_id")
        if (d["status"] != "staged" or d["revoked_at"] or d["withdrawn_at"]
                or d["channel"] != "telegram"):
            raise db.Refusal("that send is no longer waiting to go out — nothing was posted")
        req = conn.execute("SELECT * FROM package_requests WHERE delivery_id=?",
                           (delivery_id,)).fetchone()
        if req is not None:
            passes.check_package_token(conn, req["request_id"], package_token)
        pk = conn.execute("SELECT * FROM packages WHERE package_id=?",
                          (d["package_id"],)).fetchone()
        line = pk["caption"].split("\n", 1)[0]
        if d["as_built"]:
            line += f" · built {dates.short_day(pk['built_at'])}, as it was then"
        caption = views.clip(views.caption_safe(views.esc(line, plain=True)), CAPTION_MAX)
        conn.execute("UPDATE deliveries SET lease_at=? WHERE delivery_id=?",
                     (db.now(), delivery_id))
    try:
        ref = casa_broker.deposit("package", d["staged_path"], caption=caption, kind="zip",
                                  filename=pk["filename"])
    except casa_broker.DepositFailed as exc:
        with db.custody_lock():
            with db.tx(conn):
                delivery.withdraw(conn, [dict(d)], refusal="could not take back the staged "
                                                           "package — nothing changed")
                now = db.now()
                conn.execute("UPDATE deliveries SET status='failed', settled_at=?,"
                             " withdrawn_at=? WHERE delivery_id=?", (now, now, delivery_id))
                if req is not None:
                    conn.execute("UPDATE package_requests SET state='stopped', reason=?,"
                                 " updated_at=? WHERE request_id=?",
                                 (PKG_REFUSED.format(code=exc.code), now, req["request_id"]))
                    alerts.raise_package(conn, "package-stopped",
                                         f"request:{req['request_id']}:posted",
                                         quarter=pk["quarter"],
                                         reason=f"it could not be posted ({exc.code})")
        raise db.Refusal(PKG_REFUSED.format(code=exc.code))
    return {"package": ref, "delivery_id": delivery_id, "filename": pk["filename"]}


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
            return {"reading": None, "say": views.fit_message(say) if say else "", **base}
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
