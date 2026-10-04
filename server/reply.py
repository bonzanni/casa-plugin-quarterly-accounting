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
to it; independent clauses still apply; the receipt is generated from what
actually committed."""
from __future__ import annotations

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
    import json
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


def _split(line: str) -> list:
    """A line over the limit (a "Which one?" listing a hundred charges) as
    pieces that each fit a message: broken between words, a word longer than a
    message between code points. Nothing is added and nothing lost (fix wave D
    round 2: the previous splitter appended a ";" to a full piece)."""
    if views.utf16_len(line) <= views.BODY_LIMIT:
        return [line]
    out, cur = [], ""
    for word in line.split(" "):
        cand = word if not cur else cur + " " + word
        if views.utf16_len(cand) <= views.BODY_LIMIT:
            cur = cand
            continue
        if cur:
            out.append(cur)
        cur = ""
        for ch in word:
            if views.utf16_len(cur + ch) > views.BODY_LIMIT:
                out.append(cur)
                cur = ""
            cur += ch
    return out + [cur] if cur else out


def _pages(lines: list) -> list:
    """The receipt as Telegram-sized messages (fix wave D, Astra S2): EVERY
    line, in order. Paged, never summarised: spec §Flows asks for "one receipt,
    generated from what actually committed, naming vendor and effect", with
    "the exceptions [riding] in the same receipt" — a summary would drop the
    names that make a misread reply visible. Each page is what views.fit_lines
    keeps whole of the lines still to send, so every page is within
    BODY_LIMIT by the one shared fit. `receipt` is the first page; the
    caller sends `receipt_pages` in order."""
    rest = [p for line in lines for p in _split(line)]
    pages = []
    while rest:
        out, whole = views.fit_lines(rest)
        if whole == 0:              # unreachable: every piece fits on its own
            raise RuntimeError("a receipt line does not fit one message")
        pages.append("\n".join(out))
        rest = rest[whole:]
    return pages


class _Run:
    def __init__(self, conn):
        self.conn = conn
        self.lines, self.applied, self.asks, self.reshow, self.instructions = [], [], [], [], []
        self.touched_quarters = set()
        self.unresolved = 0               # corrections in this reply that did not apply
        self.rebuilds = []                # rebuild requests, released only if nothing is unresolved
        self.excepted = False             # a clause of this reply opens with an exception
        self.named = set()                # payments another clause of this reply judged
        self.stated = set()               # every count the reply's sheet-wide clauses state
        # #39: some clause was read as a verdict, setting or instruction, or named open
        # payments without one ("are they wrong or good?"). False: nothing in the message
        # was understood as a reply, so nothing was applied or asked
        self.understood = False

    def result(self, not_a_reply=False) -> dict:
        if self.rebuilds:
            if self.unresolved:
                # spec: "An unresolved correction blocks its dependent rebuild and says so."
                # Decided after every clause, whatever their order (round p6).
                self.lines.append("Not rebuilding yet: a correction in this message did not "
                                  "apply. Say \"rebuild it\" again once it has.")
            else:
                for q in self.rebuilds:
                    qs = [_quarter(q)] if q else (sorted(self.touched_quarters)
                                                  or [_quarter(None)])
                    self.instructions.extend(f"rebuild {x}" for x in qs)
        for q in sorted(self.touched_quarters):
            pk = _delivered_package_for(self.conn, q)
            if pk is not None:
                self.lines.append(f"The package sent on {dates.short_day(pk['settled_at'])} no "
                                  "longer matches — say \"rebuild it\" for a fresh one.")
        pages = _pages(self.lines)
        return {"receipt": pages[0] if pages else "", "receipt_pages": pages,
                "applied": self.applied, "asks": self.asks,
                "reshow": self.reshow, "instructions": self.instructions,
                "not_a_reply": not_a_reply, "understood": self.understood}

    def guarded(self, d, fn, ok_line):
        """Apply one operation in its own transaction; the receipt line is
        built from what that call committed, or says it was not applied."""
        try:
            res = fn()
        except (authorship.NotShown, authorship.Stale) as exc:
            # a merged payment's refusal names its survivor: that is what is shown next
            pid = getattr(exc, "pid", None) or d["pid"]
            if pid in self.reshow:
                return None                 # one payment is re-shown once, and said once
            self.reshow.append(pid)
            word = "changed since you saw it" if isinstance(exc, authorship.Stale) else \
                "hasn't been shown to you in this form yet"
            self.lines.append(f"{views.headline(d)} {word} — here it is now; nothing applied.")
            self.unresolved += 1
            return None
        except db.Refusal as exc:
            self.lines.append(f"{views.headline(d)}: not applied — {_say(self.conn, exc)}")
            self.unresolved += 1
            return None
        self.applied.append({"pid": d["pid"], **res})
        self.lines.append(ok_line(res))
        if d["quarter"]:
            self.touched_quarters.add(d["quarter"])
        return res

    def setting(self, fn, ok_line, what):
        """A store-wide setting (stop chasing, start from, the zip name, the
        ledger reset) — guarded like _broad, so a refusal rides in the same
        receipt after earlier clauses committed (pre-flight R2; spec §Flows,
        "the exceptions ride in the same receipt")."""
        try:
            res = fn()
        except db.Refusal as exc:
            self.lines.append(f"{what}: not applied — {_say(self.conn, exc)}.")
            self.unresolved += 1
            return None
        self.applied.append(res)
        self.lines.append(ok_line(res))
        return res


def _bind_projection(conn, d):
    s = _shown(conn, d["pid"])
    if s is None:
        raise authorship.NotShown(d["pid"], "not shown")
    return s["render_id"], s["projection_revision"]


def _bind_match(conn, d, match_id):
    import json
    s = _shown(conn, d["pid"])
    if s is None:
        raise authorship.NotShown(d["pid"], "not shown")
    rev = json.loads(s["match_revisions_json"]).get(str(match_id))
    if rev is None:
        raise authorship.NotShown(d["pid"], "not shown")
    return s["render_id"], rev


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


def apply_reply(conn, text: str) -> dict:
    run = _Run(conn)
    clauses = _clauses(text)
    # spec §"Asking between passes": a question is never a correction. A
    # message of questions only is not a reply; a question beside a correction
    # changes nothing for itself and the correction still applies.
    if not clauses or all(c.endswith("?") for c in clauses):
        return run.result(not_a_reply=True)
    items = _open_items(conn)
    parsed = [(c, *_parse(c)) for c in clauses]
    # R3/R4 (Astra): a reply is split into clauses, so a qualification of a sheet-wide
    # approval ("all good,\nexcept the Zapier one"; "…\nwith the exception of …";
    # "…\nexcluding …") stands in a clause of its own. A sheet as a whole is approved
    # only by a reply understood whole: any clause not understood (a question aside),
    # or one opening with an exception, and no sheet-wide approval applies
    # R6 (Astra): a question may carry one too ("… Can you leave the Zapier one out?")
    run.excepted = any(verb is None or c.endswith("?") or _NUMBERED.fullmatch(c) or
                       _EXCEPTION.match(c.lstrip(" -\u2013\u2014,:")) for c, verb, _ in parsed
                       if verb != "all_good")
    sheet_wide = []
    for clause, verb, m in parsed:
        if verb == "all_good":
            sheet_wide.append(m)          # R5 (Terra): applied after every other clause
            run.understood = True
            continue
        if clause.endswith("?"):
            run.lines.append(f"“{views.field(clause)}” is a question — nothing changed for it.")
            continue
        if _NUMBERED.fullmatch(clause):
            run.lines.append(f"“{views.field(clause)}”: there are no numbered lines — name the payee, "
                             "e.g. \"the Zapier one is wrong\".")
            run.unresolved += 1
            continue
        if verb is None:
            names = _targets(clause)
            if names and all(_resolve(conn, n, items)[0] is not None for n in names):
                run.understood = True
                pretty = " and ".join(views.field(n.title() if n.islower() else n) for n in names)
                run.lines.append(f"Nothing applied for “{views.field(clause)}”: are they wrong or good? "
                                 f"Say \"{pretty} are wrong\".")
            else:
                run.lines.append(f"I didn't understand “{views.field(clause)}” — nothing applied for it.")
            run.unresolved += 1
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
    return run.result()


def _apply(conn, run, verb, m, items):
    if verb == "all_good" and run.excepted:
        run.lines.append("Nothing applied for that: something else in the same message "
                         "leaves unclear what is approved. Say \"all good\" and \"the Zapier one is "
                         "wrong\" as two sentences, or only the one that is wrong.")
        run.unresolved += 1
        return
    if verb == "bulk_except":
        t = re.sub(r"^the\s+|\s+one$", "", m.group("t").strip())
        d, _ = _resolve(conn, t, items)
        who = d["counterparty"] if d is not None else t
        run.lines.append(f"Nothing applied for that: say \"all good\" and \"the {views.field(who)} one is "
                         "wrong\" as two sentences, or only the one that is wrong.")
        run.unresolved += 1
        return
    if verb == "all_good":
        # spec §Testing: "`all good` is a sheet reply only while a sheet is the
        # most recent thing sent" — D2 binds to the most recent DELIVERED rendering
        last = db.last_delivered(conn)
        if db.non_binding(last):
            # R6: the operator saw newer results than any sheet they could be approving
            # — never fall back to an older sheet, never act on the newer one
            run.lines.append(db.NEWER_SINCE)
            run.unresolved += 1
            return
        if last is None or last["kind"] not in ("status", "check", "all"):
            run.lines.append("Nothing applied for \"all good\": the last thing I sent you was "
                             "not a sheet to approve. Name the payment, e.g. \"the Zapier one "
                             "is good\".")
            run.unresolved += 1
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
            run.lines.append(f"Nothing applied: that sheet has {n} pairing"
                             f"{'' if n == 1 else 's'} waiting for your approval, "
                             f"not {' or '.join(str(x) for x in sorted(stated))}. Say \"all good\" to confirm all of them, or name the "
                             "ones that are right, e.g. \"the Zapier one is good\".")
            run.unresolved += 1
            return
        for d, cur in waiting:
            run.guarded(d, lambda d=d, cur=cur: matches.confirm_match(
                conn, match_id=cur["match_id"],
                expected_revision=_bind_match(conn, d, cur["match_id"])[1],
                render_id=_bind_match(conn, d, cur["match_id"])[0]),
                lambda res, d=d: f"Confirmed {views.headline(d)}.")
        if len(run.lines) == said:
            run.lines.append("Nothing on that sheet was waiting for your approval.")
        return
    if verb in ("unpair", "confirm", "exempt", "lift", "revive", "identity"):
        for phrase in _targets(m.group("t")) if verb in ("unpair", "confirm") else [m.group("t")]:
            d, problem = _resolve(conn, phrase, items)
            if d is None:
                run.asks.append(problem)
                run.lines.append(problem)
                run.unresolved += 1
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
            run.lines.append(ask)
            run.unresolved += 1
            return
        name = fits[0] if fits else None
        if name is None:
            known = kb.counterparty_for(conn, said)
            if known is None:
                # none -> say so, never create a payee from a typo (fix round 1)
                run.lines.append(f"Nothing open matches “{views.field(m.group('t').strip())}”, and I know no "
                                 "payee by that name — nothing applied.")
                run.unresolved += 1
                return
            name = known["name"]
        _broad(conn, run, lambda: kb.set_expectation_in_tx(
                   conn, scope_type="counterparty", scope=name, kind="none",
                   author="operator", render_id=_last_delivered(conn)),
               f"{views.field(name)}: never needs a document.")
        return
    if verb == "class_none":
        k = m.group("k")
        _broad(conn, run, lambda: [kb.set_expectation_in_tx(
                   conn, scope_type="chain", scope=scope, kind="none", author="operator",
                   render_id=_last_delivered(conn)) for scope in CLASS_SCOPES[k]],
               f"{k.capitalize()} are no longer needed.")
        return
    if verb == "stop":
        q = _quarter(m.group("q"))
        if run.setting(lambda: work.stop_chasing(conn, q),
                       lambda res: f"Stopped chasing {dates.quarter_label(q)}: "
                       f"{len(res['accepted_missing'])} still missing, no longer searched.",
                       f"Still chasing {dates.quarter_label(q)}"):
            run.touched_quarters.add(q)        # "rebuild it" then rebuilds that quarter
        return
    if verb == "start":
        q = _quarter(m.group("q"))
        run.setting(lambda: work.set_watermark(conn, q),
                    lambda res: f"Starting from {dates.quarter_label(q)}; its payments come in "
                    "at the next check.",
                    f"Not changing where the books start ({dates.quarter_label(q)})")
        return
    if verb == "name":
        run.setting(lambda: binding.set_package_name(conn, m.group("n")),
                    lambda res: f"The zips are now called {views.field(res['package_name'])}-….zip.",
                    "The zip name")
        return
    if verb == "ledger_reset":
        run.setting(lambda: binding.acknowledge_ledger_reset(conn),
                    lambda res: res["note"], "The bank ledger reset")
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
        run.instructions.append(m.group("s"))
        return
    if verb == "candidates":
        d, problem = _resolve(conn, m.group("t"), items)
        if d is None:
            run.asks.append(problem)
            run.lines.append(problem)
            return
        run.instructions.append(f"show item {d['pid']}")
        return


class _Unseen(Exception):
    def __init__(self, pids):
        super().__init__("unseen")
        self.pids = pids


def _broad(conn, run, change, ok_line) -> None:
    """A vendor- or class-wide operator change — or an identity, which reaches
    every payment with that bank text — binds EVERY payment whose proposition
    it changes (round p6, Terra S1: "no invoices ever for Adobe" retired a
    pairing on an Adobe payment the operator had never seen). The
    change is made inside one transaction; every payment whose digest it moved
    must have been shown at the revision it had before the change, or the whole
    change rolls back and those payments are shown first."""
    try:
        with db.tx(conn):
            before = {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT pid, revision, digest FROM projections WHERE merged_into IS NULL"
                " AND ended IS NULL")}
            res = change()
            unseen, changed = [], []
            for pid, (rev, digest) in sorted(before.items()):
                now = conn.execute("SELECT digest FROM projections WHERE pid=?",
                                   (pid,)).fetchone()[0]
                if now == digest:
                    continue
                changed.append(pid)
                s_ = _shown(conn, pid)
                if s_ is None or s_["projection_revision"] != rev:
                    unseen.append(pid)
            if unseen:
                raise _Unseen(unseen)
    except _Unseen as exc:
        for pid in exc.pids:
            if pid not in run.reshow:
                run.reshow.append(pid)
        run.lines.append(f"Not applied: it would change {len(exc.pids)} payment"
                         f"{'s' if len(exc.pids) != 1 else ''} you haven't seen as they are "
                         "now — here they are first.")
        run.unresolved += 1
        return
    except db.Refusal as exc:
        run.lines.append(f"Not applied — {_say(conn, exc)}.")
        run.unresolved += 1
        return
    run.applied.append({"broad": res if isinstance(res, dict) else {"changes": len(res)}})
    run.lines.append(ok_line() if callable(ok_line) else ok_line)
    for pid in changed:                    # "rebuild it" rebuilds the quarters it changed (p8)
        q = work.describe(conn, pid)["quarter"]
        if q:
            run.touched_quarters.add(q)


def _last_delivered(conn):
    """The provenance stamp of an operator's broad rule ("no invoices ever for X", "X
    are no longer needed"): a view the operator was shown. The rule is name-scoped and
    never reads the rendering's items or offers, so a non-binding rendering (R6) is a
    legitimate stamp: it is what the operator saw last."""
    r = db.last_delivered(conn)
    return r["render_id"] if r else None


def _set_aside_all(conn, d) -> dict:
    """"Wrong" on a payment with several displayed candidates sets them ALL
    aside or none (fix round 1): inside one transaction every candidate is
    checked against the revision the operator was shown BEFORE the first
    write; any refusal rolls the whole set back and the payment is re-shown."""
    with db.tx(conn):
        bound = []
        for c in d["candidates"]:
            rid, rev = _bind_match(conn, d, c["match_id"])
            authorship.require_match_shown(conn, d["pid"], c["match_id"], rid, rev)
            bound.append((c["match_id"], rid, rev))
        effects = matches.reject_all_in_tx(conn, d["pid"], [(mid, rid) for mid, rid, _ in bound])
        return {"set_aside": [b[0] for b in bound], "effects": effects}


def _one(conn, run, verb, d, m):
    cur = d["current"]
    if verb == "unpair":
        if cur is not None:
            run.guarded(d, lambda: matches.reject_match(
                conn, match_id=cur["match_id"],
                expected_revision=_bind_match(conn, d, cur["match_id"])[1],
                render_id=_bind_match(conn, d, cur["match_id"])[0]),
                lambda res: f"Unpaired {views.headline(d)}.")
        elif d["candidates"]:
            n = len(d["candidates"])
            run.guarded(d, lambda: _set_aside_all(conn, d),
                        lambda res: f"Set aside {'both' if n == 2 else n} candidate"
                        f"{'s' if n != 1 else ''} for {views.headline(d)}.")
        else:
            run.lines.append(f"{views.headline(d)} has nothing paired to remove — say it needs no "
                             "document, or hand me the invoice.")
            run.unresolved += 1                   # a no-op correction blocks its rebuild
    elif verb == "confirm":
        if cur is None:
            run.lines.append(f"{views.headline(d)} has no single pairing to approve.")
            run.unresolved += 1
        elif not views._needs_check(d):
            run.lines.append(f"{views.headline(d)} was already fine.")
        else:
            run.guarded(d, lambda: matches.confirm_match(
                conn, match_id=cur["match_id"],
                expected_revision=_bind_match(conn, d, cur["match_id"])[1],
                render_id=_bind_match(conn, d, cur["match_id"])[0]),
                lambda res: f"Confirmed {views.headline(d)}.")
    elif verb in ("exempt", "lift"):
        def line(res):
            if verb == "lift":
                return f"{views.headline(d)}: needs a document again."
            dropped = [e for e in res["effects"] if e.startswith("unpaired")]
            return (f"{views.headline(d)}: needs no document"
                    + ("; dropped its pairing." if dropped else "."))
        run.guarded(d, lambda: matches.set_exemption(
            conn, pid=d["pid"], exempt=(verb == "exempt"),
            expected_revision=_bind_projection(conn, d)[1],
            render_id=_bind_projection(conn, d)[0]), line)
    elif verb == "revive":
        run.guarded(d, lambda: work.record_search(conn, pid=d["pid"], token=None, revive=True),
                    lambda res: f"{views.headline(d)}: I'll look again at the next check.")
    elif verb == "identity":
        who = m.group("who").strip()
        # An identity reaches every payment with that bank text (the KB re-settles
        # them all), so it goes through the same guard as a vendor-wide rule: the
        # named payment AND every other payment it changes must have been shown as
        # they are (round p7, Astra S1). The receipt is read after the commit.
        if _shown(conn, d["pid"]) is None:
            if d["pid"] not in run.reshow:
                run.reshow.append(d["pid"])
            run.lines.append(f"{views.headline(d)} hasn't been shown to you in this form yet "
                             "— here it is now; nothing applied.")
            run.unresolved += 1
            return

        def ident():
            kb.upsert_in_tx(conn, who, patterns=[d["bank_counterparty"]])
            conn.execute("UPDATE projections SET identity_question=0 WHERE pid=?", (d["pid"],))
            lineage.settle(conn, d["pid"])
            return {"identity": who}

        def receipt():
            after = work.describe(conn, d["pid"])
            tail = "; still missing a document." if views._is_missing(after) else "."
            return f"{views.field(d['bank_counterparty'])}: {views.field(who)}{tail}"
        _broad(conn, run, ident, receipt)
