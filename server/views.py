# server/views.py
"""The views (spec §"Pull only", §"What the operator can ask for"). The server
renders; Ellen relays the text verbatim. Every count, sum, date and ordering
is computed here.

Membership is fixed first: every managed, un-ended lineage from the
watermark through the end of the view's quarter, by effective date, whatever
its expectation or state. Only then does a view filter and cap what it
prints. The two coverage dates are printed together or not at all.

Rendering is not showing. build_review persists an UNSHOWN rendering;
mark_rendering_delivered, called after the send succeeded, promotes it and
advances the shown-revision pointers that corrections bind to."""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import unicodedata

import amounts
import binding
import dates
import db
import lineage
import work

CAP = 8
TELEGRAM_LIMIT = 4096
LABEL_ALLOWANCE = 64       # Casa's "📊 <display name>" label line: the plugin cannot read it
PROPOSAL_SETTLE_RESERVE = 1 + 2 + 2 * 32     # casa:result_broker.py, a83d6aa8
BODY_LIMIT = TELEGRAM_LIMIT - PROPOSAL_SETTLE_RESERVE - LABEL_ALLOWANCE - 1   # 3964 (S7 §7.6)
VIEWS = ("status", "missing", "check", "rest", "older", "all", "item", "quarter")
KIND_WORD = {"invoice": "invoice", "sales-invoice": "sales invoice", "credit-note": "credit note",
             "payslip": "payslip", "statement": "statement", "receipt": "receipt",
             "other": "document"}
# Machinery the operator never has to learn (spec §"The reversibility ladder").
FORBIDDEN = ("proposed", "conflicted", "revision", "projection", "CAS", "no-ref",
             "partial-search", "recipient?", "acct::", "pid", "match_id",
             "render_id")


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


# Only unbounded free-text FIELDS are clipped, each to its own bound (fix wave D
# round 3): a payee / bank counterparty text, a document number or recipient, a
# runner-up description, a residue detail — FIELD_MAX; a link — LINK_MAX. What
# identifies an item or a candidate (amount, currency, date, kind, the "Could
# be:" enumeration itself) is never clipped, so a bound item always prints
# everything that identifies it (D3). A whole line is never clipped: that cut
# the amount and date, or a second candidate, off lines that stayed bound.
FIELD_MAX = 60
LINK_MAX = 500              # = kb.LINK_MAX (#57): a stored link prints whole — a
                            # clipped URL is a wrong link; only older, longer ones clip


MARK = "\u00b7"             # reserved: only generated text prints it (round 6)
LITERAL_MARK = "\u2022"     # what a literal "·" in free text prints as


class _Names:
    """Per-rendering disambiguation (rounds 5-6): displayed identities are made
    unique BY CONSTRUCTION within one rendering, before anything is composed,
    so the bind-time backstop (_bindable) never decides liveness.

    Generated disambiguators live in a namespace literal text cannot occupy:
    they follow the reserved MARK, and every literal free-text field prints a
    MARK as LITERAL_MARK (field()).
    - digest: hex length of a clipped field's digest ("…·<hex>");
    - docs: doc_id -> level; a document at level k >= 1 prints
      " ·<issuer>" (k = 1), then " ·<issuer>·<sha256 prefix of 4(k-1)>" — the
      stored content hash, so distinct documents always end up distinct;
    - pids: pid -> the generated ref hex a payment's headline adds ("ref <hex>").
    Levels grow to a fixed point over the FINAL identity strings of ALL the
    rendering's entities, not per original collision group."""
    def __init__(self):
        self.digest, self.docs, self.doc_level, self.pids = 4, {}, {}, {}


_NAMES = None
_TAG = ""                   # binding V2: the first-line tag of the rendering being composed


def printed_ref(pid):
    """The generated "ref <hex>" a payment's headline prints under the disambiguation being
    composed (`named`), or None."""
    return _NAMES.pids.get(pid) if _NAMES is not None else None


def tag_now() -> str:
    """#53 (operator ruling 2026-10-08, the day added by BRAIN's ruling on d1 Terra S2): every
    rendering's first line ends with " · 8 Oct 21:04:37", the moment it is composed in the
    operator's zone (CASA_TZ, then TZ, then UTC: Casa's
    timekeeping.resolve_tz order). A reply quoting a card binds the card it quotes; two
    renderings with the same text composed in the same second (within a year) share it, and a quote of them
    with differing facts is refused visibly (AMBIGUOUS), never bound to the wrong one."""
    import os
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(os.environ.get("CASA_TZ") or os.environ.get("TZ") or "UTC")
    except Exception:           # an unknown or malformed zone name: Casa falls back to UTC
        tz = ZoneInfo("UTC")
    t = db._clock().astimezone(tz)
    return f" {MARK} {dates.short_day(t.date().isoformat())} {t.strftime('%H:%M:%S')}"


def _limit() -> int:
    """The body budget a page is filled to: BODY_LIMIT less the tag line 1 will carry."""
    return BODY_LIMIT - utf16_len(_TAG)


def field_raw(text, units: int = FIELD_MAX) -> str:
    """A literal free-text field: MARK neutralized, clipped to `units` with a
    digest of its FULL value ("…·3f9a"), as long as this rendering needs for
    all its clipped values to print distinct. NOT escaped (see `field`)."""
    return _field(text, units, _NAMES.digest if _NAMES is not None else 4)


def field(text, units: int = FIELD_MAX) -> str:
    """A literal free-text field as DISPLAYED: clipped (field_raw), then escaped (esc)."""
    return esc(field_raw(text, units))


_ESC = set("\\`*_[]()!|#<>~-")
_LEAD_NUM_DOT = re.compile(r"^(\d+)\.")


def _flat(text: str) -> str:
    return "".join(" " if (ch == "\n" or unicodedata.category(ch) == "Cc") else ch
                   for ch in text)


def esc(text, plain: bool = False):
    """THE escape (S7 §12): a dynamic field reaches a body only through here (inside
    `field`). Control characters and newlines become spaces (§7.6: Casa's body check, and
    a caption's one line); the dialect's markers are backslash-escaped unless `plain` (a
    file caption is sent as plain text)."""
    if not text:
        return text
    flat = _flat(text)
    if plain:
        return flat
    out = "".join("\\" + ch if ch in _ESC else ch for ch in flat)
    return _LEAD_NUM_DOT.sub(lambda m: m.group(1) + "\\.", out, count=1)


_UNESC = re.compile(r"\\([!-/:-@\[-`{-~])")


def unesc(text: str) -> str:
    """What the dialect displays for escaped text (a backslash before ASCII punctuation
    is consumed): used to compare a quoted post with a stored rendering (§8)."""
    return _UNESC.sub(r"\1", text)


# ---- #99: the house style, one place. A card or a view: a bold title line saying what it is
# or what happened; a question on its own line; one tight line per item, blank lines only
# between groups; bold labels; short names; no legend for the buttons; no empty section.

def title(text: str) -> str:
    """#99: a title or a question line (bold)."""
    return f"**{text}**" if text else text


def groups(*blocks) -> list:
    """#99: the lines of each non-empty block, one blank line between blocks."""
    out = []
    for b in blocks:
        b = list(b or [])
        if b:
            out += ([""] if out else []) + b
    return out


_BOLD = re.compile(r"(?<!\\)\*\*")


def displayed(text: str) -> str:
    """#99 d1 (Astra S2): what the operator sees of a stored text — the house style's bold
    markers (unescaped `**`) are formatting, not characters, then unesc. A quote is compared
    with this."""
    return unesc(_BOLD.sub("", text))


_BOLD_SPAN = re.compile(r"(?<!\\)\*\*(.+?)(?<!\\)\*\*")


def bold_spans(text: str) -> list:
    """#99: the house style's bold spans of a stored text, as displayed — the only
    formatting a post carries (Casa's shapes gate: every entity is one of these)."""
    return [unesc(m.group(1)) for m in _BOLD_SPAN.finditer(text)]


def deposit_safe(body: str) -> str:
    """The last step on every body a posting tool deposits (S7 §7.6): any Cc character
    other than newline and tab becomes a space — renderings stored before S7 included."""
    return "".join(" " if (unicodedata.category(ch) == "Cc" and ch not in "\n\t") else ch
                   for ch in body)


def caption_safe(line: str) -> str:
    """A file caption's one line, printable as Casa's _file_caption_ok judges it."""
    return "".join(ch if ch.isprintable() else " " for ch in line)


def _field(text, units, n) -> str:
    if not text:
        return text
    shown = text.replace(MARK, LITERAL_MARK)
    if utf16_len(shown) <= units:
        return shown
    tag = MARK + hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]
    return clip(shown, units - utf16_len(tag)) + tag


def _hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def lineage_ref(pid: int) -> str:
    """The stable hex a payment's generated "ref …" is a prefix of."""
    return _hex(f"lineage:{pid}")


def _fixpoint(ents, render, raise_level, cap=17) -> None:
    """Raise the level of every entity whose FINAL printed identity (render(e),
    normalized as the bind check sees it) is shared with any other entity of
    the rendering, until all are distinct (or `cap`)."""
    for _ in range(cap):
        seen: dict = {}
        for e in ents:
            seen.setdefault(_norm(render(e)), []).append(e)
        dups = [e for es in seen.values() if len(es) > 1 for e in es]
        if not dups:
            return
        for e in dups:
            raise_level(e)


