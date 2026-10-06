# tests/sim_job.py
"""The job's worker side, done mechanically (simple loop, design rev 17 §2): claim, then
job_next(calls_made), carrying out each unit it hands out the way the job skill describes
it — against a REAL bank-feed (tests/bankfeed.py) and a Gmail fake. The cursor decides
everything; the driver never chooses a unit or a token of its own. Every tool call the
driver makes in a turn is counted into `calls_made` (a new turn — a claim — starts at 0).

  probes    bank-feed's tools, list_accounts, sync, list_backups, the four record_probes
            (bank_sync with the unit's acq), check_setup
  snapshot  export_history -> import_ledger_export(acq) -> each erase candidate:
            get_transaction; "no transaction #N" -> record_not_found
  filing    one search of the operator's own mail, record_probe(gmail, ok) — failed when
            Gmail.down — then each attachment not in `filed_refs` filed, newest first
            (download, Read, ingest_document with the reading: amount, currency, date,
            issuer, number; no vendor), and record_filing once none is left unfiled
  vendor    the skill's search rule (Task 16): only while a payment is uncovered (no exact
            fit, no unheld candidate), the hinted search (a learned hint, not yet run this
            run), then the plain one (still uncovered, not yet run); each message found
            filed with ingest_document(vendor=…). Then per payment: one holding a document
            with another same-amount candidate → propose the held one with it as the
            alternative (refused: match the held one); the exact fit or the nearest-dated
            same-currency, same-amount unheld candidate → match; else an unheld candidate →
            propose; else missing. All in ONE decide; a refused entry decided again once
            (missing, or the held match). Then each search recorded for the payments it was
            for, and upsert_counterparty(hint_sender=…) when a search found an invoice
  mirror    each call through bank-feed (a reply starting `refused`, or bank-feed's
            "Nothing was changed.", counts as failed), then one record_mirror
  view      show_view(render_id) under a broker; on the receipt (`deliver`),
            mark_rendering_delivered
  post      post_results(render_ids); on the receipt, mark_rendering_delivered

Every unit is done within its `max_calls` (d3): at the budget the driver stops where it is
and calls job_next — the unit comes again as a continuation. `casa_cut` (an int): Casa's
batch bound — a call past it ends the batch without job_next (the driver re-claims), and
three batches in a row without a reported progress (`report` with `progressed`) fail the
run, as Casa ends it.

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


class CasaCut(Exception):
    """Casa ended the batch at its call bound, before the model's next job_next."""


