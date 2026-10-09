#!/usr/bin/env python3
"""S7 §7.6/§12 (Task 14): write every deposit body this plugin composes, over hostile store
text in every dynamic source, as JSONL for scripts/check_casa_shapes.py — which judges each
with Casa's REAL validators and renderer under Casa's interpreter. Stdlib only.

    python3 tests/gen_casa_shapes.py [OUT.jsonl]      (default: tests/casa_shapes.jsonl)

The first line is a header the checker holds the rest to: {"case": "header", "kinds":
{kind: deposits}, "cases": [every deposit case], "display_checked": n} — so an empty or
truncated file fails. Then one line per deposit: {"case", "tool", "body", and exactly one of
"display_expect" | "display_skip"}. `body` is the deposit request exactly as the plugin sent
it (FakeBroker records it). `display_expect` is the composition with every field unescaped —
what Casa's renderer must display; `display_skip` names why no display is promised (a legacy
rendering, stored before S7: spec §7.6 leaves it un-re-escaped; a file caption is plain
text). Every button of every proposal adds a {"case": "stored_call", "tool",
"arguments"} line, and every WRITING button (one whose arguments carry a key) is tapped here
through qa_server.TOOLS on the generator's own store: its receipt must not be keys.NO_LONGER
(§7.6's successful keyed tap per shape, asserted in stdlib; the store is restored after).

Each shape is one generator function over a fresh store (`SHAPES`), one per brief item:
show_view (items 1–2), propose_reading (3), propose_account (4), post_results (5; its package
note comes from post_package's delivered send) and post_package (6). The checker requires a
deposit of every capability tool the manifest declares, and of every deposit kind.

A deposit whose post the operator can swipe-reply carries {"bind": {"render_id", "store"}}:
every show_view, the package note (post_results) and each package file (post_package, #44).
The checker builds Casa's real quote of that post and the plugin must bind it back to that
rendering on the store copy; the header's "binds" counts them (a legacy rendering, whose
display is not promised, is not quoted).
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests._base import LoopCase, StoreCase            # noqa: E402  (puts server/ on the path)
from tests.fakebroker import FakeBroker                # noqa: E402
from tests.test_s7_escape import HOSTILE               # noqa: E402  (Task 3's hostile set)

CLOCK = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc)    # Ruling F3: 2026-Q3
QUARTER = "2026-Q3"
STARS = "*" * 2100                    # the brief's oversized payee
FULL = 40                             # a full page: 40 payments per section
ACCOUNT_ID = "acc<biz>"               # a bank-feed id with `<` (never a stored argument)
ACCOUNT_LABEL = "*Zakelijk* www.evil.example\x01 <b>"
NO_LONGER_PREFIX = "That button no longer applies"


def hostile(i: int, filler: str = "*") -> str:
    """The i-th hostile value, made oversized (2,100+ characters)."""
    return HOSTILE[i % len(HOSTILE)] + filler * 2100


class _Store(LoopCase):
    """StoreCase's fixtures, outside a test run: a fresh data dir and store per shape. The
    simple-loop shapes use LoopCase's pay/propose after loop_store(); setUp stays
    StoreCase's (an unbound store: each shape binds its own)."""
    setUp = StoreCase.setUp

    def runTest(self):                                 # pragma: no cover — never run
        pass


# each posting tool's delivered slot and its kind (§3)
SLOTS = {"show_view": "view", "propose_reading": "reading", "propose_account": "accounts",
         "post_results": "results", "post_package": "package", "get_package": "package",
         "get_document": "document", "rename_vendor": "view",
         "rename_vendors_to_invoice_names": "results"}
KINDS = {"show_view": "operator_proposal", "propose_reading": "operator_proposal",
         "propose_account": "operator_proposal", "post_results": "operator_message",
         "post_package": "operator_file", "get_package": "operator_file",
         "get_document": "operator_file", "rename_vendor": "operator_proposal",
         "rename_vendors_to_invoice_names": "operator_message"}
PROPOSALS = {t for t, k in KINDS.items() if k == "operator_proposal"}


def kind_of(rec) -> str:
    """The kind a non-stored_call record is counted under — the header's and the checker's
    one rule: a tap's next card (#1302: its tool is the tapped `verdict`, which delivers
    nothing), a capability's no-post refusal (#1303: `result`, no deposit), else the
    deposit kind of its tool."""
    if "next" in rec:
        return "next_card"
    if "result" in rec:
        return "no_post"
    return KINDS[rec["tool"]]