@contextlib.contextmanager
def named(items, view_quarter=None, payee=True):
    """Compose under the disambiguation for `items` (what build_review does)."""
    global _NAMES
    saved = _NAMES
    _NAMES = names_for(items, view_quarter, payee)
    try:
        yield _NAMES
    finally:
        _NAMES = saved


def _doc_extra(doc, level) -> str:
    if level <= 0:
        return ""
    issuer = field(doc.get("issuer")) or ""
    sha = doc.get("sha256") or _hex(f"doc:{doc.get('doc_id')}")
    parts = ([issuer] if issuer else []) + ([sha[:4 * (level - 1)]] if level > 1 else [])
    if not parts:
        parts = [sha[:4 * level]]
    return " " + MARK + MARK.join(parts)


def names_for(items, view_quarter=None, payee=True) -> _Names:
    """The disambiguation for a rendering over `items` (every payment it may
    print: a superset of what it prints, so what it prints is distinct too)."""
    global _NAMES
    names, saved = _Names(), _NAMES
    try:
        values = set()
        docs = {}
        for d in items:
            values.add(d["counterparty"] or "")
            values.add(shown(d) or "")
            for c in ([d["current"]] if d["current"] else []) + d["candidates"]:
                doc = c["document"]
                docs[doc["doc_id"]] = doc
                values.update(x for x in (doc.get("number"), doc.get("issuer")) if x)
        values = {v for v in values if utf16_len(v) > FIELD_MAX}
        for n in range(4, 65, 4):
            if len({_field(v, FIELD_MAX, n) for v in values}) == len(values):
                names.digest = n
                break
        _NAMES = names

        def raise_doc(i):
            names.doc_level[i] = names.doc_level.get(i, 0) + 1
            names.docs[i] = _doc_extra(docs[i], names.doc_level[i])
        _fixpoint(list(docs), lambda i: ident(docs[i]), raise_doc)

        by_pid = {d["pid"]: d for d in items}
        level: dict = {}

        def raise_pid(p):
            level[p] = level.get(p, 0) + 1
            names.pids[p] = lineage_ref(p)[:4 * level[p]]
        _fixpoint(list(by_pid), lambda p: headline(by_pid[p], view_quarter, payee), raise_pid)
    finally:
        _NAMES = saved
    return names


def _day(d):
    return dates.short_day(d) if d else "no date"


def _money(d) -> str:
    return amounts.fmt(d["amount_minor"], d["currency"]) if d.get("amount_minor") is not None else "?"


def shown(d: dict) -> str:
    """Issue #59 (1): the payee's name as the operator reads it (work.describe's
    `readable`), else its stored name."""
    return d.get("readable") or d["counterparty"]


def headline(d: dict, view_quarter=None, payee=True) -> str:
    """`payee=False` (PLAY 0.11.2): a vendor card's line — its head names the vendor once;
    the generated ref still tells two otherwise-equal payments apart (names_for is composed
    with the same `payee`)."""
    parts = ([field(shown(d))] if payee else []) + [_money(d), _day(d["date"])]
    kind = d["expectation"]["kind"]
    if kind and kind not in ("invoice", "none"):
        parts.append(KIND_WORD[kind])
    if d.get("pending"):
        parts.append("pending")
    if _NAMES is not None and _NAMES.pids.get(d["pid"]):
        parts.append("ref " + _NAMES.pids[d["pid"]])
    if view_quarter and d.get("quarter") and d["quarter"] != view_quarter:
        parts.append(dates.quarter_label(d["quarter"]))
    return " · ".join(parts)


def _a(word: str) -> str:
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


def _docname(doc: dict) -> str:
    w = KIND_WORD.get(doc["kind"], "document")
    return f"{w} {field(doc['number'])}" if doc.get("number") else w


# Evidence enumerations are bounded (round 4). Runner-ups are context and never
# bind: at most RUNNERS_MAX, then "and N others". Candidates DO bind: a view
# prints at most CANDIDATES_MAX and binds only those; the rest are one phrase
# away — an item view that pages every candidate, each page binding what it prints.
RUNNERS_MAX = 3
CANDIDATES_MAX = 3


def ident(doc: dict) -> str:
    """How a pairing is named in the text: document and date. The binding
    check (_bindable) requires this exact string to be visible, and unique
    within its payment."""
    extra = _NAMES.docs.get(doc.get("doc_id"), "") if _NAMES is not None else ""
    return f"{_docname(doc)}{extra} ({_day(doc['date'])})"


def _cands(d, cands):
    return d["candidates"][:CANDIDATES_MAX] if cands is None else cands


def _amount_word(d) -> str:
    return f"{abs(d['amount_minor'] or 0) / 100:.2f}"


def candidates_phrase(d) -> str:
    return f"candidates for {_amount_word(d)} {_day(d['date'])}"


def _names_pick(d) -> bool:
    """The evidence opens with "Matched to <document>." / "Suggested: <document>."."""
    cur = d["current"]
    return cur is not None and ("guessed" not in cur["labels"] or cur["author"] == "operator")


def evidence(d: dict, cands=None) -> list:
    """`cands` are the candidates to print (default: the first
    CANDIDATES_MAX, with the phrase that shows the rest)."""
    out = []
    cur = d["current"]
    if cur is not None:
        doc = cur["document"]
        name = _docname(doc)
        if "facts-changed" in d["reasons"]:
            out.append("The bank changed this payment after it was matched — still right?")
        if "amount-unknown" in d["reasons"]:
            out.append("The invoice's amount was read two different ways — check it.")
        labels = cur["labels"]
        if _names_pick(d):
            # a line that asks for a verdict names what it is asking about (round p7:
            # a no-ref line never named its invoice, yet "all good" confirmed it)
            out.insert(0, f"{'Suggested' if d['status'] == 'proposed' else 'Matched to'}"
                          f"{':' if d['status'] == 'proposed' else ''} {ident(doc)}.")
        if "guessed" in labels and cur["author"] != "operator":
            rs = cur["runners_up"]
            others = "; ".join(field(x) for x in rs[:RUNNERS_MAX])
            if len(rs) > RUNNERS_MAX:
                others += f"; and {len(rs) - RUNNERS_MAX} others"
            out.append(f"Picked {ident(doc)}; {others} also fits." if others
                       else f"Picked {ident(doc)} among several that fit.")
        if "no-ref" in labels:
            out.append("Repeating equal charges, and no invoice number on both sides.")
        if "partial-search" in labels:
            out.append("The search was cut short, so this may not be the only fit.")
        if "recipient?" in labels:
            out.append(f"{name[0].upper() + name[1:]} names "
                       f"{field(doc.get('recipient')) or 'someone else'}, not the business.")
        # #102: no "Not sure — say if it's wrong." on every item: the view's question says it
        if (doc.get("currency") and d.get("currency") and doc["currency"] != d["currency"]
                and doc.get("amount_minor") is not None):
            # issue #30: a document in another currency (a USD invoice for a EUR card
            # charge) shows both amounts, so the operator can judge the pairing
            import fx
            at = fx.expected(d.get("fx"), d["amount_minor"], d["currency"], doc["currency"])
            bank = (f" ({amounts.fmt(at, doc['currency'])} at the bank's rate)"
                    if at is not None else "")             # issue #35 (R4)
            out.append(f"The {KIND_WORD.get(doc['kind'], 'document')} is in "
                       f"{amounts.fmt(doc['amount_minor'], doc['currency'])}; the payment is "
                       f"{_money(d)}{bank}.")
    shown = _cands(d, cands)
    if shown:
        out.append("Could be: " + ", ".join(ident(c["document"]) + _fx(c["document"], d)
                                            for c in shown) + ".")
    if cands is None and len(d["candidates"]) > len(shown):
        out.append(f"{len(d['candidates']) - len(shown)} more could fit — ask me to show "
                   "them.")
    return out


def _fx(doc: dict, d: dict) -> str:
    """Issue #30 (C1, Astra S2): a candidate in another currency names its own amount."""
    if (doc.get("currency") and d.get("currency") and doc["currency"] != d["currency"]
            and doc.get("amount_minor") is not None):
        return f" in {amounts.fmt(doc['amount_minor'], doc['currency'])}"
    return ""


def pairings(d: dict, cands=None) -> dict:
    """The pairings a block that shows evidence(d, cands) displays, by match id,
    with the identity string it prints for each."""
    out = {c["match_id"]: ident(c["document"]) for c in _cands(d, cands)}
    if d["current"] is not None:
        out[d["current"]["match_id"]] = ident(d["current"]["document"])
    return out


def _tracked(d):
    # the reducer emits `unclassified`/`conflicted` even for lineages it then
    # reports ended or ineligible: those reasons are not the operator's to act on
    return d["status"] not in ("ended", "ineligible")


def _open_required(d):
    return _tracked(d) and d["status"] == "open"


def waiting(d) -> bool:
    """#117: waiting on the bank — pending at the bank, or open and absent from the latest
    bank read (not fresh). THE one predicate: cards._bucket counts these "waiting on the
    bank" first, so a view never asks what one is or calls it missing."""
    return bool(d.get("pending")) or (d["status"] == "open" and not d.get("fresh", True))


def _searched(d):
    """Looked for, or no longer to be looked for: an item the operator stopped
    chasing (or a merge carried `accepted-missing` onto) is never searched
    again, and stays "still open, still listed" as missing (spec §"What a pass
    works on"), never "the next pass looks"."""
    return (bool(d["search"].get("last_searched_at")) or d["identity_question"]
            or d["search_state"] != "active")


