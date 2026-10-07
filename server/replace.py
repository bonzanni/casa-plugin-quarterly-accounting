"""Rev 18.4 §R18.3: a document the OPERATOR handed over that the job judges belongs to a
payment which already has one (machine-matched, operator-confirmed, or a machine
proposal). Nothing is replaced by the job: it records ONE question, and the run's one
message carries ONE card — "<payment> already has an invoice. Current: … New: …"
[Keep current] [Use new]. The question is bound to the pairing its card displayed (the
match and the payment's revisions, through the card's render_items: 18.4) and a payment has
at most one live question (a newer one supersedes it)."""
from __future__ import annotations

import db
import lineage
import matches

STATES = ("open", "kept", "used", "superseded")


def current(conn, pid):
    """The pairing the payment holds now — a match or a proposal's primary — as
    (match_id, state, author), or None."""
    r = conn.execute("SELECT s.match_id, s.state, s.author FROM projections p JOIN"
                     " match_state s ON s.match_id=p.current_match WHERE p.pid=? AND"
                     " s.state IN ('matched', 'proposed')", (pid,)).fetchone()
    return (r["match_id"], r["state"], r["author"]) if r is not None else None


def ask_in_tx(conn, job_id, pid, doc_id) -> int:
    """The job's `replace` decision (decide): the handed-over document `doc_id` belongs to
    `pid`, which holds a pairing. Inside the caller's transaction. Returns the question."""
    assert conn.in_transaction
    cur = current(conn, pid)
    if cur is None:
        raise db.Refusal("replace is for a payment that already has a document: match or "
                         "propose this one")
    import loop
    if doc_id not in loop.run_handover_docs(conn, job_id):
        raise db.Refusal("replace is only for a document the operator handed over in this run")
    doc = conn.execute("SELECT irrelevant FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
    if doc is None or doc["irrelevant"]:
        raise db.Refusal(f"there is no such document #{doc_id} to use")
    if any(h != pid for h, _ in matches.holders(conn, doc_id)):
        raise db.Refusal("that document is another payment's")
    if matches.taken_elsewhere(conn, doc_id, pid):
        # issue #48 (d1 Astra S1): its purchase backs another payment through a twin
        raise db.Refusal(matches.taken_refusal(conn, doc_id, pid))
    if conn.execute("SELECT doc_id FROM match_state WHERE match_id=?",
                    (cur[0],)).fetchone()[0] == doc_id:
        raise db.Refusal("the payment already holds that document: keep it")
    conn.execute("UPDATE replace_questions SET state='superseded', answered_at=? WHERE pid=?"
                 " AND state='open'", (db.now(), pid))
    return conn.execute(
        "INSERT INTO replace_questions(job_id, pid, match_id, new_doc_id, state, created_seq)"
        " VALUES (?,?,?,?, 'open', ?)", (job_id, pid, cur[0], doc_id, db.next_seq(conn))
    ).lastrowid


def get(conn, qid):
    return conn.execute("SELECT * FROM replace_questions WHERE question_id=?",
                        (qid,)).fetchone()


def open_ones(conn) -> list:
    """Every live question, oldest first. e1 (Astra S1): a question whose payment left
    scope, or no longer holds the pairing it asked about, is retired here — superseded, never
    offered — so no message advertises a card nothing can show. Issue #48 (d2 Astra S1): so
    is one whose new document's purchase another payment has taken since (the job's offer).
    Inside a transaction."""
    assert conn.in_transaction
    out = []
    for q in conn.execute("SELECT * FROM replace_questions WHERE state='open' ORDER BY"
                          " question_id").fetchall():
        p = lineage.projection(conn, q["pid"])
        cur = current(conn, q["pid"])
        if p is None or p["ended"] or p["merged_into"] is not None or cur is None \
                or cur[0] != q["match_id"] \
                or matches.taken_elsewhere(conn, q["new_doc_id"], q["pid"]):
            conn.execute("UPDATE replace_questions SET state='superseded', answered_at=?"
                         " WHERE question_id=?", (db.now(), q["question_id"]))
            continue
        out.append(q)
    return out


def new_doc_fp(conn, doc_id) -> str:
    """e1 (Astra S1): what the card showed of the handed document — its fingerprint."""
    import documents
    return documents.fingerprint(conn.execute("SELECT * FROM documents WHERE doc_id=?",
                                              (doc_id,)).fetchone())


def answer_in_tx(conn, grant, q, action, render_id, shown_fp=None) -> str:
    """[Keep current] / [Use new] on the question's card, under the tap's grant, the
    card's binding already checked (taps._card_tap: the payment and the displayed pairing
    as the card recorded them); `shown_fp` is the handed document's fingerprint as the card
    showed it (e1, Astra S1): a corrected document commits nothing. Returns the receipt."""
    if shown_fp is not None and new_doc_fp(conn, q["new_doc_id"]) != shown_fp:
        raise db.Refusal("the new document's details changed since this was shown")
    if q["state"] != "open":
        raise db.Refusal("this question was replaced by a newer one" if q["state"] ==
                         "superseded" else "this question was answered already")
    cur = current(conn, q["pid"])
    if cur is None or cur[0] != q["match_id"]:
        raise db.Refusal("the payment's document changed since this was shown")
    if action == "keep-current":
        conn.execute("UPDATE replace_questions SET state='kept', answered_at=? WHERE"
                     " question_id=?", (db.now(), q["question_id"]))
        return "Kept the current document; the new one stays filed."
    rev = conn.execute("SELECT revision FROM match_state WHERE match_id=?",
                       (q["match_id"],)).fetchone()[0]
    matches.reject_in_tx(conn, grant=grant, match_id=q["match_id"], expected_revision=rev,
                         render_id=render_id)
    matches._require_unheld(conn, q["new_doc_id"], q["pid"])
    matches._operator_pair(conn, q["pid"], q["new_doc_id"], render_id)
    lineage.settle_doc_holders(conn, q["new_doc_id"])
    conn.execute("UPDATE replace_questions SET state='used', answered_at=? WHERE"
                 " question_id=?", (db.now(), q["question_id"]))
    return "Used the new document; the old one stays filed."
