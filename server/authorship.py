"""Operator authorship is checked, not declared (spec §Match records), and
the revision the operator was SHOWN is what binds (spec §Flows). An
operator-authored write must carry the render_id of the most recent
DELIVERED rendering that showed the item; the revision it names must be the
one recorded there, and still the current one. A guard against a mistaken
caller, not a security boundary: both callers are trusted models."""
from __future__ import annotations

import json

import db


class NotShown(db.Refusal):
    def __init__(self, pid, message):
        super().__init__(message)
        self.pid = pid


class Stale(db.Refusal):
    def __init__(self, pid, message):
        super().__init__(message)
        self.pid = pid


def _shown(conn, pid, render_id):
    s = conn.execute("SELECT * FROM shown WHERE pid=?", (pid,)).fetchone()
    if s is None or s["render_id"] != render_id:
        raise NotShown(pid, "the operator has not been shown this item in its current form; "
                            "show it and apply nothing yet")
    return s


def require_projection_shown(conn, pid, render_id, expected_revision) -> None:
    s = _shown(conn, pid, render_id)
    current = conn.execute("SELECT revision FROM projections WHERE pid=?", (pid,)).fetchone()[0]
    if s["projection_revision"] != expected_revision or current != expected_revision:
        raise Stale(pid, "this changed since the operator looked; show the current facts")


def require_match_shown(conn, pid, match_id, render_id, expected_revision) -> None:
    s = _shown(conn, pid, render_id)
    shown_rev = json.loads(s["match_revisions_json"]).get(str(match_id))
    current = conn.execute("SELECT revision FROM match_state WHERE match_id=?",
                           (match_id,)).fetchone()
    if shown_rev is None:
        raise NotShown(pid, "that pairing was not on the view the operator saw")
    if shown_rev != expected_revision or current is None or current[0] != expected_revision:
        raise Stale(pid, "this pairing changed since the operator looked; show the current facts")