def _is_missing(d):
    """Missing means searched and not found (spec §Weekly pass: four states stay
    distinct). A required item nobody has looked for yet is `not searched` or
    `not checked`, never `missing`; #98: nor one not classified yet."""
    return (_open_required(d) and _searched(d) and not _is_unclassified(d)
            and not _is_conflict(d) and not waiting(d))


def _is_unsearched(d):
    return (_open_required(d) and not _searched(d) and not _is_unclassified(d)
            and not _is_conflict(d) and not waiting(d))


def _is_unclassified(d):
    # #98, #105: open with an unknown expectation kind (row 4: no classification yet or
    # parked; row 5: conflicting tags): no missing invoice (cards.unclassified, the same
    # fact). A conflict keeps its own "What is this?" line on the status sheet
    return (_open_required(d) and d["expectation"]["kind"] is None and not _is_conflict(d)
            and not waiting(d))


def _is_conflict(d):
    return _tracked(d) and "classification-conflict" in d["reasons"]


def _guess(d) -> bool:
    """A pairing the lists ask the operator to check: #117 r1 (Terra S2), never one waiting on
    the bank — the cards count that one "waiting on the bank", not "to confirm"."""
    return _needs_check(d) and not waiting(d)


def _needs_check(d):
    if not _tracked(d):
        return False
    if d["candidates"] or d["status"] == "proposed":
        return True
    # an operator-confirmed pairing is not a proposal (S7 §7.3), whatever its label row says:
    # a confirmation appends an operator pair but leaves the match's "guessed" label
    return (d["status"] == "matched" and d["current"] is not None
            and d["current"]["author"] != "operator" and d["current"]["labels"] != ["clean"])


def link_line(link) -> str:
    """Issue #59 (2): a vendor's download link, saying what it is."""
    return f"Where to download: {field(link, LINK_MAX)}"


def _missing_detail(d) -> list:
    if d["identity_question"]:
        return ["Who was this payment to?"]
    out = []
    if not d["search"].get("last_searched_at"):
        # #111: a payment not classified yet is asked about, never "not searched"
        if d["search_state"] == "active" and d["expectation"]["kind"] is not None:
            out.append("Not searched yet.")
    elif d["search"].get("incomplete"):
        out.append("Search incomplete — resumes next pass.")
    if d["link"]:
        out.append(link_line(d["link"]))
    if d["search_state"] == "accepted-missing":
        out.append("No longer chased.")
    return out


def membership(conn, view: str, quarter: str, pid=None) -> list:
    if view == "item":
        return [lineage.resolve_pid(conn, pid)]
    b = binding.get(conn)
    if b is None:
        return []
    end = dates.quarter_bounds(quarter)[1]
    out = []
    for p in lineage.live_pids(conn):
        proj = lineage.projection(conn, p)
        if proj["ended"]:
            continue
        row = lineage.live_row(conn, proj)
        eff = dates.effective_date(row) if row else None
        if eff is not None and b["watermark"] <= eff < end:
            out.append(p)
    return out


def coverage(conn, members) -> str:
    if not members:
        return "No transactions yet."
    snap = conn.execute("SELECT bank_through FROM snapshots ORDER BY snapshot_id DESC"
                        " LIMIT 1").fetchone()
    if snap is None or snap["bank_through"] is None:
        return "Bank not checked yet"
    obs = [lineage.projection(conn, p)["class_observed_at"] for p in members]
    seen = [o for o in obs if o]
    never = len(obs) - len(seen)
    cls = (f"classification through {dates.short_day(min(seen))}" if seen
           else "classification not checked yet")
    line = f"Bank checked through {dates.short_day(snap['bank_through'])} · {cls}"
    if never and seen:
        line += f" · {never} never checked"
    return line


def _lead(conn):
    """(stop lines or None, gmail unavailable, the interrupted pass's report or None,
    Gmail not connected for the finance specialist at all — S2's `absent` probe)."""
    setup = binding.check_setup(conn)
    if not setup["can_run"]:
        return ([setup["header"], *setup["conditions"], "Nothing else to do until then."], False,
                None, False)
    gmail = setup["probes"].get("gmail")
    gmail_down = gmail is not None and not gmail["ok"]
    absent = gmail_down and bool((gmail["data"] or {}).get("absent"))
    # A package snapshot or a document handover is not a review pass: it must not
    # erase an interrupted review's block (Task 22 review, item 5).
    last = conn.execute("SELECT * FROM passes WHERE ended_at IS NOT NULL"
                        " AND trigger NOT IN ('package', 'handover')"
                        " ORDER BY ended_at DESC, generation DESC LIMIT 1").fetchone()
    interrupted = None
    if last is not None and last["outcome"] == "interrupted":
        rep = json.loads(last["report_json"] or "{}")
        # issue #29: the counts are the server's, and a pass that never reached its Gmail
        # round stores none — then the block says only that the review was interrupted
        interrupted = ((int(rep["checked"]), int(rep["total"]))
                       if "checked" in rep and "total" in rep else ())
    return None, gmail_down, interrupted, absent


def _status_notes(conn) -> list:
    """S2: what the status head adds — how old the bank read the last check's decisions
    rested on, when its freshness window was waived (§5.2); how many payments still await
    classification (§6.4); and a check asked for that no job has started yet (§6.3)."""
    import passes
    out = []
    last = conn.execute("SELECT report_json FROM passes WHERE ended_at IS NOT NULL"
                        " AND trigger NOT IN ('package', 'handover')"
                        " ORDER BY ended_at DESC, generation DESC LIMIT 1").fetchone()
    rep = json.loads((last["report_json"] if last else None) or "{}")

    def count(key):
        v = rep.get(key)
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0
    if count("read_age_min"):
        out.append(f"These results use a bank read from {count('read_age_min')} minutes before "
                   "they were finished.")
    n = count("awaiting_classification")
    if n:
        out.append(f"{_plural(n, 'payment')} still "
                   + ("awaits classification — it's" if n == 1
                      else "await classification — they're")
                   + " checked again once classified.")
    asked = conn.execute("SELECT min(created_at) FROM work_requests WHERE state='queued'"
                         ).fetchone()[0]
    if asked is not None:
        out.append(f"A check is waiting to start, asked {passes.ago(asked)}.")
    return out


def _plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _degraded_block(gmail_down, interrupted, missing, unsearched, absent=False) -> list:
    """A degraded pass leads with its own condition, ahead of any accounting
    result, and keeps `missing` apart from `not searched` / `not checked`
    (spec §Weekly pass, "A broken pass must not read as deficient books")."""
    out = []
    if gmail_down:
        out.append("Review incomplete - Gmail unavailable.")
        if absent:
            out.append("Gmail isn't connected for the finance specialist — invoices aren't "
                       "being searched.")
        if missing:
            kinds = {d["expectation"]["kind"] for d in missing}
            word = KIND_WORD.get(kinds.pop(), "document") if len(kinds) == 1 else "document"
            out.append(f"{_plural(len(missing), word)} already missing.")
        if unsearched:
            out.append(f"{_plural(len(unsearched), 'new payment')} not searched.")
        out.append("No reply needed; I'll retry next pass.")
    if interrupted:
        checked, total = interrupted
        out += ["Review interrupted.", f"{checked} of {total} new payments checked.",
                f"{max(total - checked, 0)} not checked yet. Saved."]
    elif interrupted is not None:
        out += ["Review interrupted.", "Saved."]
    return out


class _Block:
    """One printed item: its lines, and what printing it binds on delivery."""
    __slots__ = ("lines", "pid", "pairings", "residue", "amount", "order", "name", "guessed",
                 "offer", "ident")

    def __init__(self, lines, *, pid=None, pairings=None, residue=None, amount=0, order=("", 0),
                 name=None, guessed=False, offer=None, ident=None):
        # `pairings`: match id -> the identity string printed for it; `ident`: the
        # payment's printed identity (its headline). Both are checked at bind time.
        self.lines, self.pid, self.residue = list(lines), pid, residue
        self.pairings, self.ident = dict(pairings or {}), ident
        self.offer = offer              # a package offered for "send it again"
        self.amount, self.order, self.name, self.guessed = amount or 0, order, name, guessed


class _Section:
    def __init__(self, title, blocks, *, gap=False, empty=None):
        self.title, self.blocks, self.gap, self.empty = title, blocks, gap, empty


def _residue_blocks(conn) -> tuple:
    """Unshown residue as blocks, and the ids of residue rows that print
    nothing (no amount known, a merge, a per-pairing retirement the `ended`
    line already tells). Those are marked shown with the rendering that
    carries the residue, so they cannot accumulate unseen."""
    blocks, silent = [], []
    for r in conn.execute("SELECT * FROM residue WHERE shown_render IS NULL ORDER BY id"):
        d = work.describe(conn, r["pid"]) if r["pid"] else None
        if d is None or d.get("amount_minor") is None:
            silent.append(r["id"])
            continue
        head = headline(d)
        if r["reason"] == "ended":
            freed = conn.execute("SELECT COUNT(*) FROM log WHERE pid=? AND kind='retire' AND"
                                 " cause='row-ended'", (r["pid"],)).fetchone()[0]
            tail = "its document is free again" if freed else "nothing was paired to it"
            line = f"{head} left the bank ledger ({field(r['detail'])}) — {tail}"
        elif r["reason"] == "occupied":
            line = f"{head} — that document is already on another payment"
        elif r["reason"] == "kind-mismatch":
            line = f"{head} — its category changed; its document no longer fits"
        elif r["reason"] == "exempt-doc":
            line = f"{head} — a document turned up for a payment you said needs none"
        elif r["reason"] == "broken-floor":
            line = f"{head} — bank-feed's history for it is broken; left as it was"
        else:
            silent.append(r["id"])
            continue
        blocks.append(_Block([line], residue=r["id"], amount=abs(d["amount_minor"]),
                             order=(d["date"] or "", r["id"])))
    return blocks, silent