class Shapes:
    def __init__(self):
        self.records: list = []
        self.taps = 0
        self.ticks = 0                  # #60: seconds since CLOCK (Shapes.tick)
        self.shape = None               # the running shape: its store copy is named after it

    # -- recording -----------------------------------------------------------------
    def call(self, st, broker, case, tool, args, display=True, bind=False):
        """Run posting tool `tool` through qa_server.TOOLS and record its one deposit. A
        proposal's buttons are recorded too, every writing button is tapped (the store is
        restored after each), and the buttons are returned; otherwise the body is.
        `display`: True when the display check applies (the text is composed through
        views.esc), else the reason it does not (recorded as `display_skip`). `bind`: a
        post_results post of one quotable rendering, bound back by the checker (#44);
        show_view and post_package always are."""
        import qa_server
        import tools                                    # noqa: F401 — registers the tools
        import views
        n0 = len(broker.deposits)
        self.tick()
        out = qa_server.TOOLS[tool]["fn"](dict(args))
        if isinstance(out, dict) and out.get(SLOTS[tool]) is None and out.get("post"):
            # #93: nothing on it to act on — the desk posts it plain, as the note says
            body = None
            for i, group in enumerate(out["post"]):
                body = self.call(st, broker, f"{case}:plain{i}", "post_results",
                                 {"render_ids": group}, display=display,
                                 bind=len(group) == 1)
            return body
        if not (isinstance(out, dict) and isinstance(out.get(SLOTS[tool]), str)):
            raise AssertionError(f"{case}: {tool}({args}) posted nothing: {out}")
        new = broker.deposits[n0:]
        if len(new) != 1:
            raise AssertionError(f"{case}: expected one deposit, saw {len(new)}")
        body = {k: v for k, v in new[0].items() if k != "client"}
        if KINDS[tool] == "operator_file" and display is True:
            display = "a file caption is sent as plain text"
        rec = {"case": self._unique(case), "tool": tool, "body": body}
        if KINDS[tool] == "operator_file" and tool != "get_document":
            # #56: a shown document has no caption rendering (nothing to quote back)
            # #44: the file's caption is a rendering of its own, quoted as Casa composes it
            rid = st.conn.execute("SELECT render_id FROM renders WHERE kind='package-file'"
                                  " ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            rec["bind"] = {"render_id": rid, "store": self.shape}
        elif bind:
            if tool != "post_results" or len(args["render_ids"]) != 1:
                raise AssertionError(f"{case}: bind is one post_results rendering")
            rec["bind"] = {"render_id": args["render_ids"][0], "store": self.shape}
        if tool not in PROPOSALS:
            if display is True:          # an operator_message's text is its value
                rec["display_expect"] = views.displayed(body["value"])
                rec["bold_expect"] = views.bold_spans(body["value"])
            else:
                rec["display_skip"] = display
            self.records.append(rec)
            return body
        prop = json.loads(body["value"])
        if tool in ("show_view", "rename_vendor"):
            # binding r7: the checker builds Casa's real quote of this post (label, render,
            # clip) and the plugin must bind it back to this rendering, on the store copy
            rec["bind"] = {"render_id": out["render_id"], "store": self.shape}
            if tool == "show_view" and prop.get("pages"):
                # #66: each plain page before the card is a rendering of its own — the
                # checker displays and quotes every page and binds it back to its page
                page_ids = json.loads(st.conn.execute(
                    "SELECT scope_json FROM renders WHERE render_id=?",
                    (out["render_id"],)).fetchone()[0])["list_pages"]
                rec["page_binds"] = [{"render_id": rid, "display_expect": views.displayed(t),
                                      "bold_expect": views.bold_spans(t)}
                                     for rid, t in zip(page_ids, prop["pages"], strict=True)]
            elif tool == "rename_vendor":
                # #89: the rename's line, a page of its own before the card
                rid = st.conn.execute("SELECT render_id FROM renders WHERE text=? ORDER BY"
                                      " rowid DESC LIMIT 1", (prop["pages"][0],)).fetchone()[0]
                rec["page_binds"] = [{"render_id": rid,
                                      "display_expect": views.displayed(prop["pages"][0]),
                                      "bold_expect": views.bold_spans(prop["pages"][0])}]
        if display is True:
            rec["display_expect"] = views.displayed(prop["text"])
            rec["bold_expect"] = views.bold_spans(prop["text"])
        else:
            rec["display_skip"] = display
        self.records.append(rec)
        self._stored_calls(prop["buttons"])
        for b in prop["buttons"]:
            if "call" in b and "key" in b["call"]["arguments"]:
                self.tap(st, case, b)
        return prop["buttons"]

    def _stored_calls(self, buttons):
        for b in buttons:
            if "call" not in b:
                continue                 # #72: Casa's Close button stores no call
            self.records.append({"case": "stored_call", "tool": b["call"]["tool"],
                                 "arguments": b["call"]["arguments"]})

    def tick(self):
        """#60: each posting is a step of its own, a second after the last — the render
        tag is the moment to the second (#53), so a frozen clock would compose renderings
        no quote can tell apart, which no operator ever sees."""
        import db
        self.ticks += 1
        at = CLOCK + dt.timedelta(seconds=self.ticks)
        db._clock = lambda: at

    def _unique(self, case) -> str:
        if any(r["case"] == case for r in self.records):
            raise AssertionError(f"{case}: a case name is used twice")
        return case

    def refusal(self, broker, case, tool, args) -> dict:
        """#1303: a capability button's refusal — no deposit, every slot null, and the
        words as `receipt` (Casa posts them as the tap's answer). Recorded as {"case",
        "tool", "result"}; the checker judges it with Casa's own no-link and receipt
        readers."""
        import qa_server
        import tools                                    # noqa: F401 — registers the tools
        n0 = len(broker.deposits)
        out = qa_server.TOOLS[tool]["fn"](dict(args))
        if len(broker.deposits) != n0 or not isinstance(out, dict) \
                or out.get(SLOTS[tool], "") is not None:
            raise AssertionError(f"{case}: {tool}({args}) did not refuse: {out}")
        self.records.append({"case": self._unique(case), "tool": tool, "result": out})
        return out

    def next_card(self, case, label, tool, out):
        """#1302: a tap's answer is a receipt and the next card; recorded as
        "<case>:next:<label>" for the checker to judge as specialist_desk._post_next_card
        does (proposal_ok against the tapped tool's entry), with its stored calls."""
        name = f"{case}:next:{label}"
        n = sum(1 for r in self.records if r["case"].startswith(name))
        self.records.append({"case": self._unique(name + (f":{n + 1}" if n else "")),
                             "tool": tool, "receipt": out.get("receipt"),
                             "next": out["next"],
                             # Casa #1339: the card replaces the tapped one in place
                             **({"in_place": True} if out.get("in_place") is True else {})})
        self._stored_calls(out["next"].get("buttons") or [])

    def tap(self, st, case, button, keep=False) -> dict:
        """Execute a writing button's stored call; its receipt must not be NO_LONGER.
        Unless `keep`, the store is put back as it was before the tap."""
        import qa_server
        import tools                                    # noqa: F401 — registers the tools
        snap = None
        if not keep:
            snap = sqlite3.connect(":memory:")
            st.conn.backup(snap)
        call = button["call"]
        out = qa_server.TOOLS[call["tool"]]["fn"](dict(call["arguments"]))
        receipt = out.get("receipt") if isinstance(out, dict) else None
        if not isinstance(receipt, str) or receipt.startswith(NO_LONGER_PREFIX):
            raise AssertionError(f"{case}: the {button['label']!r} tap failed: {out}")
        self.taps += 1
        if "next" in out:
            if not isinstance(out["next"], dict):
                raise AssertionError(f"{case}: the {button['label']!r} tap's next is "
                                     f"{out['next']!r}")
            self.next_card(case, button["label"], call["tool"], out)
        if snap is not None:
            snap.backup(st.conn)
            snap.close()
        return out


# -- store builders --------------------------------------------------------------------
def docnum(n: int) -> str:
    """Document n's number: hostile, oversized, distinct per issuer."""
    return f"{HOSTILE[(n + 3) % len(HOSTILE)]} {n} " + "_" * 2000


def build(st, sections: dict) -> dict:
    """A bound store (hostile account label and id) with, per section, one payment per
    payee: `missing` (searched, no document; its counterparty has a hostile document link),
    `guessed` (a machine pairing labelled guessed, to a document with a hostile number),
    `clean` (the same, labelled clean: matched, nothing to check), `optional` (nice to
    have), `older` (2026-Q2, open, searched). Returns {section: [pid]}.
    Also a delivered and an uncertain package, so `quarter` and `status` print filenames."""
    import binding
    import db
    import kb
    import matches
    import work
    st.bind(account=ACCOUNT_ID, label=ACCOUNT_LABEL, watermark="2026-04-01")
    token = st.pass_()
    with db.tx(st.conn):
        st.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                        " bank_through) VALUES ('p1', 'x', 0, 0, '2026-09-20')")
    out, n = {}, 0
    for section, payees in sections.items():
        for i, payee in enumerate(payees):
            n += 1
            day = "2026-05-14" if section == "older" else "2026-09-14"
            cents = 1000 + n
            st.row(n, account_id=ACCOUNT_ID, counterparty=payee, amount_minor=cents,
                   booking_date=day, value_date=day)
            pid = st.lineage_for(n)
            st.classify(pid, {"reimbursement"} if section == "optional" else {"software"})
            with db.tx(st.conn):
                st.conn.execute("UPDATE projections SET class_observed_at=? WHERE pid=?",
                                ("2026-09-20T10:00:00Z", pid))
            st.settle(pid)
            if section in ("guessed", "clean"):
                doc_id = st.doc(counterparty=payee, issuer=payee, amount_minor=cents,
                                document_date=day, document_number=docnum(n))
                matches.record_match(st.conn, pid=pid, doc_id=doc_id, author="auto",
                                     expected_revision=st.rev(pid),
                                     row_snapshot=StoreCase.snapshot(st, pid), token=token,
                                     labels=(section,))
            else:
                work.record_search(st.conn, pid=pid, token=token, queries=["x"])
            out.setdefault(section, []).append(pid)
    for i, payee in enumerate(sections.get("missing", [])[:3]):
        if kb.counterparty_for(st.conn, payee) is None:
            kb.upsert_counterparty(st.conn, payee, patterns=[payee],
                                   document_link=hostile(i + 7, "`")[:kb.LINK_MAX],
                                   token=token)
    name = binding.get(st.conn)["package_name"]
    with db.tx(st.conn):
        for k, status in enumerate(("delivered", "uncertain")):
            fname = f"{name}-{QUARTER}-2026-09-1{k}.zip"
            pkg = st.conn.execute(
                "INSERT INTO packages(quarter, filename, path, built_at, partial, digest, size,"
                " caption, manifest_json) VALUES (?,?,?,?,0,'d',1,'c','{}')",
                (QUARTER, fname, f"/o/{fname}", db.now())).lastrowid
            st.conn.execute(
                "INSERT INTO deliveries(package_id, channel, staged_path, status, created_at,"
                " settled_at) VALUES (?, 'telegram', ?, ?, ?, ?)",
                (pkg, f"/o/s/{fname}", status, db.now(), db.now()))
    return out


