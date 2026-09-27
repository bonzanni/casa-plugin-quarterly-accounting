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
                for pid in views.render_items(self.conn, r["render_id"]):
                    shown = views.clip(names[pid], views.LINE_MAX).split(" ")[0]
                    self.assertIn(shown, r["text"], (view, pid))
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
