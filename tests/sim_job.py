# tests/sim_job.py
"""The job's worker side, done mechanically (S2 §5): claim, then job_next, carrying out
each unit it hands out the way the quarterly-job skill describes it — against a REAL
bank-feed (tests/bankfeed.py) and a Gmail that holds no messages. The cursor decides
everything; the driver never chooses a unit, an outcome or a token of its own.

  probes      bank-feed's tools, list_accounts, sync, list_backups, the four record_probes
              (bank_sync with the unit's acq), check_setup
  snapshot    export_history -> import_ledger_export(acq) -> each erase candidate:
              get_transaction; "no transaction #N" -> record_observation(not_found)
  sweep       one list_projections page (limit 10, the unit's quarter), each row read,
              recorded, written, re-read (tests/sim.py::observe_and_repair)
  gmail-probe one search_emails, record_probe(kind="gmail")
  filing      one self-mail search; nothing found; record_filing
  item        at most 4 queries, nothing found; record_search with them
  judge       one triage page (limit 8; a package also one dates_unread page); then
              job_next(judged={page_next, triage_remaining, documents})
  post        S7 §5: post_results(render_ids); on the receipt (`deliver`), each marked
  view        show_view(render_id) (or propose_account for accounts); on the receipt
              (`deliver`), marked
"""
from __future__ import annotations

import binding
import job
import ledger
import passes
import sweep
import version
import work

from tests import bankfeed, sim

CUT = object()          # the unit's turn ended part-way (cut_after_import)
MAX_UNITS = 600         # a cursor that never finishes is a failure, never a hang


class JobLedger(bankfeed.Ledger):
    """bank-feed, with the one ledger condition the cursor tests need on a fresh store."""

    def restore_since_install(self) -> None:
        """The ledger still carries this plugin's workflow writes after its install
        restore point while the accounting store holds nothing (a store reset without
        restoring bank-feed's install backup): bank_write_gate refuses at every pass's
        ledger probe ("restore backup … first"), so every pass stops at its probes. The
        write is a real tag_transaction under version.WORKFLOW, which mints and registers
        the install backup, as the sweep's first write does."""
        rid = self.rows(state="active")[0]["row_id"]
        if version.WORKFLOW not in self.registered():
            self.call("tag_transaction", row_ids=[rid], tags=["acct::open"],
                      workflow=version.WORKFLOW, expected_generation=self.generation())
        assert version.WORKFLOW in self.registered(), self.listing()


class Gmail:
    """A mailbox with no messages: every search finds nothing."""

    def __init__(self):
        self.searches = []

    def search_emails(self, query) -> list:
        self.searches.append(query)
        return []