SECTIONS = ("missing", "guessed", "clean", "optional", "older")
VIEWS_SIMPLE = ("status", "missing", "check", "rest", "older", "quarter")


def _views(sh, st, b, tag):
    """Every list view of the store, and `all` (#66: a full store's posts its pages before
    one action card, no More)."""
    for view in VIEWS_SIMPLE:
        sh.call(st, b, f"{tag}:{view}", "show_view", {"view": view, "quarter": QUARTER})
    p1 = sh.call(st, b, f"{tag}:all", "show_view", {"view": "all", "quarter": QUARTER})
    if any(x["label"] == "More" for x in p1):
        raise AssertionError(f"{tag}: a list offers More")
    rec = next(r for r in sh.records if r["case"] == f"{tag}:all")
    if tag.startswith("full") and not rec.get("page_binds"):
        raise AssertionError(f"{tag}: a full store's all view posted no pages")


def _item_states(sh, st, b, tag, pids):
    """`item` in each item_state, reached as the operator reaches it: proposed (One by one
    from the check sheet, so it offers Next on a full sheet), paired (a clean pairing),
    none (a missing payment), exempt (after the none page's No invoice needed tap)."""
    def labels(buttons):                       # #66: every view card ends with Close
        out = [x["label"] for x in buttons]
        if out[-1:] != ["Close"]:
            raise AssertionError(f"{tag}: a view card without Close: {out}")
        return out[:-1]
    check = sh.call(st, b, f"{tag}:check-walk", "show_view", {"view": "check",
                                                               "quarter": QUARTER})
    one = next(x for x in check if x["label"] == "One by one")
    item = sh.call(st, b, f"{tag}:item:proposed", "show_view", one["call"]["arguments"])
    if labels(item)[:3] != ["Right", "Wrong", "No invoice needed"]:
        raise AssertionError(f"{tag}: a proposed item offers {labels(item)}")
    paired = sh.call(st, b, f"{tag}:item:paired", "show_view",
                     {"view": "item", "pid": pids["clean"][0]})
    if labels(paired) != ["Wrong", "No invoice needed"]:
        raise AssertionError(f"{tag}: a paired item offers {labels(paired)}")
    none = sh.call(st, b, f"{tag}:item:none", "show_view",
                   {"view": "item", "pid": pids["missing"][0]})
    if labels(none) != ["No invoice needed"]:
        raise AssertionError(f"{tag}: an unpaired item offers {labels(none)}")
    sh.tap(st, f"{tag}:item:none", none[0], keep=True)
    exempt = sh.call(st, b, f"{tag}:item:exempt", "show_view",
                     {"view": "item", "pid": pids["missing"][0]})
    if isinstance(exempt, list) and any("key" in x.get("call", {}).get("arguments", {}) for x in exempt):
        raise AssertionError(f"{tag}: an exempt item offers a verdict")


# -- the shapes (brief items 1–2) ------------------------------------------------------
def gen_show_view_full_stars(sh, st, b):
    """Item 1, full pages: 40 payments per section whose payee is 2,100 `*`."""
    pids = build(st, {s: [STARS] * FULL for s in SECTIONS})
    _views(sh, st, b, "full-stars")
    _item_states(sh, st, b, "full-stars", pids)


def gen_show_view_full_hostile(sh, st, b):
    """Item 1, full pages: 40 payments per section, each payee a hostile value made
    oversized, document numbers and links hostile too."""
    pids = build(st, {s: [hostile(i + 11 * k) for i in range(FULL)]
                      for k, s in enumerate(SECTIONS)})
    _views(sh, st, b, "full-hostile")
    _item_states(sh, st, b, "full-hostile", pids)


def gen_show_view_single(sh, st, b):
    """Item 1, one oversized entry per section."""
    pids = build(st, {s: [hostile(k) + STARS] for k, s in enumerate(SECTIONS)})
    _views(sh, st, b, "single")
    _item_states(sh, st, b, "single", pids)


def gen_show_view_setup_stop(sh, st, b):
    """An unbound store: the setup lead's status page."""
    sh.call(st, b, "setup-stop:status", "show_view", {"view": "status"})


LEGACY = [
    # (case, stored text, display): a rendering stored before S7 — raw control characters,
    # never escaped for the dialect (§7.6: deposit_safe is its only step). `display` is True
    # (checked) or the reason no display is promised
    ("legacy:ctrl", "Accounting · Q3 2026\nACME\x01Corp owes 12.00\x7f and\x1bmore", True),
    ("legacy:hostile", "Accounting · Q3 2026\n" + "\n".join(
        v.replace("\n", "\x01")[:300] for v in HOSTILE) + "\n" + "\x01".join(["*"] * 1200),
     "legacy text is not re-escaped for the dialect (spec 7.6)"),
]


