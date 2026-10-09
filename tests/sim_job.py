# tests/sim_job.py
"""The job's worker side, done mechanically (simple loop, design rev 17 §2): claim, then
job_next, carrying out each unit it hands out the way the job skill describes it —
against a REAL bank-feed (tests/bankfeed.py) and a Gmail fake. The cursor decides
everything; the driver never chooses a unit or a token of its own. Every tool call the
driver makes in a turn is counted (`calls`; a new turn — a claim — starts at 1) against
Casa's cut.

  probes    bank-feed's tools, list_accounts, sync, list_backups, the four record_probes
            (bank_sync with the unit's acq), check_setup
  snapshot  export_history -> import_ledger_export(acq)
  erasures  each handed row: get_transaction; "no transaction #N" -> record_not_found, else
            set_aside
  filing    with `search`: one search of the operator's own mail, at once
            record_probe(gmail, ok, data.refs) — failed when Gmail.down; then each of the
            unit's (or the probe's) `files` (download, Read, ingest_document with the reading:
            amount, currency, date, issuer, number; no vendor)
  payment   one payment (rev 18.4): `files` first (each filed with ingest_document(vendor=…));
            the candidates judged from their stored reading — the exact fit, else the
            nearest-dated same-currency, same-amount unheld one → match; nothing fits: its
            searches left (hinted with a learned hint, plain, wider), each recorded at once
            with its refs, then each of its answer's `files` filed, until something fits;
            then ONE decide: match, else an unheld candidate → propose, else missing; a
            payment holding a document with another same-amount candidate → propose the
            held one with it (refused: match the held one); upsert_counterparty(hint_sender=…)
            when a search found an invoice
  mirror    each call through bank-feed (a reply starting `refused`, or bank-feed's
            "Nothing was changed.", counts as failed), then one record_mirror
  view      show_view(render_id) under a broker; on the receipt (`deliver`),
            mark_rendering_delivered
  post      post_results(render_ids); on the receipt, mark_rendering_delivered

No call budget (operator ruling 2026-10-07): the driver works until `complete`.
`casa_cut` (an int): Casa's batch bound — a call past it ends the batch without job_next
(the driver re-claims; what the unit still owed comes again), three batches in a row
without a reported progress (`report` with `progressed`) fail the run, and so does a run
past CASA_BATCHES, as Casa ends it.

Plugin tools are called through qa_server.TOOLS (#43: the call shape the model makes)."""
from __future__ import annotations

import collections.abc
import contextlib
import csv
import io
import json
import os
import re

import binding
import job
import ledger
import passes
import version

from tests import bankfeed


def ledger_state(listing: str) -> dict:
    """The ledger probe's data from ONE list_backups answer (spec: generation,
    registrations and instance are captured together, under bank-feed's locks),
    each value read by its label — bank-feed may prepend sentences."""
    gen = re.search(r"^Restore generation: (\d+)$", listing, re.M)
    inst = re.search(r"^Ledger instance: ([0-9a-f]{32})$", listing, re.M)
    registered = {}
    if "Registered workflows:" in listing:
        block = listing.split("Registered workflows:", 1)[1].split("Restores:", 1)[0]
        for line in block.splitlines():
            m = re.match(r"\s+(\S+) -> (\S+)", line)
            if m:
                registered[m.group(1)] = m.group(2)
    return {"generation": int(gen.group(1)) if gen else None, "registered": registered,
            "instance": inst.group(1) if inst else None}

MAX_UNITS = 600         # a cursor that never finishes is a failure, never a hang
CASA_IDLE_BATCHES = 3   # Casa ends a run after this many batches without reported progress
CASA_BATCHES = 20       # the manifest's "batches": Casa ends the run after this many


class CasaCut(Exception):
    """Casa ended the batch at its call bound, before the model's next job_next."""


class JobLedger(bankfeed.Ledger):
    """bank-feed, with the one ledger condition the cursor tests need on a fresh store."""

    def restore_since_install(self) -> None:
        """The ledger still carries this plugin's workflow writes after its install
        restore point while the accounting store holds nothing (a store reset without
        restoring bank-feed's install backup): bank_write_gate refuses at every pass's
        ledger probe ("restore backup … first"), so every pass stops at its probes. The
        write is a real tag_transaction under version.WORKFLOW, which mints and registers
        the install backup."""
        rid = self.rows(state="active")[0]["row_id"]
        if version.WORKFLOW not in self.registered():
            self.call("tag_transaction", row_ids=[rid], tags=["acct::open"],
                      workflow=version.WORKFLOW, expected_generation=self.generation())
        assert version.WORKFLOW in self.registered(), self.listing()


