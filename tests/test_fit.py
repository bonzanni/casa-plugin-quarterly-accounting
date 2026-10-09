# tests/test_fit.py
"""fix wave D round 2: the 4096 shape recurred in views, alerts and receipts,
so it is pinned as a property. Randomized (seeded) long and non-BMP details,
counterparty names and clause counts go through the one fit and through every
site that produces operator text; every output must be deliverable and keep
its closing phrase, and what is bound must be what was printed."""
import json
import random
import unittest
from unittest import mock

from tests._base import StoreCase, apply_now
import db  # noqa: E402
import kb  # noqa: E402
import matches  # noqa: E402
import passes  # noqa: E402
import views  # noqa: E402
import work  # noqa: E402

LIMIT = views.BODY_LIMIT
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

    def test_a_literal_field_never_prints_the_reserved_mark(self):
        rng = random.Random(6)
        for _ in range(300):
            v = "".join(rng.choice("ab \u00b7ref0123") for _ in range(rng.choice((3, 30, 90))))
            out = views.field(v)
            body = out.rsplit(views.CLIP_MARK + views.MARK, 1)[0] if views.utf16_len(
                v.replace(views.MARK, views.LITERAL_MARK)) > views.FIELD_MAX else out
            self.assertNotIn(views.MARK, body, (v, out))

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
                                   document_link=link[:kb.LINK_MAX])
            with db.tx(self.conn):   # a longer link only as an earlier version stored it
                self.conn.execute("UPDATE counterparties SET document_link=? WHERE name=?",
                                  (link, cp))
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
                    self.assertTrue(views.MORE_LINE in r["text"] or 'say "all of them"' in r["text"],
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
            speak = self.end_and_speak()
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
    def test_every_reading_text_is_deliverable_and_nothing_is_lost(self):
        """S7 §8: a reading is posted as one proposal and applied with one receipt. Every
        text it produces fits one message; a reading that cannot be shown whole is refused
        and applies nothing; one that is shown applies exactly the payments it listed, and
        its receipt names every one of them."""
        import posting
        rng = random.Random(3)
        pids, names = {}, []
        for i in range(rng.randint(60, 140)):
            name = "Vendor%03d" % i + ("" if rng.random() < 0.8
                                       else " " + " ".join(["Holding"] * rng.randint(1, 40)))
            pids[name] = self.item(name, 1000 + i, True)
            names.append(name)
        # several payments under one long shared name: a long "Which one?" line
        shared = [self.item("Shared Payee Group", 50000 + i, False)
                  for i in range(rng.randint(30, 150))]
        outcomes = set()
        for k, ask in ((rng.randint(1, 8), False), (rng.randint(1, 8), True),
                       (rng.randint(60, 80), False), (rng.randint(9, 20), False)):
            live = [n for n in names if self.conn.execute(
                "SELECT 1 FROM match_state WHERE pid=? AND state IN ('matched','proposed')",
                (pids[n],)).fetchone()]
            self.show(*[pids[n] for n in live], *shared)
            picked = rng.sample(live, min(k, len(live)))
            clauses = [f"{n} is wrong" for n in picked] + ["what is this?"] + (
                ["Shared Payee Group is wrong"] if ask else [])
            rng.shuffle(clauses)
            before = self.conn.execute("SELECT COUNT(*) FROM log WHERE author='operator'"
                                       ).fetchone()[0]
            try:
                out = apply_now(self.conn, ". ".join(clauses))
            except db.Refusal as exc:
                self.assertEqual(str(exc), posting.READING_TOO_LONG)
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE"
                                                   " author='operator'").fetchone()[0], before)
                outcomes.add("refused")
                continue
            outcomes.add("applied")
            for t in (out["proposal"], out["receipt"]):
                self.assertLessEqual(views.utf16_len(t), LIMIT)
            self.assertEqual(_flat(out["proposal"]).count("· Remove the match for Vendor"), len(picked))
            self.assertEqual(_flat(out["receipt"]).count("Removed the match for Vendor"), len(picked))
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM log WHERE"
                                               " author='operator'").fetchone()[0],
                             before + len(picked))
        self.assertEqual(outcomes, {"refused", "applied"})


