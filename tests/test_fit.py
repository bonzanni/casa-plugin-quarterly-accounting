# tests/test_fit.py
"""fix wave D round 2: the 4096 shape recurred in views, alerts and receipts,
so it is pinned as a property. Randomized (seeded) long and non-BMP details,
counterparty names and clause counts go through the one fit and through every
site that produces operator text; every output must be deliverable and keep
its closing phrase, and what is bound must be what was printed."""
import json
import random
import unittest

from tests._base import StoreCase
import db  # noqa: E402
import kb  # noqa: E402
import matches  # noqa: E402
import passes  # noqa: E402
import reply  # noqa: E402
import views  # noqa: E402
import work  # noqa: E402

LIMIT = views.TELEGRAM_LIMIT
ALPHABET = "abcdefghij klmnop éü \U0001d518\U0001f6a8中"


def _word(rng, n):
    return "".join(rng.choice(ALPHABET) for _ in range(n)).strip() or "x"


def _name(rng, long=True):
    """A payee name: sometimes ordinary, sometimes very long or non-BMP."""
    base = "Payee" + "".join(rng.choice("QRSTUVWXYZ") for _ in range(6))
    if long and rng.random() < 0.4:
        return base + " " + _word(rng, rng.choice((80, 700, 5000)))
    return base


class TestFitLines(unittest.TestCase):
    def test_the_fit_is_always_deliverable_and_keeps_its_closing(self):
        rng = random.Random(20260927)
        for _ in range(400):
            lines = [_word(rng, rng.choice((0, 5, 60, 900, 4100, 9000)))
                     for _ in range(rng.randint(0, 12))]
            closing = rng.choice((None, "The rest did not fit.", 'say "more".',
                                  "\U0001f6a8" * 40))
            always = rng.random() < 0.3
            out, whole = views.fit_lines(lines, closing, always)
            text = "\n".join(out)
            self.assertLessEqual(views.utf16_len(text), LIMIT)
            self.assertEqual(out[:whole], lines[:whole])        # bound lines printed in full
            cut = whole < len(lines)
            if closing and (cut or always):
                self.assertEqual(out[-1], closing)
            if not cut and not always:
                self.assertEqual(out, lines)
            if cut and whole == 0 and lines and out and out[0] != closing:
                self.assertTrue(out[0].endswith(views.CLIP_MARK))
            # no half surrogate pair: the text encodes cleanly
            text.encode("utf-16-le")

    def test_clip_never_splits_a_non_bmp_character(self):
        for n in range(1, 12):
            c = views.clip("\U0001f6a8" * 10, n)
            self.assertLessEqual(views.utf16_len(c), n)
            c.encode("utf-16-le", "strict")