class Gmail:
    """A mailbox. `invoice(...)` puts one invoice mail in it (its PDF published through
    casa_handoff, as gmail's download_attachment does). A hinted search
    (`from:<sender> …`) finds the messages whose sender is exactly that address; a plain
    search (`<vendor> invoice after:… before:…`) finds the vendor's messages; both only
    within the query's `after:`/`before:` dates. Any other query is a reference search: it
    finds the messages that mention one of its words (`mentions`), dated or not. With a
    `cap` (Gmail's page, 20), a search answers at most that many, newest first. A message's
    attachment is in the search answer unless `listed`: then only list_attachments shows
    it (inline images first, a snippet that names no invoice). The operator's own-mail
    search (`from:me to:me …`) finds the operator's own mail (`own`), newest first. `down`:
    every search fails (the probe records ok=false)."""


    def __init__(self, test):
        self.test = test
        self.searches = []
        self.messages = []
        self.own_mail = []
        self.down = False
        self.cap = None

    def own(self, amount_minor, currency="EUR", day="2026-07-05", number=None,
            issuer="Zapier", ref=None) -> str:
        """One self-addressed mail with an invoice attachment (the operator forwarding a
        document to themselves). Its ref, <message id>:<attachment id> (`ref`: as given —
        Gmail's attachment ids run to hundreds of characters)."""
        k = len(self.own_mail) + 1
        number = number or f"OWN-{k:03d}"
        path = self.test.publish(f"own-{number}.pdf", b"%PDF-1.4 own " + number.encode() + b"\n")
        ref = ref or f"own-msg-{k:03d}:att-1"
        self.own_mail.append({"ref": ref, "path": path, "amount_minor": amount_minor,
                              "currency": currency, "date": day, "number": number,
                              "issuer": issuer, "seq": k})
        return ref

    def invoice(self, vendor, amount_minor, currency, day, number, sender=None,
                kind="invoice", sent=False, message=None, mentions=(), listed=False) -> str:
        """One message from `sender` (default billing@<vendor>.com) carrying a PDF of
        `kind`. `sent`: a sales invoice in the operator's Sent folder. `message`: the id of
        an earlier message this PDF is one more attachment of. `mentions`: the references
        its text carries (an order number). `listed`: its attachment shows only through
        list_attachments. Its ref, <message id>:<attachment id>."""
        sender = sender or "billing@%s.com" % re.sub(r"[^a-z0-9]", "", vendor.lower())
        path = self.test.publish(f"{number}.pdf", b"%PDF-1.4 " + number.encode() + b"\n")
        mid = message or f"msg-{len(self.messages) + 1:04d}"
        att = 1 + sum(1 for m in self.messages if m["id"] == mid)
        self.messages.append({"vendor": vendor, "amount_minor": amount_minor,
                              "currency": currency, "date": day, "number": number,
                              "path": path, "sender": sender, "kind": kind, "sent": sent,
                              "id": mid, "ref": f"{mid}:att-{att}",
                              "mentions": [x.lower() for x in mentions], "listed": listed})
        return f"{mid}:att-{att}"

    def notices(self, vendor, days, sender=None) -> None:
        """One attachment-less message from the vendor on each of `days` (shipping
        notices): they fill a search's page and carry no invoice."""
        sender = sender or "billing@%s.com" % re.sub(r"[^a-z0-9]", "", vendor.lower())
        for day in days:
            self.messages.append({"vendor": vendor, "date": day, "sender": sender,
                                  "id": f"msg-{len(self.messages) + 1:04d}", "ref": None,
                                  "kind": "notice", "sent": False, "mentions": [],
                                  "listed": False})

    @staticmethod
    def visible(found) -> list:
        """The attachment refs a search answer shows."""
        return [m["ref"] for m in found if m["ref"] is not None and not m["listed"]]

    def list_attachments(self, message_id) -> list:
        """Every attachment ref of the message."""
        return [m["ref"] for m in self.messages if m["id"] == message_id and m["ref"]]

    def search_emails(self, query):
        """The messages the query finds; None when Gmail is down."""
        self.searches.append(query)
        if self.down:
            return None
        q = query.lower()
        if q.startswith("from:me to:me"):
            return sorted(self.own_mail, key=lambda m: -m["seq"])      # newest first
        after = re.search(r"after:(\d{4}-\d{2}-\d{2})", q)
        before = re.search(r"before:(\d{4}-\d{2}-\d{2})", q)
        hint = re.match(r"from:(\S+)", q)
        words = [w for w in q.split() if not w.startswith(("after:", "before:"))]
        out = []
        for m in self.messages:
            if hint is not None:
                if m["sender"].lower() != hint.group(1):
                    continue
            elif q.startswith(m["vendor"].lower() + " "):
                pass
            elif not any(w in m["mentions"] for w in words):
                continue
            if (after and m["date"] < after.group(1)) or (before and m["date"] >= before.group(1)):
                continue
            out.append(m)
        if self.cap is not None and len(out) > self.cap:
            out = sorted(out, key=lambda m: m["date"], reverse=True)[:self.cap]
        return out


class _Pids(collections.abc.Mapping):
    """{fixture row number: pid}, resolved when read (the pids exist once a run imported
    the rows; a row booked later resolves to its lineage then)."""

    def __init__(self, drv, numbers):
        self.drv, self.numbers = drv, numbers

    def __getitem__(self, no):
        if no not in self.numbers:
            raise KeyError(no)
        return self.drv.pid_of(no)

    def __iter__(self):
        return iter(self.numbers)

    def __len__(self):
        return len(self.numbers)


