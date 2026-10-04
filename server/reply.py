# server/reply.py
"""The reply grammar — an executable contract (spec §Weekly pass, "The reply
grammar is an executable contract, not 'Ellen understands free text'").

Ellen decides that a message IS a reply (model judgment, stated as such in
the spec) and passes its text verbatim. Everything after that is here:
split into clauses; a clause must be consumed WHOLE by one pattern; a
description resolves against the store's open items with no fuzzy matching
(several -> ask with dates and amounts; none -> say so, never redirect); each
application is bound to the revision the operator was SHOWN (authorship.py)
— an item never shown, or changed since, is re-shown and nothing is applied
to it; independent clauses still apply.

S7 §8: nothing commits from text. reading_in_tx runs the grammar under a rehearsal
(always rolled back) and returns the plan of writes, each with the revisions it read;
taps.apply_reading replays it under the operator's grant and commits only an identical
plan. The receipt is generated from what the replay wrote."""
from __future__ import annotations

import json
import re

import authorship
import binding
import dates
import db
import kb
import lineage
import matches
import views
import work

_MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}
_AMOUNT = re.compile(r"(?:eur\s*|€\s*)?(\d{1,3}(?:,\d{3})*\.\d{2}|\d+[.,]\d{2})")
_DATE = re.compile(r"\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b")
_T = r"(?:the\s+)?(?P<t>.+?)(?:\s+one)?"
# issue #11: the sheet's pairings named together ("those six guesses are all right,
# confirm them"; "all six proposals are right — confirm them"; "confirm all six") are
# the sheet reply. The collective must be NAMED in the clause — "all", "both" or a
# count — never a bare "the guesses" or a pronoun, which may mean the ones just named
# ("Zapier and Vercel are right. Confirm them.", R2). EVERY count the clause states
# must be the sheet's own — read from the whole clause, so no position escapes it
_NUMBERS = {w: i for i, w in enumerate(("two", "three", "four", "five", "six", "seven",
                                         "eight", "nine", "ten", "eleven", "twelve"), 2)}
_NUM = r"(?:\d{1,3}|" + "|".join(_NUMBERS) + r")"
_OK = r"(?:all\s+)?(?:good|fine|correct|right|ok|okay)"
_NOUN = r"(?:guesses|guessed ones|proposals|pairings|matches|suggestions)"
_THEM = (r"(?:(?:all|both)(?:\s+(?:of\s+)?(?:the|those|these))?(?:\s+" + _NUM + r")?"
         r"|(?:the|those|these)\s+" + _NUM + r")\s+" + _NOUN)
_YES = r"(?:(?:yes|yep|ok|okay)\s*[,:]?\s+)?"
# the tail's pronoun has its antecedent in the same clause
_TAIL = (r"(?:please\s+)?confirm\s+(?:them(?:\s+all)?|all\s+of\s+them|all\s+" + _NUM + r"|" + _THEM
         + r")(?:\s+please)?")
_COLLECTIVE = re.compile(_YES + _THEM + r"\s+(?:are|look)\s+" + _OK
                         + r"(?:\s*[,:\u2013\u2014-]?\s*(?:so\s+|and\s+)?" + _TAIL + r")?")
_CONFIRM_ALL = re.compile(_YES + r"(?:please\s+)?confirm\s+(?:all\s+" + _NUM + r"|" + _THEM
                          + r")(?:\s+please)?")
_ALL_GOOD = re.compile(r"all (?:good|fine|correct|right)")
_COUNT = re.compile(r"\b(\d{1,3}|both|" + "|".join(_NUMBERS) + r")\b")


def _counts(clause: str) -> set:
    """Every number of pairings a collective reply states, anywhere in it."""
    return {2 if w == "both" else _NUMBERS.get(w) or int(w) for w in _COUNT.findall(clause)}


