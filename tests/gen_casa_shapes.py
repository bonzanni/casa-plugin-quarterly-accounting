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

from tests._base import StoreCase                      # noqa: E402  (puts server/ on the path)
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


class _Store(StoreCase):
    """StoreCase's fixtures, outside a test run: a fresh data dir and store per shape."""
    def runTest(self):                                 # pragma: no cover — never run
        pass


# each posting tool's delivered slot and its kind (§3)
SLOTS = {"show_view": "view", "propose_reading": "reading", "propose_account": "accounts",
         "post_results": "results", "post_package": "package"}
KINDS = {"show_view": "operator_proposal", "propose_reading": "operator_proposal",
         "propose_account": "operator_proposal", "post_results": "operator_message",
         "post_package": "operator_file"}
PROPOSALS = {t for t, k in KINDS.items() if k == "operator_proposal"}


class Shapes:
    def __init__(self):
        self.records: list = []
        self.taps = 0

    # -- recording -----------------------------------------------------------------
    def call(self, st, broker, case, tool, args, display=True):
        """Run posting tool `tool` through qa_server.TOOLS and record its one deposit. A
        proposal's buttons are recorded too, every writing button is tapped (the store is
        restored after each), and the buttons are returned; otherwise the body is.
        `display`: True when the display check applies (the text is composed through
        views.esc), else the reason it does not (recorded as `display_skip`)."""
        import qa_server
        import tools                                    # noqa: F401 — registers the tools
        import views
        n0 = len(broker.deposits)
        out = qa_server.TOOLS[tool]["fn"](dict(args))
        if not (isinstance(out, dict) and isinstance(out.get(SLOTS[tool]), str)):
            raise AssertionError(f"{case}: {tool}({args}) posted nothing: {out}")
        new = broker.deposits[n0:]
        if len(new) != 1:
            raise AssertionError(f"{case}: expected one deposit, saw {len(new)}")
        body = {k: v for k, v in new[0].items() if k != "client"}
        if KINDS[tool] == "operator_file" and display is True:
            display = "a file caption is sent as plain text"
        if any(r["case"] == case for r in self.records):
            raise AssertionError(f"{case}: a case name is used twice")
        rec = {"case": case, "tool": tool, "body": body}
        if tool not in PROPOSALS:
            if display is True:          # an operator_message's text is its value
                rec["display_expect"] = views.unesc(body["value"])
            else:
                rec["display_skip"] = display
            self.records.append(rec)
            return body
        prop = json.loads(body["value"])
        if display is True:
            rec["display_expect"] = views.unesc(prop["text"])
        else:
            rec["display_skip"] = display
        self.records.append(rec)
        for b in prop["buttons"]:
            self.records.append({"case": "stored_call", "tool": b["call"]["tool"],
                                 "arguments": b["call"]["arguments"]})
        for b in prop["buttons"]:
            if "key" in b["call"]["arguments"]:
                self.tap(st, case, b)
        return prop["buttons"]

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
                st.handed(pid)
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
    """Every list view of the store, `all` pages 1 and 2 (page 2 by p1's More button's
    stored call), and the item walk from the check sheet's One by one button."""
    for view in VIEWS_SIMPLE:
        sh.call(st, b, f"{tag}:{view}", "show_view", {"view": view, "quarter": QUARTER})
    p1 = sh.call(st, b, f"{tag}:all:1", "show_view", {"view": "all", "quarter": QUARTER})
    more = [x for x in p1 if x["label"] == "More"]
    if tag.startswith("full"):
        if not more:
            raise AssertionError(f"{tag}: a full store's all page 1 offers no More")
        sh.call(st, b, f"{tag}:all:2", "show_view", more[0]["call"]["arguments"])


def _item_states(sh, st, b, tag, pids):
    """`item` in each item_state, reached as the operator reaches it: proposed (One by one
    from the check sheet, so it offers Next on a full sheet), paired (a clean pairing),
    none (a missing payment), exempt (after the none page's No invoice needed tap)."""
    def labels(buttons):
        return [x["label"] for x in buttons]
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
    if any("key" in x["call"]["arguments"] for x in exempt):
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
        posted = next(r for r in reversed(sh.records) if r["case"] == case)
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
    """Item 5 (the package note is posted in gen_post_package, where its send makes it):
    three BODY_LIMIT renderings of hostile lines in one post (package notices whose reasons
    are hostile, composed by alerts.pending_in_tx as the job's cursor composes them, each
    leaving out the ones before); two legacy 4,096-unit renderings in one post, plain and
    hostile; the job-left line."""
    import alerts, db, job, views
    with db.tx(st.conn):
        for i in range(ALERTS):
            alerts.raise_package(st.conn, "package-stopped", f"gate:{i}", quarter=QUARTER,
                                 reason=hostile(i, "_"), pass_id="")
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
    with db.tx(st.conn):
        left = job._left_render(st.conn, "aaaaaaaa-1")
    body = sh.call(st, b, "results:job-left", "post_results", {"render_ids": [left]})
    if body["value"] != job.LEFT_WAITING:
        raise AssertionError("results: the job-left line is not posted as itself")


