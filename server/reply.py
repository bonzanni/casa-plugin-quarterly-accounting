# server/reply.py
"""A reading (S7 §8, #121): the operations the desk read in the operator's words, applied
under a rehearsal and posted to the operator to Apply.

The desk model reads the words — which payment, which decision, in any wording or language
— and calls propose_reading with explicit operations naming payments by pid (from
reading_context). Code decides only mechanical facts: the operation is known and its
arguments well formed, a payment it names is open and on the ONE rendering the reading is
bound to (views.bound_rendering: the quoted post, else the last delivered one), and each
write's own validation (authorship.py: the revision that rendering recorded; an item it did
not show, or changed since, is refused and re-shown; the other operations still apply).

Binding (rounds-2026-10-03-s7-diff/binding/): every write reads only the bound rendering
R's record (_Scope): its render_items rows and the scope fields views.FACT_FIELDS lists.

S7 §8: nothing commits from a turn. reading_in_tx runs the operations under a rehearsal
(always rolled back) and returns the plan of writes, each with the revisions it read;
taps.apply_reading replays the stored operations under the operator's grant and commits
only an identical plan. The receipt is generated from what the replay wrote."""
from __future__ import annotations

import json
import re

import authorship
import binding
import dates
import db
import expectation
import kb
import lineage
import matches
import views
import work

# #121: every operation a reading may carry, with its arguments (all required)
OPS = {
    "confirm": ("pid",),           # yes to the payment's suggested document
    "reject": ("pid",),            # no to it (or to every candidate it shows)
    "no_document": ("pid",),       # the payment needs no document
    "needs_document": ("pid",),    # it needs one again
    "look_again": ("pid",),        # search for its document again at the next check
    "identity": ("pid", "who"),    # whose payment it is (every payment with that bank text)
    "never": ("pid",),             # the payment's vendor never needs a document
    "class_none": ("kind",),       # payslips, statements or receipts are not needed
    "stop_chasing": ("quarter",),  # stop looking for what a quarter still misses
    "zip_name": ("name",),         # the packages' file name
    "ledger_reset": (),            # the bank ledger was reset on purpose
}
OPS_MAX = 200                    # a sanity bound on the call; the card's size limit decides
CLASS_SCOPES = {"payslips": ("salary", "payroll"), "statements": ("fees", "interest", "tax"),
                "receipts": ("reimbursement",)}
OPS_HELP = ("ops is a list of 1 to %d operations, each {\"op\": …} with its arguments: "
            % OPS_MAX + "; ".join(f"{k}({', '.join(v)})" if v else k for k, v in OPS.items()))


def check_ops(ops) -> list:
    """#121: the operations' shape — the only thing code checks before running them. Returns
    them canonical (only the named arguments); refuses in words naming the operations."""
    if not isinstance(ops, list) or not 1 <= len(ops) <= OPS_MAX:
        raise db.Refusal(OPS_HELP)
    out = []
    for o in ops:
        name = o.get("op") if isinstance(o, dict) else None
        if not isinstance(name, str) or name not in OPS:
            raise db.Refusal(f"unknown operation {name!r}: {OPS_HELP}")
        c = {"op": name}
        for a in OPS[name]:
            v = o.get(a)
            ok = (isinstance(v, int) and not isinstance(v, bool) if a == "pid"
                  else isinstance(v, str) and v.strip() != "")
            if not ok:
                raise db.Refusal(f"{name} takes {a} ({'a payment id' if a == 'pid' else 'text'})")
            c[a] = v.strip() if isinstance(v, str) else v
        if name == "class_none" and c["kind"] not in CLASS_SCOPES:
            raise db.Refusal("class_none's kind is payslips, statements or receipts")
        if name == "stop_chasing":
            q = dates.normalize_quarter(c["quarter"], db.now()[:10])
            if q is None:
                raise db.Refusal("stop_chasing's quarter is written 2026-Q2")
            c["quarter"] = q
        out.append(c)
    return out


_KIND_TOKEN = re.compile(r"\b(an?) (" + "|".join(sorted(views.KIND_WORD, key=len, reverse=True))
                         + r")\b")