class JobDriver:
    DATES = ("2026-07-05", "2026-08-05", "2026-09-05")

    def __init__(self, test, payments=2, bf=None):
        """`bf`: a bank-feed the test made and fills itself (no payments are added); else
        a JobLedger of the driver's own with `payments` payments."""
        self.test, self.conn = test, test.conn
        if bf is None:
            n = getattr(test, "_job_ledgers", 0) + 1
            test._job_ledgers = n
            bf = JobLedger(test.tmp / f"bankfeed-job{n}")
            test.addCleanup(bf.close)
            bf.account()
        else:
            payments = 0
        self.bankfeed = self.bf = bf
        self.gmail = Gmail(test)
        self._sync_fail = None
        self._no_tools = False
        self.last = None
        self.token = None
        self.calls = 0
        self.units = []
        self.imports = []               # every import_ledger_export answer, in order
        self.refusals = None            # a list: a unit's refusal is kept there, not raised
        self.deliver = True             # Casa's receipt arrives for every post
        self.search_log = []            # (vendor, kind, query) of every vendor search
        self.ingested = []              # the source_ref of every ingest_document call
        # how the payment unit searches: "reference" (the skill: a hint, the remittance's
        # reference with no dates, vendor-and-dates, wider; a message naming the payment
        # has its attachments listed) or "dated" (Q2 R7's model: vendor searches only)
        self.search_mode = "reference"
        self.posted = {}                # render_id -> the deposit a view unit posted
        self.fx_rates = {}              # provider_ref -> (exchange_rate, unit): see _export
        self._spec = {}                 # fixture row number -> its bank row (quarter fixtures)
        self._broker_now = None
        self.batch_calls = []           # the calls at each batch's end, the last run_job
        self.casa_cut = None            # Casa's batch call bound, when the test enforces it
        self.cuts = 0                   # batches Casa cut (casa_cut)
        self.batch_reported = []        # per batch: a progress report was handed (progressed)
        self.tool_calls = {}            # plugin tool name -> calls made, every run
        self.replace_days = 3           # a handed document this near an exact fit replaces
        self.skip_attached_reports = False   # Q2 run 1: the model ignores report:true beside
        #                                      a work unit (only `report`, complete count)
        self._at_next = 0               # calls at the previous job_next
        # a stray call count the model sends (ignored since the no-budget ruling): "delta"
        # (run 1), "total", None
        self.calls_mode = None
        self.cut_reports = 0            # g1: report calls Casa cuts (a model that stops there)
        self.broker = None              # a FakeBroker the whole run deposits to (else one a unit)
        self.read_mode = "store"        # how a found attachment is read (see _file_one)
        self.reads = []                 # every Read path
        self.bank_log = []              # every bank-feed call: (tool, canonical args)
        self.near_days = 10             # the skill's "certain": a match dated this near
        self.propose_days = 20          # a look-alike this near is proposed; farther: not it
        self.printed = {}               # #67: doc_id -> what the reading unit reads on it
        self.add_payments([self.DATES[i % len(self.DATES)] for i in range(payments)])

    # --- the bank, as the operator's bank has it ------------------------------------
    def add_payments(self, dates, counterparty="Zapier") -> list:
        """Rows fetched into bank-feed (all of them again: a fetch reconciles the whole
        interval), each classified `software` (an invoice expected), as tx-classifier
        would before the run. Returns the new rows' ids."""
        before = {r["row_id"] for r in self.bf.rows()}
        have = [r for r in self.bf.rows(state="active")]
        rows = [self.bf.row(r["booking_date"], amount=r["amount_minor"], ref=r["provider_ref"],
                            counterparty=r["counterparty"]) for r in have]
        k = len(have)
        for i, d in enumerate(dates):
            rows.append(self.bf.row(d, amount=1000 * (k + i + 1), ref=f"P{k + i + 1}",
                                    counterparty=counterparty))
        if dates:
            self.bf.fetch(rows)
        new = [r["row_id"] for r in self.bf.rows(state="active") if r["row_id"] not in before]
        if new:
            self.bf.call("tag_transaction", row_ids=new, tags=["software"])
        return new

    # --- a quarter, as the operator's bank and mailbox have it (Task 16) ---------------
    def _bank_rows(self) -> list:
        """Every row to fetch: the active rows no fixture row owns, then the fixture's."""
        mine = {r["ref"] for r in self._spec.values()}
        rows = [self.bf.row(r["booking_date"], amount=r["amount_minor"], ref=r["provider_ref"],
                            counterparty=r["counterparty"], status=r["status"],
                            direction=r["direction"], value_date=r["value_date"])
                for r in self.bf.rows(state="active") if r["provider_ref"] not in mine]
        for r in self._spec.values():
            rows.append(self.bf.row(r["date"], amount=r["amount"], ref=r["ref"],
                                    counterparty=r["vendor"], status=r["status"],
                                    direction=r["direction"], value_date=r["value_date"],
                                    remittance=r.get("remittance", "")))
        return rows

    def _fetch_spec(self) -> None:
        """A fetch of the whole interval (bank-feed reconciles it), then each fixture row
        bank-feed newly holds tagged with its tags, as tx-classifier would before the run."""
        before = {r["row_id"] for r in self.bf.rows()}
        self.bf.fetch(self._bank_rows())
        groups: dict = {}
        for no, r in self._spec.items():
            rid = self.row_id_of(no)
            if rid is not None and rid not in before:
                groups.setdefault(tuple(r["tags"]), []).append(rid)
        for tags, ids in sorted(groups.items()):
            self.bf.call("tag_transaction", row_ids=sorted(ids), tags=list(tags))

    def _add_rows(self, rows) -> list:
        """Fixture rows (vendor, amount, day, tags[, status, direction]) appended to the
        spec under the next numbers, fetched and tagged. Their numbers."""
        nos = []
        for r in rows:
            vendor, amount, day, tags = r[:4]
            status = r[4] if len(r) > 4 else "BOOK"
            direction = r[5] if len(r) > 5 else "DBIT"
            no = max(self._spec, default=0) + 1
            self._spec[no] = {"vendor": vendor, "amount": amount, "status": status,
                              "date": day, "value_date": day,
                              "remittance": r[6] if len(r) > 6 else "",
                              "direction": direction, "ref": f"Q{no:03d}",
                              "tags": sorted(tags.split(","))}
            nos.append(no)
        self._fetch_spec()
        return nos

    def row_id_of(self, no):
        """The bank row id fixture row `no` has now (bank-feed's active row of its ref)."""
        ref = self._spec[no]["ref"]
        ids = [r["row_id"] for r in self.bf.rows(state="active") if r["provider_ref"] == ref]
        return ids[-1] if ids else None

    def pid_of(self, no):
        """Fixture row `no`'s payment: the lineage of its current bank row
        (lineage.resolve_pid); None before an import observed it."""
        import lineage
        r = self.conn.execute("SELECT pid FROM aliases WHERE row_id=?",
                              (self.row_id_of(no),)).fetchone()
        return lineage.resolve_pid(self.conn, r[0]) if r is not None else None

    def book(self, row_no, day="2026-09-30") -> None:
        """A later fetch books pending row `row_no` on `day` (bank-feed may give it a new
        row id, superseding the pending one)."""
        self._spec[row_no].update(status="BOOK", date=day)
        self._fetch_spec()

    def pay_once(self, vendor, amount, day, tags="software", remittance="") -> int:
        """One more payment fetched into the bank; its fixture row number (its pid exists
        once a run imported it: pid_of)."""
        return self._add_rows([(vendor, amount, day, tags, "BOOK", "DBIT", remittance)])[0]

    def searches_of(self, vendor) -> list:
        """The (kind, query) of every vendor search the sim made for `vendor`."""
        return [(k, q) for v, k, q in self.search_log if v == vendor]

    def _export(self) -> str:
        """export_history's path. With `fx_rates`, the export gains bank-feed 0.22.0's two
        columns (exchange_rate, exchange_unit_currency; casa-specialist-finance#91) for those
        rows: the vendored tree is 0.21.0, which exports no rate (#35 dormant until 0.22.0)."""
        path = self.bf.export()
        if not self.fx_rates:
            return path
        import casa_handoff
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        cols = list(rows[0]) + ["exchange_rate", "exchange_unit_currency"] if rows else []
        refs = {r["row_id"]: r["provider_ref"] for r in self.bf.rows()}
        buf = io.StringIO(newline="")
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        for r in rows:
            rate, unit = self.fx_rates.get(refs.get(int(r["row_id"])), ("", ""))
            w.writerow({**r, "exchange_rate": rate, "exchange_unit_currency": unit})
        return casa_handoff.publish("bank-feed", "ledger-export-fx.csv",
                                    data=buf.getvalue().encode())["path"]

    def file_document(self, vendor=None, amount_minor=10000, currency="EUR",
                      document_date="2026-07-09", kind="invoice", number=None,
                      author="desk") -> int:
        """A PDF published to the handoff folder and filed (ingest_document, no pass): the
        desk's filing, `vendor` recorded when given. Its doc_id."""
        import documents
        self._docs = getattr(self, "_docs", 0) + 1
        number = number or f"F-{self._docs:03d}-{vendor or 'desk'}"
        path = self.test.publish(f"{number}.pdf", b"%PDF-1.4 filed " + number.encode() + b"\n")
        return documents.ingest_document(
            self.conn, source_path=path, kind=kind, source="gmail", extraction_author=author,
            counterparty=vendor, issuer=vendor, document_date=document_date,
            document_number=number, amount_minor=amount_minor, currency=currency,
            vendor=vendor)["doc_id"]

    def hand_over(self, **reading) -> int:
        """The desk's filing handed to the job: ingest_document(extraction_author="desk"),
        then request_work(kind="handover", doc_ids=[it]). Its doc_id."""
        import asks
        doc = self.file_document(**reading)
        asks.request_work(self.conn, "handover", "operator", doc_ids=[doc])
        return doc

    def file_unread(self, number, vendor, amount_minor, currency="EUR",
                    document_date="2026-07-09", kind="invoice") -> int:
        """#67 (prod 2026-10-08): the desk files a document with only its number — no
        issuer, amount, currency or date (read_at stays NULL). What is printed on it is kept
        in `printed`, for the job's `reading` unit. Its doc_id."""
        import documents
        self._docs = getattr(self, "_docs", 0) + 1          # each file its own bytes
        path = self.test.publish(f"{number}.pdf", f"%PDF-1.4 desk {self._docs} {number}\n"
                                 .encode())
        doc = documents.ingest_document(
            self.conn, source_path=path, kind=kind, source="manual-telegram",
            extraction_author="desk", document_number=number)["doc_id"]
        self.printed[doc] = {"issuer": vendor, "amount_minor": amount_minor,
                             "currency": currency, "document_date": document_date,
                             "document_number": number}
        return doc

    def posted_end(self, job_id) -> dict:
        """Run `job_id`'s one message as its view unit deposited it (show_view under the
        driver's FakeBroker): its text and its buttons' stored calls."""
        rid = self.conn.execute("SELECT end_render_id FROM runs WHERE job_id=?",
                                (job_id,)).fetchone()[0]
        return self.posted[rid]

    @staticmethod
    def tap(deposit, label) -> dict:
        """The operator taps `label`: that button's stored call, through qa_server.TOOLS."""
        import qa_server
        import tools  # noqa: F401
        b = next(b for b in deposit["buttons"] if b["label"] == label)
        return qa_server.TOOLS[b["call"]["tool"]]["fn"](dict(b["call"]["arguments"]))

    def quarter_fixture(self) -> _Pids:
        """Task 16's nine-row quarter (§6): Adobe ×2 with exact invoices, OpenRouter with a
        USD invoice, Twilio with nothing, Zapier with a receipt, a 0.00 card authorisation,
        a pending Figma row, a tax payment, and AWS whose exact invoice is already filed.
        Returns {row number 1-9: pid}."""
        g = self.gmail
        nos = self._add_rows([
            ("Adobe", 10000, "2026-07-03", "software"),
            ("Adobe", 10000, "2026-08-03", "software"),
            ("OpenRouter", 1899, "2026-07-14", "software"),
            ("Twilio", 2000, "2026-07-21", "software"),
            ("Zapier", 999, "2026-08-11", "software"),
            ("Visa auth", 0, "2026-08-15", "software"),
            ("Figma", 1200, "2026-09-28", "software", "PDNG"),
            ("Belastingdienst", 50000, "2026-07-31", "taxes,vat"),
            ("AWS", 4120, "2026-09-02", "software")])
        g.invoice("Adobe", 10000, "EUR", "2026-07-03", "ADB-07")
        g.invoice("Adobe", 10000, "EUR", "2026-08-03", "ADB-08")
        g.invoice("OpenRouter", 2200, "USD", "2026-07-13", "OR-551")
        g.invoice("Zapier", 999, "EUR", "2026-08-11", "ZAP-R-88", kind="receipt")
        self.file_document(vendor="AWS", amount_minor=4120, document_date="2026-09-01",
                           number="AWS-0901")
        return _Pids(self, nos)

    def quarter_fixture_60(self) -> _Pids:
        """A Q3-shaped quarter of 60 payments (§5, §6.1; plan round 6): 12 recurring vendors
        × 3 months (nine with exact invoices, six of those with learned hints; Zapier's
        receipts; Twilio and Mailchimp each one month missing), 4 FX vendors × 2 months, 6
        tax / fee / salary / interest rows, 4 revenue rows with sales invoices in Sent, 2
        one-off vendors whose invoice is already filed, 2 with nothing anywhere, one 0.00
        authorisation and one pending row. Returns {row number: pid}; `expected` holds each
        row's bucket after one run."""
        import kb
        g, rows, want = self.gmail, [], []
        recurring = [("Adobe", 5999), ("Atlassian", 1450), ("Notion", 960), ("Slack", 1275),
                     ("Dropbox", 1199), ("Google Workspace", 2304), ("Microsoft", 1260),
                     ("Hetzner", 3870), ("KPN", 4500), ("Zapier", 2939), ("Twilio", 2111),
                     ("Mailchimp", 1300)]
        for i, (v, amt) in enumerate(recurring):
            for m in (7, 8, 9):
                day = f"2026-{m:02d}-{i + 2:02d}"
                rows.append((v, amt, day, "software"))
                gap = (v == "Twilio" and m == 8) or (v == "Mailchimp" and m == 9)
                if not gap:
                    g.invoice(v, amt, "EUR", day, f"{v[:3].upper()}-{m:02d}",
                              kind="receipt" if v == "Zapier" else "invoice")
                want.append("missing" if gap else "matched")
        for v, _ in recurring[:6]:                        # six vendors carry learned hints
            kb.upsert_counterparty(self.conn, v, hint_sender="billing@%s.com" % re.sub(
                r"[^a-z0-9]", "", v.lower()), hint_subject=f"{v} invoice")
        # FX: the bank's rate (unit EUR, 1.16 USD per EUR) on OpenAI's and GitHub's rows
        fx = [("OpenAI", 1899, "2026-07-16", 2200, "proposed"),
              ("OpenAI", 1912, "2026-08-16", 2215, "proposed"),
              ("Cursor", 1720, "2026-07-18", None, "matched"),
              ("Cursor", 1721, "2026-08-18", None, "matched"),
              ("GitHub", 3450, "2026-07-20", 5200, "missing"),     # the rate rules it out
              ("GitHub", 3455, "2026-08-20", 4005, "proposed"),
              ("Vercel", 1700, "2026-07-22", 2000, "proposed"),    # no rate on the row
              ("Vercel", 1705, "2026-08-22", 2010, "proposed")]
        for v, amt, day, usd, b in fx:
            rows.append((v, amt, day, "software"))
            if usd is None:
                g.invoice(v, amt, "EUR", day, f"{v[:3].upper()}-{day[5:7]}")
            else:
                g.invoice(v, usd, "USD", day, f"{v[:3].upper()}-{day[5:7]}")
            want.append(b)
        rows += [("Belastingdienst", 412000, "2026-07-31", "taxes,vat"),
                 ("ING", 1250, "2026-07-31", "fees"), ("ING", 1250, "2026-08-31", "fees"),
                 ("Salaris J. de Boer", 285000, "2026-07-25", "payroll,salary"),
                 ("Salaris J. de Boer", 285000, "2026-08-25", "payroll,salary"),
                 ("ING Lening", 8450, "2026-09-01", "finance,interest")]
        want += ["not_needed"] * 6
        for v, amt, day in (("Acme BV", 250000, "2026-07-06"), ("Acme BV", 250000, "2026-08-06"),
                            ("Bakker Logistiek", 180000, "2026-07-27"),
                            ("Bakker Logistiek", 180000, "2026-09-27")):
            rows.append((v, amt, day, "income,recurring,revenue", "BOOK", "CRDT"))
            g.invoice(v, amt, "EUR", day, f"S-{v[:3].upper()}-{day[5:7]}",
                      sender="me@voorbeeld.nl", kind="sales-invoice", sent=True)
            want.append("matched")
        for v, amt, day in (("IKEA Business", 34900, "2026-08-09"), ("Conrad", 8995, "2026-09-09")):
            rows.append((v, amt, day, "software"))
            self.file_document(vendor=v, amount_minor=amt, document_date=day,
                               number=f"{v[:3].upper()}-1")
            want.append("matched")
        rows += [("Parkeren Utrecht", 450, "2026-08-12", "software"),
                 ("Restaurant De Gans", 6780, "2026-09-18", "software"),
                 ("Visa auth", 0, "2026-09-03", "software"),
                 ("Figma", 1500, "2026-09-29", "software", "PDNG")]
        want += ["missing", "missing", "not_needed", "pending"]
        nos = self._add_rows(rows)
        for no, r in self._spec.items():
            if r["vendor"] in ("OpenAI", "GitHub"):
                self.fx_rates[r["ref"]] = ("1.16", "EUR")
        self.expected = dict(zip(nos, want))
        return _Pids(self, nos)

    def fail_next_sync(self, detail) -> None:
        """The next probes unit's sync fails with `detail`."""
        self._sync_fail = detail

    def no_bank_tools(self) -> None:
        """bank-feed's tools are not visible: the probes unit records bank_tools=false and
        stops there."""
        self._no_tools = True

    # --- the loop --------------------------------------------------------------------
    def claim(self, job_id, started_by="operator") -> int:
        """A job turn's first call: job_next(job_id, started_by) claims. Returns the token."""
        self.token = job.claim(self.conn, job_id, started_by=f"Started by: {started_by}")
        self.calls = 1
        self._at_next = 1
        return self.token

    def next(self):
        """job_next as the model calls it, through the tool (#43), with `calls_mode`'s stray
        count (Q2 run 1 sent the calls since the previous job_next). The caller counts the
        job_next call itself."""
        import db
        import qa_server
        import tools  # noqa: F401  -- registers every tool
        args = {"pass_token": self.token}
        if self.calls_mode == "delta":
            args["calls_made"] = self.calls - self._at_next   # removed-name: asserted absent
        elif self.calls_mode == "total":
            args["calls_made"] = self.calls       # removed-name: asserted absent
        u = qa_server.TOOLS["job_next"]["fn"](args)
        if isinstance(u, dict) and isinstance(u.get("refused"), str):
            raise db.Refusal(u["refused"])
        self._at_next = self.calls
        return u

    def to_unit(self, job_id, unit, started_by="operator"):
        """Claim, then carry out every unit until `unit` is handed out (not done): its
        hand-out, as the model holds it then."""
        self.claim(job_id, started_by)
        with self._broker():
            for _ in range(MAX_UNITS):
                u = self.next()
                self.calls += 1
                self._at_next = self.calls
                self.last = u
                if u["unit"] == unit:
                    return u
                if u["unit"] == "complete":
                    raise AssertionError(f"{unit} was not handed out before {u['unit']}")
                self.do(u, self.token)
        raise AssertionError(f"{unit} was never handed out")

    def run_job(self, job_id, started_by="operator") -> list:
        """Claim, then job_next until `complete`, re-claiming (a new turn, a new batch) at
        Casa's cut. Returns every unit handed out."""
        self.claim(job_id, started_by)
        self.batch_calls = []
        self.batch_reported = []
        return self._loop(job_id)

    @property
    def calls_total(self) -> int:
        """The tool calls the last run_job made, every batch (turn) summed."""
        return sum(self.batch_calls)

    def _loop(self, job_id) -> list:
        units, idle, reported = [], 0, False

        def batch_end():
            nonlocal idle, reported
            self.batch_calls.append(self.calls)
            self.batch_reported.append(reported)
            idle = 0 if reported else idle + 1
            reported = False
            if self.casa_cut is not None and idle >= CASA_IDLE_BATCHES:
                raise AssertionError(f"Casa ends the run: {idle} batches without reported "
                                     f"progress (calls {self.batch_calls})")
            if self.casa_cut is not None and len(self.batch_calls) >= CASA_BATCHES \
                    and not (units and units[-1]["unit"] == "complete"):
                raise AssertionError(f"Casa ends the run: {CASA_BATCHES} batches (calls "
                                     f"{self.batch_calls})")
        with self._broker():
            for _ in range(MAX_UNITS):
                if self.casa_cut is not None and self.calls + 1 > self.casa_cut:
                    self.cuts += 1                 # cut before the job_next
                    batch_end()
                    self.token = job.claim(self.conn, job_id)
                    self.calls = 1
                    self._at_next = 1
                    continue
                u = self.next()
                self.calls += 1
                self._at_next = self.calls
                units.append(u)
                self.units.append(u)
                self.last = u
                assert u.get("pass_token") == self.token, u
                said = reported
                # g1 (Terra S1): a `report` reaches Casa only when its report_job_progress
                # call is made (_report) — a cut before it delivers nothing
                if u["report"] and u["unit"] != "report" and (
                        not self.skip_attached_reports or u["unit"] == "complete"):
                    reported = bool(u["progress"]["progressed"])   # Casa keeps the LAST
                if u["report"] and u["unit"] == "complete":
                    # the closing report_job_progress is a call of its own; past Casa's cut
                    # it never reaches Casa
                    if self.casa_cut is not None and self.calls + 1 > self.casa_cut:
                        reported = said
                    else:
                        self.calls += 1
                if u["unit"] == "complete":
                    batch_end()
                    return units
                import db
                try:
                    self.do(u, self.token)
                    if u["unit"] == "report":
                        reported = bool(u["progress"]["progressed"])   # delivered
                except CasaCut:
                    self.cuts += 1
                    batch_end()
                    self.token = job.claim(self.conn, job_id)
                    self.calls = 1
                    self._at_next = 1
                except db.Refusal as exc:   # the model reads `refused:` and calls job_next
                    if self.refusals is None:
                        raise
                    self.refusals.append(str(exc))
        raise AssertionError(f"the cursor handed out {MAX_UNITS} units without finishing: "
                             f"{[x['unit'] for x in units[-20:]]}")

    @contextlib.contextmanager
    def _broker(self):
        """The test's own broker when one listens, else a FakeBroker of the driver's."""
        if self.broker is not None:             # one broker for the run (Casa #1312's keys)
            self._broker_now = self.broker
            yield self.broker
            return
        if os.environ.get("CASA_BROKER_SOCKET"):
            yield None
            return
        from tests.fakebroker import FakeBroker
        with FakeBroker() as b:
            self._broker_now = b
            try:
                yield b
            finally:
                self._broker_now = None

    # --- the units ---------------------------------------------------------------------
    def do(self, u, token):
        """Carry out unit `u` under claim `token` (the model then calls job_next)."""
        self.token = token
        with self._broker():
            return getattr(self, "_" + u["unit"].replace("-", "_"))(u, token)

    def _spend(self, k=1) -> None:
        """k tool calls: past Casa's bound the batch is cut (the call is not made)."""
        if self.casa_cut is not None and self.calls + k > self.casa_cut:
            raise CasaCut()
        self.calls += k

    def _tool(self, name, args):
        """#43: the plugin tool `name` called the way the model calls it — through
        qa_server.TOOLS. A posting tool's no-post shape (`refused`) is raised as the refusal
        it carries, as a direct call would raise it."""
        import db
        import qa_server
        import tools  # noqa: F401  -- registers every tool
        self._spend()
        self.tool_calls[name] = self.tool_calls.get(name, 0) + 1
        out = qa_server.TOOLS[name]["fn"](args)
        if isinstance(out, dict) and isinstance(out.get("refused"), str):
            raise db.Refusal(out["refused"])
        if name == "ingest_document":
            self.ingested.append(args.get("source_ref"))
        return out

    def _bank(self, tool, **args) -> str:
        self._spend()
        self.bank_log.append((tool, json.dumps(args, sort_keys=True)))
        return self.bf.call(tool, **args)

    def _probes(self, u, token):
        conn, bf = self.conn, self.bf
        self._spend(1)                                   # bank-feed's tools looked up
        if self._no_tools:
            self._tool("record_probe", {"pass_token": token, "kind": "bank_tools", "ok": False,
                                        "detail": "bank-feed's tools are not visible"})
            return None
        self._tool("record_probe", {"pass_token": token, "kind": "bank_tools", "ok": True})
        self._spend(1)                                   # list_accounts
        accounts = [{"account_id": r["account_id"], "category": r["category"],
                     "label": r["name"]}
                    for r in bf.conn.execute("SELECT account_id, category, name FROM accounts")]
        self._tool("record_probe", {"pass_token": token, "kind": "bank_accounts", "ok": True,
                                    "data": {"accounts": accounts}})
        ok, detail = True, ""
        self._spend(1)                                   # the sync
        if self._sync_fail is not None:
            ok, detail, self._sync_fail = False, self._sync_fail, None
        self._tool("record_probe", {"pass_token": token, "kind": "bank_sync", "ok": ok,
                                    "detail": detail, "acq": u["acq"]})
        self._spend(1)                                   # list_backups
        self._tool("record_probe", {"pass_token": token, "kind": "ledger", "ok": True,
                                    "data": ledger_state(bf.listing())})
        self._spend(1)
        binding.check_setup(conn)
        return None

    def _snapshot(self, u, token):
        conn, bf = self.conn, self.bf
        self._spend(2)                                   # export_history, the import
        imp = ledger.import_ledger_export(conn, path=self._export(), token=token,
                                          ledger_instance=bf.last_export_instance, acq=u["acq"])
        self.imports.append(imp)
        return None

    def _erasures(self, u, token):
        """The skill's erasures: each row asked of bank-feed; gone → record_not_found, still
        there → set_aside."""
        for r in u["rows"]:
            if self._bank("get_transaction", row_id=r["row_id"]).startswith("no transaction #"):
                self._tool("record_not_found", {"pass_token": token, "pid": r["pid"],
                                                "snapshot_id": u["snapshot_id"]})
            else:
                self._tool("set_aside", {"pass_token": token, "items": [{"pid": r["pid"]}],
                                         "reason": "still in bank-feed"})
        return None

    def _mail(self, ref):
        """The message attachment `ref` names (own mail or a vendor's)."""
        return next(m for m in self.gmail.own_mail + self.gmail.messages if m["ref"] == ref)

    def _filing(self, u, token):
        """The skill's filing: with `search`, the own-mail search and at once the gmail probe
        with every attachment found; then each of `files` (the unit's, or the probe's)
        downloaded, read once and filed with the reading (no vendor: own mail is no
        vendor's). A unit Casa cut comes again (queues)."""
        files = u["files"]
        if u["search"]:
            self._spend(1)
            found = self.gmail.search_emails("from:me to:me has:attachment newer_than:8d")
            if found is None:
                self._tool("record_probe", {"pass_token": token, "kind": "gmail", "ok": False,
                                            "detail": "Gmail search failed"})
                return None
            out = self._tool("record_probe", {"pass_token": token, "kind": "gmail", "ok": True,
                                              "data": {"refs": [m["ref"] for m in found]}})
            files = out["files"]
        for ref in files:
            m = self._mail(ref)
            self._file_one(token, m, {"kind": "invoice", "source": "manual-email",
                                      "issuer": m["issuer"]})
        return None

    SESSION = "/config/cc-home/.claude/projects/-config-agent-home-finance/s/tool-results/"

    def _read(self, path) -> bool:
        """Read, under Casa's path_scope for finance: only the session's own files (where
        Claude Code saves read_document's PDF) — never the handoff folder nor the store."""
        self._spend(1)
        self.reads.append(path)
        return path.startswith(self.SESSION)

    def _file_one(self, token, m, base) -> dict | None:
        """One found attachment filed as the skill says. read_mode "store" (#Q2-R7): download,
        ingest with only what the mail states, read_document, Read the session copy, then
        update_document_metadata with what is printed. "handoff" (the R7 skill): Read the
        download first — denied by path_scope, so the attachment is set aside."""
        self._spend(1)                                    # download_attachment
        args = {"source_path": m["path"], "extraction_author": "specialist",
                "source_ref": m["ref"], "pass_token": token, **base}
        if self.read_mode == "handoff":
            if not self._read(m["path"]):
                self._tool("set_aside", {"pass_token": token, "items": [{"ref": m["ref"]}],
                                         "reason": "the attachment could not be read"})
                return None
            return self._tool("ingest_document", {
                **args, "amount_minor": m["amount_minor"], "currency": m["currency"],
                "document_date": m["date"], "document_number": m["number"]})
        out = self._tool("ingest_document", args)
        self._tool("read_document", {"doc_id": out["doc_id"]})
        if self._read(self.SESSION + f"doc-{out['doc_id']}.pdf"):
            self._tool("update_document_metadata", {
                "doc_id": out["doc_id"], "amount_minor": m["amount_minor"],
                "currency": m["currency"], "document_date": m["date"],
                "document_number": m["number"], "pass_token": token})
        return out

    def _file_vendor(self, token, vendor, refs) -> list:
        """The vendor's found attachments, each downloaded, read once and filed (vendor=);
        the newly filed ones as candidates."""
        filed = []
        for ref in refs:
            m = self._mail(ref)
            out = self._file_one(token, m, {"kind": m["kind"], "source": "gmail",
                                            "counterparty": m["vendor"],
                                            "issuer": m["vendor"], "vendor": vendor})
            if out is not None and out["created"]:
                filed.append({"doc_id": out["doc_id"], "amount_minor": m["amount_minor"],
                              "currency": m["currency"], "date": m["date"], "held": None})
        return filed

    def _payment(self, u, token):
        """The skill's payment unit (rev 18.4 §R18.1): `files` first; the candidates judged
        from their stored reading (the exact fit, else the nearest-dated same-currency,
        same-amount unheld one → match); nothing fits: the searches left — hinted (a
        learned hint), plain, wider — each recorded at once with its refs, then every found
        invoice of its answer filed, until something fits; then ONE decide: match, else an
        unheld candidate → propose (alternatives: the others), else missing. A payment that
        holds a document with another same-amount candidate: propose the held one with it
        as the alternative (refused: match the held one). Then the hint saved from the
        search that found an invoice."""
        import dates
        vendor, pid = u["vendor"], u["pid"]
        filed = self._file_vendor(token, vendor, u["files"])
        for c in u.get("candidates", []):    # a files-only hand-out carries none
            if c.get("unread"):                 # Q2 R7: filed, its reading cut — read it now
                ref = self.conn.execute("SELECT source_ref FROM documents WHERE doc_id=?",
                                        (c["doc_id"],)).fetchone()[0]
                m = self._mail(ref)
                self._tool("read_document", {"doc_id": c["doc_id"]})
                if self._read(self.SESSION + f"doc-{c['doc_id']}.pdf"):
                    self._tool("update_document_metadata", {
                        "doc_id": c["doc_id"], "amount_minor": m["amount_minor"],
                        "currency": m["currency"], "document_date": m["date"],
                        "document_number": m["number"], "pass_token": token})
                    c.update(amount_minor=m["amount_minor"], currency=m["currency"],
                             date=m["date"], number=m["number"])
        if u.get("decided") or u["files_total"] > len(u["files"]):
            return None                 # the skill: the rest come with the next job_next
        hint = u["kb"].get("hint_sender") if u["kb"].get("known") else None
        win = u["search_window"]
        span = f" after:{win['after']} before:{win['before']}" if win["after"] else ""

        def gap(c):
            if not (c["date"] and u["date"]):
                return 10 ** 6
            return abs((dates.parse_day(c["date"][:10]) - dates.parse_day(u["date"][:10])).days)

        def plausible(c):
            """What the server would list (loop.candidates): the same currency and amount,
            or another currency (the FX screen is the server's)."""
            return c["currency"] != u["currency"] or c["amount_minor"] == u["amount_minor"]

        def unheld():
            return [c for c in u["candidates"] + [f for f in filed if plausible(f)]
                    if c["held"] is None]

        def exact():
            """Same currency and amount, dated near the payment: a recurring charge's
            invoice dated weeks away is another month's — the skill's judgment."""
            return sorted((c for c in unheld() if c["currency"] == u["currency"]
                           and c["amount_minor"] == u["amount_minor"]
                           and gap(c) <= self.near_days),
                          key=lambda c: (c["doc_id"] != u["exact_fit"], gap(c), c["doc_id"]))
        found_any = []
        refno = [w for w in re.findall(r"[A-Za-z0-9]+", u.get("remittance") or "")
                 if len(w) >= 6 and any(ch.isdigit() for ch in w)]
        if self.search_mode == "dated":
            refno = []
        plan = ([("hinted", f"from:{hint}{span}")] if hint else []) \
            + ([("payment", " ".join(refno))] if refno else []) \
            + [("plain", f"{vendor} invoice{span}"), ("payment", f"{vendor}{span}")]
        plan = plan[u["searches"]:][:u["searches_left"]]
        if not u["holds"]:
            for k, (kind, query) in enumerate(plan):
                if exact():
                    break
                self._spend(1)
                found = self.gmail.search_emails(query) or []
                self.search_log.append((vendor, kind, query))
                refs = Gmail.visible(found)
                if self.search_mode == "reference":
                    for m in found:          # a message naming the payment: its attachments
                        if refno and any(w.lower() in m["mentions"] for w in refno):
                            self._spend(1)
                            refs += [r for r in self.gmail.list_attachments(m["id"])
                                     if r not in refs]
                found = [m for m in found if m["ref"] in refs]
                found_any += found
                out = self._tool("record_search", {
                    "pass_token": token, "pids": [pid], "search": kind, "queries": [query],
                    "found_candidate": bool(refs), "refs": refs,
                    "exhausted": k == len(plan) - 1})
                filed.extend(self._file_vendor(token, vendor, out["files"]))
                if out["files_total"] > len(out["files"]):
                    return None         # more found than one answer carries: job_next
        base = {"pid": pid, "expected_revision": u["revision"]}
        held = (u["holds"] or {}).get("doc_id")
        if held is not None and u["why"] == "handover":
            # the skill (rev 18.4 §R18.3): the handed document, the first candidate, belongs
            # to this payment when it fits exactly → replace (the operator is asked); else keep
            c = next((x for x in u["candidates"] if x["doc_id"] in u["handed_over"]), None)
            fits = (c is not None and c["doc_id"] != held and c["currency"] == u["currency"]
                    and c["amount_minor"] == u["amount_minor"] and gap(c) <= self.replace_days)
            entry = ({**base, "outcome": "replace", "doc_id": c["doc_id"]} if fits
                     else {**base, "outcome": "keep"})
            out = self._tool("decide", {"pass_token": token, "entries": [entry]})
            assert out["refused"] == 0, out
            return None
        if held is not None:
            other = [c for c in exact() if c["doc_id"] != held]
            day = u["holds"].get("date") or u["date"]
            keep = {**base, "outcome": "match", "doc_id": held, "document_date": day}
            entry = ({**base, "outcome": "propose", "doc_id": held,
                      "alternatives": [other[0]["doc_id"]], "document_date": day}
                     if other else keep)
        elif exact():
            c = exact()[0]
            # #67: the operator's handed document is proposed — the operator confirms it
            entry = {**base, "outcome": "propose" if c["doc_id"] in u.get("handed_over", ())
                     else "match", "doc_id": c["doc_id"],
                     "document_date": c["date"] or u["date"]}
        elif [c for c in unheld() if gap(c) <= self.propose_days]:
            cs = sorted((c for c in unheld() if gap(c) <= self.propose_days),
                        key=lambda c: (gap(c), c["doc_id"]))
            entry = {**base, "outcome": "propose", "doc_id": cs[0]["doc_id"],
                     "alternatives": [c["doc_id"] for c in cs[1:4]],
                     "document_date": cs[0]["date"] or u["date"]}
        else:
            entry = {**base, "outcome": "missing", "reason": "no invoice found"}
        out = self._tool("decide", {"pass_token": token, "entries": [entry]})
        r = out["results"][0]
        if not r["applied"]:
            if "changed since it was handed out" in (r.get("refused") or ""):
                return None    # the skill: a refusal → job_next (a search that aged it out)
            redo = keep if held is not None else {
                **base, "outcome": "missing", "reason": "no fitting invoice found"}
            if "answer replace" in (r.get("refused") or ""):
                redo = {**base, "outcome": "keep"}     # the refusal's words: keep it
            assert entry["outcome"] != "missing", out   # the sim decides what the floor takes
            out = self._tool("decide", {"pass_token": token, "entries": [redo]})
            assert out["refused"] == 0, out
        senders = [m["sender"] for m in found_any if not m["sent"]]
        if senders and senders[0] != hint:
            self._tool("upsert_counterparty", {"name": vendor, "hint_sender": senders[0],
                                               "hint_subject": f"{vendor} invoice",
                                               "pass_token": token})
        return None

    def _reading(self, u, token):
        """#67: each handed document read — read_document, Read, then
        update_document_metadata with what is printed (`printed`; nothing readable: an
        empty reading, which still records it)."""
        for doc in u["docs"]:
            self._tool("read_document", {"doc_id": doc})
            if self._read(self.SESSION + f"doc-{doc}.pdf"):
                self._tool("update_document_metadata", {"doc_id": doc, "pass_token": token,
                                                        **self.printed.get(doc, {})})
        return None

    def _report(self, u, token):
        """`report` → report_job_progress(progress) (a Casa tool: one call), then job_next.
        `cut_reports`: that many report calls are cut by Casa (the batch ends there)."""
        if self.cut_reports > 0:
            self.cut_reports -= 1
            raise CasaCut()
        self._spend(1)
        return None

    def _mirror(self, u, token):
        done, failed = [], []
        for c in u["calls"]:
            out = self._bank(c["tool"], **c["args"])
            if out.lower().startswith("refused") or "Nothing was changed." in out:
                failed.append({"n": c["n"], "error": out[:200]})
            else:
                done.append(c["n"])
        self._tool("record_mirror", {"pass_token": token, "done": done, "failed": failed})
        return None

    def _view(self, u, token):
        """show_view(render_id); on Casa's receipt, mark_rendering_delivered(render_id). The
        deposit is kept in `posted` (the operator's message, its buttons to tap)."""
        self._tool("show_view", {"render_id": u["render_id"]})
        if self._broker_now is not None:
            self.posted[u["render_id"]] = self._broker_now.proposal()
        if self.deliver:
            self._tool("mark_rendering_delivered", {"render_id": u["render_id"]})
        return None

    def _post(self, u, token):
        """post_results(render_ids); on Casa's receipt, mark_rendering_delivered each."""
        out = self._tool("post_results", {"render_ids": u["render_ids"]})
        if self.deliver and out.get("render_ids"):
            self._tool("mark_rendering_delivered", {"render_ids": out["render_ids"]})
        return None