PATTERNS = [
    # round 4: the phrase a view offers when a payment has more candidates than it prints
    ("candidates", re.compile(r"(?:show\s+(?:me\s+)?)?(?:the\s+)?candidates for\s+(?P<t>.+)")),
    ("bulk_except", re.compile(r"all (?:good|fine|correct|right) (?:except|but) (?P<t>.+)")),
    ("all_good", _ALL_GOOD),
    ("all_good", _COLLECTIVE),
    ("all_good", _CONFIRM_ALL),
    ("unpair", re.compile(_T + r"\s+(?:is|are)\s+(?:wrong|not right|incorrect)")),
    ("unpair", re.compile(r"no to\s+(?:the\s+)?(?P<t>.+?)(?:\s+one)?")),
    ("confirm", re.compile(_T + r"\s+(?:is\s+|are\s+)?(?:good|right|correct|fine|ok)")),
    ("exempt", re.compile(_T + r"\s+(?:needs|has)\s+no\s+(?:invoice|document|receipt)")),
    ("lift", re.compile(_T + r"\s+does\s+need\s+(?:an?\s+)?(?:invoice|document)(?:\s+after all)?")),
    ("never", re.compile(r"no (?:invoices|documents) ever for\s+(?P<t>.+)")),
    ("class_none", re.compile(r"(?P<k>payslips|statements|receipts) don'?t matter")),
    ("identity", re.compile(_T + r"\s+is\s+(?P<who>(?:my|our)\s+.+)")),
    ("stop", re.compile(r"stop chasing\s+(?P<q>q[1-4](?:\s+\d{4})?)")),
    ("start", re.compile(r"start from\s+(?P<q>q[1-4](?:\s+\d{4})?)")),
    ("name", re.compile(r"call the zips\s+(?P<n>.+)")),
    ("ledger_reset", re.compile(r"the bank ledger was (?:reset|wiped)")),
    ("revive", re.compile(r"have another look at\s+(?:the\s+)?(?P<t>.+?)(?:\s+one)?")),
    ("rebuild", re.compile(r"rebuild(?:\s+it|\s+(?P<q>q[1-4](?:\s+\d{4})?))?")),
    ("resend", re.compile(r"send it again")),
    # issue #15: the previous build, asked for by name (a fresh one is the default)
    ("send_last", re.compile(r"(?:send|give)(?: me)? (?:the |my )?(?:last|previous|old)"
                             r" (?:package|zip)(?: (?:you|that you|i) (?:built|made|sent))?"
                             r"(?: (?:for|of) (?P<q>q[1-4](?:\s+\d{4})?))?")),
    ("show", re.compile(r"(?P<s>show the rest|show older|all of them|check emailed invoices|more)")),
]
CLASS_SCOPES = {"payslips": ("salary", "payroll"), "statements": ("fees", "interest", "tax"),
                "receipts": ("reimbursement",)}


# A period ends a sentence ("Zapier is fine.Vercel is wrong" is two), except
# between digits (99.00, 1.234,56, 14.09) or where it opens a file extension
# (accounting.zip, invoice.pdf). The extensions are a closed list, not "any
# 1-5 letters", because a vendor name after a missing space is the same shape
# ("Zapier is fine.Adobe is wrong" must stay two sentences) (fix round 3).
_EXTENSIONS = ("zip", "pdf", "csv", "xlsx", "xls", "txt", "png", "jpg", "jpeg", "heic",
               "doc", "docx", "xml", "json", "eml")
_SENTENCE_END = re.compile(r"(?:(?<!\d)\.|\.(?!\d))(?!(?:%s)\b)|;|\n|(?<=\?)"
                           % "|".join(_EXTENSIONS), re.I)
_ESCAPE_LEAD = re.compile(r"^accounting\s*[:,\-\u2013\u2014]\s*")
_ESCAPE_TAIL = re.compile(r"\s*,\s*accounting$")


def _clauses(text: str) -> list:
    """Sentences, lower-cased; a sentence ending in "?" keeps its "?" so the
    caller can tell a question from a correction.

    spec §Recognising a reply: "accounting" anywhere binds the message to this
    plugin. It is removed only where it stands as a marker at a clause boundary
    ("accounting: …", "…, accounting", or a clause of its own), never from
    inside a clause's content ("the ABC Accounting Services one", fix round 2).
    A period ends a sentence only before whitespace or the end, so a name like
    "accounting.zip" and an amount like "99.00" stay whole."""
    parts = re.split(_SENTENCE_END, text.strip())
    out = []
    for p in parts:
        c = re.sub(r"\s+", " ", p).strip(" ,:").lower()
        c = _ESCAPE_TAIL.sub("", _ESCAPE_LEAD.sub("", c)).strip(" ,:")
        if c and c != "accounting":
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
    msg = re.sub(r"'([^']*)'", "\u201c\\1\u201d", msg)
    msg = msg.replace("the operator", "you")
    msg = _KIND_TOKEN.sub(lambda m: f"{m.group(1)} {views.KIND_WORD[m.group(2)]}", msg)
    return re.sub(r"\b([aA]) (?=[aeiouAEIOU])", r"\1n ", msg)


def _quarter(token: str | None) -> str:
    """The quarter a reply names (the grammar only lets "qN" or "qN YYYY"
    through), by the one rule the tool layer shares (dates.normalize_quarter)."""
    today = db.now()[:10]
    if not token:
        return dates.quarter_of(today)
    return dates.normalize_quarter(token, today)


def _open_items(conn) -> list:
    out = []
    for pid in lineage.live_pids(conn):
        d = work.describe(conn, pid)
        if d["ended"] or d["status"] == "ineligible":
            continue
        out.append(d)
    return out


_REF = re.compile(r"\bref\s+([0-9a-f]{4,64})\b")


def _parse_target(phrase: str) -> dict:
    p = phrase.strip()
    amount = None
    m = _AMOUNT.search(p)
    if m:
        raw = m.group(1).replace(",", "") if "." in m.group(1) else m.group(1).replace(",", ".")
        amount = int(round(float(raw) * 100))
        p = (p[:m.start()] + p[m.end():]).strip()
    day = None
    m = _DATE.search(p)
    if m:
        day = (int(m.group(1)), _MONTHS[m.group(2)[:3]])
        p = (p[:m.start()] + p[m.end():]).strip()
    p = re.sub(r"^(?:the|from|on)\s+|\s+(?:one|from|on)$", "", p).strip()
    return {"vendor": kb.norm(p) or None, "amount": amount, "day": day}


