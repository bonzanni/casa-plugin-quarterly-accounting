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
  filing    one search of the operator's own mail (nothing found), record_probe(gmail, ok)
            — failed when Gmail.down — then record_filing
  vendor    one search of the vendor's mail; each invoice found filed with
            ingest_document(vendor=…); then, per payment: its exact_fit → match; else a
            same-currency, same-amount unheld candidate → match; else any candidate →
            propose; else missing. All in ONE decide, then one record_search(pids=[the
            missing ones], search=hinted|plain)
  mirror    each call through bank-feed (a reply starting `refused`, or bank-feed's
            "Nothing was changed.", counts as failed), then one record_mirror
  view      show_view(render_id) under a broker; on the receipt (`deliver`),
            mark_rendering_delivered
  post      post_results(render_ids); on the receipt, mark_rendering_delivered

Plugin tools are called through qa_server.TOOLS (#43: the call shape the model makes)."""
from __future__ import annotations

import contextlib
import os

import binding
import job
import ledger
import passes
import version

from tests import bankfeed, sim

MAX_UNITS = 600         # a cursor that never finishes is a failure, never a hang


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
    casa_handoff, as gmail's download_attachment does); a search finds the invoices whose
    vendor the query names. `down`: every search fails (the probe records ok=false)."""

    def __init__(self, test):
        self.test = test
        self.searches = []
        self.messages = []
        self.down = False

    def invoice(self, vendor, amount, day, number, currency="EUR") -> None:
        path = self.test.publish(f"{number}.pdf", b"%PDF-1.4 " + number.encode() + b"\n")
        self.messages.append({"vendor": vendor, "amount_minor": amount, "currency": currency,
                              "date": day, "number": number, "path": path})

    def search_emails(self, query):
        """The messages the query finds; None when Gmail is down."""
        self.searches.append(query)
        if self.down:
            return None
        return [m for m in self.messages if m["vendor"].lower() in query.lower()]


class JobDriver:
    DATES = ("2026-07-05", "2026-08-05", "2026-09-05")

    def __init__(self, test, payments=2):
        self.test, self.conn = test, test.conn
        n = getattr(test, "_job_ledgers", 0) + 1
        test._job_ledgers = n
        self.bankfeed = self.bf = JobLedger(test.tmp / f"bankfeed-job{n}")
        test.addCleanup(self.bf.close)
        self.bf.account()
        self.gmail = Gmail(test)
        self._sync_fail = None
        self._no_tools = False
        self.last = None
        self.token = None
        self.calls = 0
        self.units = []
        self.deliver = True             # Casa's receipt arrives for every post
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
        return self._loop(job_id)

    def _loop(self, job_id) -> list:
        units = []
        with self._broker():
            for _ in range(MAX_UNITS):
                u = job.next_unit(self.conn, self.token, self.calls)
                self.calls += 1
                units.append(u)
                self.units.append(u)
                self.last = u
                assert u.get("pass_token") == self.token, u
                if u["unit"] == "complete":
                    return units
                if u["unit"] == "end-batch":
                    self.token = job.claim(self.conn, job_id)
                    self.calls = 1
                    continue
                self.do(u, self.token)
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
            yield b

    # --- the units ---------------------------------------------------------------------
    def do(self, u, token):
        """Carry out unit `u` under claim `token`."""
        self.token = token
        with self._broker():
            return getattr(self, "_" + u["unit"].replace("-", "_"))(u, token)

    def _tool(self, name, args):
        """#43: the plugin tool `name` called the way the model calls it — through
        qa_server.TOOLS. A posting tool's no-post shape (`refused`) is raised as the refusal
        it carries, as a direct call would raise it."""
        import db
        import qa_server
        import tools  # noqa: F401  -- registers every tool
        self.calls += 1
        out = qa_server.TOOLS[name]["fn"](args)
        if isinstance(out, dict) and isinstance(out.get("refused"), str):
            raise db.Refusal(out["refused"])
        return out

    def _bank(self, tool, **args) -> str:
        self.calls += 1
        return self.bf.call(tool, **args)

    def _probes(self, u, token):
        conn, bf = self.conn, self.bf
        self.calls += 1                                   # bank-feed's tools looked up
        if self._no_tools:
            self._tool("record_probe", {"pass_token": token, "kind": "bank_tools", "ok": False,
                                        "detail": "bank-feed's tools are not visible"})
            return None
        self._tool("record_probe", {"pass_token": token, "kind": "bank_tools", "ok": True})
        self.calls += 1                                   # list_accounts
        accounts = [{"account_id": r["account_id"], "category": r["category"],
                     "label": r["name"]}
                    for r in bf.conn.execute("SELECT account_id, category, name FROM accounts")]
        self._tool("record_probe", {"pass_token": token, "kind": "bank_accounts", "ok": True,
                                    "data": {"accounts": accounts}})
        ok, detail = True, ""
        self.calls += 1                                   # the sync
        if self._sync_fail is not None:
            ok, detail, self._sync_fail = False, self._sync_fail, None
        self._tool("record_probe", {"pass_token": token, "kind": "bank_sync", "ok": ok,
                                    "detail": detail, "acq": u["acq"]})
        self.calls += 1                                   # list_backups
        self._tool("record_probe", {"pass_token": token, "kind": "ledger", "ok": True,
                                    "data": sim.ledger_state(bf.listing())})
        self.calls += 1
        binding.check_setup(conn)
        return None

    def _snapshot(self, u, token):
        conn, bf = self.conn, self.bf
        self.calls += 2                                   # export_history, the import
        imp = ledger.import_ledger_export(conn, path=bf.export(), token=token,
                                          ledger_instance=bf.last_export_instance, acq=u["acq"])
        for c in imp["erase_candidates"]:
            if self._bank("get_transaction", row_id=c["row_id"]).startswith("no transaction #"):
                self._tool("record_not_found", {"pass_token": token, "pid": c["pid"],
                                                "snapshot_id": imp["snapshot"]})
        return None

    def _filing(self, u, token):
        self.calls += 1
        found = self.gmail.search_emails("from:me to:me has:attachment newer_than:8d")
        self._tool("record_probe", {"pass_token": token, "kind": "gmail",
                                    "ok": found is not None,
                                    **({"detail": "Gmail search failed"} if found is None
                                       else {})})
        self._tool("record_filing", {"pass_token": token})
        return None

    def _vendor(self, u, token):
        vendor, pays = u["vendor"], u["payments"]
        hinted = bool(u["kb"].get("hint_sender"))
        query = (f"from:{u['kb']['hint_sender']}" if hinted else f"{vendor} invoice")
        self.calls += 1
        found = self.gmail.search_emails(query) or []
        filed = []
        for m in found:
            self.calls += 1                               # download_attachment
            out = self._tool("ingest_document", {
                "source_path": m["path"], "kind": "invoice", "source": "gmail",
                "extraction_author": "specialist", "counterparty": m["vendor"],
                "issuer": m["vendor"], "document_date": m["date"],
                "document_number": m["number"], "amount_minor": m["amount_minor"],
                "currency": m["currency"], "vendor": vendor, "pass_token": token})
            filed.append({"doc_id": out["doc_id"], "amount_minor": m["amount_minor"],
                          "currency": m["currency"], "date": m["date"], "held": None})
        entries, taken, rest = [], set(), []
        for pay in pays:
            cands = [c for c in pay["candidates"] + filed if c["doc_id"] not in taken]
            ids = {c["doc_id"] for c in cands}
            fit = pay["exact_fit"] if pay["exact_fit"] in ids else None
            same = [c["doc_id"] for c in cands if c["held"] is None
                    and c["currency"] == pay["currency"]
                    and c["amount_minor"] == pay["amount_minor"]]
            if fit is None and same:
                fit = same[0]
            pick, outcome = (fit, "match") if fit is not None else (
                (cands[0]["doc_id"], "propose") if cands else (None, "missing"))
            if outcome == "missing":
                rest.append(pay)
                continue
            taken.add(pick)
            day = next(c["date"] for c in cands if c["doc_id"] == pick) or pay["date"]
            entries.append({"pid": pay["pid"], "outcome": outcome, "doc_id": pick,
                            "expected_revision": pay["revision"], "document_date": day})
        entries += [{"pid": p["pid"], "outcome": "missing", "reason": "no invoice found",
                     "expected_revision": p["revision"]} for p in rest]
        # decided first, then the search recorded: the record that ages a payment out
        # (D7) settles it and moves its revision, which a later `missing` at the handed
        # revision would find changed (review round 1)
        out = self._tool("decide", {"pass_token": token, "entries": entries})
        assert out["refused"] == 0, out          # the sim decides only what the floor takes
        if rest:
            self._tool("record_search", {"pass_token": token, "pids": [p["pid"] for p in rest],
                                         "search": "hinted" if hinted else "plain",
                                         "queries": [query]})
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
        """show_view(render_id); on Casa's receipt, mark_rendering_delivered(render_id)."""
        self._tool("show_view", {"render_id": u["render_id"]})
        if self.deliver:
            self._tool("mark_rendering_delivered", {"render_id": u["render_id"]})
        return None

    def _post(self, u, token):
        """post_results(render_ids); on Casa's receipt, mark_rendering_delivered each."""
        out = self._tool("post_results", {"render_ids": u["render_ids"]})
        if self.deliver and out.get("render_ids"):
            self._tool("mark_rendering_delivered", {"render_ids": out["render_ids"]})
        return None
