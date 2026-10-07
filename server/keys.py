"""S7 §7.5: tap keys. 128 random bits minted when a proposal is rendered, stored with the
rendering (render_keys) or the reading (readings.key) or the account page
(account_choices.key), carried ONLY inside the deposit Casa takes out of band, never in a
tool's result. A writing tool refuses a call whose key does not match; the first accepted
use spends it."""
from __future__ import annotations

import re
import secrets

import db

KEY_RE = re.compile(r"^[0-9a-f]{32}$")
NO_LONGER = "That button no longer applies — nothing was changed. Ask me for the list again."


def mint() -> str:
    return secrets.token_hex(16)


def store_render(conn, render_id, action, pid, key, doc_id=None) -> None:
    """`doc_id`: a named candidate's document (simple loop §1: the key binds it too)."""
    assert conn.in_transaction
    conn.execute("INSERT INTO render_keys(key, render_id, action, pid, created_at, doc_id)"
                 " VALUES (?,?,?,?,?,?)", (key, render_id, action, pid, db.now(), doc_id))


def spend_render(conn, key, render_id, action, pid, doc_id=None) -> None:
    assert conn.in_transaction
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise db.Refusal(NO_LONGER)
    row = conn.execute("SELECT * FROM render_keys WHERE key=?", (key,)).fetchone()
    if (row is None or row["spent_at"] is not None or row["render_id"] != render_id
            or row["action"] != action or row["pid"] != pid or row["doc_id"] != doc_id):
        raise db.Refusal(NO_LONGER)
    conn.execute("UPDATE render_keys SET spent_at=? WHERE key=?", (db.now(), key))
