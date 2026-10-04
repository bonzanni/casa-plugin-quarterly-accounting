#!/usr/bin/env python3
"""S7 §7.6/§12 (Task 14): write every deposit body this plugin composes, over hostile store
text in every dynamic source, as JSONL for scripts/check_casa_shapes.py — which judges each
with Casa's REAL validators and renderer under Casa's interpreter. Stdlib only.

    python3 tests/gen_casa_shapes.py [OUT.jsonl]      (default: tests/casa_shapes.jsonl)

One line per deposit: {"case", "tool", "body", "display_expect"}. `body` is the deposit
request exactly as the plugin sent it (FakeBroker records it). `display_expect` is the
composition with every field unescaped — what Casa's renderer must display — or null where
no display is promised (a legacy rendering, stored before S7: spec §7.6 leaves it
un-re-escaped). Every button of every proposal adds a {"case": "stored_call", "tool",
"arguments"} line, and every WRITING button (one whose arguments carry a key) is tapped here
through qa_server.TOOLS on the generator's own store: its receipt must not be keys.NO_LONGER
(§7.6's successful keyed tap per shape, asserted in stdlib; the store is restored after).

Each shape is one generator function over a fresh store (`SHAPES`). Part 14a covers brief
items 1–2. TODO (part 14b, one function each, appended to SHAPES once the tool exists):
  - gen_propose_reading  — item 3: a reading of eight writes over hostile payees (Task 6)
  - gen_propose_account  — item 4: pages 1 and 2 over 7 hostile accounts, Task 7's LABELS,
                           ids with `<` (Task 7)
  - gen_post_results     — item 5: three BODY_LIMIT renderings of hostile lines, two legacy
                           4,096-unit renderings, one package note, the job-left line (Task 10)
  - gen_post_package     — item 6: first send, resend, send-last, a hostile quarter caption
                           line and a 2,000-character count line; operator_file with filename
                           (Task 11). Then the brief's Step 2 filename mutant.
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


# each posting tool's delivered slot (§3); 14b adds results/reading/accounts/package
SLOTS = {"show_view": "view"}
PROPOSALS = {"show_view"}             # operator_proposal tools; 14b adds the two propose_*


class Shapes:
    def __init__(self):
        self.records: list = []
        self.taps = 0

    # -- recording -----------------------------------------------------------------
    def call(self, st, broker, case, tool, args, display=True):
        """Run posting tool `tool` through qa_server.TOOLS and record its one deposit. A
        proposal's buttons are recorded too, every writing button is tapped (the store is
        restored after each), and the buttons are returned; otherwise the body is.
        `display`: the display check applies (the text is composed through views.esc)."""
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
        if tool not in PROPOSALS:
            # an operator_message's text is its value; a file's caption is plain text
            self.records.append({"case": case, "tool": tool, "body": body,
                                 "display_expect": views.unesc(body["value"])
                                 if display and SLOTS[tool] == "results" else None})
            return body
        prop = json.loads(body["value"])
        self.records.append({"case": case, "tool": tool, "body": body,
                             "display_expect": views.unesc(prop["text"]) if display else None})
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
    # (case, stored text, display promised): a rendering stored before S7 — raw control
    # characters, never escaped for the dialect (§7.6: deposit_safe is its only step)
    ("legacy:ctrl", "Accounting · Q3 2026\nACME\x01Corp owes 12.00\x7f and\x1bmore", True),
    ("legacy:hostile", "Accounting · Q3 2026\n" + "\n".join(
        v.replace("\n", "\x01")[:300] for v in HOSTILE) + "\n" + "\x01".join(["*"] * 1200),
     False),
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
        if display and posted["display_expect"] != views.deposit_safe(text):
            raise AssertionError(f"{case}: a legacy text is displayed as itself, cleaned")


SHAPES = [gen_show_view_full_stars, gen_show_view_full_hostile, gen_show_view_single,
          gen_show_view_setup_stop, gen_legacy_rendering]
# 14b appends: gen_propose_reading, gen_propose_account, gen_post_results, gen_post_package


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


def main(argv) -> int:
    out = pathlib.Path(argv[1]) if len(argv) > 1 else ROOT / "tests" / "casa_shapes.jsonl"
    sh = generate()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in sh.records))
    deposits = sum(1 for r in sh.records if r["case"] != "stored_call")
    print(f"{len(sh.records)} records ({deposits} deposits, {sh.taps} keyed taps) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