def _names(d, seen=None) -> set:
    """Every name a payment answers to: its stored names, and the name the
    latest delivered rendering showed it as (round 7: "A·B" is shown "A•B",
    so "the A•B one" may mean either payment)."""
    out = {kb.norm(d["counterparty"]), kb.norm(d["bank_counterparty"])}
    if seen and str(d["pid"]) in seen:
        out.add(kb.norm(seen[str(d["pid"])]))
    return out


def _shown_scopes(conn) -> list:
    """(pid, scope) of the rendering each payment was last SHOWN in — its own
    `shown.render_id`, the rendering D3 binds its shown revision to (round 8:
    the globally latest delivery may be an unrelated item view or an alert)."""
    return [(r[0], json.loads(r[1])) for r in conn.execute(
        "SELECT s.pid, r.scope_json FROM shown s JOIN renders r ON r.render_id=s.render_id"
        " WHERE r.delivered_at IS NOT NULL")]


def _seen_names(conn) -> dict:
    """pid (str) -> the payee name the rendering that last showed it printed."""
    out = {}
    for pid, scope in _shown_scopes(conn):
        name = scope.get("names", {}).get(str(pid))
        if name is not None:
            out[str(pid)] = name
    return out


def _matches(d, t, seen=None) -> bool:
    if t["vendor"] and t["vendor"] not in _names(d, seen):
        return False
    if t["amount"] is not None and d["amount_minor"] != t["amount"]:
        return False
    if t["day"] is not None:
        if not d["date"]:
            return False
        dd = dates.parse_day(d["date"])
        if (dd.day, dd.month) != t["day"]:
            return False
    return bool(t["vendor"] or t["amount"] is not None)


def _delivered_refs(conn) -> dict:
    """hex -> the pids whose last-shown rendering printed that generated ref."""
    out: dict = {}
    for pid, scope in _shown_scopes(conn):
        for k, v in scope.get("refs", {}).items():
            if pid in (v if isinstance(v, list) else [v]):
                out.setdefault(k, set()).add(pid)
    return out


def _resolve(conn, phrase, items):
    """A description resolves to exactly one open item. The literal reading
    (the whole phrase as payee, amount, date) and a ref reading are both
    tried; a "ref <hex>" counts only as a generated ref the latest DELIVERED
    rendering printed, exactly (round 6: a payee literally named "Adobe ref
    e40c" is not a ref). Several readings, or several payments: ask."""
    t = _parse_target(phrase)
    seen_names = _seen_names(conn)
    hits = [d for d in items if _matches(d, t, seen_names)]
    refs = _delivered_refs(conn)
    m = _REF.search(phrase)
    if m and m.group(1) in refs:
        rest = (phrase[:m.start()] + phrase[m.end():]).strip(" ,")
        rt = _parse_target(rest)
        rt_ok = rt["vendor"] or rt["amount"] is not None or rt["day"] is not None
        hits += [d for d in items if d["pid"] in refs[m.group(1)]
                 and (not rt_ok or _matches(d, rt, seen_names) or (rt["vendor"] is None
                                                       and _matches_loose(d, rt)))]
    seen, uniq = set(), []
    for d in hits:
        if d["pid"] not in seen:
            seen.add(d["pid"])
            uniq.append(d)
    hits = uniq
    if len(hits) == 1:
        return hits[0], None
    if not hits:
        same = [d for d in items if t["vendor"] and t["vendor"] in _names(d, seen_names)]
        msg = f"Nothing open matches “{views.field(phrase)}”."
        if same:
            msg += " Open for that name: " + "; ".join(views.headline(d) for d in same) + "."
        return None, msg
    by_pid = {p: k for k, ps in refs.items() for p in ps}
    heads = [views.headline(d) + (f" · ref {by_pid[d['pid']]}" if d["pid"] in by_pid else "")
             for d in hits]
    ask = "say it with the amount or the date"
    if len(set(heads)) < len(heads) or len(set(views.headline(d) for d in hits)) < len(hits):
        ask += ", or the ref"
    return None, "Which one? " + "; ".join(heads) + f" — {ask}."


def _matches_loose(d, t) -> bool:
    """A ref plus only an amount or a date: those must agree too."""
    if t["amount"] is not None and d["amount_minor"] != t["amount"]:
        return False
    if t["day"] is not None:
        if not d["date"]:
            return False
        dd = dates.parse_day(d["date"])
        if (dd.day, dd.month) != t["day"]:
            return False
    return True


def _shown(conn, pid):
    return conn.execute("SELECT * FROM shown WHERE pid=?", (pid,)).fetchone()


def _targets(phrase: str) -> list:
    # a comma between digits is a thousands separator (EUR 1,210.00), not a list
    return [p.strip() for p in re.split(r"(?<!\d),(?!\d)|\s+and\s+", phrase) if p.strip()]


def _delivered_package_for(conn, quarter):
    return conn.execute("SELECT p.filename, d.settled_at FROM packages p JOIN deliveries d ON"
                        " d.package_id=p.package_id WHERE p.quarter=? AND d.status='delivered'"
                        " ORDER BY d.settled_at DESC LIMIT 1", (quarter,)).fetchone()