def gen_legacy_rendering(sh, st, b):
    """Item 2: show_view(render_id=…) of a stored legacy rendering with `\\x01`."""
    import db
    import views
    for n, (case, text, display) in enumerate(LEGACY):
        rid = f"r{9000 + n}"
        with db.tx(st.conn):
            st.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at,"
                            " text, membership_json) VALUES (?,'status','{}','x',?,'[]')",
                            (rid, text))
        sh.call(st, b, case, "show_view", {"render_id": rid}, display=display)
        posted = next(r for r in reversed(sh.records) if r["case"].startswith(case))
        if display is True and posted["display_expect"] != views.deposit_safe(text):
            raise AssertionError(f"{case}: a legacy text is displayed as itself, cleaned")


# -- the shapes (brief items 3–6) ------------------------------------------------------
READING_WRITES = 8
# the operator's own words, echoed in the reading's "Not included" lines: a question (it
# changes nothing, and a correction beside it still applies) with every marker, a control
# character and 2,000+ characters; no dot (a dot ends a clause — the payees carry `www.`)
ECHO = "is *Acme* _x_ `y` [a](b) a<b>c ACME\x01Corp 1) " + "Z" * 2000 + "?"


def gen_propose_reading(sh, st, b):
    """Item 3: a reading of eight writes ("all good": confirm the sheet's eight guesses,
    seven of whose payees are hostile). Then a reading that echoes the operator's hostile
    words as not included, beside one write (a question beside a correction leaves the
    correction standing; beside "all good" it would approve nothing). Apply and Cancel are
    tapped on each."""
    import views
    build(st, {"guessed": [hostile(i) for i in range(READING_WRITES - 1)] + ["Zapier"]})
    r = views.build_review(st.conn, view="check", quarter=QUARTER)
    views.mark_rendering_delivered(st.conn, r["render_id"])
    for case, text, writes in (("reading:eight", "all good", READING_WRITES),
                               ("reading:echo", f"the Zapier one is wrong. {ECHO}", 1)):
        n0 = len(sh.records)
        buttons = sh.call(st, b, case, "propose_reading", {"text": text})
        if [x["label"] for x in buttons] != ["Apply", "Cancel"]:
            raise AssertionError(f"{case}: the buttons are {buttons}")
        rid = buttons[0]["call"]["arguments"]["reading_id"]
        plan = json.loads(st.conn.execute("SELECT plan_json FROM readings WHERE"
                                          " reading_id=?", (rid,)).fetchone()[0])
        if len(plan) != writes:
            raise AssertionError(f"{case}: {len(plan)} writes, not {writes}")
        if writes == 1 and "Not included:" not in sh.records[n0]["display_expect"]:
            raise AssertionError(f"{case}: the operator's hostile words are not echoed")


ACCOUNT_SETS = [
    # (tag, labels, id of account i): Task 7's LABELS with ids carrying `<`, then the
    # hostile set itself (every marker, a link, a control character, 2,100+ characters)
    # with ids whose last four characters — the ones printed — are hostile too
    ("task7", ["Zakelijk <B.V.>", "www.bank.example", "x\x01y", "L" * 4050, "Ops", "Tax",
               "Payroll"], lambda i: f"acc<{i}>"),
    ("hostile", [hostile(i) for i in range(7)], lambda i: f"acc*_`\x01<{i}>"),
]


def gen_propose_account(sh, st, b):
    """Item 4: the account question, pages 1 and 2 (page 2 by page 1's More button's stored
    call), over 7 hostile accounts; every Account button is tapped."""
    for tag, labels, acc in ACCOUNT_SETS:
        snap = sqlite3.connect(":memory:")
        st.conn.backup(snap)
        st.accounts_probe([{"account_id": acc(i), "category": "company", "label": label}
                           for i, label in enumerate(labels)])
        p1 = sh.call(st, b, f"accounts:{tag}:1", "propose_account", {})
        if [x["label"] for x in p1] != [f"Account {i}" for i in range(1, 6)] + ["More"]:
            raise AssertionError(f"accounts:{tag}: page 1 offers {p1}")
        p2 = sh.call(st, b, f"accounts:{tag}:2", "propose_account", p1[-1]["call"]["arguments"])
        if [x["label"] for x in p2] != ["Account 1", "Account 2"]:
            raise AssertionError(f"accounts:{tag}: page 2 offers {p2}")
        snap.backup(st.conn)
        snap.close()


def _units(text: str, n: int) -> str:
    """`text` repeated and cut to exactly `n` UTF-16 units (whole code points)."""
    import views
    out, used = [], 0
    while used < n:
        for ch in text:
            u = views.utf16_len(ch)
            if used + u > n:
                return "".join(out) + "x" * (n - used)
            out.append(ch)
            used += u
    return "".join(out)


LEGACY_4096 = [
    # (case, two stored texts of 4,096 UTF-16 units each — Telegram's limit, what a
    # rendering stored before S7 could reach — display: True or the skip reason)
    ("results:legacy-plain",
     [_units("Accounting · Q3 2026 ACME\x01Corp owes 12.00\x7f and\x1bmore café 𝔘\n", 4096),
      _units("Older · ACME\x1fLtd\x7f paid 3.50\n", 4096)], True),
    ("results:legacy-hostile",
     [_units("\n".join(v.replace("\n", "\x01") for v in HOSTILE) + "\n", 4096),
      _units("*\x01_`[a](b) www.evil.example <b> 1. Ltd\n", 4096)],
     "legacy text is not re-escaped for the dialect (spec 7.6)"),
]
ALERTS = 48               # hostile package notices: more than three full renderings hold


def _render(st, kind, text) -> str:
    import db
    with db.tx(st.conn):
        rid = f"r{db.next_seq(st.conn)}"
        st.conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                        " membership_json) VALUES (?,?,'{}',?,?,'[]')",
                        (rid, kind, db.now(), text))
    return rid