def _item_blocks(ds, detail, q, shows_pairings=False, guessed=False, inline=False) -> list:
    """`pairings` are the match ids whose proposition the text displays. Only
    those are bound for a later correction (round p1, Astra S1: a missing view
    that bound candidates it never showed let "Adobe is wrong" reject them). `inline`
    (#99): the detail follows the headline on its one line."""
    def lines(d):
        return [" — ".join([headline(d, q), *detail(d)])] if inline \
            else [headline(d, q), *detail(d)]
    return [_Block(lines(d), pid=d["pid"], ident=headline(d, q),
                   pairings=pairings(d) if shows_pairings else {},
                   amount=abs(d["amount_minor"] or 0), order=(d["date"] or "", d["pid"]),
                   name=d["counterparty"], guessed=guessed)
            for d in ds]


def _first_review(conn) -> bool:
    return conn.execute(
        "SELECT COUNT(*) FROM renders WHERE delivered_at IS NOT NULL AND kind IN ('status','all')"
        " AND coalesce(json_extract(scope_json, '$.stop'), 0) = 0").fetchone()[0] == 0


CHECK_Q = "Are these the right documents?"      # #102: the check view's question


WHAT_IS_IT = "what is it?"     # #111: a payment not classified yet asks, on its line


def view_title(view, q) -> str:
    return {"status": f"Accounting · {dates.quarter_label(q)}",
            "all": f"Accounting · {dates.quarter_label(q)}",
            "missing": f"Missing · {dates.quarter_label(q)}",
            # #77: the list holds every quarter's matches to confirm — no quarter in its head
            "check": "To check",
            "rest": f"Nice to have · {dates.quarter_label(q)}",
            "older": "Older, still open",
            "quarter": f"Accounting · {dates.quarter_label(q)}"}[view]


def _compose(conn, view, q, items, members, lead):
    """The parts of a view, before any capping or paging: a head printed on
    every page, the announcement printed once, the sections, and a tail built
    from what was actually printed."""
    stop, gmail_down, interrupted, absent = lead
    cur = [d for d in items if d["quarter"] == q]
    # every older open item, searched or not: an older payment nobody has looked
    # for yet is still open work the default quarter must not hide (issue #4)
    older_open = [d for d in items if d["quarter"] and d["quarter"] < q and _open_required(d)]
    older_missing = [d for d in older_open if _is_missing(d)]
    older_unsearched = [d for d in older_open if _is_unsearched(d)]
    older_unclassified = [d for d in older_open if _is_unclassified(d)]
    missing = [d for d in cur if _is_missing(d)]
    unsearched = [d for d in items if _is_unsearched(d)]
    guessed = [d for d in items if _guess(d)]
    nice = [d for d in cur if _tracked(d) and d["status"] == "optional" and not waiting(d)]
    uncl = [d for d in cur if _is_unclassified(d)]
    conflicts = [d for d in cur if _is_conflict(d) and not waiting(d)]
    # #117 r1: waiting on the bank comes first, whatever the status (cards._bucket's order)
    waits = [d for d in cur if _tracked(d) and waiting(d)]
    matched_clean = [d for d in cur if d["status"] == "matched" and not _needs_check(d)
                     and not waiting(d)]
    b = binding.get(conn)
    parts = {"view": view, "head": [], "announce": [], "sections": [], "silent": [],
             "tail": None}
    if view == "item":
        parts["tail"] = lambda printed_guessed: []
        return parts                    # composed page by page in _item_page

    titles = {v: view_title(v, q) for v in VIEWS if v != "item"}
    # the same quarter figure the coverage line prints; older ones have their own line
    parts["head"] = _degraded_block(gmail_down, interrupted, missing, unsearched, absent)
    if view in ("status", "all"):
        parts["head"] += _status_notes(conn)
    if parts["head"]:
        parts["head"].append("")
    parts["head"].append(title(titles[view]))
    # #79: "To check" spans earlier quarters too; one quarter's bank coverage says nothing of it.
    # #111: a list (missing, rest, older) carries none either: only the quarter's sheets do
    cov = coverage(conn, members) if view in ("status", "all", "quarter") else None
    if view in ("status", "all", "quarter") and members:
        cov += f" · {_plural(len(cur), 'transaction')}, {len(missing)} missing a document."
    if view in ("status", "all") and _first_review(conn):
        # "Same sheet, preceded by `First review · bank checked through 20 Sep`"
        cov = "First review · " + cov[0].lower() + cov[1:]
    if cov is not None:
        parts["head"].append(cov)
    if view == "check" and guessed:
        # #102: the question once, above the items it asks about
        parts["head"].append(title(CHECK_Q))
    if b is not None and not b["watermark_announced"] and view in ("status", "all", "missing"):
        start_q = dates.quarter_of(b["watermark"])
        n = dates.parse_quarter(start_q)[1]
        before = f"Q{n - 1}" if n > 1 else "Q4"
        parts["announce"].append(f"Starting from {dates.quarter_label(start_q)} — ask me to do "
                                 f"{before} to go further back")
    secs = parts["sections"]
    if view in ("status", "all"):
        res, parts["silent"] = _residue_blocks(conn)
        secs.append(_Section("", res))
        # a send that may not have arrived is offered, never resent by itself
        # (spec §Packaging, "Delivery"); a block like any other, so the cap,
        # the paging and the final fit all apply to it. The rendering records
        # the packages it printed: "send it again" resolves against those only
        # (delivery.resend_target).
        import delivery
        secs.append(_Section("", [_Block(delivery.offer_lines(fname, status),
                                         order=("", pkg_id), offer=pkg_id)
                                  for pkg_id, fname, status in delivery.offerable(conn, q)]))
    if view in ("status", "all", "missing", "quarter"):
        # #99: one tight line per payment, its detail after it
        # #102: on the missing view the title already says it; no second heading
        secs.append(_Section(title("Missing") if view != "missing" else "",
                             _item_blocks(missing, _missing_detail, q, inline=True),
                             empty="Nothing is missing." if view == "missing" else None))
        # #106 r1: the payments not classified yet, each a bindable line, under their title;
        # #111: each line asks what it is (the title is not said twice)
        secs.append(_Section(title("Not classified yet"), _item_blocks(
            uncl, lambda d: [WHAT_IS_IT], q, inline=True)))
    if view in ("status", "all", "quarter"):
        # #117: the payments the cards count "waiting on the bank", under that title
        secs.append(_Section(title("Waiting on the bank"), _item_blocks(
            waits, lambda d: [], q, inline=True)))
    if view in ("status", "all"):
        secs.append(_Section(title("What is this?"), _item_blocks(
            conflicts, lambda d: ["The categories on it disagree — which is it?"], q)))
        secs.append(_Section(title("I guessed these"), _item_blocks(guessed, evidence, q, True, True)))
    if view == "check":
        secs.append(_Section("", _item_blocks(guessed, evidence, q, True, True),
                             empty="Nothing to check."))
    if view == "rest":
        secs.append(_Section("", _item_blocks(nice, lambda d: [], q),
                             empty="Nothing else is missing."))
    if view == "older":
        secs.append(_Section("", _item_blocks(older_open, _missing_detail, q),
                             empty="Nothing older is open."))

    packages = []
    if view == "quarter":
        for pk in conn.execute("SELECT p.filename, d.settled_at FROM packages p JOIN deliveries d"
                               " ON d.package_id=p.package_id WHERE p.quarter=? AND"
                               " d.status='delivered' ORDER BY d.settled_at", (q,)):
            packages.append(f"Sent {pk['filename']} on {_day(pk['settled_at'])}.")
        packages.append("Ask me for a fresh package when you need one.")

    def tail(printed_guessed):
        out = list(packages)
        if view in ("status", "all", "missing"):
            counts = []
            # one line, one source: an interrupted pass's lead already says how
            # many it did not reach, from the run record
            # older ones are counted on their own quarter's line below
            new = [d for d in unsearched if d not in older_unsearched]
            if new and not gmail_down and interrupted is None:
                counts.append(f"{_plural(len(new), 'new payment')} not checked yet"
                              " — the next pass looks.")
            if counts:
                out += ["", *counts]
            if nice:
                out.append(f"+{len(nice)} nice-to-have not shown")
            # missing and not-searched stay distinct states, each on its own line
            for ds, state in ((older_missing, "still missing"),
                              (older_unsearched, "not searched yet"),
                              (older_unclassified, "not classified yet")):      # #98 r1
                if ds:
                    qs = sorted({dates.quarter_label(d["quarter"]).split()[0] for d in ds})
                    out.append(f'+{len(ds)} older {state} ({", ".join(qs)})')
        if view in ("status", "all"):
            if printed_guessed or matched_clean:
                out.append("")
            if matched_clean:
                # #117: "Everything" only when the sheet shows nothing else open
                others = (guessed or missing or uncl or conflicts or waits or older_open
                          or unsearched)
                out.append("Everything else matched cleanly." if others
                           else "Everything matched cleanly.")
            if printed_guessed:
                # #117: no wording to copy — the operator says it their way
                out.append("Tell me if one is wrong.")
        if view in ("status", "all", "missing") and missing:
            out.append("Download the PDFs and email them to yourself, then ask me to check "
                       "emailed invoices.")
        return out
    parts["tail"] = tail
    return parts