class Base(StoreCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.token = self.pass_()
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO snapshots(pass_id, imported_at, rows, max_row_id,"
                              " bank_through) VALUES ('p', 'x', 0, 0, '2026-09-20')")
        self.n = 0

    def item(self, cp, amount, paired, link=None):
        self.n += 1
        self.row(self.n, counterparty=cp, amount_minor=amount,
                 booking_date="2026-09-%02d" % (1 + self.n % 28),
                 value_date="2026-09-%02d" % (1 + self.n % 28))
        pid = self.lineage_for(self.n)
        self.classify(pid, {"software"})
        self.settle(pid)
        if link:
            kb.upsert_counterparty(self.conn, cp, patterns=[cp], source="portal",
                                   document_link=link)
        if paired:
            d = self.doc(counterparty=cp, issuer=cp, amount_minor=amount,
                         document_date="2026-09-%02d" % (1 + self.n % 28),
                         document_number=_word(random.Random(self.n), 30))
            matches.record_match(self.conn, pid=pid, doc_id=d, author="auto",
                                 expected_revision=self.rev(pid), row_snapshot=self.snapshot(pid),
                                 token=self.token, labels=("guessed",))
        else:
            work.record_search(self.conn, pid=pid, token=self.token, queries=[cp])
        return pid


def _flat(text):
    return " ".join(text.split())


class TestViewsProperty(Base):
    def test_every_view_and_page_is_deliverable_and_binds_what_it_prints(self):
        rng = random.Random(7)
        names = {}
        for i in range(rng.randint(25, 45)):
            name = _name(rng)
            link = ("https://x.example/" + _word(rng, 3000)) if rng.random() < 0.15 else None
            names[self.item(name, 100 + i, rng.random() < 0.5, link)] = name
        clipped, pages = 0, 0
        for view in ("status", "missing", "check", "all", "rest"):
            page, after = (1 if view == "all" else None), None
            for _ in range(60):
                args = {"view": view, "quarter": "2026-Q3"}
                if page is not None:
                    args["page"] = page
                if after is not None:
                    args["after"] = after
                r = views.build_review(self.conn, **args)
                self.assertLessEqual(views.utf16_len(r["text"]), LIMIT, view)
                clipped += views.CLIP_MARK in r["text"]
                nxt = r["next"]
                if nxt is not None:
                    self.assertTrue('say "more"' in r["text"] or 'say "all of them"' in r["text"],
                                    (view, r["text"][-120:]))
                flat = _flat(r["text"])
                for pid in views.render_items(self.conn, r["render_id"]):
                    # every bound item prints what identifies it: amount and date
                    d = work.describe(self.conn, pid)
                    self.assertIn(views._money(d) + " · " + views._day(d["date"]), flat,
                                  (view, pid))
                    self.assertIn(names[pid][:20], flat, (view, pid))
                    # and every pairing it binds is printed by its document's name
                    row = self.conn.execute("SELECT match_revisions_json FROM render_items"
                                            " WHERE render_id=? AND pid=?",
                                            (r["render_id"], pid)).fetchone()
                    for mid in json.loads(row[0]):
                        doc = self.conn.execute(
                            "SELECT d.document_number FROM match_state m JOIN documents d"
                            " ON d.doc_id=m.doc_id WHERE m.match_id=?", (int(mid),)).fetchone()
                        self.assertIn(_flat(views.field(doc[0]))[:20], flat, (view, pid, mid))
                if nxt is None or "after" not in nxt:
                    break
                page, after = nxt["page"], nxt["after"]
                pages += 1
        self.assertGreater(clipped, 0)              # the generator reached the clip
        self.assertGreater(pages, 0)                # and paging
        for pid in names:
            r = views.build_review(self.conn, view="item", pid=pid)
            self.assertLessEqual(views.utf16_len(r["text"]), LIMIT)


class TestAlertsProperty(Base):
    def test_every_alert_rendering_is_deliverable_and_each_occurrence_said_once(self):
        rng = random.Random(11)
        with db.tx(self.conn):
            self.conn.execute("INSERT INTO packages(quarter, filename, path, built_at, partial,"
                              " digest, size, caption, manifest_json) VALUES ('2026-Q3',"
                              " 'books-2026-Q3-2026-10-14.zip','/x','x',0,'d',1,'c','{}')")
        n = rng.randint(40, 120)
        for i in range(n):
            self.row(1000 + i, counterparty=_name(rng), amount_minor=100 + i,
                     booking_date="2026-07-14")
            self.settle(self.lineage_for(1000 + i))
            with db.tx(self.conn):
                self.conn.execute("INSERT INTO alerts(kind, occurrence_key, detail, raised_at)"
                                  " VALUES ('delivered-changed', ?, ?, 'x')",
                                  ("k%d" % i, db.canonical({
                                      "package": "books-2026-Q3-2026-10-14.zip",
                                      "quarter": "2026-Q3", "row_id": 1000 + i,
                                      "change": rng.choice(("corrected", "vanished"))})))
        sent, renders, clipped = {}, 0, 0
        for rounds in range(40):
            t = self.pass_()
            if rounds < 3:
                passes.record_probe(self.conn, t, rng.choice(("gmail", "bank_sync")), False,
                                    _word(rng, rng.choice((10, 400, 6000))))
            speak = passes.end_pass(self.conn, t, "complete", {})["speak"]
            if speak is None:
                break
            text = speak["text"]
            self.assertLessEqual(views.utf16_len(text), LIMIT)
            renders += 1
            clipped += views.CLIP_MARK in text
            r = self.conn.execute("SELECT scope_json FROM renders WHERE render_id=?",
                                  (speak["render_id"],)).fetchone()
            bound = json.loads(r["scope_json"])["alerts"]
            self.assertTrue(bound)
            left = self.conn.execute("SELECT COUNT(*) FROM alerts WHERE sent_at IS NULL")\
                .fetchone()[0]
            if left > len(bound):
                self.assertTrue(_flat(text).endswith(_flat(
                    "More changed than fits in one message — the rest comes with the next "
                    "check.")))
            views.mark_rendering_delivered(self.conn, speak["render_id"])
            for a in bound:
                sent[a] = sent.get(a, 0) + 1
        self.assertGreater(renders, 1)
        self.assertGreater(clipped, 0)
        total = self.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM alerts WHERE sent_at IS NULL")
                         .fetchone()[0], 0)
        self.assertEqual(len(sent), total)
        self.assertEqual(set(sent.values()), {1})