def gen_post_results(sh, st, b):
    """Item 5: three BODY_LIMIT renderings of hostile lines in one post (package notices
    whose reasons are hostile, composed by alerts.pending_in_tx as the run's post composes
    them, each leaving out the ones before); two legacy 4,096-unit renderings in one post,
    plain and hostile."""
    import alerts, db, views
    with db.tx(st.conn):
        for i in range(ALERTS):
            # reasons kept to 150 characters: each rendering then fills to within the 400
            # units the fullness check below allows (one escaped hostile notice is longer)
            alerts.raise_package(st.conn, "package-not-sent", f"gate:{i}", quarter=QUARTER,
                                 reason=hostile(i, "_")[:150], pass_id="")
    rids, skip = [], set()
    for _ in range(3):
        with db.tx(st.conn):
            r = alerts.pending_in_tx(st.conn, skip=skip)
        units = views.utf16_len(r["text"])
        if not views.BODY_LIMIT - 400 <= units <= views.BODY_LIMIT:
            raise AssertionError(f"results: an alerts rendering of {units} units is not full")
        rids.append(r["render_id"])
        skip |= set(json.loads(st.conn.execute(
            "SELECT scope_json FROM renders WHERE render_id=?",
            (r["render_id"],)).fetchone()[0])["alerts"])
    sh.call(st, b, "results:three-full", "post_results", {"render_ids": rids})
    for case, texts, display in LEGACY_4096:
        if any(views.utf16_len(t) != 4096 for t in texts):
            raise AssertionError(f"{case}: a legacy rendering is not 4,096 units")
        body = sh.call(st, b, case, "post_results",
                       {"render_ids": [_render(st, "status", t) for t in texts]},
                       display=display)
        if display is True and body["value"] != views.deposit_safe("\n\n".join(texts)):
            raise AssertionError(f"{case}: a legacy text is posted as itself, cleaned")


# the stored caption's first line, made hostile: every marker, a link, a control character
# and 2,000+ characters (the file's own caption: escaped plain, clipped)
CAPTION_FIRST = ("Accounting Q3 2026 · *Acme* _x_ `y` a<b>c www.evil.example ACME\x01Corp "
                 + "Z" * 2000)


def gen_post_package(sh, st, b):
    """Item 6: the package as a file. get_package's send (simple loop §1, Task 9: built now,
    posted, the file the receipt — its own shape comes with Task 17), then a first send
    whose receipt was withheld (uncertain), its offer posted, its resend ("send it again")
    and a send-last ("send me the last package you built"), each posted with a hostile
    caption line."""
    import db, delivery, package, posting, views
    st.bind(label=ACCOUNT_LABEL)                    # the zip's name: the label's slug
    st.import_again()                               # get_package builds from a checked store
    n0 = len(b.deposits)
    got = posting.get_package(st.conn, QUARTER)
    if len(b.deposits) != n0 + 1 or b.deposits[-1]["filename"] != got["filename"]:
        raise AssertionError("package: get_package did not post its file under its name")
    pk = package.build_quarterly_package(st.conn, QUARTER)
    with db.tx(st.conn):
        st.conn.execute("UPDATE packages SET caption=? WHERE package_id=?",
                        (CAPTION_FIRST, pk["package_id"]))

    def post(case, did):
        body = sh.call(st, b, case, "post_package", {"delivery_id": did})
        if body.get("filename") != pk["filename"] or body.get("kind") != "zip":
            raise AssertionError(f"{case}: posted as {body.get('filename')!r}, not the "
                                 f"package's name {pk['filename']!r}")
        if "\n" in body["caption"] or len(body["caption"]) > 900:
            raise AssertionError(f"{case}: the caption is not one clipped line")
        return body

    did = delivery.stage_for_delivery(st.conn, package_id=pk["package_id"])["delivery_id"]
    post("package:first", did)
    out = delivery.record_delivery(st.conn, delivery_id=did, outcome="uncertain")
    # the offer, posted as the desk posts record_delivery's `speak` — quotable (#44: a
    # swipe-reply "send it again" on it binds the package it offers)
    sh.call(st, b, "results:package-offer", "post_results",
            {"render_ids": [out["speak"]["render_id"]]}, bind=True)
    views.mark_rendering_delivered(st.conn, out["speak"]["render_id"])   # the offer is seen
    s = delivery.stage_for_delivery(st.conn, resend=True,
                                    package_id=delivery.resend_target(st.conn))
    post("package:resend", s["delivery_id"])
    delivery.record_delivery(st.conn, delivery_id=s["delivery_id"], outcome="delivered")
    s = delivery.stage_for_delivery(st.conn, last_built=True)
    post("package:last", s["delivery_id"])


# -- the simple loop's shapes (Task 17: §1, #1301–#1303) ----------------------------------
# Every card is posted as the loop posts it: a show_view re-post of its stored rendering
# (Task 7's path), so Casa's real deposit judges its text and every button — [Get package]
# (#1303) included; every writing button is tapped, and each tap's next card (#1302) is
# recorded and judged as specialist_desk._post_next_card judges it.
Q3_DAY = "2026-09-14"
Q2_DAY = "2026-05-14"


def loop_store(st):
    """A simple-loop store: bound under the hostile account label from 2026-Q2 on (an
    earlier quarter in scope), a run claimed."""
    st.bind(label=ACCOUNT_LABEL, watermark="2026-04-01")
    st.token = st.run_claim()
    st.n = 0


def _c(st, fn, *a, **k):
    """A cards composer, inside its own transaction (each asserts it runs in one)."""
    import db
    with db.tx(st.conn):
        return fn(st.conn, *a, **k)


def _post(sh, st, b, case, rid, must=None):
    """Post stored rendering `rid` as the loop does (show_view's re-post); `must` is a
    phrase its text has to hold — the shape is the one asked for."""
    import views
    text = st.conn.execute("SELECT text FROM renders WHERE render_id=?", (rid,)).fetchone()[0]
    if must is not None and must not in views.displayed(text):
        raise AssertionError(f"{case}: {must!r} is not in {text[:200]!r}")
    return sh.call(st, b, case, "show_view", {"render_id": rid})


def _proposals(st, k, start=0, alternatives=0, day=Q3_DAY):
    """`k` proposals, each to a hostile oversized payee, its document number, issuer and
    alternatives' numbers hostile too. Returns the pids."""
    pids = []
    for i in range(start, start + k):
        cents = 5000 + i
        pid = st.pay(hostile(i), cents, day)
        alts = [st.doc(counterparty=hostile(i), issuer=hostile(i + j + 1), amount_minor=cents,
                       document_date=day, document_number=docnum(100 * i + j))
                for j in range(alternatives)]
        st.propose(pid, alternatives=alts, counterparty=hostile(i), issuer=hostile(i),
                   amount_minor=cents, document_date=day, document_number=docnum(i))
        pids.append(pid)
    return pids


def _missing(st, vendors, per=1, day=Q3_DAY, links=True):
    """`per` searched, documentless payments for each vendor; each vendor's counterparty
    carries a hostile document link (printed on its vendor page)."""
    import kb
    pids = []
    for v, vendor in enumerate(vendors):
        for i in range(per):
            pids.append(st.pay(vendor, 100 + i, day if per == 1 else
                               "2026-%02d-%02d" % (7 + i % 3, i % 28 + 1)))
        if links and kb.counterparty_for(st.conn, vendor) is None:
            kb.upsert_counterparty(st.conn, vendor, patterns=[vendor],
                                   document_link=hostile(v + 7, "`")[:kb.LINK_MAX],
                                   token=st.token)
    return pids