def _say(conn, exc) -> str:
    """A store refusal in the operator's words (pre-flight R4): the store's
    messages are written for the model that calls the tools, and a receipt
    carries no render id, no #number and no machine kind."""
    msg = str(exc)
    if "render_id" in msg:
        return "there is no sheet you have been sent yet that this could come from"

    def payment(m):
        try:
            return "another payment (" + views.headline(work.describe(conn, int(m.group(1)))) + ")"
        except db.Refusal:
            return "another payment"
    msg = re.sub(r"(?:another )?payment #(\d+)", payment, msg)
    noun = {"pairing": "pairing", "document": "document", "transaction": "payment"}
    msg = re.sub(r"\bno (pairing|document|transaction) #\d+",
                 lambda m: f"no such {noun[m.group(1)]}", msg)
    msg = re.sub(r"\b(?:the |this |that )?(pairing|document|transaction) #\d+",
                 lambda m: f"that {noun[m.group(1)]}", msg)
    msg = re.sub(r"\s*#\d+", "", msg)
    msg = re.sub(r"'([^']*)'", "“\\1”", msg)
    msg = msg.replace("the operator", "you")
    msg = _KIND_TOKEN.sub(lambda m: f"{m.group(1)} {views.KIND_WORD[m.group(2)]}", msg)
    return re.sub(r"\b([aA]) (?=[aeiouAEIOU])", r"\1n ", msg)


def _open_items(conn) -> list:
    out = []
    for pid in lineage.live_pids(conn):
        d = work.describe(conn, pid)
        if d["ended"] or d["status"] == "ineligible":
            continue
        out.append(d)
    return out


def _delivered_package_for(conn, quarter):
    return conn.execute("SELECT p.filename, d.settled_at FROM packages p JOIN deliveries d ON"
                        " d.package_id=p.package_id WHERE p.quarter=? AND d.status='delivered'"
                        " ORDER BY d.settled_at DESC LIMIT 1", (quarter,)).fetchone()


def package_lines(conn, quarters) -> list:
    """A receipt's closing lines: for each touched quarter whose package was already sent,
    say it no longer matches (a reading's receipt and a verdict's, S7 §7.3)."""
    out = []
    for q in sorted(quarters):
        pk = _delivered_package_for(conn, q)
        if pk is not None:
            out.append(f"The package sent on {dates.short_day(pk['settled_at'])} no "
                       "longer matches — I can build a fresh one.")
    return out


# A step records every binding column that decides a verdict; import bookkeeping and the
# announce flags are not among them, so an unrelated bank import (or a delivered rendering
# announcing the watermark) never stales an open reading (final fix wave T6-c, M-2)
BINDING_NOT_READ = ("row_high_water", "watermark_announced", "package_name_announced")

# S7 §8: each write's two phrasings — the proposal's line, then the Apply receipt's. Every
# value is a field (views.field; views.headline already takes its fields through it)
PHRASE = {
    "confirm": ("Confirm {h}.", "Confirmed {h}."),
    # PLAY 0.11.2: a proposal is rejected, a match is removed (plain words for what it is)
    "unpair": ("{act} {h}.", "{done} {h}."),
    "set_aside": ("Rule out {n} for {h}.", "Ruled out {n} for {h}."),
    "exempt": ("{h}: needs no document{drop}.", "{h}: needs no document{dropped}."),
    "lift": ("{h}: needs a document again.", "{h}: needs a document again."),
    "revive": ("{h}: look again at the next check.", "{h}: I'll look again at the next check."),
    "identity": ("{bank}: {who}.", "{bank}: {who}{tail}"),
    "never": ("{name}: never needs a document.", "{name}: never needs a document."),
    "class_none": ("{kind} are no longer needed.", "{kind} are no longer needed."),
    "stop": ("Stop chasing {q}: {n} still missing, no longer searched.",
             "Stopped chasing {q}: {n} still missing, no longer searched."),
    "name": ("Call the zips {slug}-….zip.", "The zips are now called {slug}-….zip."),
    "ledger_reset": ("{note}", "{note}"),
}
UNPAIR_WORDS = {True: {"act": "Reject the suggested document for",
                        "done": "Rejected the suggested document for"},
                False: {"act": "Remove the match for", "done": "Removed the match for"}}
_LIVE = "SELECT pid, revision FROM projections WHERE merged_into IS NULL AND ended IS NULL"