def package_lines(conn, quarters) -> list:
    """A receipt's closing lines: for each touched quarter whose package was already sent,
    say it no longer matches (a reply's receipt and a verdict's, S7 §7.3)."""
    out = []
    for q in sorted(quarters):
        pk = _delivered_package_for(conn, q)
        if pk is not None:
            out.append(f"The package sent on {dates.short_day(pk['settled_at'])} no "
                       "longer matches — say \"rebuild it\" for a fresh one.")
    return out

# A step records every binding column that decides a verdict; import bookkeeping and the
# announce flags are not among them, so an unrelated bank import (or a delivered rendering
# announcing the watermark) never stales an open reading (final fix wave T6-c, M-2)
BINDING_NOT_READ = ("row_high_water", "watermark_announced", "package_name_announced")
NOTHING_MORE = "There is nothing more to show on that list."
REBUILD_PENDING = "Not rebuilding yet: apply the change first, then say \"rebuild it\"."
REBUILD_BLOCKED = ("Not rebuilding yet: a correction in this message did not apply. Say "
                   "\"rebuild it\" again once it has.")

# S7 §8: each write's two phrasings — the proposal's line, then the Apply receipt's. Every
# value is a field (views.field; views.headline already takes its fields through it)
PHRASE = {
    "confirm": ("Confirm {h}.", "Confirmed {h}."),
    "unpair": ("Unpair {h}.", "Unpaired {h}."),
    "set_aside": ("Set aside {n} for {h}.", "Set aside {n} for {h}."),
    "exempt": ("{h}: needs no document{drop}.", "{h}: needs no document{dropped}."),
    "lift": ("{h}: needs a document again.", "{h}: needs a document again."),
    "revive": ("{h}: look again at the next check.", "{h}: I'll look again at the next check."),
    "identity": ("{bank}: {who}.", "{bank}: {who}{tail}"),
    "never": ("{name}: never needs a document.", "{name}: never needs a document."),
    "class_none": ("{kind} are no longer needed.", "{kind} are no longer needed."),
    "stop": ("Stop chasing {q}: {n} still missing, no longer searched.",
             "Stopped chasing {q}: {n} still missing, no longer searched."),
    "start": ("Start from {q}; its payments come in at the next check.",
              "Starting from {q}; its payments come in at the next check."),
    "name": ("Call the zips {slug}-….zip.", "The zips are now called {slug}-….zip."),
    "ledger_reset": ("{note}", "{note}"),
}
_LIVE = "SELECT pid, revision FROM projections WHERE merged_into IS NULL AND ended IS NULL"


class _Run:
    def __init__(self, conn, grant, bound):
        self.conn, self.grant, self.bound = conn, grant, bound
        self.lines, self.asks, self.reshow, self.instructions = [], [], [], []
        self.plan, self.propose, self.unresolved_lines = [], [], []
        self.touched_quarters = set()
        self.unresolved = 0               # corrections in this reply that did not apply
        self.rebuilds = []                # rebuild requests, released only if nothing is unresolved
        self.excepted = False             # a clause of this reply opens with an exception
        self.named = set()                # payments another clause of this reply judged
        self.stated = set()               # every count the reply's sheet-wide clauses state
        self.not_a_reply = False
        # #39: some clause was read as a verdict, setting or instruction, or named open
        # payments without one ("are they wrong or good?"). False: nothing in the message
        # was understood as a reply, so nothing was applied or asked
        self.understood = False

    def note(self, line, unresolved=True):
        """A line that is not a write: said in the receipt, and listed in the proposal as
        not included. `unresolved`: a correction that did not apply (it blocks a rebuild)."""
        self.lines.append(line)
        self.unresolved_lines.append(line)
        if unresolved:
            self.unresolved += 1

    def result(self) -> dict:
        if self.rebuilds:
            if self.unresolved or self.plan:
                # spec: "An unresolved correction blocks its dependent rebuild and says so."
                # Decided after every clause, whatever their order (round p6). A write
                # waiting for Apply is unresolved too: the rebuild never runs before it (§8)
                self.lines.append(REBUILD_PENDING if self.plan else REBUILD_BLOCKED)
            else:
                for q in self.rebuilds:
                    qs = [_quarter(q)] if q else (sorted(self.touched_quarters)
                                                  or [_quarter(None)])
                    self.instructions.extend(f"rebuild {x}" for x in qs)
        return {"plan": self.plan, "propose": self.propose, "unresolved": self.unresolved_lines,
                "receipt": self.lines, "instructions": self.instructions, "asks": self.asks,
                "reshow": self.reshow, "understood": self.understood,
                "not_a_reply": self.not_a_reply, "quarters": sorted(self.touched_quarters)}

    def write(self, op, params, fn, phrase_args, check=None):
        """THE one way a clause writes (S7 §8): `fn` runs in the savepoint `clause`; the step
        records `op`, its parameters, the revision BEFORE the write of every live payment
        whose revision it changed (`read`), and the binding row it saw. `check(read)` may
        refuse inside the savepoint. Returns (fn's result, read)."""
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
        read there) are the step's; `fn(params)` writes. A refusal is said as today and
        leaves the step out of the plan."""
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
        """A store-wide setting (stop chasing, start from, the zip name, the
        ledger reset) — guarded like _broad, so a refusal rides in the same
        receipt beside the other clauses (pre-flight R2; spec §Flows,
        "the exceptions ride in the same receipt")."""
        try:
            res, _ = self.write(op, params, fn, phrase_args)
        except db.Refusal as exc:
            self.note(f"{what}: not applied — {_say(self.conn, exc)}.")
            return None
        return res


def _item(conn, run, pid):
    """The row the bound rendering recorded for `pid`, or None."""
    if run.bound is None:
        return None
    return conn.execute("SELECT * FROM render_items WHERE render_id=? AND pid=?",
                        (run.bound["render_id"], pid)).fetchone()


def _bind_projection(conn, run, d):
    """(render_id, revision, bind): the bound rendering's own row for the payment when it
    has one (bind="rendered"), else the payment's latest shown rendering (bind="shown")."""
    it = _item(conn, run, d["pid"])
    if it is not None:
        return it["render_id"], it["projection_revision"], "rendered"
    s = _shown(conn, d["pid"])
    if s is None:
        raise authorship.NotShown(d["pid"], "not shown")
    return s["render_id"], s["projection_revision"], "shown"