def _text(lines) -> str:
    """The lines, one logical item each: the client wraps them (#54)."""
    return "\n".join(lines)


def _emit(parts, picks, *, announce, more=None, cap=None, all_sections_empty_msgs=True):
    """Lines for the chosen blocks. `picks` maps a section index to the blocks
    to print (in order); `cap`, when set, adds the `+N more` line to every
    section with more blocks than it printed; `more` is a continuation line."""
    out = list(parts["head"]) + (list(parts["announce"]) if announce else [])
    chosen, printed_guessed = [], []
    for i, sec in enumerate(parts["sections"]):
        blocks = picks.get(i, [])
        if not sec.blocks:
            if sec.empty and all_sections_empty_msgs:
                out.append(sec.empty)
            continue
        if not blocks and cap is None:
            continue
        if sec.title is not None:
            out.append("")
            if sec.title:
                out.append(sec.title)
        for j, blk in enumerate(blocks):
            if j and sec.gap:
                out.append("")
            out.extend(blk.lines)
            chosen.append(blk)
            if blk.guessed:
                printed_guessed.append(blk.name)
        if cap is not None and len(sec.blocks) > len(blocks):
            out.append(f"+{len(sec.blocks) - len(blocks)} more not shown")
    if more is not None:
        out += ["", more]
    else:
        out += parts["tail"](printed_guessed)
    return out, chosen


def _capped(parts, cap):
    picks = {}
    for i, sec in enumerate(parts["sections"]):
        if len(sec.blocks) > cap:
            picks[i] = sorted(sec.blocks, key=lambda b: (-b.amount, b.order))[:cap]
        else:
            picks[i] = sorted(sec.blocks, key=lambda b: b.order)
    return _emit(parts, picks, announce=True, cap=cap)


# #66: show_view posts every page of a list at once (Casa `pages`), so a page that continues
# says so; the typed "more" is the action card's own line (list_card)
MORE_LINE = "Continued in the next message."
SAY_MORE = "There are more after these."


def _day_ordinal(day) -> int:
    """A block's date as an int for the paging cursor (S7 §7.6: a stored More button's
    `after` holds only ints, never bank text); a missing or malformed date is 0."""
    try:
        return dates.parse_day(day).toordinal() if day else 0
    except ValueError:
        return 0


def _key(i, blk) -> list:
    return [i, _day_ordinal(blk.order[0]), blk.order[1]]


def _page(parts, after, first):
    """The next page of the whole list: every block after the cursor `after`,
    in section and date order, as many as fit one message with the
    continuation line. A cursor rather than a page number, so blocks that
    delivering an earlier page removed (residue) do not shift later pages.
    The continuation phrase is never the one that asked for the whole list
    (spec: "the rest stays one word away")."""
    # ordered by the cursor's own key, so the cursor and the page order always agree
    flat = sorted(((i, blk) for i, sec in enumerate(parts["sections"]) for blk in sec.blocks),
                  key=lambda e: _key(*e))
    if after is not None:
        flat = [e for e in flat if _key(*e) > list(after)]

    def emit(page, last):
        picks = {}
        for i, blk in page:
            picks.setdefault(i, []).append(blk)
        return _emit(parts, picks, announce=first, more=None if last else MORE_LINE,
                     all_sections_empty_msgs=first)

    def fits(page, last):
        return utf16_len(_text(emit(page, last)[0])) <= _limit()

    if not flat and not first:
        return list(parts["head"]) + ["", "That is everything."], [], None
    if fits(flat, True):
        lines, chosen = emit(flat, True)
        return lines, chosen, None
    page = flat[:1]
    for e in flat[1:]:
        if not fits(page + [e], False):
            break
        page.append(e)
    lines, chosen = emit(page, False)
    return lines, chosen, _key(*page[-1])


FIT_CLOSING = "The rest did not fit in one message."
CLIP_MARK = "\u2026"


def clip(text: str, units: int) -> str:
    """`text` cut to at most `units` UTF-16 units, ending in CLIP_MARK when it
    was cut — for displayed detail with no natural bound (a probe's diagnostic,
    a bank counterparty text). Cuts between code points, so a non-BMP
    character is never split into half a surrogate pair."""
    if utf16_len(text) <= units:
        return text
    out, used = [], 0
    budget = units - utf16_len(CLIP_MARK)
    for ch in text:
        n = utf16_len(ch)
        if used + n > budget:
            break
        out.append(ch)
        used += n
    return "".join(out) + CLIP_MARK


def fit_lines(lines, closing=None, always_close=False, tag="") -> tuple:
    """THE fit (fix wave D round 2, generalized after the same shape recurred in
    views, alerts and receipts): every operator-facing message is produced
    through here, and what it returns joins with "\n" to at most
    BODY_LIMIT UTF-16 units — every separator and the closing line
    included, whatever the inputs.

    Returns (out_lines, whole): `whole` is how many leading input lines are
    printed IN FULL — the only ones a caller may bind (D3). When everything fits,
    out_lines is the input (plus `closing` if always_close). Otherwise whole
    lines while they fit beside the closing line; when not even the first line
    fits, it is clipped with CLIP_MARK so the message still says something (and
    `whole` is 0). The closing line is never cut away (it is clipped only if it
    alone exceeds the limit).

    `tag` (binding V2) ends line 1 and is counted inside the fit; it is never cut: a line 1
    that must be clipped is clipped before its tag."""
    lines = list(lines)
    if tag:
        lines = [(lines[0] if lines else "") + tag] + lines[1:]
    tail = [clip(closing, BODY_LIMIT)] if closing else []
    if utf16_len("\n".join(lines + (tail if always_close else []))) <= BODY_LIMIT:
        return lines + (tail if always_close else []), len(lines)
    budget = BODY_LIMIT - (utf16_len(tail[0]) + 1 if tail else 0)
    kept, used = [], 0
    for ln in lines:
        need = utf16_len(ln) + (1 if kept else 0)
        if used + need > budget:
            break
        kept.append(ln)
        used += need
    whole = len(kept)
    if whole == 0 and lines and budget > utf16_len(CLIP_MARK) + utf16_len(tag):
        head = lines[0][:len(lines[0]) - len(tag)] if tag else lines[0]
        kept = [clip(head, budget - utf16_len(tag)) + tag]
    if not kept and tail:
        return tail, 0
    return kept + tail, whole


def fit_message(lines, closing=None, always_close=False) -> str:
    """fit_lines, joined: a text that is always deliverable. `lines` may be
    a str (split at newlines)."""
    if isinstance(lines, str):
        lines = lines.split("\n")
    return "\n".join(fit_lines(lines, closing, always_close)[0])


def _item_sentence(d) -> str:
    cur, kind = d["current"], d["expectation"]["kind"]
    word = KIND_WORD.get(kind or "", "document")
    if d["ended"]:
        return "It has left the bank ledger."
    if d["status"] == "matched":
        return f"{_docname(cur['document'])[0].upper() + _docname(cur['document'])[1:]} is filed with it."
    if d["status"] == "proposed" and cur is None:
        return "Several documents could fit, and none is picked."   # D3: a joint machine set
    if d["status"] == "proposed":
        return f"Paired with {_docname(cur['document'])}, not confirmed."
    if d["status"] in ("exempt", "no-document"):
        return "Needs no document."
    if d["status"] == "ineligible":
        return "Before the start date; not tracked."
    if waiting(d):
        return "Waiting on the bank."          # #117: as the cards count it
    if d["status"] == "optional":
        return f"No {word} found (nice to have)."
    if kind is None:
        # #111: the payment's own card asks what it is; #117: no example words to copy
        return f"Not classified yet — {WHAT_IS_IT}"
    n = len(d["search"].get("queries", []))
    return f"No {word} yet." + (f" Searched {n} ways." if n else "")


def _item_block(d, cands, more) -> _Block:
    # #79: the evidence's "Matched to …" / "Suggested: …" already names the document
    said = ([] if d["status"] in ("matched", "proposed") and _names_pick(d)
            else [_item_sentence(d)])
    lines = [title(headline(d)), *said, *evidence(d, cands=cands)]     # #99: its title
    if _open_required(d) and not waiting(d):
        lines += _missing_detail(d)
    if more:
        lines += ["", MORE_LINE]
    return _Block(lines, pid=d["pid"], ident=headline(d), pairings=pairings(d, cands))


def _item_page(d, after):
    """The item view pages EVERY candidate (round 4): as many as fit one
    message after the cursor (the last match id printed), each page binding
    the candidates it prints. Returns (block, cursor or None)."""
    rest = [c for c in d["candidates"] if after is None or c["match_id"] > after[0]]
    n = 1 if rest else 0
    while n < len(rest) and utf16_len(_text(_item_block(d, rest[:n + 1],
                                                         n + 1 < len(rest)).lines)) \
            <= _limit():
        n += 1
    more = n < len(rest)
    return _item_block(d, rest[:n], more), ([rest[n - 1]["match_id"]] if more else None)