class _Scope:
    """THE bound rendering R's own record (binding §1): every write reads only this — R's
    render_items rows (`rev`, `mrevs`, by the pid R recorded) and the scope fields
    views.FACT_FIELDS lists (an AST pin holds this module to that list). With no R it is
    empty. A payment merged since R was posted is on R by neither pid: its old pid is no
    longer open, and R recorded no row for its survivor."""

    def __init__(self, conn, bound):
        self.conn = conn
        self.rid = bound["render_id"] if bound is not None else None
        self.kind = bound["kind"] if bound is not None else None
        scope = json.loads(bound["scope_json"]) if bound is not None else {}
        rows = conn.execute("SELECT * FROM render_items WHERE render_id=?",
                            (self.rid,)).fetchall() if bound is not None else []
        self.rev = {r["pid"]: r["projection_revision"] for r in rows}
        self.mrevs = {r["pid"]: json.loads(r["match_revisions_json"]) for r in rows}
        # r1 (Terra S1): the payments R recorded, by the pid it recorded — a merge's survivor
        # is not on R (it was never shown in that form): the desk posts its own card
        self.pids = set(self.rev)
        self.quarter = scope.get("quarter")
        # r1 (Astra S2): the ref R printed on each payment ("Adobe · … · ref e40c"), so the
        # desk can tell two otherwise-equal payments apart as the operator does
        self.refs = {}
        for ref, ps in (scope.get("refs") or {}).items():
            for p in (ps if isinstance(ps, list) else [ps]):
                self.refs[p] = ref
        # a list page's continuation (reading_context). r3 #4: a view page an earlier version
        # composed stored no `next` — "more" on it starts the same view again at page 1 (an
        # explicit null stays "nothing more")
        self.next = scope.get("next")
        if bound is not None and self.kind in views.VIEWS and "next" not in scope:
            self.next = {"view": self.kind, "page": 1,
                         **({"quarter": self.quarter} if self.quarter else {}),
                         **({"pid": scope.get("pid")} if self.kind == "item" else {})}


class _Run:
    def __init__(self, conn, grant, bound):
        self.conn, self.grant, self.bound = conn, grant, bound
        self.scope = _Scope(conn, bound)
        self.lines, self.reshow = [], []
        self.plan, self.propose, self.unresolved_lines = [], [], []
        self.touched_quarters = set()

    def recorded(self, d, cur) -> bool:
        """§2 #10: the pairing a verdict judges is the one R recorded — False when R
        recorded the payment with another (or no) pairing."""
        return d["pid"] not in self.scope.rev or \
            str(cur["match_id"]) in self.scope.mrevs[d["pid"]]

    def note(self, line):
        """A line that is not a write: said in the receipt, and listed in the proposal as
        not included."""
        self.lines.append(line)
        self.unresolved_lines.append(line)

    def result(self) -> dict:
        return {"plan": self.plan, "propose": self.propose, "unresolved": self.unresolved_lines,
                "receipt": self.lines, "reshow": self.reshow,
                "quarters": sorted(self.touched_quarters)}

    def write(self, op, params, fn, phrase_args, check=None):
        """THE one way an operation writes (S7 §8): `fn` runs in the savepoint `clause`; the
        step records `op`, its parameters, the revision BEFORE the write of every live
        payment whose revision it changed (`read`), and the binding row it saw. `check(read)`
        may refuse inside the savepoint. Returns (fn's result, read)."""
        conn = self.conn
        b = conn.execute("SELECT * FROM binding").fetchone()
        with db.savepoint(conn, "clause"):
            before = dict(conn.execute(_LIVE).fetchall())
            res = fn()
            after = dict(conn.execute(_LIVE).fetchall())
            read = {pid: rev for pid, rev in before.items() if after.get(pid) != rev}
            if check is not None:
                check(read)
            args = phrase_args(res) if callable(phrase_args) else phrase_args
        step = {"op": op, **params, "read": {str(p): r for p, r in sorted(read.items())},
                "binding": ({k: b[k] for k in b.keys() if k not in BINDING_NOT_READ}
                            if b is not None else None)}
        self.plan.append(json.loads(db.canonical(step)))
        self.propose.append(PHRASE[op][0].format(**args))
        self.lines.append(PHRASE[op][1].format(**args))
        return res, read

    def guarded(self, d, op, params, fn, phrase_args):
        """One payment's write. `params` (or the callable building them: the binding is
        read there) are the step's; `fn(params)` writes. A refusal is said in the receipt
        and leaves the step out of the plan."""
        try:
            p = params() if callable(params) else params
            res, _ = self.write(op, p, lambda: fn(p), phrase_args)
        except (authorship.NotShown, authorship.Stale) as exc:
            # a merged payment's refusal names its survivor: that is what is shown next
            pid = getattr(exc, "pid", None) or d["pid"]
            if pid in self.reshow:
                return None                 # one payment is re-shown once, and said once
            self.reshow.append(pid)
            word = "changed since you saw it" if isinstance(exc, authorship.Stale) else \
                "hasn't been shown to you in this form yet"
            self.note(f"{views.headline(d)} {word} — here it is now; nothing applied.")
            return None
        except db.Refusal as exc:
            self.note(f"{views.headline(d)}: not applied — {_say(self.conn, exc)}")
            return None
        if d["quarter"]:
            self.touched_quarters.add(d["quarter"])
        return res

    def setting(self, op, params, fn, phrase_args, what):
        """A store-wide setting (stop chasing, the zip name, the ledger reset) — guarded like
        _broad, so a refusal rides in the same receipt beside the other operations."""
        try:
            res, _ = self.write(op, params, fn, phrase_args)
        except db.Refusal as exc:
            self.note(f"{what}: not applied — {_say(self.conn, exc)}.")
            return None
        return res