class TestReplyProperty(Base):
    def test_every_receipt_page_is_deliverable_and_nothing_is_lost(self):
        rng = random.Random(3)
        pids, names = [], []
        for i in range(rng.randint(60, 140)):
            name = "Vendor%03d" % i + ("" if rng.random() < 0.8
                                       else " " + " ".join(["Holding"] * rng.randint(1, 40)))
            pids.append(self.item(name, 1000 + i, True))
            names.append(name)
        # several payments under one long shared name: a long "Which one?" line
        for i in range(rng.randint(30, 150)):
            pids.append(self.item("Shared Payee Group", 50000 + i, False))
        self.show(*pids)
        picked = rng.sample(names, rng.randint(20, len(names)))
        clauses = [f"{n} is wrong" for n in picked] + ["Shared Payee Group is wrong",
                                                       "what is this?"]
        rng.shuffle(clauses)
        out = reply.apply_reply(self.conn, ". ".join(clauses))
        pages = out["receipt_pages"]
        self.assertEqual(out["receipt"], pages[0])
        for page in pages:
            self.assertLessEqual(views.utf16_len(page), LIMIT)
        whole = _flat("\n".join(pages))
        self.assertEqual(whole.count("Unpaired Vendor"), len(picked))
        self.assertGreater(len(pages), 1)
        self.assertGreater(views.utf16_len(out["asks"][0]), LIMIT)
        self.assertIn(_flat(out["asks"][0]), whole)            # the long ask, split, intact


if __name__ == "__main__":
    unittest.main()


def _norm(s):
    return "".join(ch for ch in s if not ch.isspace() and ch != "·")