if __name__ == "__main__":
    unittest.main()


def _norm(s):
    return "".join(ch for ch in s if not ch.isspace() and ch != "·")


def _digest_collisions(prefix, k=6):
    """Suffixes whose "A"*65-style values share a 4-hex digest (birthday search):
    forced collisions of the clipped-field digest."""
    import hashlib
    seen, out = {}, []
    i = 0
    while len(out) < k:
        v = prefix + str(i)
        h = hashlib.sha256(v.encode()).hexdigest()[:4]
        if h in seen:
            out += [seen.pop(h), v]
        else:
            seen[h] = v
        i += 1
    return out


class TestIdentityProperty(Base):
    """fix wave D round 4, the property stated once: every entity a rendering
    binds is uniquely identified by text visibly in it, and every item (and
    every candidate) is bindable in at least one reachable view."""

    def candidates(self, pid, numbers, issuers=None):
        docs = [self.doc(document_number=n,
                         issuer=(issuers[i] if issuers else "Issuer %d" % i),
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

    def check(self, r, bound_where, view, pid=None):
        members = views.membership(self.conn, view, "2026-Q3", pid)
        with views.named([work.describe(self.conn, p) for p in members],
                         None if view == "item" else "2026-Q3"):
            self._check(r, bound_where)

    def _check(self, r, bound_where):
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
        forced = _digest_collisions("A" * 65)                     # 4-hex digest collisions
        pids = []
        for i in range(rng.randint(12, 20)):
            payee = rng.choice(("Adobe", "Adobe", "Figma", "Z" * 70 + rng.choice("AB")))
            if pids and rng.random() < 0.3:          # literal text shaped like generated text
                other = rng.choice(pids)
                payee = rng.choice(("Adobe ref " + views.lineage_ref(other)[:4],
                                    "Adobe ref " + views.lineage_ref(other)[:8],
                                    "Adobe \u00b7 EUR 54.45", "Adobe\u00b7ref\u00b7"
                                    + views.lineage_ref(other)[:4]))
            amount = rng.choice((5445, 5445, 1000))
            pid = self.item(payee, amount, False)
            pids.append(pid)
            shape = rng.choice(("guessed", "candidates", "plain", "same", "forced", "forge"))
            prefix = "A" * rng.choice((10, 65))
            if shape == "guessed":
                d = self.doc(counterparty=payee, issuer="I%d" % i,
                             document_number=prefix + "G%d" % i,
                             document_date="2026-09-02", amount_minor=amount)
                matches.record_match(
                    self.conn, pid=pid, doc_id=d, author="auto", expected_revision=self.rev(pid),
                    row_snapshot=self.snapshot(pid), token=self.token, labels=("guessed",),
                    runners_up=tuple(_word(rng, rng.choice((5, 90))) for _ in
                                     range(rng.randint(0, 120))))
            elif shape == "same":                 # equal numbers across (and within) issuers
                n = rng.randint(2, 6)
                self.candidates(pid, ["SAME"] * n,
                                [rng.choice(("Adobe", "Adobe Ireland")) for _ in range(n)])
            elif shape == "forge":                # numbers/issuers shaped like generated parts
                shas = [r[0] for r in self.conn.execute("SELECT sha256 FROM documents")] or ["0"]
                sha = rng.choice(shas)
                specs = [("SAME", "Adobe"), ("SAME", "Adobe"),
                         ("SAME \u00b7Adobe\u00b7" + sha[:4], "Adobe"),
                         ("SAME from Adobe \u00b7" + sha[:4], "Adobe"),
                         ("SAME \u00b7Adobe", "Adobe"), ("SAME", "Adobe\u00b7" + sha[:4]),
                         ("SAMEAdobe" + sha[:8], "Adobe")]
                rng.shuffle(specs)
                k = rng.randint(2, len(specs))
                self.candidates(pid, [n for n, _ in specs[:k]], [i for _, i in specs[:k]])
            elif shape == "forced":
                d = self.doc(counterparty=payee, issuer="Adobe", document_number=forced[i % 6],
                             document_date="2026-09-02", amount_minor=amount)
                matches.record_match(
                    self.conn, pid=pid, doc_id=d, author="auto", expected_revision=self.rev(pid),
                    row_snapshot=self.snapshot(pid), token=self.token, labels=("guessed",))
            elif shape == "candidates":
                n = rng.randint(2, 45)
                self.candidates(pid, [prefix + rng.choice(("CORRECT", "WRONG", "X%d" % k))
                                      for k in range(n)])
        # the backstop never refuses two DISTINCT entities: identities are distinct by
        # construction, so on an uncut rendering it binds every block and pairing
        real = views._bindable
        refused = []

        def watching(chosen, text):
            out = real(chosen, text)
            for c in chosen:
                if c.pid is not None and (c.pid not in out
                                          or set(c.pairings) - out[c.pid]):
                    refused.append((c.pid, c.ident))
            return out
        patch = mock.patch.object(views, "_bindable", watching)
        patch.start()
        self.addCleanup(patch.stop)
        bound = {}
        for view in ("status", "check", "all"):
            page, after = (1 if view == "all" else None), None
            for _ in range(40):
                kw = {k: v for k, v in (("page", page), ("after", after)) if v is not None}
                r = views.build_review(self.conn, view=view, quarter="2026-Q3", **kw)
                self.check(r, bound, view)
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
                self.check(r, bound, "item", pid)
                if r["next"] is None:
                    break
                self.assertIn(views.MORE_LINE, r["text"])
                page, after = r["next"]["page"], r["next"]["after"]
        for pid in pids:
            self.assertIn(pid, bound)                                # every item bindable
            d = work.describe(self.conn, pid)
            every = {c["match_id"] for c in d["candidates"]}
            if d["current"]:
                every.add(d["current"]["match_id"])
            self.assertLessEqual(every, bound[pid])                  # every candidate
        self.assertEqual(refused, [])


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


class TestSeenNameProperty(Base):
    """fix wave D round 7: a reply resolves names against what the operator
    SAW as well as the stored names. Payees whose literal values display alike
    ("A·B" and "A•B"): a correction never lands on a payment other than the
    one meant — it applies to exactly that one, or asks and applies nothing."""
    def test_a_correction_never_lands_on_another_payment(self):
        rng = random.Random(7)
        family = ("A\u00b7B", "A\u2022B", "a\u2022b", "A\u00b7 B", "A \u2022B")
        pids = {}
        for i in range(rng.randint(6, 12)):
            name = rng.choice(family)
            amount = rng.choice((5445, 7000))
            day = rng.choice(("2026-09-14", "2026-09-16"))
            self.n += 1
            self.row(self.n, counterparty=name, amount_minor=amount, booking_date=day,
                     value_date=day)
            pid = self.lineage_for(self.n)
            self.classify(pid, {"software"})
            self.settle(pid)
            d = self.doc(counterparty=name, issuer=name, amount_minor=amount,
                         document_date=day, document_number="N%d" % i)
            matches.record_match(self.conn, pid=pid, doc_id=d, author="auto",
                                 expected_revision=self.rev(pid), row_snapshot=self.snapshot(pid),
                                 token=self.token, labels=("guessed",))
            pids[pid] = (name, amount, day)

        def paired(p):
            return self.conn.execute("SELECT 1 FROM match_state WHERE pid=? AND state IN"
                                     " ('matched','proposed')", (p,)).fetchone() is not None
        other = self.item("Zapier", 9900, True)            # unrelated to the family
        for step in range(12):
            r = views.build_review(self.conn, view="status", quarter="2026-Q3")
            views.mark_rendering_delivered(self.conn, r["render_id"])
            # round 8: unrelated deliveries between the sheet and the reply
            for _ in range(rng.randint(0, 2)):
                if rng.random() < 0.5:
                    it = views.build_review(self.conn, view="item", pid=other)
                    views.mark_rendering_delivered(self.conn, it["render_id"])
                else:
                    t = self.pass_()
                    passes.record_probe(self.conn, t, "gmail", False, "down %d %d" % (step, _))
                    speak = self.end_and_speak()
                    if speak:
                        views.mark_rendering_delivered(self.conn, speak["render_id"])
                    self.token = self.pass_()
                    passes.record_probe(self.conn, self.token, "gmail", True)
            live = [p for p in pids if paired(p)]
            if not live:
                break
            target = rng.choice(live)
            name, amount, day = pids[target]
            shown = views.field(name)
            words = [shown] + rng.choice(([], ["%.2f" % (amount / 100)],
                                          ["%.2f" % (amount / 100), views._day(day)]))
            before = {p: paired(p) for p in pids}
            # binding R1/R3: a reply quoting the sheet resolves on it; unquoted words bind
            # whatever came last, and refuse when it does not show the payee
            quoted = views.unesc(r["text"]) if rng.random() < 0.5 else None
            out = apply_now(self.conn, "the %s one is wrong" % " ".join(words), quoted=quoted)
            changed = [p for p in pids if before[p] != paired(p)]
            self.assertLessEqual(set(changed), {target}, (words, out["receipt"]))
            if not changed:
                self.assertTrue("Which one?" in out["receipt"] or out["reshow"]
                                or "not applied" in out["receipt"]
                                or "isn't on the last list I sent" in out["receipt"],
                                out["receipt"])


def _ref_collisions(k=3, upto=4000):
    """Pairs of pids whose 4-hex lineage refs coincide."""
    seen, out = {}, []
    for p in range(2, upto):
        h = views.lineage_ref(p)[:4]
        if h in seen and (not out or seen[h] > out[-1][1]):
            out.append((seen[h], p))
            if len(out) == k:
                break
        seen.setdefault(h, p)
    return out


class TestRefCollisionProperty(Base):
    """fix wave D round 9: refs are distinct only within a payee-collision group,
    so two groups can print the same "ref <hex>". A ref reading unions every
    payment printed with that ref: a correction never lands on another payment."""
    def test_a_ref_correction_never_lands_on_another_payment(self):
        rng = random.Random(9)
        self.item("Seed", 1, False)
        pids = {}
        for a, b in _ref_collisions():
            for start, name in ((a, "Alpha%d" % a), (b, "Beta%d" % b)):
                with db.tx(self.conn):
                    self.conn.execute("UPDATE sqlite_sequence SET seq=? WHERE"
                                      " name='projections'", (start - 1,))
                amount = rng.choice((5445, 7000))
                for _ in range(2):                            # twins: a ref is printed
                    self.n += 1
                    self.row(self.n, counterparty=name, amount_minor=amount,
                             booking_date="2026-09-14", value_date="2026-09-14")
                    pid = self.lineage_for(self.n)
                    self.classify(pid, {"software"})
                    self.settle(pid)
                    d = self.doc(counterparty=name, issuer=name, amount_minor=amount,
                                 document_date="2026-09-14", document_number="N%d" % pid)
                    matches.record_match(self.conn, pid=pid, doc_id=d, author="auto",
                                         expected_revision=self.rev(pid),
                                         row_snapshot=self.snapshot(pid), token=self.token,
                                         labels=("guessed",))
                    pids[pid] = name

        def paired(p):
            return self.conn.execute("SELECT 1 FROM match_state WHERE pid=? AND state IN"
                                     " ('matched','proposed')", (p,)).fetchone() is not None
        asked = 0
        for _ in range(10):
            r = views.build_review(self.conn, view="check", quarter="2026-Q3")
            views.mark_rendering_delivered(self.conn, r["render_id"])
            refs = json.loads(self.conn.execute("SELECT scope_json FROM renders WHERE"
                                                " render_id=?", (r["render_id"],))
                              .fetchone()[0]).get("refs", {})
            live = [p for p in pids if paired(p) and any(p in v for v in refs.values())]
            if not live:
                break
            target = rng.choice(live)
            hexes = [h for h, v in refs.items() if target in v]
            words = rng.choice((["ref", hexes[0]], [pids[target], "ref", hexes[0]]))
            before = {p: paired(p) for p in pids}
            out = apply_now(self.conn, "the %s one is wrong" % " ".join(words))
            changed = [p for p in pids if before[p] != paired(p)]
            self.assertLessEqual(set(changed), {target}, (words, out["receipt"]))
            if words[0] == "ref" and len(refs[hexes[0]]) > 1:
                self.assertEqual(changed, [], out["receipt"])
                asked += 1
        self.assertGreater(asked, 0)                  # the generator reached a shared ref
