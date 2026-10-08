"""The job's claim and its run's closing words (simple loop, design rev 17 §2): one Casa
job run is one pass; the cursor is loop.next_unit. A job turn's first, token-less job_next
claims: the claim rotates the token and records itself in `claims`, and only the newest
claim's token may act (check_claim)."""
from __future__ import annotations

import json
import re

import db
import passes

JOB_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")
TURNS_PER_BATCH = 80       # turnsPerBatch: the manifest's casa.jobs


def live_job_pass(conn):
    m = passes._marker(conn)
    if m is None or not m["live"]:
        return None
    p = conn.execute("SELECT * FROM passes WHERE pass_id=?", (m["pass_id"],)).fetchone()
    return p if p is not None and p["protocol"] == "job" else None


def check_claim(conn, token) -> None:
    assert conn.in_transaction
    if token is None:
        raise db.Refusal("a job turn starts with job_next(job_id=…): pass the pass_token it gave you")
    top = conn.execute("SELECT max(gen) FROM claims").fetchone()[0]
    if top is None or int(token) != top:
        raise db.Refusal("this job turn is no longer the current one (a newer turn claimed "
                         "the work); stop — nothing was written")
    p = live_job_pass(conn)
    if p is not None and p["holder_job"] == conn.execute(
            "SELECT job_id FROM claims WHERE gen=?", (int(token),)).fetchone()[0]:
        # the pass fence is the holder's: a finished run's late claim, which leaves another
        # job's live pass alone (claim), answers only its own `complete`
        passes.check_token(conn, token)


# #45: the line Casa (0.344.31 on) writes right after the first `Job id:` of the job's launch
# prompt and brief, as the job model copies it. Only §4.1's implicit check reads it: the
# trigger it records. `scheduled` and `agent` keep today's cron; anything else, and no line
# (an older Casa), is "Casa did not say" — today's cron too. The job model sometimes copies
# only the value (live 2026-10-07: "operator"), so the bare value counts as its line.
STARTED_BY = {"Started by: operator": "operator", "Started by: scheduled": "cron",
              "Started by: agent": "cron", "operator": "operator", "scheduled": "cron",
              "agent": "cron"}


def starter_trigger(started_by) -> str:
    """The implicit check's trigger for the copied starter line, or its bare value:
    surrounding whitespace (CR and LF included) stripped, then an exact match; otherwise
    cron."""
    if not isinstance(started_by, str):
        return "cron"
    return STARTED_BY.get(started_by.strip(), "cron")


def _queued_any(conn) -> bool:
    return conn.execute("SELECT 1 FROM work_requests WHERE state='queued'").fetchone() is not None


def _completed(conn, job_id) -> bool:
    r = conn.execute("SELECT completed_at FROM runs WHERE job_id=?", (job_id,)).fetchone()
    return r is not None and r[0] is not None