def gen_end_message_operator(sh, st, b):
    """The operator run's end message: hostile proposals (one with alternatives), hostile
    vendors with missing invoices, an earlier quarter's open item — then the same with 30
    proposals (lines dropped, no Confirm all)."""
    import cards
    loop_store(st)
    _proposals(st, 3, alternatives=1)
    _missing(st, [hostile(20), hostile(21)])
    _missing(st, [hostile(22)], day=Q2_DAY)
    end = _c(st, cards.compose_end, st.job_id, scheduled=False)
    labels = [x["label"] for x in _post(sh, st, b, "end:operator", end, "Q3 checked")]
    # #93 #94: Close beside the actions; a job's end card offers no package
    if labels != ["Review", "Confirm all", "Invoice links", "Close"]:
        raise AssertionError(f"end:operator: the buttons are {labels}")
    _proposals(st, 27, start=3)
    end = _c(st, cards.compose_end, st.job_id, scheduled=False)
    labels = [x["label"] for x in _post(sh, st, b, "end:operator-full", end, "more to confirm")]
    if labels != ["Review", "Invoice links", "Close"]:
        raise AssertionError(f"end:operator-full: the buttons are {labels}")


def gen_end_message_scheduled(sh, st, b):
    """A scheduled run's end message: only the items no delivered message showed, plus
    "earlier items still open"."""
    import cards, views
    loop_store(st)
    _proposals(st, 1)
    _missing(st, [hostile(20)])
    views.mark_rendering_delivered(st.conn, _c(st, cards.compose_end, st.job_id,
                                               scheduled=False))
    _proposals(st, 2, start=1)
    _missing(st, [hostile(20), hostile(23)])
    end = _c(st, cards.compose_end, st.job_id, scheduled=True)
    _post(sh, st, b, "end:scheduled", end, "earlier items still open")


def gen_end_message_nothing_to_ask(sh, st, b):
    """Every payment accounted for: "all accounted for", nothing to tap — a plain message
    (#93, #94)."""
    import cards
    loop_store(st)
    for i in range(3):
        pid = st.pay(hostile(i), 700 + i)
        st.machine_match(pid, st.doc(counterparty=hostile(i), issuer=hostile(i),
                                     amount_minor=700 + i, document_date=Q3_DAY,
                                     document_number=docnum(i)), st.token)
    end = _c(st, cards.compose_end, st.job_id, scheduled=False)
    body = _post(sh, st, b, "end:nothing", end, "all accounted for")
    if not isinstance(body, dict) or body.get("slot") != "results":
        raise AssertionError(f"end:nothing: not a plain message: {body}")


def gen_end_message_handover(sh, st, b):
    """A handover's end message (§2.5): one "Filed." line per handed document, and the
    proposals among them."""
    import cards
    loop_store(st)
    paired = st.pay(hostile(0), 10000)
    filed = st.doc(counterparty=hostile(0), issuer=hostile(0), amount_minor=10000,
                   document_date=Q3_DAY, document_number=docnum(0))
    st.machine_match(paired, filed, st.token)
    prop = st.pay(hostile(1), 4120)
    held = st.propose(prop, counterparty=hostile(1), issuer=hostile(1), amount_minor=4120,
                      document_date=Q3_DAY, document_number=docnum(1))
    lone = st.doc(counterparty=hostile(2), amount_minor=1, document_number=docnum(2))
    _missing(st, [hostile(3)])                                  # not shown: not handed
    end = _c(st, cards.compose_end, st.job_id, scheduled=False,
             handover_docs=[filed, held, lone])
    _post(sh, st, b, "end:handover", end, ": matched automatically to the")


def gen_end_message_handover_close(sh, st, b):
    """#93: a handover card with nothing to tap (a copy already filed, a document fitting no
    payment) is a plain message — never a card with Close alone."""
    import cards
    loop_store(st)
    first = st.doc(counterparty=hostile(0), issuer=hostile(0), amount_minor=10000,
                   document_date=Q3_DAY, document_number=docnum(0), recipient="Voorbeeld BV")
    copy = st.doc(counterparty=hostile(0), issuer=hostile(0), amount_minor=10000,
                  document_date=Q3_DAY, document_number=docnum(0), recipient="Voorbeeld BV")
    lone = st.doc(counterparty=hostile(2), amount_minor=1, document_number=docnum(2))
    end = _c(st, cards.compose_end, st.job_id, scheduled=False, handover_docs=[copy, lone])
    body = _post(sh, st, b, "end:handover-close", end, f": already filed as #{first}")
    if not isinstance(body, dict) or body.get("slot") != "results":
        raise AssertionError(f"end:handover-close: not a plain message: {body}")


def gen_end_message_with_completion(sh, st, b):
    """D19: an owed completion notice as a line of an end message with items, and as the
    message itself when nothing is left to ask."""
    import cards, work
    loop_store(st)
    q2 = st.pay(hostile(0), 900, Q2_DAY)
    st.machine_match(q2, st.doc(counterparty=hostile(0), issuer=hostile(0), amount_minor=900,
                                document_date=Q2_DAY, document_number=docnum(0)), st.token)
    left = _missing(st, [hostile(1)])
    end = _c(st, cards.compose_end, st.job_id, scheduled=False, ready=["2026-Q2"])
    _post(sh, st, b, "end:completion", end, "Q2 complete · package ready")
    st.granted(lambda c, grant: work.leave_missing_in_tx(c, left, grant=grant))
    end = _c(st, cards.compose_end, st.job_id, scheduled=False, ready=["2026-Q2", "2026-Q3"])
    _post(sh, st, b, "end:completion-ready", end, "Q3 complete · 1 of 1 accounted for")


def gen_open_items(sh, st, b):
    """The open-items card (show_view(view="open")): proposals and missing invoices."""
    loop_store(st)
    _proposals(st, 2, alternatives=2)
    _missing(st, [hostile(20), hostile(21)])
    out = sh.call(st, b, "open-items", "show_view", {"view": "open", "quarter": QUARTER})
    if [x["label"] for x in out] != ["Review", "Confirm all", "Invoice links", "Close"]:
        raise AssertionError(f"open-items: the buttons are {[x['label'] for x in out]}")
    # #94: the desk judged the operator wants the package
    out = sh.call(st, b, "open-items:package", "show_view", {"view": "open", "quarter": QUARTER,
                                                              "package": True})
    if [x["label"] for x in out] != ["Review", "Confirm all", "Invoice links", "Get package",
                                     "Close"]:
        raise AssertionError(f"open-items:package: the buttons are {[x['label'] for x in out]}")


def gen_all_answered(sh, st, b):
    """The open-items card once everything is answered (left missing counts)."""
    import work
    loop_store(st)
    left = _missing(st, [hostile(20)])
    st.granted(lambda c, grant: work.leave_missing_in_tx(c, left, grant=grant))
    sh.call(st, b, "all-answered", "show_view", {"view": "open", "quarter": QUARTER})
    # #93: nothing left to tap — the card goes as a plain message
    if "all accounted for" not in next(r for r in sh.records
                                       if r["case"].startswith("all-answered")
                                       )["display_expect"]:
        raise AssertionError("all-answered: the card does not say so")