def _bind_match(conn, run, d, match_id):
    it = _item(conn, run, d["pid"])
    if it is not None:
        rev = json.loads(it["match_revisions_json"]).get(str(match_id))
        if rev is not None:
            return it["render_id"], rev, "rendered"
    s = _shown(conn, d["pid"])
    if s is None:
        raise authorship.NotShown(d["pid"], "not shown")
    rev = json.loads(s["match_revisions_json"]).get(str(match_id))
    if rev is None:
        raise authorship.NotShown(d["pid"], "not shown")
    return s["render_id"], rev, "shown"


def _bind_candidates(conn, run, d) -> dict:
    """A set-aside's step parameters: every displayed candidate bound to ONE rendering —
    the bound one when it showed them all, else the payment's latest shown rendering."""
    mids = [c["match_id"] for c in d["candidates"]]
    it = _item(conn, run, d["pid"])
    src, bind = it, "rendered"
    if it is None or any(str(m) not in json.loads(it["match_revisions_json"]) for m in mids):
        src, bind = _shown(conn, d["pid"]), "shown"
        if src is None:
            raise authorship.NotShown(d["pid"], "not shown")
    mrevs = json.loads(src["match_revisions_json"])
    if any(str(m) not in mrevs for m in mids):
        raise authorship.NotShown(d["pid"], "not shown")
    return {"pid": d["pid"], "bound": [[m, src["render_id"], mrevs[str(m)]] for m in mids],
            "bind": bind}


def _match_params(conn, run, d, match_id):
    rid, rev, bind = _bind_match(conn, run, d, match_id)
    return {"pid": d["pid"], "match_id": match_id, "render_id": rid, "rev": rev, "bind": bind}


def _projection_params(conn, run, d):
    rid, rev, bind = _bind_projection(conn, run, d)
    return {"pid": d["pid"], "render_id": rid, "rev": rev, "bind": bind}


_EXCEPTION = re.compile(r"(?:except|but|apart from|other than|besides|save for|excluding|"
                        r"with the exception|not\b)")


_NUMBERED = re.compile(r"\d{1,2}(?:\s+\w+)?")


def _parse(clause: str):
    """(verb, match) for the one pattern that consumes the clause whole, or (None, None)."""
    for name, rx in PATTERNS:
        m = rx.fullmatch(clause)
        if m:
            return name, m
    return None, None