def build_review(conn, view="status", quarter=None, pid=None, page=None, after=None,
                 prev=None) -> dict:
    """See _build_review. The per-rendering disambiguation (_NAMES) lives only
    for the duration of one call."""
    global _NAMES, _TAG
    try:
        return _build_review(conn, view, quarter, pid, page, after, prev)
    finally:
        _NAMES, _TAG = None, ""


def _build_review(conn, view="status", quarter=None, pid=None, page=None, after=None,
                  prev=None) -> dict:
    """review_in_tx under its own write transaction."""
    with db.tx(conn):
        return review_in_tx(conn, view, quarter, pid, page, after, prev=prev)


def review_in_tx(conn, view="status", quarter=None, pid=None, page=None, after=None,
                 prev=None) -> dict:
    """The rendering, inside the caller's write transaction (S2 §6.4: job_report composes
    a result in its own one transaction). The per-rendering disambiguation (_NAMES) is
    reset after, as build_review does."""
    global _NAMES, _TAG
    assert conn.in_transaction, "a review is composed inside the write transaction"
    try:
        return _review(conn, view, quarter, pid, page, after, prev)
    finally:
        _NAMES, _TAG = None, ""


_RENDER_ID = re.compile(r"^r\d{1,18}$")


def _predecessor_mrevs(conn, prev, pid, page, after) -> dict:
    """Binding V1: the candidates a continued item page (page >= 2) adds from its explicit
    predecessor `prev` — that rendering's RECORDED match revisions for `pid` — only when it
    is an item rendering of the same pid whose `next` is exactly this page's call and whose
    projection revision is still current. Anything else adds nothing; a candidate changed
    since then is refused at bind as stale. A posting attempt alone never adds them."""
    r = conn.execute("SELECT kind, scope_json FROM renders WHERE render_id=?",
                     (prev,)).fetchone()
    if r is None or r["kind"] != "item":
        return {}
    scope = json.loads(r["scope_json"])
    want = {"view": "item", "pid": pid, "page": page, "after": after, "prev": prev}
    if scope.get("pid") != pid or scope.get("next") != want:
        return {}
    it = conn.execute("SELECT projection_revision, match_revisions_json FROM render_items"
                      " WHERE render_id=? AND pid=?", (prev, pid)).fetchone()
    cur = conn.execute("SELECT revision FROM projections WHERE pid=?", (pid,)).fetchone()
    if it is None or cur is None or it["projection_revision"] != cur[0]:
        return {}
    return json.loads(it["match_revisions_json"])


def _review(conn, view, quarter, pid, page, after, prev=None) -> dict:
    """`page` (1, 2, ...) renders the view uncapped, one message per page, and
    `after` is the cursor the previous page's `next` returned; the `all` view
    is the status view paged. The answer's `next` is the call that the phrase
    the text ends with asks for (`all of them`, `more`), or None; it names this
    rendering as `prev` (binding V1)."""
    global _NAMES, _TAG
    if view not in VIEWS:
        raise db.Refusal(f"view is one of {', '.join(VIEWS)}")
    if view == "item" and pid is None:
        raise db.Refusal("an item view names one transaction")
    if page is not None and (not isinstance(page, int) or page < 1):
        raise db.Refusal("page is 1, 2, 3, ...")
    if view == "all" and page is None:
        page = 1
    if after is not None and (page is None or page < 2 or not isinstance(after, list)
                              or not all(isinstance(x, int) and not isinstance(x, bool)
                                         for x in after)):
        raise db.Refusal("after is the cursor a previous page's `next` returned")
    if prev is not None and (not isinstance(prev, str) or not _RENDER_ID.fullmatch(prev)):
        raise db.Refusal("prev is the render id the More button carried")
    import cards
    # ruling Q2b: after an operator's "check Q2" a view with no quarter is Q2's
    q = quarter or cards.named_quarter(conn) or dates.quarter_of(db.now()[:10])
    dates.parse_quarter(q)
    # binding V2: the render id is minted before composing — its tag ends line 1, counted
    # inside every page budget (_limit), and this page's `next` names it as `prev` (V1)
    rid = f"r{db.next_seq(conn)}"
    _TAG = tag_now()
    lead = _lead(conn)                  # may record the pass's gate
    # Compose and persist under ONE write lock, so the revisions recorded are
    # exactly those of the facts the text shows (round p1, Astra S1: a write
    # between composing and recording bound the operator to an unseen document).
    # The caller holds it (review_in_tx).
    members, chosen, scope, nxt = [], [], {"quarter": q, "pid": pid}, None
    items = []
    if lead[0] is not None:
        notes = _status_notes(conn) if view in ("status", "all") else []
        text = "\n".join(fit_lines(_text(lead[0] + notes).split("\n"), FIT_CLOSING,
                                    tag=_TAG)[0])
        scope["stop"] = True
    else:
        members = membership(conn, view, q, pid)
        items = [work.describe(conn, p) for p in members]
        # every identity this rendering prints is made distinct before composing
        _NAMES = names_for(items, None if view == "item" else q)
        parts = _compose(conn, view, q, items, members, lead)
        if view == "item":
            blk, cursor = _item_page(items[0], after)
            lines, chosen = blk.lines, [blk]
            text = _text(lines)
            if cursor is not None:
                nxt = {"view": "item", "pid": pid, "page": (page or 1) + 1, "after": cursor,
                       "prev": rid}
            scope["page"], scope["after"] = page, after
        elif page is not None:
            lines, chosen, cursor = _page(parts, after, page == 1)
            text = _text(lines)
            if cursor is not None:
                nxt = {"view": view, "quarter": q, "page": page + 1, "after": cursor,
                       "prev": rid}
            scope["page"], scope["after"] = page, after
        else:
            for cap in range(CAP, -1, -1):
                lines, chosen = _capped(parts, cap)
                text = _text(lines)
                if utf16_len(text) <= _limit():
                    break
            if any(len(s.blocks) > sum(1 for c in chosen if c in s.blocks)
                   for s in parts["sections"]):
                nxt = {"view": "all" if view == "status" else view, "quarter": q, "page": 1,
                       "prev": rid}
        # The final text goes through the one fit. If it cut anything, nothing it
        # prints is bound: the text may not show every block chosen. The closing
        # line carries the phrase `next` answers, so it is never cut.
        closing = FIT_CLOSING
        if nxt is not None:
            closing = (MORE_LINE if "after" in nxt
                       else "The rest did not fit.")
        body = text.split("\n")
        out, whole = fit_lines(body, closing, tag=_TAG)
        text, cut = "\n".join(out), whole < len(body)
        if cut:
            chosen = []
        if page in (None, 1) and parts["announce"] and not cut \
                and parts["announce"][0] in lines:
            scope["announce_watermark"] = True
        if view in ("status", "all"):
            scope["residue"] = [c.residue for c in chosen if c.residue is not None]
            scope["offers"] = [c.offer for c in chosen if c.offer is not None]
            if page in (None, 1):
                scope["residue_silent"] = parts["silent"]
    printed = _bindable(chosen, text)
    # S7 §7.2: what the page's buttons act on, recorded with the rendering so a stored
    # rendering rebuilds them. Its own map, defined on every branch: `by_pid` below exists
    # only when names were composed, and a setup-stop page composes none (`items` is []).
    # `printed` keeps the printed order (_bindable builds it from `chosen` in order).
    described = {d["pid"]: d for d in items}
    scope["proposed"] = [p for p in printed if p in described
                         and _needs_check(described[p])
                         and described[p]["current"] is not None
                         and described[p]["current"]["match_id"] in printed[p]]
    if view == "item" and items and items[0]["pid"] in printed:
        # an item that is not bound (its text was cut, or ambiguous) records no state: a
        # verdict on it would refuse, so its page offers none (buttons_for)
        d0 = items[0]
        scope["item_state"] = ("exempt" if d0["status"] == "exempt"
                               else "proposed" if d0["pid"] in scope["proposed"]
                               else "paired" if d0["current"] is not None
                               and d0["current"]["match_id"] in printed[d0["pid"]]
                               else "none")
    scope["next"] = nxt
    if _NAMES is not None:
        # the generated refs this rendering printed on payments it binds: a reply's
        # "ref <hex>" is honoured only against these (round 6)
        # hex -> EVERY payment printed with that ref (round 9: refs are distinct only
        # within a payee-collision group, so two groups can print the same hex)
        refs: dict = {}
        for p in sorted(printed):
            if _NAMES.pids.get(p):
                refs.setdefault(_NAMES.pids[p], []).append(p)
        if refs:
            scope["refs"] = refs
        # the payee name each bound payment was SHOWN as: a reply resolves names
        # against what the operator saw, as well as the stored names (round 7)
        by_pid = {d["pid"]: d for d in items}
        seen = {str(p): field_raw(shown(by_pid[p])) for p in printed if p in by_pid}
        if seen:
            scope["names"] = seen
    # r5: every grammar-read field (FACT_FIELDS) is stored, an empty one explicitly — a
    # field a rendering LACKS is one an earlier version never recorded (reply._Lacks)
    for k, empty in (("names", {}), ("refs", {}), ("offers", []), ("walk", None)):
        scope.setdefault(k, empty)
    conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                 " membership_json) VALUES (?,?,?,?,?,?)",
                 (rid, view, db.canonical(scope), db.now(), text, json.dumps(members)))
    for p, shown_ids in printed.items():
        prev_rev = conn.execute("SELECT revision FROM projections WHERE pid=?",
                                (p,)).fetchone()[0]
        mrevs = {str(r[0]): r[1] for r in conn.execute(
            "SELECT match_id, revision FROM match_state WHERE pid=?", (p,))
            if r[0] in shown_ids}
        if view == "item" and page and page > 1 and prev is not None:
            # V1: a later page of one item adds its explicit predecessor's candidates, at
            # the revisions that predecessor recorded (a stale one refuses at bind)
            mrevs = {**_predecessor_mrevs(conn, prev, p, page, after), **mrevs}
        conn.execute("INSERT INTO render_items(render_id, pid, projection_revision,"
                     " match_revisions_json) VALUES (?,?,?,?)",
                     (rid, p, prev_rev, db.canonical(mrevs)))
    return {"render_id": rid, "text": text, "printed": len(printed), "next": nxt}