def gen_ready_notice(sh, st, b):
    """The "package ready" notice (D19), first and updated, with an earlier quarter's line."""
    import cards, views
    loop_store(st)
    for n, day in enumerate((Q2_DAY, Q3_DAY), start=1):
        pid = st.pay(hostile(1), 900, day)
        # one purchase per payment (issue #48): each its own number
        st.machine_match(pid, st.doc(counterparty=hostile(1), issuer=hostile(1),
                                     amount_minor=900, document_date=day,
                                     document_number=docnum(n)), st.token)
    rid = _c(st, cards.compose_ready, ["2026-Q2", "2026-Q3"])
    _post(sh, st, b, "ready", rid, "Q3 complete · 1 of 1 accounted for · package ready")
    views.mark_rendering_delivered(st.conn, rid)
    _post(sh, st, b, "ready:updated", _c(st, cards.compose_ready, ["2026-Q3"]),
          "Q3 complete · updated · package ready")


def gen_review_cards(sh, st, b):
    """Review cards: a proposal of four candidates (the chosen one and three alternatives),
    a legacy joint set (no chosen one: named picks), and a single proposal."""
    import cards
    loop_store(st)
    (four,) = _proposals(st, 1, alternatives=3)
    joint = st.pay(hostile(5), 3300)
    for j in range(3):
        st.machine_entry(joint, st.doc(counterparty=hostile(5), issuer=hostile(5 + j),
                                       amount_minor=3300, document_date=Q3_DAY,
                                       document_number=docnum(50 + j)))
    (single,) = _proposals(st, 1, start=9)
    end = _c(st, cards.compose_end, st.job_id, scheduled=False)
    order = [o.get("p") for o in json.loads(st.conn.execute(
        "SELECT scope_json FROM renders WHERE render_id=?", (end,)).fetchone()[0])["order"]]
    for case, pid, want in (("review:candidates", four, 4), ("review:set", joint, 3),
                            ("review:single", single, 0)):
        buttons = _post(sh, st, b, case, _c(st, cards.card, end, order.index(pid)),
                        "· to confirm")
        picks = [x for x in buttons
                 if x.get("call", {}).get("arguments", {}).get("action") == "pick"]
        if len(picks) != want:
            raise AssertionError(f"{case}: {len(picks)} named candidates, not {want}")


def gen_replace_cards(sh, st, b):
    """Rev 18.4 §R18.3: a handed-over document for a payment that already has one — the end
    message's "to check" line, the replace card (a machine match, and an operator-confirmed
    one), and both taps."""
    import cards
    import db
    loop_store(st)
    rows = []
    for i, how in ((0, "job"), (1, "operator")):
        pid = st.pay(hostile(i), 10000 + i)
        cur = st.doc(counterparty=hostile(i), issuer=hostile(i), amount_minor=10000 + i,
                     document_date=Q3_DAY, document_number=docnum(i))
        st.machine_match(pid, cur, st.token)
        if how == "operator":
            mid = st.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                                  (pid,)).fetchone()[0]
            rid = st.show(pid)
            st.granted(lambda c, grant, mid=mid, rid=rid: __import__("matches").confirm_in_tx(
                c, grant=grant, match_id=mid, expected_revision=st.rev(match_id=mid),
                render_id=rid, bind="rendered"))
        new = st.doc(counterparty=hostile(i + 5), issuer=hostile(i + 5),
                     amount_minor=10000 + i, document_date=Q3_DAY,
                     document_number=docnum(10 + i))
        mid = st.conn.execute("SELECT current_match FROM projections WHERE pid=?",
                              (pid,)).fetchone()[0]
        with db.tx(st.conn):
            st.conn.execute("INSERT INTO replace_questions(job_id, pid, match_id, new_doc_id,"
                            " state, created_seq) VALUES (?,?,?,?, 'open', ?)",
                            (st.job_id, pid, mid, new, db.next_seq(st.conn)))
        rows.append(pid)
    end = _c(st, cards.compose_end, st.job_id, scheduled=False)
    labels = [x["label"] for x in _post(sh, st, b, "end:replace", end, "to check")]
    if labels != ["Review", "Close"]:
        raise AssertionError(f"end:replace: the buttons are {labels}")
    for k, case in ((0, "replace:job"), (1, "replace:operator")):
        buttons = _post(sh, st, b, case, _c(st, cards.card, end, k), "already has an invoice")
        if [x["label"] for x in buttons] != ["Keep current", "Use new", "Close"]:
            raise AssertionError(f"{case}: the buttons are {buttons}")
        for x in buttons[:-1]:
            sh.tap(st, f"{case}:{x['label']}", x)


VENDOR_PAYMENTS = 240                     # one vendor's missing invoices, many pages


def _vendor_walk(sh, st, b, tag, end, *, scheduled):
    import cards
    first = _c(st, cards.card, end, 0)
    pages = json.loads(st.conn.execute("SELECT scope_json FROM renders WHERE render_id=?",
                                       (first,)).fetchone()[0])["pages"]
    if len(pages) < 3:
        raise AssertionError(f"{tag}: {len(pages)} pages, not a walk of many")
    for case, page in ((f"{tag}page1", 1), (f"{tag}page2", 2), (f"{tag}last", len(pages))):
        rid = first if page == 1 else _c(st, cards.card, end, 0, page=page)
        labels = [x["label"] for x in _post(sh, st, b, case, rid, f"page {page} of")]
        never = any(x.startswith("Never for") for x in labels)
        if never != (page == len(pages) and not scheduled):
            raise AssertionError(f"{case}: the buttons are {labels}")


def gen_vendor_pages(sh, st, b):
    """One hostile vendor with 240 missing invoices (some left missing): pages 1, 2 and
    the last, each with every button tapped ([Never for X] on the last)."""
    import cards, work
    loop_store(st)
    pids = _missing(st, [hostile(0)], per=VENDOR_PAYMENTS)
    st.granted(lambda c, grant: work.leave_missing_in_tx(c, pids[1:25], grant=grant))
    end = _c(st, cards.compose_end, st.job_id, scheduled=False)
    _vendor_walk(sh, st, b, "vendor:", end, scheduled=False)