def _run(conn, text, grant, bound) -> "_Run":
    """The reply grammar applied inside the caller's transaction (S7 §8): every write under
    `grant` and inside a savepoint of its clause, bound to `bound` (a renders row or None)."""
    run = _Run(conn, grant, bound)
    clauses = _clauses(text)
    # spec §"Asking between passes": a question is never a correction. A
    # message of questions only is not a reply; a question beside a correction
    # changes nothing for itself and the correction still applies.
    if not clauses or all(c.endswith("?") for c in clauses):
        run.not_a_reply = True
        return run
    items = _open_items(conn)
    parsed = [(c, *_parse(c)) for c in clauses]
    # R3/R4 (Astra): a reply is split into clauses, so a qualification of a sheet-wide
    # approval ("all good,\nexcept the Zapier one"; "…\nwith the exception of …";
    # "…\nexcluding …") stands in a clause of its own. A sheet as a whole is approved
    # only by a reply understood whole: any clause not understood (a question aside),
    # or one opening with an exception, and no sheet-wide approval applies
    # R6 (Astra): a question may carry one too ("… Can you leave the Zapier one out?")
    run.excepted = any(verb is None or c.endswith("?") or _NUMBERED.fullmatch(c) or
                       _EXCEPTION.match(c.lstrip(" -–—,:")) for c, verb, _ in parsed
                       if verb != "all_good")
    sheet_wide = []
    for clause, verb, m in parsed:
        if verb == "all_good":
            sheet_wide.append(m)          # R5 (Terra): applied after every other clause
            run.understood = True
            continue
        if clause.endswith("?"):
            run.note(f"“{views.field(clause)}” is a question — nothing changed for it.",
                     unresolved=False)
            continue
        if _NUMBERED.fullmatch(clause):
            run.note(f"“{views.field(clause)}”: there are no numbered lines — name the payee, "
                     "e.g. \"the Zapier one is wrong\".")
            continue
        if verb is None:
            names = _targets(clause)
            if names and all(_resolve(conn, n, items)[0] is not None for n in names):
                run.understood = True
                pretty = " and ".join(views.field(n.title() if n.islower() else n) for n in names)
                run.note(f"Nothing applied for “{views.field(clause)}”: are they wrong or good? "
                         f"Say \"{pretty} are wrong\".")
            else:
                run.note(f"I didn't understand “{views.field(clause)}” — nothing applied for it.")
            continue
        run.understood = True
        _apply(conn, run, verb, m, items)
    # A sheet as a whole is approved last, and only for what no other clause judged:
    # "All good. The Zapier one is wrong." unpairs Zapier and confirms the rest. Any
    # other clause left unresolved (ambiguous, stale, refused) and it approves nothing
    # R7 (Astra): the reply's sheet-wide clauses are ONE approval — every count any of
    # them states is checked together, and it applies once or not at all
    if sheet_wide:
        # R8 (Astra): a collective ("those two guesses", "all those proposals") beside a
        # verdict on a single pairing may refer back to the pairings just judged —
        # only the bare "all good" names the whole sheet whatever else is said
        collective = any(m.re is not _ALL_GOOD for m in sheet_wide)
        if run.unresolved > 0 or (collective and run.named):
            run.excepted = True
        run.stated = set().union(*(_counts(m.group(0)) for m in sheet_wide))
        _apply(conn, run, "all_good", sheet_wide[0], items)
    return run


def reading_in_tx(conn, text, quoted=None) -> dict:
    """S7 §8 propose: what `text` would do, learnt by running it under a rehearsal that is
    always rolled back. Nothing it wrote survives."""
    import authority
    bound = views.bound_rendering(conn, quoted)
    with authority.rehearsal(conn) as r:
        out = _run(conn, text, r, bound).result()
    out["render_id"] = bound["render_id"] if bound is not None else None
    return out


def replay(conn, row, grant) -> dict:
    """S7 §8 apply: the stored reading run again under the operator's grant, against the
    rendering it was bound to; the caller compares the plan and commits or rolls back."""
    bound = (conn.execute("SELECT * FROM renders WHERE render_id=?", (row["render_id"],))
             .fetchone() if row["render_id"] else None)
    return _run(conn, row["text"], grant, bound).result()


