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


def _keyed(conn, render_id, specs) -> tuple:
    """The buttons of `specs` (views.buttons_for), each writing one with a fresh key stored
    under the rendering, and the keys minted. Inside the caller's transaction."""
    out, minted = [], []
    for label, tool, args, key_spec in specs:
        args = dict(args)
        if key_spec is not None:
            key = keys.mint()
            keys.store_render(conn, render_id, key_spec[0], key_spec[1], key)
            args["key"] = key
            minted.append(key)
        out.append((label, tool, args))
    return out, minted


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
        buttons, minted = _keyed(conn, r["render_id"], views.buttons_for(conn, r, walk))
        revision = f"view:{r['kind']}:{scope.get('quarter') or ''}"[:64]
        value = _proposal(r["text"], buttons, revision)
    try:
        ref = casa_broker.deposit("view", value)
    except casa_broker.DepositFailed:
        # no tap can carry a key Casa never took: an unspent key names a deposited page
        if minted:
            with db.tx(conn):
                keys.revoke_render(conn, minted)
        raise
    return {"view": ref, "render_id": r["render_id"], "next": scope.get("next")}