def gen_vendor_pages_scheduled(sh, st, b):
    """A scheduled walk of the same vendor's new payments: no [Never for X]."""
    import cards, views
    loop_store(st)
    _missing(st, [hostile(0)])
    views.mark_rendering_delivered(st.conn, _c(st, cards.compose_end, st.job_id,
                                               scheduled=False))
    _missing(st, [hostile(0)], per=VENDOR_PAYMENTS // 2)
    end = _c(st, cards.compose_end, st.job_id, scheduled=True)
    _vendor_walk(sh, st, b, "vendor:scheduled:", end, scheduled=True)


def gen_get_package(sh, st, b):
    """#1303: [Get package]'s stored call — first before any bank check (the no-post shape
    with its `receipt`, nothing deposited), then the file itself, built now."""
    import posting
    loop_store(st)
    out = sh.refusal(b, "get_package:no-check", "get_package", {"quarter": QUARTER})
    if out.get("receipt") != posting.NO_CHECK:
        raise AssertionError(f"get_package:no-check: {out}")
    st.import_again()
    _missing(st, [hostile(0), hostile(1)])
    body = sh.call(st, b, "get_package:file", "get_package", {"quarter": QUARTER})
    if body.get("kind") != "zip" or "\n" in (body.get("caption") or "\n"):
        raise AssertionError(f"get_package:file: posted {body}")


def gen_get_document(sh, st, b):
    """#56: [See PDF]'s stored call (keep_card) — the filed PDF itself, as one document."""
    import hashlib, db, documents
    loop_store(st)
    data = b"%PDF-1.4\n%%EOF\n"
    doc_id = st.doc(counterparty=hostile(0), issuer=hostile(0), amount_minor=4200)
    with db.tx(st.conn):
        st.conn.execute("UPDATE documents SET sha256=?, ext='pdf' WHERE doc_id=?",
                        (hashlib.sha256(data).hexdigest(), doc_id))
    path = documents.path_of(st.conn, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    # #68: a job pass is open (loop_store's run) — the job's call posts nothing, a refusal
    # Casa reads as words; the operator's [See PDF] tap carries its key and sends the file
    import keys, posting
    out = sh.refusal(b, "get_document:job-running", "get_document", {"doc_id": doc_id})
    if out.get("receipt") != posting.JOB_RUNNING:
        raise AssertionError(f"get_document:job-running: {out}")
    key = keys.mint()
    with db.tx(st.conn):
        keys.store_render(st.conn, "r-see", "see", None, key, doc_id=doc_id)
    body = sh.call(st, b, "get_document:file", "get_document", {"doc_id": doc_id, "key": key},
                   display="a file has no text")
    if body.get("kind") != "document" or not body.get("filename", "").endswith(".pdf"):
        raise AssertionError(f"get_document:file: posted {body}")


def _renamable(st):
    """#89: two payees whose matched invoices print one hostile issuer (the second keeps its
    name: the first took it) and one with no invoice."""
    import db
    pids = build(st, {"clean": [hostile(0), hostile(1)], "missing": [hostile(2)]})
    with db.tx(st.conn):
        st.conn.execute("UPDATE documents SET issuer=?", (hostile(5, "_")[:300],))
    return pids


def gen_rename_vendor(sh, st, b):
    """#89: one rename — the plain line before the vendor's card — by a given hostile name
    and by the name on its invoice."""
    import views
    _renamable(st)
    for case, args in (("rename_vendor:given", {"vendor": hostile(2),
                                                "new_name": hostile(6, "`")[:300]}),
                       ("rename_vendor:invoice", {"vendor": hostile(0)})):
        n0 = len(b.deposits)
        sh.call(st, b, case, "rename_vendor", args)
        prop = json.loads(b.deposits[n0]["value"])
        if len(prop.get("pages") or []) != 1 or "is now called" not in views.unesc(
                prop["pages"][0]):
            raise AssertionError(f"{case}: posted {prop.get('pages')}")


def gen_rename_all(sh, st, b):
    """#89: the invoice names for all vendors — one summary message over hostile names."""
    _renamable(st)
    body = sh.call(st, b, "rename_all:summary", "rename_vendors_to_invoice_names", {})
    if not body["value"].startswith("Renamed 1 vendor to the name on their invoice. 1 keeps"):
        raise AssertionError(f"rename_all:summary: posted {body['value'][:200]}")


SHAPES = [gen_show_view_full_stars, gen_show_view_full_hostile, gen_show_view_single,
          gen_show_view_setup_stop, gen_legacy_rendering, gen_propose_reading,
          gen_propose_account, gen_post_results, gen_post_package,
          gen_end_message_operator, gen_end_message_scheduled, gen_end_message_nothing_to_ask,
          gen_end_message_handover, gen_end_message_handover_close,
          gen_end_message_with_completion, gen_open_items,
          gen_all_answered, gen_ready_notice, gen_review_cards, gen_vendor_pages,
          gen_vendor_pages_scheduled, gen_get_package, gen_get_document, gen_replace_cards,
          gen_rename_vendor, gen_rename_all]


def generate(stores=None) -> Shapes:
    """Every shape over a fresh store; with `stores` (a directory), each store is copied
    there as <shape>.sqlite once its shape ran — the quote check binds against it."""
    sh = Shapes()
    for fn in SHAPES:
        st = _Store()
        st.setUp()
        sh.shape = fn.__name__
        try:
            with st.patch_clock(CLOCK), FakeBroker() as b:
                fn(sh, st, b)
            if stores is not None:
                stores.mkdir(parents=True, exist_ok=True)
                dest = stores / f"{fn.__name__}.sqlite"
                dest.unlink(missing_ok=True)
                copy = sqlite3.connect(dest)
                st.conn.backup(copy)
                copy.close()
        finally:
            st.doCleanups()
    return sh


def header(records) -> dict:
    """What the checker must find judged: every deposit case, the deposits per kind, and
    how many display checks — so an empty, truncated or thinned file fails."""
    deposits = [r for r in records if r["case"] != "stored_call"]
    kinds: dict = {}
    for r in deposits:
        kinds[kind_of(r)] = kinds.get(kind_of(r), 0) + 1
    pages = sum(len(r.get("page_binds") or []) for r in deposits)
    return {"case": "header", "kinds": kinds, "cases": [r["case"] for r in deposits],
            "display_checked": sum(1 for r in deposits if "display_expect" in r) + pages,
            # a quote is built for a bound post whose display is promised, and every file;
            # #66: and for each plain page before a card
            "binds": sum(1 for r in deposits if "bind" in r
                         and ("display_expect" in r or KINDS[r["tool"]] == "operator_file"))
            + pages}


def main(argv) -> int:
    out = pathlib.Path(argv[1]) if len(argv) > 1 else ROOT / "tests" / "casa_shapes.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    sh = generate(out.parent / (out.name + ".stores"))
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                           for r in [header(sh.records)] + sh.records))
    deposits = sum(1 for r in sh.records if r["case"] != "stored_call"
                   and kind_of(r) not in ("next_card", "no_post"))
    nexts = sum(1 for r in sh.records if "next" in r)
    print(f"{len(sh.records)} records ({deposits} deposits, {nexts} next cards, "
          f"{sh.taps} keyed taps) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