def _apply(conn, run, verb, m, items):
    if verb == "all_good" and run.excepted:
        run.note("Nothing applied for that: something else in the same message "
                 "leaves unclear what is approved. Say \"all good\" and \"the Zapier one is "
                 "wrong\" as two sentences, or only the one that is wrong.")
        return
    if verb == "bulk_except":
        t = re.sub(r"^the\s+|\s+one$", "", m.group("t").strip())
        d, _ = _resolve(conn, t, items)
        who = d["counterparty"] if d is not None else t
        run.note(f"Nothing applied for that: say \"all good\" and \"the {views.field(who)} one is "
                 "wrong\" as two sentences, or only the one that is wrong.")
        return
    if verb == "all_good":
        # spec §Testing: "`all good` is a sheet reply only while a sheet is the
        # most recent thing sent" — S7 §8: it binds to the rendering the reading is
        # bound to (the quoted post's, else the most recent DELIVERED rendering)
        last = run.bound
        if last is None or last["kind"] not in ("status", "check", "all"):
            run.note("Nothing applied for \"all good\": the last thing I sent you was "
                     "not a sheet to approve. Name the payment, e.g. \"the Zapier one "
                     "is good\".")
            return
        said = len(run.lines)
        waiting, decided = [], 0
        for pid in views.render_items(conn, last["render_id"]):
            if pid in run.named:
                decided += 1              # another clause of this reply decides it
                continue
            d = work.describe(conn, pid)
            cur = d["current"]
            if cur is None or not views._needs_check(d) or d["candidates"]:
                continue
            waiting.append((d, cur))
        stated = run.stated
        # the count names the sheet: its pairings waiting, the ones decided here included
        if stated and stated != {len(waiting) + decided}:
            # a count that is not the sheet's: the operator means a sheet other than
            # this one, or only some of its lines — confirm none of them
            n = len(waiting) + decided
            run.note(f"Nothing applied: that sheet has {n} pairing"
                     f"{'' if n == 1 else 's'} waiting for your approval, "
                     f"not {' or '.join(str(x) for x in sorted(stated))}. Say \"all good\" to confirm all of them, or name the "
                     "ones that are right, e.g. \"the Zapier one is good\".")
            return
        for d, cur in waiting:
            _confirm(conn, run, d, cur)
        if len(run.lines) == said:
            run.note("Nothing on that sheet was waiting for your approval.", unresolved=False)
        return
    if verb in ("unpair", "confirm", "exempt", "lift", "revive", "identity"):
        for phrase in _targets(m.group("t")) if verb in ("unpair", "confirm") else [m.group("t")]:
            d, problem = _resolve(conn, phrase, items)
            if d is None:
                run.asks.append(problem)
                run.note(problem)
                continue
            if verb in ("unpair", "confirm"):
                run.named.add(d["pid"])   # its own verdict, said in its own receipt line
            _one(conn, run, verb, d, m)
        return
    if verb == "never":
        said = kb.norm(m.group("t"))
        seen_names = _seen_names(conn)
        fits = sorted({d["counterparty"] for d in items if said in _names(d, seen_names)})
        if len(fits) > 1:           # the name the operator saw fits several payees: ask
            ask = (f"Which one? “{views.field(m.group('t').strip())}” could be " + " or ".join(
                views.field(n) for n in fits) + " — nothing applied.")
            run.asks.append(ask)
            run.note(ask)
            return
        name = fits[0] if fits else None
        if name is None:
            known = kb.counterparty_for(conn, said)
            if known is None:
                # none -> say so, never create a payee from a typo (fix round 1)
                run.note(f"Nothing open matches “{views.field(m.group('t').strip())}”, and I know no "
                         "payee by that name — nothing applied.")
                return
            name = known["name"]
        _broad(conn, run, "never", {"scope": name}, lambda: kb.set_expectation_in_tx(
                   conn, scope_type="counterparty", scope=name, kind="none",
                   author="operator", render_id=_last_delivered(run), grant=run.grant),
               {"name": views.field(name)})
        return
    if verb == "class_none":
        k = m.group("k")
        _broad(conn, run, "class_none", {"scopes": list(CLASS_SCOPES[k])},
               lambda: [kb.set_expectation_in_tx(
                   conn, scope_type="chain", scope=scope, kind="none", author="operator",
                   render_id=_last_delivered(run), grant=run.grant) for scope in CLASS_SCOPES[k]],
               {"kind": k.capitalize()})
        return
    if verb == "stop":
        q = _quarter(m.group("q"))
        if run.setting("stop", {"quarter": q},
                       lambda: work.stop_chasing_in_tx(conn, q, grant=run.grant),
                       lambda res: {"q": dates.quarter_label(q),
                                    "n": len(res["accepted_missing"])},
                       f"Still chasing {dates.quarter_label(q)}"):
            run.touched_quarters.add(q)        # "rebuild it" then rebuilds that quarter
        return
    if verb == "start":
        q = _quarter(m.group("q"))
        run.setting("start", {"day": dates.quarter_bounds(q)[0]},
                    lambda: work.set_watermark_in_tx(conn, q, grant=run.grant),
                    {"q": dates.quarter_label(q)},
                    f"Not changing where the books start ({dates.quarter_label(q)})")
        return
    if verb == "name":
        run.setting("name", {"slug": binding.slug(m.group("n"))},
                    lambda: binding.set_package_name_in_tx(conn, m.group("n"), grant=run.grant),
                    lambda res: {"slug": views.field(res["package_name"])}, "The zip name")
        return
    if verb == "ledger_reset":
        run.setting("ledger_reset", {},
                    lambda: binding.acknowledge_ledger_reset_in_tx(conn, grant=run.grant),
                    lambda res: {"note": res["note"]}, "The bank ledger reset")
        return
    if verb == "rebuild":
        run.rebuilds.append(m.group("q"))        # decided after the WHOLE reply (result())
        return
    if verb == "resend":
        run.instructions.append("resend")
        return
    if verb == "send_last":
        q = m.group("q")
        run.instructions.append(f"send last {_quarter(q)}" if q else "send last")
        return
    if verb == "show":
        if m.group("s") in ("more", "all of them"):
            # a desk turn is a fresh session (S7 §2): it cannot know the cursor, so the
            # bound rendering's own `next` is returned as ready show_view arguments
            scope = json.loads(run.bound["scope_json"]) if run.bound is not None else {}
            nxt = scope.get("next")
            if run.bound is not None and run.bound["kind"] in views.VIEWS \
                    and "next" not in scope:
                # r3 #4: a page delivered before S7 stored no `next` (an explicit null is
                # "nothing more"): a fresh paged view of the same view and quarter
                nxt = {"view": run.bound["kind"], "quarter": scope.get("quarter"), "page": 1}
                if run.bound["kind"] == "item":
                    nxt["pid"] = scope.get("pid")
            elif nxt and run.bound["kind"] == "item" and scope.get("walk"):
                nxt = dict(nxt, walk=scope["walk"])      # r3 #5: as the More button does
            if nxt:
                run.instructions.append({"show_view": nxt})
            else:
                run.note(NOTHING_MORE, unresolved=False)
            return
        run.instructions.append(m.group("s"))
        return
    if verb == "candidates":
        d, problem = _resolve(conn, m.group("t"), items)
        if d is None:
            run.asks.append(problem)
            run.note(problem, unresolved=False)
            return
        run.instructions.append(f"show item {d['pid']}")
        return


class _Unseen(Exception):
    def __init__(self, pids):
        super().__init__("unseen")
        self.pids = pids


