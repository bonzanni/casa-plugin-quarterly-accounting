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