JOB = "aaaaaaaa-1"
# the stored caption's lines, as the package build composed them, made hostile: a first
# line (the file's own caption: escaped plain, clipped) carrying every marker, a link, a
# control character and 2,000+ characters; then a 2,000-character count line, which with
# the build's own further lines becomes the package note (§6.1)
CAPTION_FIRST = ("Accounting Q3 2026 · *Acme* _x_ `y` a<b>c www.evil.example ACME\x01Corp "
                 + "Z" * 2000)
COUNT_LINE = _units("12 still missing, 3 not yet classified — listed in notes.md. ", 2000)


def gen_post_package(sh, st, b):
    """Item 6: the package posted as a first send (the job's deliver unit, request-bound),
    a resend ("send it again" after an uncertain first send) and a send-last ("send me the
    last package you built"), with a hostile caption line; the resend's delivery makes
    the package note, posted (item 5)."""
    import db, delivery, views
    import asks
    st.bind(label=ACCOUNT_LABEL)                    # the zip's name: the label's slug
    asks.request_package(st.conn, QUARTER)
    did, tok = st.drive_to_staged(JOB)
    pk = st.conn.execute("SELECT p.package_id, p.caption, p.filename FROM packages p JOIN"
                         " deliveries d ON d.package_id=p.package_id WHERE"
                         " d.delivery_id=?", (did,)).fetchone()
    rest = pk["caption"].split("\n")[1:]
    with db.tx(st.conn):
        st.conn.execute("UPDATE packages SET caption=? WHERE package_id=?",
                        ("\n".join([CAPTION_FIRST, COUNT_LINE] + rest), pk["package_id"]))

    def post(case, args):
        body = sh.call(st, b, case, "post_package", args)
        if body.get("filename") != pk["filename"] or body.get("kind") != "zip":
            raise AssertionError(f"{case}: posted as {body.get('filename')!r}, not the "
                                 f"package's name {pk['filename']!r}")
        if "\n" in body["caption"] or len(body["caption"]) > 900:
            raise AssertionError(f"{case}: the caption is not one clipped line")
        return body

    post("package:first", {"delivery_id": did, "package_token": tok})
    out = delivery.record_delivery(st.conn, delivery_id=did, outcome="uncertain",
                                   package_token=tok)
    views.mark_rendering_delivered(st.conn, out["speak"]["render_id"])   # the offer is seen
    s = delivery.stage_for_delivery(st.conn, resend=True,
                                    package_id=delivery.resend_target(st.conn))
    post("package:resend", {"delivery_id": s["delivery_id"]})
    out = delivery.record_delivery(st.conn, delivery_id=s["delivery_id"], outcome="delivered")
    note = out.get("note_render_id")
    if note is None:
        raise AssertionError("package: the delivered send made no package note")
    body = sh.call(st, b, "results:package-note", "post_results", {"render_ids": [note]})
    if COUNT_LINE not in views.unesc(body["value"]):
        raise AssertionError("package: the note does not carry the count line")
    s = delivery.stage_for_delivery(st.conn, last_built=True)
    post("package:last", {"delivery_id": s["delivery_id"]})


SHAPES = [gen_show_view_full_stars, gen_show_view_full_hostile, gen_show_view_single,
          gen_show_view_setup_stop, gen_legacy_rendering, gen_propose_reading,
          gen_propose_account, gen_post_results, gen_post_package]


def generate() -> Shapes:
    sh = Shapes()
    for fn in SHAPES:
        st = _Store()
        st.setUp()
        try:
            with st.patch_clock(CLOCK), FakeBroker() as b:
                fn(sh, st, b)
        finally:
            st.doCleanups()
    return sh


def header(records) -> dict:
    """What the checker must find judged: every deposit case, the deposits per kind, and
    how many display checks — so an empty, truncated or thinned file fails."""
    deposits = [r for r in records if r["case"] != "stored_call"]
    kinds: dict = {}
    for r in deposits:
        kinds[KINDS[r["tool"]]] = kinds.get(KINDS[r["tool"]], 0) + 1
    return {"case": "header", "kinds": kinds, "cases": [r["case"] for r in deposits],
            "display_checked": sum(1 for r in deposits if "display_expect" in r)}


def main(argv) -> int:
    out = pathlib.Path(argv[1]) if len(argv) > 1 else ROOT / "tests" / "casa_shapes.jsonl"
    sh = generate()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n"
                           for r in [header(sh.records)] + sh.records))
    deposits = sum(1 for r in sh.records if r["case"] != "stored_call")
    print(f"{len(sh.records)} records ({deposits} deposits, {sh.taps} keyed taps) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