def _bind_projection(conn, run, d):
    """(render_id, revision, bind): the bound rendering's own row for the payment, or
    NotShown (binding §2 #5)."""
    if d["pid"] not in run.scope.rev:
        raise authorship.NotShown(d["pid"], "not shown")
    return run.scope.rid, run.scope.rev[d["pid"]], "rendered"


def _bind_match(conn, run, d, match_id):
    """The revision R recorded for the pairing, or NotShown (§2 #6: no fallback)."""
    rev = run.scope.mrevs.get(d["pid"], {}).get(str(match_id))
    if rev is None:
        raise authorship.NotShown(d["pid"], "not shown")
    return run.scope.rid, rev, "rendered"


def _bind_candidates(conn, run, d) -> dict:
    """A set-aside's step parameters: every displayed candidate, each bound to R, which
    must record them all (§2 #7; a continued page records its predecessor's, V1)."""
    mids = [c["match_id"] for c in d["candidates"]]
    mrevs = run.scope.mrevs.get(d["pid"])
    if mrevs is None or any(str(m) not in mrevs for m in mids):
        raise authorship.NotShown(d["pid"], "not shown")
    return {"pid": d["pid"], "bound": [[m, run.scope.rid, mrevs[str(m)]] for m in mids],
            "bind": "rendered"}


def _match_params(conn, run, d, match_id):
    rid, rev, bind = _bind_match(conn, run, d, match_id)
    return {"pid": d["pid"], "match_id": match_id, "render_id": rid, "rev": rev, "bind": bind}


def _projection_params(conn, run, d):
    rid, rev, bind = _bind_projection(conn, run, d)
    return {"pid": d["pid"], "render_id": rid, "rev": rev, "bind": bind}


NOT_ON_POST = ("payment {pid} is not on the post the operator's words are about: post its own "
               "card instead (show_view(view=\"item\", pid={pid})) — nothing was read")
NOT_OPEN = "there is no open payment {pid}: take the pid from reading_context"


def _target(conn, run, op, items) -> dict:
    """The open payment a pid operation names, on R — or a refusal to the desk (#121: a
    mechanical fact about the call, never the operator's to read)."""
    pid = op["pid"]
    if pid not in items:
        # r1 (Terra S1): a merged payment's old pid never stands for its survivor — the
        # survivor is decided from its own place on the post, through reading_context
        raise db.Refusal(NOT_OPEN.format(pid=pid))
    if pid not in run.scope.pids:
        raise db.Refusal(NOT_ON_POST.format(pid=pid))
    return work.describe(conn, pid)     # as it stands after the reading's earlier operations


PID_OPS = ("confirm", "reject", "no_document", "needs_document", "look_again", "identity",
           "never")