SHEET_VIEWS = ("check", "missing")


def fits_proposal(text: str) -> bool:
    """A stored rendering can be posted with buttons (S7 §7.1): its deposited body fits the
    proposal budget, and Casa's proposal text bound."""
    body = deposit_safe(text)
    return utf16_len(body) <= BODY_LIMIT and len(body) <= 4000


def show_counts(conn, q) -> tuple:
    """#66: the sizes of the two lists the next-step buttons show — the quarter's missing
    payments (the `missing` view) and the proposed matches to confirm (the `check` view)."""
    items = [work.describe(conn, p) for p in membership(conn, "status", q)]
    return (sum(1 for d in items if d["quarter"] == q and _is_missing(d)),
            sum(1 for d in items if _guess(d)))


def check_label(conn, q, check) -> str:
    """#102: [Show matches to confirm] opens the check view, which spans every quarter (#77);
    a quarter's card counts its own. The label says both when they differ and it fits
    Casa's 32 characters (d1 Terra), else the count of what it opens."""
    items = [work.describe(conn, p) for p in membership(conn, "status", q)]
    mine = sum(1 for d in items if _guess(d) and d["quarter"] == q)
    if mine == check:
        return f"Show matches to confirm ({check})"
    if not mine:
        return f"Show {check} earlier to confirm"
    split = f"Show {mine} to confirm (+{check - mine} earlier)"
    return split if len(split) <= 32 else f"Show {check} to confirm"


CLOSE = ("Close", None, None, None)          # Casa v0.344.64: clears the keyboard, runs nothing


def buttons_for(conn, r) -> list:
    """S7 §7.2: the stored calls of a posted rendering `r` (a renders row), in order, at
    most six, as (label, tool, args, key_spec); the last is Close (#66), beside at least
    one action (#93: none at all when there is nothing to act on). A writing
    button carries key_spec=(action, pid, None); the caller mints and stores its key. No
    More (#66): show_view posts a whole list at once. "Show …" opens the missing or the
    check list, with its count, only when it is not empty and is not this view."""
    scope = json.loads(r["scope_json"])
    rid, kind = r["render_id"], r["kind"]
    proposed = scope.get("proposed") or []
    out = []
    if kind in SHEET_VIEWS and proposed:
        out = [("All good", "verdict", {"render_id": rid, "action": "all-good"},
                ("all-good", None, None)),
               ("One by one", "verdict", {"render_id": rid, "action": "show-item",
                                          "pid": proposed[0]}, ("show-item", proposed[0], None))]
    elif kind == "item":
        pid = scope.get("pid")
        verdicts = {"proposed": ("right", "wrong", "no-invoice"),
                    "paired": ("wrong", "no-invoice"),
                    "none": ("no-invoice",)}.get(scope.get("item_state"), ())
        words = {"right": "Right", "wrong": "Wrong", "no-invoice": "No invoice needed"}
        out = [(words[a], "verdict", {"render_id": rid, "action": a, "pid": pid},
                (a, pid, None))
               for a in verdicts]
    q = scope.get("quarter")
    if kind != "item" and q:
        missing, check = show_counts(conn, q)
        # d1, r1, r2 (Astra S2): every button that shows another view is a tap that decides
        # when tapped whether that view is a card or, with nothing to act on, a plain answer
        # (a stored show_view call could not post it plain)
        if missing and kind != "missing":
            out.append((f"Show missing invoices ({missing})", "verdict",
                        {"render_id": rid, "action": "show-missing"},
                        ("show-missing", None, None)))
        if check and kind != "check":
            out.append((check_label(conn, q, check), "verdict",
                        {"render_id": rid, "action": "show-check"},
                        ("show-check", None, None)))
    # #93: Close beside the actions, never alone — a view with nothing to act on is posted
    # as a plain message (posting.plain_post)
    return out[:5] + [CLOSE] if out else []


MAX_PAGES = 6             # Casa v0.344.67: a card brings at most six plain pages before it


def list_card(conn, page_ids, nxt) -> str:
    """#66: the action card of a list posted as pages (Casa `pages`): a rendering of the
    pages' own kind whose render_items are the UNION of the pages' rows — every item each
    page bound, at the revisions that page recorded — so its "All good" (or an item's
    verdict) covers the whole list and refuses, all or nothing, if any of it changed. Its
    text says what is above; `nxt` (more pages than one post carries) is its "more"."""
    rows = [conn.execute("SELECT * FROM renders WHERE render_id=?", (p,)).fetchone()
            for p in page_ids]
    scopes = [json.loads(r["scope_json"]) for r in rows]
    kind, s0 = rows[0]["kind"], scopes[0]
    items: dict = {}
    for p in page_ids:
        for it in conn.execute("SELECT * FROM render_items WHERE render_id=?", (p,)):
            prev = items.get(it["pid"])
            mrevs = json.loads(it["match_revisions_json"])
            items[it["pid"]] = (it["projection_revision"],
                                {**(prev[1] if prev else {}), **mrevs})
    rid = f"r{db.next_seq(conn)}"
    if nxt is not None:
        # r1 (Astra S2): the rest continues from THIS card, whose rows hold every page's
        # candidates — so an item's next page merges them all (binding V1), as the last
        # More page did
        nxt = {**nxt, "prev": rid}
    proposed, names, refs, offers = [], {}, {}, []
    for sc in scopes:
        proposed += [p for p in sc.get("proposed") or [] if p not in proposed]
        names.update(sc.get("names") or {})
        for k, ps in (sc.get("refs") or {}).items():
            refs[k] = sorted(set(refs.get(k, [])) | set(ps))
        offers += [o for o in sc.get("offers") or [] if o not in offers]
    scope = {"quarter": s0.get("quarter"), "pid": s0.get("pid"), "list_pages": list(page_ids),
             "proposed": proposed, "names": names, "refs": refs, "offers": offers,
             "walk": None, "next": nxt}
    if kind == "item":
        states = {sc.get("item_state") for sc in scopes}
        for st in ("proposed", "paired", "exempt", "none"):
            if st in states:
                scope["item_state"] = st
                break
    q, n, k = s0.get("quarter"), len(items), len(page_ids)
    if kind == "item":
        d = work.describe(conn, s0.get("pid"))
        lines = [title(headline(d)),
                 f"Every possible document for it is listed above, in {k} messages."]
    else:
        what = {"missing": _plural(n, "payment") + " without an invoice",
                "check": _plural(len(proposed), "match", "matches") + " to confirm"}.get(
                    kind, _plural(n, "payment"))
        lines = [title(view_title(kind, q)),
                 f"{what[0].upper()}{what[1:]} — listed above, in {k} messages."]
    if nxt is not None:
        lines.append(SAY_MORE)
    lines[0] += tag_now()          # #99: no legend — the buttons say it
    conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                 " membership_json) VALUES (?,?,?,?,?,?)",
                 (rid, kind, db.canonical(scope), db.now(), _text(lines),
                  rows[0]["membership_json"]))
    for pid, (prev_rev, mrevs) in items.items():
        conn.execute("INSERT INTO render_items(render_id, pid, projection_revision,"
                     " match_revisions_json) VALUES (?,?,?,?)",
                     (rid, pid, prev_rev, db.canonical(mrevs)))
    return rid


def _norm(s: str) -> str:
    return re.sub(r"[\s\u00b7]+", "", s)


def _bindable(chosen, text) -> dict:
    """THE bind-time check (round 4): a payment is bound only if its printed
    identity (headline) is visible in the text and no other bound payment in
    this rendering prints the same identity; a pairing only if its identity
    string is visible and unique within its payment. Whatever the formatting,
    every entity a rendering binds is uniquely identified by text visibly in it.
    Returns pid -> the match ids bound."""
    flat = _norm(text)
    by_pid: dict = {}
    for c in chosen:
        if c.pid is None:
            continue
        e = by_pid.setdefault(c.pid, {"ident": c.ident, "pairings": {}})
        e["pairings"].update(c.pairings)
    seen: dict = {}
    for e in by_pid.values():
        k = _norm(e["ident"] or "")
        seen[k] = seen.get(k, 0) + 1
    out = {}
    for p, e in by_pid.items():
        k = _norm(e["ident"] or "")
        if not k or seen[k] > 1 or k not in flat:
            continue
        names = [_norm(v) for v in e["pairings"].values()]
        out[p] = {m for m, v in e["pairings"].items()
                  if names.count(_norm(v)) == 1 and _norm(v) in flat}
    return out