class UnitBudget(Exception):
    """The unit's `max_calls` is reached: the model stops and calls job_next."""


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
    within the query's `after:`/`before:` dates. The operator's own-mail search
    (`from:me to:me …`) finds the operator's own mail (`own`), newest first. `down`: every
    search fails (the probe records ok=false)."""

    def __init__(self, test):
        self.test = test
        self.searches = []
        self.messages = []
        self.own_mail = []
        self.down = False

    def own(self, amount_minor, currency="EUR", day="2026-07-05", number=None,
            issuer="Zapier") -> str:
        """One self-addressed mail with an invoice attachment (the operator forwarding a
        document to themselves). Its ref, <message id>:<attachment id>."""
        k = len(self.own_mail) + 1
        number = number or f"OWN-{k:03d}"
        path = self.test.publish(f"own-{number}.pdf", b"%PDF-1.4 own " + number.encode() + b"\n")
        ref = f"own-msg-{k:03d}:att-1"
        self.own_mail.append({"ref": ref, "path": path, "amount_minor": amount_minor,
                              "currency": currency, "date": day, "number": number,
                              "issuer": issuer, "seq": k})
        return ref

    def invoice(self, vendor, amount_minor, currency, day, number, sender=None,
                kind="invoice", sent=False) -> None:
        """One message from `sender` (default billing@<vendor>.com) carrying a PDF of
        `kind`. `sent`: a sales invoice in the operator's Sent folder."""
        sender = sender or "billing@%s.com" % re.sub(r"[^a-z0-9]", "", vendor.lower())
        path = self.test.publish(f"{number}.pdf", b"%PDF-1.4 " + number.encode() + b"\n")
        self.messages.append({"vendor": vendor, "amount_minor": amount_minor,
                              "currency": currency, "date": day, "number": number,
                              "path": path, "sender": sender, "kind": kind, "sent": sent,
                              "id": f"msg-{len(self.messages) + 1:04d}"})

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
        out = []
        for m in self.messages:
            if hint is not None:
                if m["sender"].lower() != hint.group(1):
                    continue
            elif not q.startswith(m["vendor"].lower() + " "):
                continue
            if (after and m["date"] < after.group(1)) or (before and m["date"] >= before.group(1)):
                continue
            out.append(m)
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
        self.posted = {}                # render_id -> the deposit a view unit posted
        self.fx_rates = {}              # provider_ref -> (exchange_rate, unit): see _export
        self._spec = {}                 # fixture row number -> its bank row (quarter fixtures)
        self._broker_now = None
        self.batch_calls = []           # calls_made at each batch's end, the last run_job
        self.casa_cut = None            # Casa's batch call bound, when the test enforces it
        self.cuts = 0                   # batches Casa cut (casa_cut)
        self.batch_reported = []        # per batch: a progress report was handed (progressed)
        self._limit = None              # the unit in hand's call budget
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
                                    direction=r["direction"], value_date=r["value_date"]))
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

    def pay_once(self, vendor, amount, day, tags="software") -> int:
        """One more payment fetched into the bank; its fixture row number (its pid exists
        once a run imported it: pid_of)."""
        return self._add_rows([(vendor, amount, day, tags)])[0]

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
        return self.token

    def run_job(self, job_id, started_by="operator") -> list:
        """Claim, then job_next until `complete`, re-claiming (a new turn, a new batch) on
        `end-batch`. Returns every unit handed out."""
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
        with self._broker():
            for _ in range(MAX_UNITS):
                if self.casa_cut is not None and self.calls + 1 > self.casa_cut:
                    self.cuts += 1                 # cut before the job_next
                    batch_end()
                    self.token = job.claim(self.conn, job_id)
                    self.calls = 1
                    continue
                u = job.next_unit(self.conn, self.token, self.calls)
                self.calls += 1
                units.append(u)
                self.units.append(u)
                self.last = u
                assert u.get("pass_token") == self.token, u
                reported = reported or (u["report"] and u["progress"]["progressed"])
                if u["unit"] == "complete":
                    batch_end()
                    return units
                if u["unit"] == "end-batch":
                    batch_end()
                    self.token = job.claim(self.conn, job_id)
                    self.calls = 1
                    continue
                import db
                try:
                    self.do(u, self.token)
                except CasaCut:
                    self.cuts += 1
                    batch_end()
                    self.token = job.claim(self.conn, job_id)
                    self.calls = 1
                except db.Refusal as exc:   # the model reads `refused:` and calls job_next
                    if self.refusals is None:
                        raise
                    self.refusals.append(str(exc))
        raise AssertionError(f"the cursor handed out {MAX_UNITS} units without finishing: "
                             f"{[x['unit'] for x in units[-20:]]}")

    @contextlib.contextmanager
    def _broker(self):
        """The test's own broker when one listens, else a FakeBroker of the driver's."""
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
        """Carry out unit `u` under claim `token`, within its `max_calls` (d3): at the
        budget the unit stops where it is (the model then calls job_next)."""
        self.token = token
        self._limit = self.calls + u["max_calls"] if "max_calls" in u else None
        try:
            with self._broker():
                return getattr(self, "_" + u["unit"].replace("-", "_"))(u, token)
        except UnitBudget:
            return None
        finally:
            self._limit = None

    def _spend(self, k=1) -> None:
        """k tool calls: past Casa's bound the batch is cut; past the unit's budget the unit
        stops (neither call is made)."""
        if self.casa_cut is not None and self.calls + k > self.casa_cut:
            raise CasaCut()
        if self._limit is not None and self.calls + k > self._limit:
            raise UnitBudget()
        self.calls += k

    def _tool(self, name, args):
        """#43: the plugin tool `name` called the way the model calls it — through
        qa_server.TOOLS. A posting tool's no-post shape (`refused`) is raised as the refusal
        it carries, as a direct call would raise it."""
        import db
        import qa_server
        import tools  # noqa: F401  -- registers every tool
        self._spend()
        out = qa_server.TOOLS[name]["fn"](args)
        if isinstance(out, dict) and isinstance(out.get("refused"), str):
            raise db.Refusal(out["refused"])
        return out

    def _bank(self, tool, **args) -> str:
        self._spend()
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
        for c in imp["erase_candidates"]:
            if self._bank("get_transaction", row_id=c["row_id"]).startswith("no transaction #"):
                self._tool("record_not_found", {"pass_token": token, "pid": c["pid"],
                                                "snapshot_id": imp["snapshot"]})
        return None

    def _filing(self, u, token):
        """The skill's filing: the search and its probe, then each attachment not in
        `filed_refs` filed, newest first, downloaded and read once, with the reading (no
        vendor: own mail is no vendor's); record_filing once none is left — a unit cut at
        its `max_calls` comes again (d3)."""
        self._spend(1)
        found = self.gmail.search_emails("from:me to:me has:attachment newer_than:8d")
        self._tool("record_probe", {"pass_token": token, "kind": "gmail",
                                    "ok": found is not None,
                                    **({"detail": "Gmail search failed"} if found is None
                                       else {})})
        for m in found or ():
            if m["ref"] in u["filed_refs"]:
                continue
            self._spend(2)                                # download_attachment, Read
            self._tool("ingest_document", {
                "source_path": m["path"], "kind": "invoice", "source": "manual-email",
                "extraction_author": "specialist", "source_ref": m["ref"],
                "amount_minor": m["amount_minor"], "currency": m["currency"],
                "document_date": m["date"], "issuer": m["issuer"],
                "document_number": m["number"], "pass_token": token})
        self._tool("record_filing", {"pass_token": token})
        return None

    def _vendor(self, u, token):
        """The skill's vendor unit (§2.2, plan round 6): filed documents first; the vendor
        search only while a payment is uncovered — the hinted one (a learned hint, not yet
        run this run), then the plain one (still uncovered, not yet run); each message found
        filed; ONE decide; each search recorded for the payments it was for; the hint saved
        from the search that found an invoice (step 5)."""
        import dates
        vendor, pays = u["vendor"], u["payments"]
        hint = u["kb"].get("hint_sender") if u["kb"].get("known") else None
        win = u["search_window"]
        span = f" after:{win['after']} before:{win['before']}" if win["after"] else ""
        filed, seen, searched = [], set(), []

        def gap(c, pay):
            if not (c["date"] and pay["date"]):
                return 10 ** 6
            return abs((dates.parse_day(c["date"][:10]) - dates.parse_day(pay["date"][:10])).days)

        def unheld(pay):
            return [c for c in pay["candidates"] + filed if c["held"] is None]

        def uncovered():
            return [p for p in pays if p["exact_fit"] is None and not unheld(p)]

        def search(kind, query):
            want = [p["pid"] for p in uncovered()]
            self._spend(1)
            found = self.gmail.search_emails(query) or []
            self.search_log.append((vendor, kind, query))
            searched.append((kind, query, want, found))
            for m in found:
                if m["path"] in seen or m["id"] in u.get("filed_refs", ()):
                    continue                   # d3: a continuation skips what it filed
                seen.add(m["path"])
                self._spend(1)                           # download_attachment
                out = self._tool("ingest_document", {
                    "source_path": m["path"], "kind": m["kind"], "source": "gmail",
                    "extraction_author": "specialist", "counterparty": m["vendor"],
                    "source_ref": m["id"], "issuer": m["vendor"], "document_date": m["date"],
                    "document_number": m["number"], "amount_minor": m["amount_minor"],
                    "currency": m["currency"], "vendor": vendor, "pass_token": token})
                if not out["created"]:
                    # filed before (ingest's own answer): if it can fit, it is already among
                    # the unit's candidates with its `held` flag; if not, it is no candidate
                    continue
                filed.append({"doc_id": out["doc_id"], "amount_minor": m["amount_minor"],
                              "currency": m["currency"], "date": m["date"], "held": None})
            return found

        if uncovered() and hint and not u["searches"]["hinted"]:
            search("hinted", f"from:{hint}{span}")
        if uncovered() and not u["searches"]["plain"]:
            search("plain", f"{vendor} invoice{span}")

        # the same-currency, same-amount pairs, nearest dates first across the whole group
        # (the exact fit first of all): a later month's invoice never takes an earlier
        # payment's place when its own month's is there
        pairs = sorted((-1 if c["doc_id"] == p["exact_fit"] else gap(c, p), i, c["doc_id"])
                       for i, p in enumerate(pays) if not p["holds"]
                       for c in p["candidates"] + filed
                       if c["held"] is None and c["currency"] == p["currency"]
                       and c["amount_minor"] == p["amount_minor"])
        nearest, used = {}, set()
        for _, i, doc in pairs:
            if i not in nearest and doc not in used:
                nearest[i] = doc
                used.add(doc)
        entries, again, taken, rest = [], {}, set(used), []
        for i, pay in enumerate(pays):
            cands = [c for c in pay["candidates"] + filed if c["doc_id"] not in taken]
            held = (pay["holds"] or {}).get("doc_id")
            base = {"pid": pay["pid"], "expected_revision": pay["revision"]}
            if held is not None:
                # it holds a document: another unheld one of the same currency and amount
                # → propose the held one with the other as its alternative; refused by the
                # floor (the other is another payment's alternative already) it is decided
                # again once as `match` of the held one, which writes nothing; no other →
                # `match` of the held one
                other = sorted((c for c in pay["candidates"] + filed
                                if c["held"] is None and c["doc_id"] != held
                                and c["currency"] == pay["currency"]
                                and c["amount_minor"] == pay["amount_minor"]),
                               key=lambda c: (gap(c, pay), c["doc_id"]))
                day = pay["holds"].get("date") or pay["date"]
                keep = {**base, "outcome": "match", "doc_id": held, "document_date": day}
                if other:
                    entries.append({**base, "outcome": "propose", "doc_id": held,
                                    "alternatives": [other[0]["doc_id"]], "document_date": day})
                    again[pay["pid"]] = keep
                    taken.update({held, other[0]["doc_id"]})
                else:
                    entries.append(keep)
                    taken.add(held)
                continue
            fit = nearest.get(i)
            if fit is None:
                cands = [c for c in cands if c["held"] is None]
            else:
                cands = [c for c in pay["candidates"] + filed if c["doc_id"] == fit]
            pick, outcome = (fit, "match") if fit is not None else (
                (cands[0]["doc_id"], "propose") if cands else (None, "missing"))
            if outcome == "missing":
                rest.append(pay)
                continue
            taken.add(pick)
            day = next(c["date"] for c in cands if c["doc_id"] == pick) or pay["date"]
            entries.append({**base, "outcome": outcome, "doc_id": pick, "document_date": day})
        entries += [{"pid": p["pid"], "outcome": "missing", "reason": "no invoice found",
                     "expected_revision": p["revision"]} for p in rest]
        # decided first, then the searches recorded: the record that ages a payment out
        # (D7) settles it and moves its revision, which a later `missing` at the handed
        # revision would find changed (review round 1)
        out = self._tool("decide", {"pass_token": token, "entries": entries})
        # the refused entries decided again, once: a held document's proposal as `match` of
        # it; a document the floor rules out for the payment (the bank's rate, #35) as
        # `missing` — as the skill says, "re-decide only the refused entries"
        redo = [again.get(e["pid"]) or {"pid": e["pid"], "outcome": "missing",
                                        "reason": "no fitting invoice found",
                                        "expected_revision": e["expected_revision"]}
                for e, r in zip(entries, out["results"]) if not r["applied"]]
        assert all(e["outcome"] != "missing" for e, r in zip(entries, out["results"])
                   if not r["applied"]), out     # the sim decides only what the floor takes
        if redo:
            out = self._tool("decide", {"pass_token": token, "entries": redo})
            assert out["refused"] == 0, out
        for kind, query, want, found in searched:
            if want:
                self._tool("record_search", {"pass_token": token, "pids": want,
                                             "search": kind, "queries": [query],
                                             "found_candidate": bool(found)})
        senders = [m["sender"] for _, _, _, found in searched for m in found if not m["sent"]]
        if senders and senders[0] != hint:
            self._tool("upsert_counterparty", {"name": vendor, "hint_sender": senders[0],
                                               "hint_subject": f"{vendor} invoice",
                                               "pass_token": token})
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