class JobDriver:
    DATES = ("2026-07-05", "2026-08-05", "2026-09-05")

    def __init__(self, test, payments=2, queue=None, cut_after_import=False):
        self.test, self.conn = test, test.conn
        n = getattr(test, "_job_ledgers", 0) + 1
        test._job_ledgers = n
        self.bankfeed = self.bf = JobLedger(test.tmp / f"bankfeed-job{n}")
        test.addCleanup(self.bf.close)
        self.bf.account()
        self.gmail = Gmail()
        self.queue = queue
        self.cut_after_import = cut_after_import
        self._cut = False
        self._sync_fail = None
        self._no_tools = False
        self.last = None
        self.token = None
        self.units = []
        self.deliver = True             # S7 §5: Casa's receipt arrives for every post
        self.spend_before_posts = None
        self.add_payments([self.DATES[i % len(self.DATES)] for i in range(payments)])

    # --- the bank, as the operator's bank has it ------------------------------------
    def add_payments(self, dates) -> list:
        """Rows fetched into bank-feed (all of them again: a fetch reconciles the whole
        interval), each classified `software` (an invoice expected), as tx-classifier
        would before the pass. Returns the new rows' ids."""
        before = {r["row_id"] for r in self.bf.rows()}
        have = [r for r in self.bf.rows(state="active")]
        rows = [self.bf.row(r["booking_date"], amount=r["amount_minor"], ref=r["provider_ref"],
                            counterparty=r["counterparty"]) for r in have]
        k = len(have)
        for i, d in enumerate(dates):
            rows.append(self.bf.row(d, amount=1000 * (k + i + 1), ref=f"P{k + i + 1}",
                                    counterparty="Zapier"))
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
        stops there (the skill: "If bank-feed's tools are not visible to you …")."""
        self._no_tools = True

    # --- the loop --------------------------------------------------------------------
    def _job_of(self, token) -> str:
        return self.conn.execute("SELECT job_id FROM claims WHERE gen=?",
                                 (token,)).fetchone()[0]

    def run_job(self, job_id) -> list:
        """Claim, then job_next until `complete`, re-claiming (a new batch) on
        `end-batch` and when a unit's turn was cut. Returns every unit handed out."""
        self.token = job.claim(self.conn, job_id)
        return self._loop(self.token, None)

    def run_until(self, job_id, unit) -> dict:
        """Claim, then do units until one of kind `unit` is handed out (not done)."""
        self.token = job.claim(self.conn, job_id)
        return self.next_until(self.token, unit)

    def next_until(self, token, unit) -> dict:
        """Continue the claim `token`: do units until one of kind `unit` is handed out."""
        self.token = token
        self._loop(token, unit)
        return self.last

    def _loop(self, token, until) -> list:
        units, judged = [], None
        job_id = self._job_of(token)
        for _ in range(MAX_UNITS):
            if self.spend_before_posts is not None:
                self._spend_before(judged)
            u = job.next_unit(self.conn, self.token, judged=judged)
            judged = None
            units.append(u)
            self.units.append(u)
            self.last = u
            assert u.get("pass_token") == self.token, u
            if u["unit"] == until:
                return units
            if u["unit"] == "complete":
                if until is not None:
                    raise AssertionError(f"the job completed before a {until} unit: {units}")
                return units
            if u["unit"] == "end-batch":
                self.token = job.claim(self.conn, job_id)
                continue
            r = self.do(u, self.token)
            if r is CUT:
                self.token = job.claim(self.conn, job_id)
                continue
            judged = r
        raise AssertionError(f"the cursor handed out {MAX_UNITS} units without finishing: "
                             f"{[x['unit'] for x in units[-20:]]}")

    # --- the units ---------------------------------------------------------------------
    def do(self, u, token):
        """Carry out unit `u` under claim `token`. Returns the judge's `judged`, CUT when
        the unit's turn ended part-way, else None."""
        return getattr(self, "_" + u["unit"].replace("-", "_"))(u, token)

    def _spend_before(self, judged) -> None:
        """When the next unit would be the run's first post/view, raise the batch's spend
        to `spend_before_posts` − BATCH_RESERVE − 1, so _account swaps that unit for
        end-batch exactly once. Read off the cursor in a savepoint rolled back."""
        import db
        with db.tx(self.conn):
            self.conn.execute("SAVEPOINT peek")
            try:
                if judged is not None:
                    job._judged(self.conn, self.token, judged)
                nxt = job._choose(self.conn, self.token)["unit"]
            finally:
                self.conn.execute("ROLLBACK TO peek")
                self.conn.execute("RELEASE peek")
            if nxt in ("post", "view"):
                self.conn.execute("UPDATE claims SET spent=? WHERE gen=?",
                                  (self.spend_before_posts - job.BATCH_RESERVE - 1, self.token))
                self.spend_before_posts = None

    def _post(self, u, token):
        """post_results(render_ids); on Casa's receipt, mark_rendering_delivered each."""
        import views
        if self.deliver:
            for rid in u["render_ids"]:
                views.mark_rendering_delivered(self.conn, rid)
        return None

    def _view(self, u, token):
        """show_view(render_id) — or propose_account() when it says accounts (nothing to
        mark); on Casa's receipt, mark_rendering_delivered(render_id)."""
        import views
        if self.deliver and not u.get("accounts"):
            views.mark_rendering_delivered(self.conn, u["render_id"])
        return None

    def _probes(self, u, token):
        conn, bf = self.conn, self.bf
        if self._no_tools:
            passes.record_probe(conn, token, "bank_tools", False,
                                "bank-feed's tools are not visible to the finance specialist")
            return None
        passes.record_probe(conn, token, "bank_tools", True)
        accounts = [{"account_id": r["account_id"], "category": r["category"],
                     "label": r["name"]}
                    for r in bf.conn.execute("SELECT account_id, category, name FROM accounts")]
        passes.record_probe(conn, token, "bank_accounts", True, data={"accounts": accounts})
        ok, detail = True, ""
        if self._sync_fail is not None:             # bank-feed's sync replied with a failure
            ok, detail, self._sync_fail = False, self._sync_fail, None
        data = None
        if self.queue is not None:                   # the sync trailer's `Queue:` counts
            data = {"queue": {"workable": self.queue[0], "parked": self.queue[1]}}
        passes.record_probe(conn, token, "bank_sync", ok, detail, data, acq=u["acq"])
        passes.record_probe(conn, token, "ledger", True, data=sim.ledger_state(bf.listing()))
        binding.check_setup(conn)
        return None

    def _snapshot(self, u, token):
        conn, bf = self.conn, self.bf
        imp = ledger.import_ledger_export(conn, path=bf.export(), token=token,
                                          ledger_instance=bf.last_export_instance, acq=u["acq"])
        if self.cut_after_import and not self._cut:
            self._cut = True
            return CUT
        for c in imp["erase_candidates"]:
            if bf.call("get_transaction", row_id=c["row_id"]).startswith("no transaction #"):
                sweep.record_observation(conn, pid=c["pid"], token=token,
                                         snapshot_id=imp["snapshot"], not_found=True)
        return None

    def _sweep(self, u, token):
        page = sweep.list_projections(self.conn, token=token, limit=10, quarter=u["quarter"])
        for item in page["projections"]:
            sim.observe_and_repair(self.conn, self.bf, token, item, page["snapshot_id"])
        return None

    def _gmail_probe(self, u, token):
        self.gmail.search_emails("newer_than:1d")
        passes.record_probe(self.conn, token, "gmail", True)
        return None

    def _filing(self, u, token):
        self.gmail.search_emails("from:me to:me has:attachment newer_than:8d")
        job.record_filing(self.conn, token)
        return None

    def _item(self, u, token):
        it = u["item"]
        queries = [f"{it['counterparty']} invoice", f"{it['counterparty']} receipt"][:4]
        for q in queries:
            self.gmail.search_emails(q)
        work.record_search(self.conn, pid=it["pid"], token=token, queries=queries)
        return None

    def _judge(self, u, token):
        page = work.list_quarter_state(self.conn, quarter=u["quarter"], triage_only=True,
                                       limit=8, after=u["after"])
        if u["quarter"] is not None and page["next"] is None:
            work.list_quarter_state(self.conn, quarter=u["quarter"], unread_dates=True, limit=8)
        return {"judgment": u["judgment"], "after": u["after"],
                "page_next": page["next"], "triage_remaining": page["remaining"],
                "documents": {str(d): "no-payment-yet" for d in u["documents_first"]}}