_LABEL_LINE = "\U0001f4ca "        # Casa's "📊 <display name>" label, first line of a post
QUOTE_CAP = 2000                   # Casa quotes a post's first 2,000 characters (§2)
# binding V3: every scope field the grammar (reply.py) reads — "same binding facts" is
# equality of these, the kind and the rendering's render_items rows. A new grammar read
# joins this list (an AST pin holds reply.py to it)
FACT_FIELDS = ("names", "refs", "proposed", "offers", "next", "walk", "quarter", "pid")
AMBIGUOUS = "I sent more than one version of that list — reply to the newest one."
UNMATCHED = "I can't find the message you replied to — here is the list as it is now."
LACKS = ("that message is from an earlier version of me and lacks what this needs — here is "
         "the list as it is now; nothing applied for it.")


class QuoteRefusal(Exception):
    """A quote that binds no one rendering (binding R1/R3): `line` is said, nothing is
    applied, and `view` is the show_view arguments of the recovery."""

    def __init__(self, line, view):
        super().__init__(line)
        self.line, self.view = line, view


def _bnorm(s: str) -> str:
    """A text as compared for binding: whitespace runs collapsed to one space, stripped.
    A stored rendering's body is compared this way, whole — its first line is data (a
    payee named "📊 Analytics" heads its item view), never Casa's label (r6)."""
    return re.sub(r"\s+", " ", s).strip()


# What Casa does to a post before it reaches the desk as `quoted` — undone, exactly, by
# _qnorm (r7, after r6's label; read at the Casa tree, bcebd66b):
# - result_broker.py:147 POST_LABEL_GLYPH "📊", :363 post_label() -> "📊 <display name>",
#   :372 compose_operator_message() -> f"{label}\n{body}": the label line heads the post;
# - specialist_desk.py:48 DESK_QUOTE_CHARS = 2000, :52 CLIP = "[…]", :78-84 clip(): a text
#   over the cap becomes text[:cap - len(CLIP)] + CLIP; :692 quotes the post through it.
CASA_QUOTE_CHARS = 2000
CASA_CLIP = "[\u2026]"


def _qnorm(s: str) -> str:
    """The incoming QUOTE as compared for binding: Casa's clip marker dropped when the quote
    is exactly the clipped length (only then did Casa cut it), Casa's label line (which Casa
    adds to the post, never to the stored body) dropped, then _bnorm. A shorter prefix
    still binds by the whole-overlap rule."""
    if len(s) == CASA_QUOTE_CHARS and s.endswith(CASA_CLIP):
        s = s[:-len(CASA_CLIP)]
    lines = s.split("\n")
    if lines and lines[0].startswith(_LABEL_LINE):
        lines = lines[1:]
    return _bnorm("\n".join(lines))


def binding_facts(conn, r) -> str:
    """V3: what a reading bound to rendering `r` can read — its kind, its render_items rows
    (pid, projection revision, match revisions) and every FACT_FIELDS value (a field the
    rendering never stored is told apart from an explicit null) — canonically."""
    scope = json.loads(r["scope_json"])
    items = [[it["pid"], it["projection_revision"], json.loads(it["match_revisions_json"])]
             for it in conn.execute("SELECT * FROM render_items WHERE render_id=? ORDER BY pid",
                                    (r["render_id"],))]
    return db.canonical({"kind": r["kind"], "items": items,
                         "scope": {k: [k in scope, scope.get(k)] for k in FACT_FIELDS}})


def _rid_order(rid: str):
    return (0, int(rid[1:]), "") if _RENDER_ID.fullmatch(rid) else (1, 0, rid)


def _common_view(rows) -> dict:
    """The show_view arguments of the candidates' common view, else the status view."""
    import cards
    kinds = {r["kind"] for r in rows}
    scopes = [json.loads(r["scope_json"]) for r in rows]
    quarters = {sc.get("quarter") for sc in scopes}
    pids = {sc.get("pid") for sc in scopes}
    if len(kinds) == 1 and len(quarters) == 1 and len(pids) == 1:
        kind, (q,), (pid,) = next(iter(kinds)), quarters, pids
        if kind in cards.KINDS:          # simple loop §1: a card recovers as open items
            return {"view": "open", **({"quarter": q} if q else {})}
        if kind in VIEWS and (kind != "item" or pid is not None):
            out = {"view": kind}
            if q:
                out["quarter"] = q
            if kind == "item":
                out["pid"] = pid
            return out
    return {"view": "status"}


def bound_rendering(conn, quoted):
    """S7 §8, binding R1/R3: THE one rendering a reading binds. Words with no quote bind
    db.last_delivered. A quote's candidates are every SEEN rendering (db.seen_render:
    delivered, or posted), db.UNQUOTABLE_KINDS excluded, with no row limit and
    no ordering. A candidate matches when its normalised text and the normalised quote agree
    over their whole overlap, capped at QUOTE_CAP (the shorter is a prefix of the other: a
    post joins up to job.POST_MAX renderings, §5, and its quote binds the first). One match
    binds. Several with the same binding facts (V3) bind the lowest render id — the choice
    cannot change what commits. Several with differing facts raise QuoteRefusal (AMBIGUOUS:
    possible only among pre-S7 renderings, V2 tags the rest); none raises it too
    (UNMATCHED) — an explicit quote never falls back. Recency never picks."""
    q = _qnorm(quoted)[:QUOTE_CAP] if isinstance(quoted, str) else ""
    if not q:
        return db.last_delivered(conn)
    found = []
    for r in conn.execute("SELECT * FROM renders WHERE (delivered_at IS NOT NULL OR posted_seq"
                          " IS NOT NULL) AND kind NOT IN (%s)"
                          % ",".join("?" * len(db.UNQUOTABLE_KINDS)),
                          db.UNQUOTABLE_KINDS):
        # the body as posted: deposit_safe is the last step of every deposit (§7.6), so a
        # pre-S7 body's control characters are spaces in what the operator saw
        t = _bnorm(displayed(deposit_safe(r["text"] or "")))[:QUOTE_CAP]
        n = min(len(t), len(q))
        if db.seen_render(r) and n and t[:n] == q[:n]:
            found.append(r)
    if not found:
        raise QuoteRefusal(UNMATCHED, {"view": "status"})
    found.sort(key=lambda r: _rid_order(r["render_id"]))
    if len({binding_facts(conn, r) for r in found}) == 1:
        return found[0]
    raise QuoteRefusal(AMBIGUOUS, _common_view(found))


def render_items(conn, render_id) -> list:
    return [r[0] for r in conn.execute("SELECT pid FROM render_items WHERE render_id=?",
                                       (render_id,))]


def mark_rendering_delivered(conn, render_id: str) -> dict:
    """#66: an action card's pages (scope `pages`) went out before it, in one post: they are
    delivered first, so the card is the last rendering delivered."""
    with db.tx(conn):
        r = conn.execute("SELECT * FROM renders WHERE render_id=?", (render_id,)).fetchone()
        if r is None:
            raise db.Refusal(f"there is no rendering {render_id}")
        for page in json.loads(r["scope_json"]).get("list_pages") or []:
            _mark_delivered(conn, page)
        return _mark_delivered(conn, render_id)


def _mark_delivered(conn, render_id: str) -> dict:
    r = conn.execute("SELECT * FROM renders WHERE render_id=?", (render_id,)).fetchone()
    if r is None:
        raise db.Refusal(f"there is no rendering {render_id}")
    if r["delivered_at"] is not None:
        return {"render_id": render_id, "already": True}
    now = db.now()
    # the delivery ORDER is the store sequence, not the one-second timestamp (db.last_delivered)
    conn.execute("UPDATE renders SET delivered_at=?, delivered_seq=? WHERE render_id=?",
                 (now, db.next_seq(conn), render_id))
    scope = json.loads(r["scope_json"])
    # every delivered rendering's items become `shown` (S7 §9 retires renders.binding,
    # the stamp job_report's hand-out wrote: it is ignored, whatever it holds)
    # (binding §2 #13: a continued page's candidates are merged when it is composed,
    # from its explicit predecessor only — never here; nothing outside views reads
    # `shown`)
    for it in conn.execute("SELECT * FROM render_items WHERE render_id=?",
                           (render_id,)).fetchall():
        conn.execute("INSERT OR REPLACE INTO shown(pid, render_id, projection_revision,"
                     " match_revisions_json, delivered_at) VALUES (?,?,?,?,?)",
                     (it["pid"], render_id, it["projection_revision"],
                      it["match_revisions_json"], now))
    for rid_ in scope.get("residue", []) + scope.get("residue_silent", []):
        conn.execute("UPDATE residue SET shown_render=? WHERE id=?", (render_id, rid_))
    if scope.get("announce_watermark"):
        conn.execute("UPDATE binding SET watermark_announced=1 WHERE id=1")
    for a in scope.get("alerts", []):
        conn.execute("UPDATE alerts SET sent_at=?, render_id=? WHERE alert_id=?",
                     (now, render_id, a))
    # simple loop §1 (D19, shape d): a "package ready" notice — or an end message that
    # carries one — counts as given only once delivered, for the completion composed
    for q, sig in (scope.get("ready_sigs") or {}).items():
        conn.execute("INSERT OR IGNORE INTO quarter_notices(quarter) VALUES (?)", (q,))
        conn.execute("UPDATE quarter_notices SET sig=?, times=times+1, render_id=? WHERE"
                     " quarter=?", (sig, render_id, q))
    if scope.get("announce_package_name"):
        conn.execute("UPDATE binding SET package_name_announced=1 WHERE id=1")
    return {"render_id": render_id, "delivered_at": now}