def _run(conn, ops, grant, bound) -> "_Run":
    """The operations applied inside the caller's transaction (S7 §8): every write under
    `grant` and inside a savepoint of its own, bound to `bound` (a renders row or None)."""
    run = _Run(conn, grant, bound)
    items = {d["pid"] for d in _open_items(conn)}
    for op in ops:
        _op(conn, run, op, items)
    return run


def _op(conn, run, op, items) -> None:
    name = op["op"]
    if name in PID_OPS:
        d = _target(conn, run, op, items)
        if name == "never":
            # a vendor-wide rule, bound by provenance: the payee of a payment R records; its
            # effect set is listed in the proposal (_broad)
            who = d["counterparty"]
            _broad(conn, run, "never", {"scope": who}, lambda: kb.set_expectation_in_tx(
                       conn, scope_type="counterparty", scope=who, kind="none",
                       author="operator", render_id=_provenance(run), grant=run.grant),
                   {"name": views.field(who)})
        else:
            _one(conn, run, name, d, op)
    elif name == "class_none":
        k = op["kind"]
        # R2: R records at least one payment the class's chain scope covers
        if not any(_class_covers(conn, p, CLASS_SCOPES[k]) for p in run.scope.pids):
            raise db.Refusal(f"the post the operator's words are about shows no payment "
                             f"{k} would cover — nothing was read")
        _broad(conn, run, "class_none", {"scopes": list(CLASS_SCOPES[k])},
               lambda: [kb.set_expectation_in_tx(
                   conn, scope_type="chain", scope=scope, kind="none", author="operator",
                   render_id=_provenance(run), grant=run.grant) for scope in CLASS_SCOPES[k]],
               {"kind": k.capitalize()})
    elif name == "stop_chasing":
        q = op["quarter"]
        if run.setting("stop", {"quarter": q},
                       lambda: work.stop_chasing_in_tx(conn, q, grant=run.grant),
                       lambda res: {"q": dates.quarter_label(q),
                                    "n": len(res["accepted_missing"])},
                       f"Still chasing {dates.quarter_label(q)}"):
            run.touched_quarters.add(q)
    elif name == "zip_name":
        run.setting("name", {"slug": binding.slug(op["name"])},
                    lambda: binding.set_package_name_in_tx(conn, op["name"], grant=run.grant),
                    lambda res: {"slug": views.field(res["package_name"])}, "The zip name")
    elif name == "ledger_reset":
        run.setting("ledger_reset", {},
                    lambda: binding.acknowledge_ledger_reset_in_tx(conn, grant=run.grant),
                    lambda res: {"note": res["note"]}, "The bank ledger reset")


def reading_in_tx(conn, ops, quoted=None) -> dict:
    """S7 §8 propose: what `ops` (check_ops'd) would do, learnt by running them under a
    rehearsal that is always rolled back. Nothing it wrote survives. Bound to ONE rendering
    (binding §1). A quote that binds no one rendering raises views.QuoteRefusal."""
    import authority
    bound = views.bound_rendering(conn, quoted)
    with authority.rehearsal(conn) as r:
        out = _run(conn, ops, r, bound).result()
    out["render_id"] = bound["render_id"] if bound is not None else None
    return out


def replay(conn, row, grant) -> dict:
    """S7 §8 apply: the stored operations run again under the operator's grant, against the
    rendering they were bound to; the caller compares the plan and commits or rolls back.
    A refusal (or a reading stored before #121, whose `text` is words) raises db.Refusal."""
    try:
        ops = check_ops(json.loads(row["text"]))
    except ValueError:
        raise db.Refusal("not a stored reading") from None
    bound = (conn.execute("SELECT * FROM renders WHERE render_id=?", (row["render_id"],))
             .fetchone() if row["render_id"] else None)
    return _run(conn, ops, grant, bound).result()


# --- reading_context (#121) ---------------------------------------------------------------
CONTEXT_MAX = 150                # open payments listed; the post's own always come first
STATE_WORDS = {"pending": "waiting on the bank", "unclassified": "not classified",
               "matched": "matched", "proposed": "suggested", "missing": "missing a document",
               "not_needed": "no document needed"}


def _state(d) -> str:
    """One word for where the payment stands, from the cards' own partition."""
    import cards
    bucket = cards._bucket(d)
    if bucket != "pending" and d["candidates"]:
        return "several suggested"
    if bucket != "pending" and views._needs_check(d):
        return "suggested"
    return STATE_WORDS[bucket]