def claim(conn, job_id, started_by=None) -> int:
    """A job turn's token-less job_next (simple loop §2: one Casa job run is one pass).
    Under the custody lock, taken before the transaction (the stalled-send recovery removes
    staged bytes), in one transaction:
    1. a posted send nobody recorded is settled `uncertain` (D15);
    2. the token rotates and the claim is recorded with its batch (`_batch_of`);
    3. a job id's first claim makes its run: started_by "operator" iff Casa's starter line
       says so (#45), else "scheduled";
    4. a live pass held by another job id is ended `interrupted` (state is persisted:
       nothing to adopt), its taken requests queued again for this run;
    5. the run's one pass starts on its first claim (loop.start_pass); a later claim moves
       the live pass's marker to its token;
    6. a first claim that finds nothing queued records the implicit check (§4.1), and the
       pass takes every queued request (loop.take);
    7. a stalled staged send is recovered (§6.1)."""
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise db.Refusal("job_id is the `Job id:` line of your brief, as given")
    import delivery, loop
    with db.custody_lock():                 # the stalled-send recovery removes staged bytes
        with db.tx(conn):
            # r3 #1 (INV-S7-6): a posted send has left the plugin — before anything can
            # requeue its ask, it is settled `uncertain`, whatever its lease (a late
            # receipt still upgrades it; "send it again" is the only second file)
            for d in delivery.posted_unrecorded(conn):
                delivery.recover_staged(conn, d)
            first = conn.execute("SELECT 1 FROM claims WHERE job_id=?",
                                 (job_id,)).fetchone() is None
            p = live_job_pass(conn)
            if p is not None and p["holder_job"] != job_id and _completed(conn, job_id):
                p = None            # a finished run never ends another job's live pass
            token = passes.rotate(conn)
            changed = p is not None and p["holder_job"] != job_id
            conn.execute("INSERT INTO claims(gen, job_id, at, batch, seq) VALUES (?,?,?,?,?)",
                         (token, job_id, db.now(), _batch_of(conn, job_id, token, changed),
                          db.next_seq(conn)))
            if first:
                conn.execute("INSERT OR IGNORE INTO runs(job_id, started_by) VALUES (?,?)",
                             (job_id, "operator" if starter_trigger(started_by) == "operator"
                              else "scheduled"))
            if p is not None:
                conn.execute("UPDATE pass_marker SET generation=? WHERE id=1", (token,))
                if changed:
                    # its requests carry over: queued again, this run's pass takes them
                    import asks
                    asks.requeue_taken(conn, p["pass_id"])
                    loop.end_pass(conn, token, "interrupted",
                                  {"interrupted_by": job_id})
            run = conn.execute("SELECT * FROM runs WHERE job_id=?", (job_id,)).fetchone()
            if run["pass_id"] is None and run["completed_at"] is None:
                loop.start_pass(conn, token, job_id)
            live = loop.run_pass(conn, conn.execute("SELECT * FROM runs WHERE job_id=?",
                                                    (job_id,)).fetchone())
            if live is not None:
                if first and not _queued_any(conn):
                    conn.execute("INSERT INTO work_requests(kind, trigger, doc_ids_json,"
                                 " created_seq, created_at, state) VALUES ('check', ?, '[]',"
                                 " ?, ?, 'queued')", (starter_trigger(started_by),
                                                      db.next_seq(conn), db.now()))
                loop.take(conn, job_id, live["pass_id"])
            for d in delivery.stalled_sends(conn, passes.LEASE_S):            # §6.1
                delivery.recover_staged(conn, d)
            return token


def _batch_of(conn, job_id, token, holder_changed) -> int:
    """The batch claim `token` belongs to (design r6, round 5): a new one — named by this
    claim's own gen — iff it is the job id's first claim, or that job id's latest claim
    belongs to a batch answered `complete`, or already holding
    TURNS_PER_BATCH claims, or the live pass's holder changes with it; otherwise the
    latest claim's batch (a re-claim inside one batch). The claim count: a turn makes one
    claim and a Casa batch has at most turnsPerBatch turns, so the window is bounded even
    when every Casa batch is cut (no call budget: Casa's cut ends every batch but the
    last)."""
    prev = conn.execute("SELECT batch FROM claims WHERE job_id=? ORDER BY gen DESC LIMIT 1",
                        (job_id,)).fetchone()
    if prev is None or holder_changed:
        return token
    closed, n = conn.execute("SELECT max(closed), count(*) FROM claims WHERE batch=?",
                             (prev["batch"],)).fetchone()
    return token if closed or n >= TURNS_PER_BATCH else prev["batch"]


def next_unit(conn, token) -> dict:
    """The cursor is the simple loop's (loop.next_unit, design rev 17 §2)."""
    import loop
    return loop.next_unit(conn, token)


# --- the job posts its own results (S7 §5) -------------------------------------------
OFFER_MAX = 2              # hand-outs of one rendering per run (§5: a broken channel)
POST_MAX = 3               # renderings per post: 3 × BODY_LIMIT + label < 12,000
POST_CHARS = 11_900        # a post's joined text: 12,000 minus the label