class TestIdentityProperty(Base):
    """fix wave D round 4, the property stated once: every entity a rendering
    binds is uniquely identified by text visibly in it, and every item (and
    every candidate) is bindable in at least one reachable view."""

    def candidates(self, pid, numbers):
        docs = [self.doc(document_number=n, issuer="Issuer %d" % i,
                         document_date="2026-09-%02d" % (1 + i % 3))
                for i, n in enumerate(numbers)]
        with db.tx(self.conn):
            for doc in docs:
                mid = self.conn.execute(
                    "INSERT INTO matches(pid_created, doc_id, label, runners_up_json,"
                    " created_seq) VALUES (?,?,'clean','[]',?)",
                    (pid, doc, db.next_seq(self.conn))).lastrowid
                self.conn.execute("INSERT INTO match_state(match_id, pid, doc_id, state, author,"
                                  " activation) VALUES (?,?,?,'conflicted','auto',0)",
                                  (mid, pid, doc))

    def check(self, r, bound_where):
        text = r["text"]
        self.assertLessEqual(views.utf16_len(text), LIMIT)
        flat = _norm(text)
        idents = {}
        for row in self.conn.execute("SELECT pid, match_revisions_json FROM render_items WHERE"
                                     " render_id=?", (r["render_id"],)).fetchall():
            d = work.describe(self.conn, row[0])
            head = _norm(views.headline(d, "2026-Q3") if r.get("_view") != "item"
                         else views.headline(d))
            self.assertIn(head, flat)
            idents.setdefault(head, []).append(row[0])
            docs = {c["match_id"]: c["document"] for c in
                    ([d["current"]] if d["current"] else []) + d["candidates"]}
            names = [_norm(views.ident(docs[int(m)])) for m in json.loads(row[1])]
            for n in names:
                self.assertIn(n, flat)
            self.assertEqual(len(names), len(set(names)))           # unique within the payment
            bound_where.setdefault(row[0], set()).update(int(m) for m in json.loads(row[1]))
        for head, pids in idents.items():
            self.assertEqual(len(pids), 1, head)                     # unique among payments

    def test_identity_is_visible_unique_and_every_entity_reachable(self):
        rng = random.Random(4)
        pids = []
        for i in range(rng.randint(12, 20)):
            payee = rng.choice(("Adobe", "Adobe", "Figma", "Z" * 70 + rng.choice("AB")))
            amount = rng.choice((5445, 5445, 1000))
            pid = self.item(payee, amount, False)
            pids.append(pid)
            shape = rng.choice(("guessed", "candidates", "plain"))
            prefix = "A" * rng.choice((10, 65))
            if shape == "guessed":
                d = self.doc(counterparty=payee, issuer="I%d" % i,
                             document_number=prefix + "G%d" % i,
                             document_date="2026-09-02")
                matches.record_match(
                    self.conn, pid=pid, doc_id=d, author="auto", expected_revision=self.rev(pid),
                    row_snapshot=self.snapshot(pid), token=self.token, labels=("guessed",),
                    runners_up=tuple(_word(rng, rng.choice((5, 90))) for _ in
                                     range(rng.randint(0, 120))))
            elif shape == "candidates":
                n = rng.randint(2, 45)
                self.candidates(pid, [prefix + rng.choice(("CORRECT", "WRONG", "X%d" % k))
                                      for k in range(n)])
        bound = {}
        for view in ("status", "check", "all"):
            page, after = (1 if view == "all" else None), None
            for _ in range(40):
                kw = {k: v for k, v in (("page", page), ("after", after)) if v is not None}
                r = views.build_review(self.conn, view=view, quarter="2026-Q3", **kw)
                self.check(r, bound)
                nxt = r["next"]
                if nxt is None or "after" not in nxt:
                    break
                page, after = nxt["page"], nxt["after"]
        for pid in pids:                  # the item view: reached by a re-show or the phrase
            page, after = None, None
            for _ in range(40):
                kw = {k: v for k, v in (("page", page), ("after", after)) if v is not None}
                r = views.build_review(self.conn, view="item", pid=pid, **kw)
                r["_view"] = "item"
                self.check(r, bound)
                if r["next"] is None:
                    break
                self.assertIn('say "more"', r["text"])
                page, after = r["next"]["page"], r["next"]["after"]
        for pid in pids:
            self.assertIn(pid, bound)                                # every item bindable
            d = work.describe(self.conn, pid)
            names = [views.ident(c["document"]) for c in d["candidates"]]
            reachable = {c["match_id"] for c in d["candidates"]
                         if names.count(views.ident(c["document"])) == 1}
            self.assertLessEqual(reachable, bound[pid])              # every distinct candidate


class TestBindable(unittest.TestCase):
    """The bind-time backstop on its own: visible AND unique, or not bound."""
    def blk(self, pid, ident, pairings):
        return views._Block([ident], pid=pid, ident=ident, pairings=pairings)

    def test_only_visible_unique_identities_bind(self):
        text = "Adobe · EUR 1.00 · 1 Sep\ninvoice A (2 Sep), invoice B (2 Sep)\nFigma · EUR 2.00 · 2 Sep"
        chosen = [self.blk(1, "Adobe · EUR 1.00 · 1 Sep",
                           {10: "invoice A (2 Sep)", 11: "invoice B (2 Sep)",
                            12: "invoice C (2 Sep)"}),                 # C is not in the text
                  self.blk(2, "Figma · EUR 2.00 · 2 Sep", {}),
                  self.blk(3, "Zapier · EUR 3.00 · 3 Sep", {})]      # not in the text
        self.assertEqual(views._bindable(chosen, text), {1: {10, 11}, 2: set()})

    def test_shared_identities_bind_neither(self):
        text = "Adobe · EUR 1.00 · 1 Sep\ninvoice (2 Sep), invoice (2 Sep)"
        chosen = [self.blk(1, "Adobe · EUR 1.00 · 1 Sep", {10: "invoice (2 Sep)",
                                                          11: "invoice (2 Sep)"}),
                  self.blk(2, "Adobe · EUR 1.00 · 1 Sep", {})]
        self.assertEqual(views._bindable(chosen, text), {})
        self.assertEqual(views._bindable(chosen[:1], text), {1: set()})

    def test_a_wrapped_identity_is_still_visible(self):
        text = "Adobe\nEUR 1.00 · 1\nSep"
        self.assertEqual(views._bindable([self.blk(1, "Adobe · EUR 1.00 · 1 Sep", {})], text),
                         {1: set()})