def _doc_line(doc) -> str:
    import amounts
    parts = [views.KIND_WORD.get(doc["kind"], "document")
             + (f" {doc['number']}" if doc.get("number") else ""), doc.get("issuer") or ""]
    if doc.get("amount_minor") is not None:
        parts.append(amounts.fmt(doc["amount_minor"], doc["currency"]))
    if doc.get("date"):
        parts.append(dates.short_day(doc["date"]))
    return " · ".join(p for p in parts if p)


def context(conn, quoted=None) -> dict:
    """#121: the facts the desk reads the operator's words against — the post they are about
    (the quoted one, else the last delivered: views.bound_rendering), every open payment
    with its pid (the post's own first, `on_post`), the post's continuation (`next`) and its
    render id. Reads only; posts nothing."""
    try:
        bound = views.bound_rendering(conn, quoted)
    except views.QuoteRefusal as exc:
        return {"say": exc.line, "show_view": exc.view}
    sc = _Scope(conn, bound)
    items = _open_items(conn)
    items.sort(key=lambda d: d["date"] or "", reverse=True)
    items.sort(key=lambda d: d["pid"] not in sc.pids)          # stable: the post's own first
    out = []
    for d in items[:CONTEXT_MAX]:
        e = {"pid": d["pid"], "line": views.unesc(views.headline(d)), "quarter": d["quarter"],
             "state": _state(d), "on_post": d["pid"] in sc.pids}
        if d["pid"] in sc.refs:
            e["ref"] = sc.refs[d["pid"]]
        if d["current"] is not None:
            e["document"] = _doc_line(d["current"]["document"])
        if d["candidates"]:
            e["candidates"] = [_doc_line(c["document"]) for c in d["candidates"]]
        out.append(e)
    post = {"kind": sc.kind, "quarter": sc.quarter} if bound is not None else None
    return {"post": post, "items": out, "more_open": max(0, len(items) - CONTEXT_MAX),
            "next": sc.next, "render_id": sc.rid}


def _broad(conn, run, op, params, change, phrase_args) -> None:
    """A vendor- or class-wide operator change — or an identity, which reaches every
    payment with that bank text — binds by provenance (binding R2: the caller checked that
    the bound rendering shows what the operation names). Its effect set, every payment whose
    revision the change moves (the step's `read`), is computed in this rehearsal and listed
    in the proposal, one headline each and all of them (round p6, Terra S1; #121 d1: never
    truncated — a list that cannot fit is refused by the reading's size limit); `read` is in
    the plan, so Apply's replay-and-compare refuses a changed set."""
    try:
        _, read = run.write(op, params, change, phrase_args)
    except db.Refusal as exc:
        run.note(f"Not applied — {_say(conn, exc)}.")
        return
    heads = [views.headline(work.describe(conn, pid)) for pid in sorted(read)]
    if heads:
        n = len(heads)
        run.propose[-1] += (f" It changes {n} payment{'s' if n != 1 else ''}:\n"
                            + "\n".join(f"  {h}" for h in heads))
    for pid in read:                       # a package build covers the quarters it changed (p8)
        q = work.describe(conn, pid)["quarter"]
        if q:
            run.touched_quarters.add(q)


def _provenance(run):
    """The provenance stamp of an operator's broad rule (`never`, `class_none`): the
    rendering the reading is bound to (S7 §8)."""
    return run.bound["render_id"] if run.bound is not None else None


def _class_covers(conn, pid, scopes) -> bool:
    """R2: a chain scope of the class covers the payment — the row and key its
    classification selects meet expectation.normalize_scope's (rows, key) for one of
    `scopes`, as expectation.derive applies an override."""
    try:
        proj = lineage.projection(conn, pid)
        row = lineage.live_row(conn, proj)
    except db.Refusal:
        return False
    if row is None:
        return False
    tags = json.loads(proj["class_tags_json"]) if proj["class_tags_json"] else []
    if expectation.classification_state(tags) != "classified":
        return False
    chosen = expectation.decisive(tags, row["direction"])
    if chosen is None:
        return False
    r, key = chosen
    for scope in scopes:
        norm = expectation.normalize_scope(kb.parse_scope(scope))
        if norm is not None and r in norm[0] and norm[1] <= key:
            return True
    return False