def _broad(conn, run, op, params, change, phrase_args) -> None:
    """A vendor- or class-wide operator change — or an identity, which reaches
    every payment with that bank text — binds EVERY payment whose proposition
    it changes (round p6, Terra S1: "no invoices ever for Adobe" retired a
    pairing on an Adobe payment the operator had never seen). The change is
    made inside its clause's savepoint; every payment whose revision it moved
    (the step's `read`) must have been shown at the revision it had before the
    change, or the whole change rolls back and those payments are shown first."""
    def check(read):
        unseen = []
        for pid, rev in sorted(read.items()):
            s_ = _shown(conn, pid)
            if s_ is None or s_["projection_revision"] != rev:
                unseen.append(pid)
        if unseen:
            raise _Unseen(unseen)
    try:
        _, read = run.write(op, params, change, phrase_args, check=check)
    except _Unseen as exc:
        for pid in exc.pids:
            if pid not in run.reshow:
                run.reshow.append(pid)
        run.note(f"Not applied: it would change {len(exc.pids)} payment"
                 f"{'s' if len(exc.pids) != 1 else ''} you haven't seen as they are "
                 "now — here they are first.")
        return
    except db.Refusal as exc:
        run.note(f"Not applied — {_say(conn, exc)}.")
        return
    for pid in read:                       # "rebuild it" rebuilds the quarters it changed (p8)
        q = work.describe(conn, pid)["quarter"]
        if q:
            run.touched_quarters.add(q)


def _last_delivered(run):
    """The provenance stamp of an operator's broad rule ("no invoices ever for X", "X
    are no longer needed"): the rendering the reading is bound to (S7 §8)."""
    return run.bound["render_id"] if run.bound is not None else None


def _confirm(conn, run, d, cur):
    run.guarded(d, "confirm", lambda: _match_params(conn, run, d, cur["match_id"]),
                lambda p: matches.confirm_in_tx(
                    conn, grant=run.grant, match_id=p["match_id"], expected_revision=p["rev"],
                    render_id=p["render_id"], bind=p["bind"]),
                {"h": views.headline(d)})


def _set_aside_all(conn, run, p) -> dict:
    """"Wrong" on a payment with several displayed candidates sets them ALL
    aside or none (fix round 1): inside the clause's savepoint every candidate
    is checked against the revision the operator was shown BEFORE the first
    write; any refusal rolls the whole set back and the payment is re-shown."""
    for mid, rid, rev in p["bound"]:
        authorship.require_match(conn, p["pid"], mid, rid, rev, bind=p["bind"])
    effects = matches.reject_all_in_tx(conn, p["pid"], [(mid, rid) for mid, rid, _ in p["bound"]],
                                       grant=run.grant)
    return {"set_aside": [b[0] for b in p["bound"]], "effects": effects}


def _one(conn, run, verb, d, m):
    cur = d["current"]
    h = views.headline(d)
    if verb == "unpair":
        if cur is not None:
            run.guarded(d, "unpair", lambda: _match_params(conn, run, d, cur["match_id"]),
                        lambda p: matches.reject_in_tx(
                            conn, grant=run.grant, match_id=p["match_id"],
                            expected_revision=p["rev"], render_id=p["render_id"],
                            bind=p["bind"]),
                        {"h": h})
        elif d["candidates"]:
            n = len(d["candidates"])
            run.guarded(d, "set_aside", lambda: _bind_candidates(conn, run, d),
                        lambda p: _set_aside_all(conn, run, p),
                        {"h": h, "n": "both candidates" if n == 2 else
                         f"{n} candidate{'s' if n != 1 else ''}"})
        else:
            run.note(f"{h} has nothing paired to remove — say it needs no "
                     "document, or hand me the invoice.")      # a no-op blocks its rebuild
    elif verb == "confirm":
        if cur is None:
            run.note(f"{h} has no single pairing to approve.")
        elif not views._needs_check(d):
            run.note(f"{h} was already fine.", unresolved=False)
        else:
            _confirm(conn, run, d, cur)
    elif verb in ("exempt", "lift"):
        def phrase(res):
            dropped = any(e.startswith("unpaired") for e in res.get("effects", ()))
            return {"h": h, "drop": "; drop its pairing" if dropped else "",
                    "dropped": "; dropped its pairing" if dropped else ""}
        run.guarded(d, verb, lambda: _projection_params(conn, run, d),
                    lambda p: matches.set_exemption_in_tx(
                        conn, grant=run.grant, pid=p["pid"], exempt=(verb == "exempt"),
                        expected_revision=p["rev"], render_id=p["render_id"], bind=p["bind"]),
                    phrase)
    elif verb == "revive":
        run.guarded(d, "revive", {"pid": d["pid"]},
                    lambda p: work.record_search_in_tx(conn, pid=p["pid"], token=None,
                                                       revive=True),
                    {"h": h})
    elif verb == "identity":
        who = m.group("who").strip()
        # An identity reaches every payment with that bank text (the KB re-settles
        # them all), so it goes through the same guard as a vendor-wide rule: the
        # named payment AND every other payment it changes must have been shown as
        # they are (round p7, Astra S1). The receipt is read after the write.
        if _shown(conn, d["pid"]) is None:
            if d["pid"] not in run.reshow:
                run.reshow.append(d["pid"])
            run.note(f"{h} hasn't been shown to you in this form yet — here it is now; "
                     "nothing applied.")
            return

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