def offers(conn, render_id, job_id) -> int:
    """How often run `job_id` handed rendering `render_id` out (the accounts unit is keyed
    "accounts")."""
    r = conn.execute("SELECT n FROM post_offers WHERE render_id=? AND job_id=?",
                     (render_id, job_id)).fetchone()
    return r[0] if r else 0


def _offer(conn, render_id, job_id) -> None:
    conn.execute("INSERT INTO post_offers(render_id, job_id, n) VALUES (?,?,1) ON CONFLICT"
                 "(render_id, job_id) DO UPDATE SET n=n+1", (render_id, job_id))


def status(conn, job_id) -> dict:
    """Never a claim (ha-casa-app#1180; S7 §10): may this job end now? `done` once the run
    answered `complete` (runs.completed_at) — an operator-message turn in the job's topic
    calls it last, so a completion Casa refused is re-issued from the store."""
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise db.Refusal("job_id is the `Job id:` line of your brief, as given")
    with db.tx(conn):
        ok = _completed(conn, job_id)
        return {"done": ok, "text": run_end(conn, job_id)[0] if ok else None}


RUN_FINISHED = "Accounting work finished."
TOPIC_MAX = 200     # one topic line: Casa keeps a summary's or completion's first line, cut
                    # at 300 characters; the full stop line is posted by the job (S7 §5)


CARD_POSTED = "The result card is posted in the chat; there is nothing to add."

def run_end(conn, job_id) -> tuple:
    """THE closing words of job run `job_id` (PLAY T7 F2): (the completion's text, its
    progress summary), shared by job_next's `complete` and job_status. The run has one pass
    (simple loop §2): "Finished" when it ended complete (or never started), else its end
    in the operator's words, with the stored stop reason."""
    import loop, views
    r = conn.execute("SELECT p.outcome, p.report_json FROM runs u JOIN passes p ON"
                     " p.pass_id=u.pass_id WHERE u.job_id=? AND p.ended_at IS NOT NULL",
                     (job_id,)).fetchone()
    if r is None or r["outcome"] == "complete":
        # issue #47: what happened, for whoever started the run (an assistant relays it);
        # h3: as it was said at completion (runs.end_text), never re-derived later
        kept = conn.execute("SELECT end_text FROM runs WHERE job_id=?", (job_id,)).fetchone()
        if kept is not None and kept[0]:
            return kept[0], loop.WORDS["complete"]
        import binding, cards
        if binding.get(conn) is None:
            return RUN_FINISHED, loop.WORDS["complete"]
        shown = conn.execute("SELECT 1 FROM runs u JOIN renders r ON r.render_id="
                             "u.end_render_id WHERE u.job_id=? AND r.delivered_at IS NOT NULL",
                             (job_id,)).fetchone()
        if shown is not None:
            # 0.11.2 (one answer per ask; Casa #1332): the end card says it all
            return CARD_POSTED, loop.WORDS["complete"]
        line = views.clip(cards.checked_line(conn, cards.main_quarter(conn, job_id)),
                          TOPIC_MAX)
        return line, loop.WORDS["complete"]
    text = views.clip(_end_line(r["outcome"], json.loads(r["report_json"] or "{}")),
                      TOPIC_MAX)
    return text, text


def _end_line(outcome, rep) -> str:
    """One pass's end, in the operator's words."""
    if outcome == "stopped":
        reason = " ".join(str(rep.get("stopped_reason") or "").split()).rstrip(". ")
        return f"Accounting check stopped: {reason}." if reason else "Accounting check stopped."
    if outcome == "interrupted":
        if "checked" in rep and "total" in rep:
            return (f"Accounting check interrupted: {int(rep['checked'])} of "
                    f"{int(rep['total'])} new payments checked.")
        return "Accounting check interrupted before it finished."
    return "Accounting check failed."