def _confirm(conn, run, d, cur):
    run.guarded(d, "confirm", lambda: _match_params(conn, run, d, cur["match_id"]),
                lambda p: matches.confirm_in_tx(
                    conn, grant=run.grant, match_id=p["match_id"], expected_revision=p["rev"],
                    render_id=p["render_id"], bind=p["bind"]),
                {"h": views.headline(d)})


def _set_aside_all(conn, run, p) -> dict:
    """"Wrong" on a payment with several displayed candidates sets them ALL
    aside or none (fix round 1): inside the operation's savepoint every candidate
    is checked against the revision the operator was shown BEFORE the first
    write; any refusal rolls the whole set back and the payment is re-shown."""
    for mid, rid, rev in p["bound"]:
        authorship.require_match(conn, p["pid"], mid, rid, rev, bind=p["bind"])
    effects = matches.reject_all_in_tx(conn, p["pid"], [(mid, rid) for mid, rid, _ in p["bound"]],
                                       grant=run.grant)
    return {"set_aside": [b[0] for b in p["bound"]], "effects": effects}


def _one(conn, run, name, d, op):
    cur = d["current"]
    h = views.headline(d)
    if name == "reject":
        if cur is not None and not run.recorded(d, cur):
            run.note(f"{h}: the pairing on that sheet has changed since — nothing applied.")
        elif cur is not None:
            run.guarded(d, "unpair", lambda: _match_params(conn, run, d, cur["match_id"]),
                        lambda p: matches.reject_in_tx(
                            conn, grant=run.grant, match_id=p["match_id"],
                            expected_revision=p["rev"], render_id=p["render_id"],
                            bind=p["bind"]),
                        {"h": h, **UNPAIR_WORDS[d["status"] == "proposed"]})
        elif d["candidates"]:
            n = len(d["candidates"])
            run.guarded(d, "set_aside", lambda: _bind_candidates(conn, run, d),
                        lambda p: _set_aside_all(conn, run, p),
                        {"h": h, "n": "both invoices" if n == 2 else
                         f"{n} invoice{'s' if n != 1 else ''}"})
        else:
            run.note(f"{h} has no match to remove.")
    elif name == "confirm":
        if cur is None:
            run.note(f"{h} has no single match to confirm.")
        elif not run.recorded(d, cur):
            run.note(f"{h}: the pairing on that sheet has changed since — nothing applied.")
        elif not views._needs_check(d):
            run.note(f"{h} was already fine.")
        else:
            _confirm(conn, run, d, cur)
    elif name in ("no_document", "needs_document"):
        verb = "exempt" if name == "no_document" else "lift"

        def phrase(res):
            dropped = any(e.startswith("unpaired") for e in res.get("effects", ()))
            return {"h": h, "drop": "; remove its match" if dropped else "",
                    "dropped": "; removed its match" if dropped else ""}
        run.guarded(d, verb, lambda: _projection_params(conn, run, d),
                    lambda p: matches.set_exemption_in_tx(
                        conn, grant=run.grant, pid=p["pid"], exempt=(verb == "exempt"),
                        expected_revision=p["rev"], render_id=p["render_id"], bind=p["bind"]),
                    phrase)
    elif name == "look_again":
        run.guarded(d, "revive", {"pid": d["pid"]},
                    lambda p: work.record_search_in_tx(conn, pid=p["pid"], token=None,
                                                       revive=True),
                    {"h": h})
    elif name == "identity":
        who = op["who"]
        # An identity reaches every payment with that bank text (the KB re-settles
        # them all), so it binds like a vendor-wide rule (R2): its provenance is the
        # named payment, on R; every payment it changes is listed in the proposal
        # (round p7, Astra S1). The receipt is read after the write.

        def ident():
            kb.upsert_in_tx(conn, who, patterns=[d["bank_counterparty"]])
            conn.execute("UPDATE projections SET identity_question=0 WHERE pid=?", (d["pid"],))
            lineage.settle(conn, d["pid"])
            return {"identity": who}

        def phrase(res):
            after = work.describe(conn, d["pid"])
            return {"bank": views.field(d["bank_counterparty"]), "who": views.field(who),
                    "tail": "; still missing a document." if views._is_missing(after) else "."}
        _broad(conn, run, "identity", {"pid": d["pid"], "who": who}, ident, phrase)
