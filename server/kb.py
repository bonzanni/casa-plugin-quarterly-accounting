"""Counterparty knowledge base and the expectation mapping's overrides
(spec §"Document expectation", "Counterparty KB"). Read side here; the
write side (upsert_counterparty, set_expectation) is Task 10.

A pattern is the counterparty text exactly as bank-feed shows it
(`BCK*ZAPIER`); matching is exact after case and whitespace normalisation —
the `*` is part of the bank's text, never a wildcard, and there is no fuzzy
matching anywhere in this plugin."""
from __future__ import annotations

import json
import re

import authority
import db
import expectation as ex

_TAG = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def norm(s) -> str:
    return _spaced(s).lower()


def _spaced(s) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def counterparty_for(conn, bank_counterparty):
    text = norm(bank_counterparty)
    if not text:
        return None
    for r in conn.execute("SELECT * FROM counterparties ORDER BY cp_id"):
        if text == norm(r["name"]) or text in {norm(p) for p in json.loads(r["patterns_json"])}:
            return r
    return None


def override_of(cp):
    if cp is None or cp["exp_kind"] is None:
        return None
    return (cp["exp_kind"], cp["exp_tier"] if cp["exp_kind"] != "none" else None)


def is_portal(cp) -> bool:
    return cp is not None and cp["source"] == "portal"


def parse_scope(scope: str) -> frozenset:
    return frozenset(t.strip().lower() for t in (scope or "").split(",") if t.strip())


def chain_overrides(conn) -> list:
    return [(frozenset(json.loads(r["rows_json"])), frozenset(json.loads(r["key_json"])),
             r["kind"], r["tier"])
            for r in conn.execute("SELECT * FROM chain_overrides ORDER BY scope")]


def display_name(conn, bank_counterparty) -> str:
    cp = counterparty_for(conn, bank_counterparty)
    return cp["name"] if cp is not None else (bank_counterparty or "Unknown payee")


def given_name(cp, bank_texts=()) -> bool:
    """The entry's name is one someone gave, not a bank text it stands for: the operator
    renamed it (#84, named_at, r2: recorded, never inferred from its capitals), or no text it
    stands for reads the same, case aside (0.11.12's rule, for every other entry)."""
    if cp is None:
        return False
    if cp["named_at"] is not None:
        return True
    texts = [t for t in (*bank_texts, *json.loads(cp["patterns_json"])) if t]
    return norm(cp["name"]) not in {norm(t) for t in texts}


def readable_name(conn, bank_counterparty, cp=None, pid=None) -> str:
    """Issue #59 (1): the name a person reads for a payee — the KB entry's name when it is a
    name someone gave (not the bank's own text), else the issuer printed on the latest
    document matched to another of its payments (never payment `pid`'s own pairing, so a
    pairing never names the payment it is judged against), else display_name. Display
    only: grouping, rule scopes and reply matching keep display_name."""
    cp = cp if cp is not None else counterparty_for(conn, bank_counterparty)
    texts = {norm(bank_counterparty)} - {""}
    if cp is not None:
        if given_name(cp, [bank_counterparty]):
            return cp["name"]
        texts |= {norm(p) for p in json.loads(cp["patterns_json"])} | {norm(cp["name"])}
    if texts:
        r = conn.execute(
            "SELECT d.issuer FROM match_state m JOIN documents d ON d.doc_id=m.doc_id"
            " JOIN projections p ON p.pid=m.pid JOIN bank_rows b ON b.row_id=p.dest_row_id"
            " WHERE m.state='matched' AND trim(coalesce(d.issuer, ''))<>'' AND m.pid<>? AND"
            " lower(trim(b.counterparty)) IN (%s) ORDER BY m.match_id DESC LIMIT 1"
            % ",".join("?" * len(texts)),
            (pid if pid is not None else -1, *sorted(texts))).fetchone()
        if r is not None:
            return r[0].strip()
    return cp["name"] if cp is not None else (bank_counterparty or "Unknown payee")


def _entry(conn, name):
    for r in conn.execute("SELECT * FROM counterparties"):
        if norm(r["name"]) == norm(name):
            return r
    return None


def get_counterparty(conn, text):
    r = counterparty_for(conn, text)
    if r is None:
        return None
    out = dict(r)
    out["patterns"] = json.loads(out.pop("patterns_json"))
    return out


LINK_MAX = 500              # issue #3: every stored field a listing carries is bounded
HINT_MAX = 200             # the learned search hint, each of its two values (§2.2 step 5)


def upsert_counterparty(conn, name, *, patterns=(), source=None, document_link=None,
                        link_note=None, search_hint=None, notes=None, window_days=None,
                        hint_sender=None, hint_subject=None, new_name=None, token=None) -> dict:
    import passes
    with db.tx(conn):
        passes.check_token(conn, token)
        return upsert_in_tx(conn, name, patterns=patterns, source=source,
                            document_link=document_link, link_note=link_note,
                            search_hint=search_hint, notes=notes, window_days=window_days,
                            hint_sender=hint_sender, hint_subject=hint_subject,
                            new_name=new_name)


def upsert_in_tx(conn, name, *, patterns=(), source=None, document_link=None, link_note=None,
                 search_hint=None, notes=None, window_days=None, hint_sender=None,
                 hint_subject=None, new_name=None) -> dict:
    """The upsert inside the caller's transaction (a reading's identity clause
    checks the shown revision in the same transaction as this write). hint_sender and
    hint_subject are the vendor's learned search hint (design rev 17 §2.2 step 5, §3).
    new_name (#84, ruling on #77 item 3): the operator renames the vendor `name` (its current
    name or one of its bank texts). The same entry keeps everything it holds and its old name
    stays one of its bank texts, so every text that found it still finds it; with no entry,
    one is made under new_name for the bank text `name`."""
    import lineage
    if not (name or "").strip():
        raise db.Refusal("a counterparty needs a name")
    if new_name is not None:
        if not isinstance(new_name, str) or not new_name.strip():
            raise db.Refusal("new_name is the name to show for this vendor")
        current = _entry(conn, name) or counterparty_for(conn, name)
        patterns = [*patterns, current["name"] if current is not None else name]
        # r1 (Astra + Terra S1): checked before the entry is looked up by its new name, also
        # when there is no entry yet — a rename never lands on another vendor's entry
        _refuse_shared_bank_text(conn, current, [new_name.strip(), *patterns, *(
            json.loads(current["patterns_json"]) if current is not None else [])])
        _refuse_unlearned_vendor(conn, current, name, new_name)
        if current is not None:
            conn.execute("UPDATE counterparties SET name=? WHERE cp_id=?",
                         (new_name.strip(), current["cp_id"]))
        # #87: the sentence the desk says for one rename, in the operator's words
        line = f"{(current['name'] if current is not None else name).strip()} is now called " \
               f"{new_name.strip()}"
        line += "" if line.endswith(".") else "."
        name = new_name
    if source not in (None, "email", "portal"):
        raise db.Refusal("source is 'email' or 'portal'")
    if document_link is not None and len(document_link) > LINK_MAX:
        raise db.Refusal(f"a document link is at most {LINK_MAX} characters")
    for hint in (hint_sender, hint_subject):
        if hint is not None and (not isinstance(hint, str) or len(hint) > HINT_MAX):
            raise db.Refusal("a learned hint is a sender address and a subject pattern, each "
                             f"at most {HINT_MAX} characters")
    if window_days is not None and not (1 <= int(window_days) <= 60):
        raise db.Refusal("window_days is between 1 and 60")
    existing = _entry(conn, name)
    held = json.loads(existing["patterns_json"]) if existing is not None else []
    merged = sorted(set(held) | {p.strip() for p in patterns})
    if new_name is not None:
        # d1 (Astra S2): a pattern equal to the new name (a name given earlier, renamed back
        # to) would make the name read as a bank text; the name itself still resolves it
        merged = [p for p in merged if _spaced(p) != _spaced(name)]
    # Every bank text resolves to at most one entry (fix wave B, Astra S1; round
    # B2, Terra S1): ALL of this upsert's names and patterns — the ones it adds and
    # the ones the entry already holds — are checked against every OTHER entry's,
    # on create and on update. A collision would leave a ruling stored on one
    # entry that lookup never reaches. Refused, not merged: the operator names the
    # owner, and set_expectation by that bank text already lands on it.
    _refuse_shared_bank_text(conn, existing, [name.strip()] + merged)
    if existing is None:
        conn.execute("INSERT INTO counterparties(name, patterns_json, updated_at)"
                     " VALUES (?, '[]', ?)", (name.strip(), db.now()))
        existing = _entry(conn, name)
    fields = {"patterns_json": json.dumps(merged), "source": source,
              "document_link": document_link, "link_note": link_note,
              "search_hint": search_hint, "notes": notes, "window_days": window_days,
              "hint_sender": hint_sender, "hint_subject": hint_subject}
    sets = {k: v for k, v in fields.items() if v is not None}
    sets["updated_at"] = db.now()
    if new_name is not None:
        sets["named_at"] = sets["updated_at"]
    conn.execute("UPDATE counterparties SET %s WHERE cp_id=?"
                 % ", ".join(f"{k}=?" for k in sets), (*sets.values(), existing["cp_id"]))
    lineage.settle_all(conn)
    out = get_counterparty(conn, name) or {}
    if new_name is not None:
        out["line"] = line
    return out


# #90: a merge moves each of these whole onto the kept entry when it holds none of its own
MERGED_FIELDS = (("exp_kind", "exp_tier", "exp_author"),
                 ("source", "document_link", "link_note"),
                 ("hint_sender", "hint_subject"), ("search_hint",), ("notes",))


def merge_in_tx(conn, x, y) -> None:
    """#90: vendor x joins vendor y (work.vendors entries, or y a KB entry with no payment),
    inside the caller's transaction. Every text x stood for (its bank texts, its entry's name
    and patterns) becomes one of y's bank texts, so its payments, filed documents, cards' and
    a running job's vendor names all resolve to y's entry (same_vendor, counterparty_for).
    x's entry goes; its rules fill what y's entry lacks (y's own win; the wider search
    window); y gets an entry when it had none, and its name is pinned (named_at)."""
    import lineage
    ex, ey = x["cp"], y["cp"]
    texts = [*x["texts"], x["name"]]
    if ex is not None:
        texts += [ex["name"], *json.loads(ex["patterns_json"])]
        conn.execute("DELETE FROM counterparties WHERE cp_id=?", (ex["cp_id"],))
    if ey is None:
        conn.execute("INSERT INTO counterparties(name, patterns_json, updated_at)"
                     " VALUES (?, '[]', ?)", (y["name"].strip(), db.now()))
        ey = _entry(conn, y["name"])
    held = {}
    for t in [*json.loads(ey["patterns_json"]), *texts]:
        if norm(t) and norm(t) != norm(ey["name"]):
            held.setdefault(norm(t), _spaced(t))
    patterns = sorted(held.values())
    _refuse_shared_bank_text(conn, ey, [ey["name"], *patterns])
    sets = {"patterns_json": json.dumps(patterns), "updated_at": db.now()}
    # d1 (Astra S2): y's name is the merged vendor's from now on — pinned, so its cards never
    # show an invoice name of x's instead (readable_name)
    sets["named_at"] = ey["named_at"] or sets["updated_at"]
    if ex is not None:
        for group in MERGED_FIELDS:
            if all(ey[f] is None for f in group) and any(ex[f] is not None for f in group):
                sets.update({f: ex[f] for f in group})
        sets["window_days"] = max(ex["window_days"], ey["window_days"])
    conn.execute("UPDATE counterparties SET %s WHERE cp_id=?"
                 % ", ".join(f"{k}=?" for k in sets), (*sets.values(), ey["cp_id"]))
    lineage.settle_all(conn)


def same_vendor(conn, a, b) -> bool:
    """#84: two vendor names are one vendor — the same text, or texts the knowledge base
    resolves to one entry. A rename changes the display name (loop.vendor_of) while a filed
    document, a running job's work list and a posted card keep the name they were given; the
    renamed entry keeps its old name as a bank text, so both still resolve to it."""
    if norm(a) == norm(b):
        return True
    ea, eb = counterparty_for(conn, a), counterparty_for(conn, b)
    return ea is not None and eb is not None and ea["cp_id"] == eb["cp_id"]


def _texts(name, patterns) -> set:
    return {t for t in (norm(name), *(norm(p) for p in patterns)) if t}


def _refuse_unlearned_vendor(conn, entry, name, new_name) -> None:
    """r2 (Astra S1): a new name that is the bank text of another vendor no entry knows yet
    would make this entry take that vendor's payments — refused like a shared bank text."""
    import lineage
    import views
    mine = {norm(name)} | ({norm(entry["name"])} | {norm(p) for p in json.loads(
        entry["patterns_json"])} if entry is not None else set())
    target = norm(new_name)
    if target in mine:
        return
    for pid in lineage.live_pids(conn):
        p = lineage.projection(conn, pid)
        row = lineage.live_row(conn, p) or json.loads(p["last_facts_json"] or "{}")
        text = row.get("counterparty")
        if norm(text) == target and counterparty_for(conn, text) is None:
            raise db.Refusal(f"'{views.field(text.strip())}' is another vendor's bank text; "
                             "give this one another name")


def _refuse_shared_bank_text(conn, entry, texts) -> None:
    mine = {norm(t): t for t in texts if norm(t)}
    for r in conn.execute("SELECT * FROM counterparties ORDER BY cp_id"):
        if entry is not None and r["cp_id"] == entry["cp_id"]:
            continue
        shared = set(mine) & _texts(r["name"], json.loads(r["patterns_json"]))
        if shared:
            # the names are printed escaped (S7 INV-S7-7): this refusal reaches the
            # operator's receipt through reply._say
            import views
            owner = views.field(r["name"])
            raise db.Refusal(f"the bank text '{views.field(mine[min(shared)])}' already "
                             f"belongs to {owner}; change {owner} instead (to rename it, "
                             f"name={owner} with new_name), or give this counterparty another "
                             "name")


def _require_seen_render(conn, render_id) -> None:
    """An operator rule's provenance is a SEEN rendering — delivered, or posted by show_view
    (db.seen_render; binding §2 #11: the reading's one bound rendering R)."""
    r = conn.execute("SELECT delivered_at, posted_seq FROM renders WHERE render_id=?",
                     (render_id,)).fetchone() if render_id else None
    if not db.seen_render(r):
        raise db.Refusal("an operator decision must come from a view the operator was shown "
                         "(a delivered or posted render_id)")


def set_expectation(conn, *, scope_type, scope, kind, tier=None, author, render_id=None,
                    token=None) -> dict:
    import passes
    if author == "operator":
        # S7 §8.1: the operator's expectation is set by a tap (set_expectation_in_tx under
        # a grant), never by a tool call
        raise db.Refusal(authority.TAP_ONLY)
    if author != "specialist":
        raise db.Refusal("author is 'specialist'")
    if scope_type not in ("counterparty", "chain"):
        raise db.Refusal("scope_type is 'counterparty' or 'chain'")
    if kind != "default":
        if kind not in ex.KINDS + ("none",):
            raise db.Refusal(f"kind is one of {', '.join(ex.KINDS)}, 'none' or 'default'")
        if kind == "none":
            tier = None
        elif tier not in ex.TIERS:
            raise db.Refusal("a document kind needs a tier: 'required' or 'optional'")
    with db.tx(conn):
        passes.check_token(conn, token)
        return set_expectation_in_tx(conn, scope_type=scope_type, scope=scope, kind=kind,
                                     tier=tier, author=author, render_id=render_id)


def set_expectation_in_tx(conn, *, scope_type, scope, kind, tier=None, author,
                          render_id=None, grant=None) -> dict:
    """The override inside the caller's transaction, so a reading can check, in that same
    transaction, that every payment it changes was shown. An operator author needs a tap's
    grant (S7 §8.1)."""
    import lineage
    if kind == "none":
        tier = None
    if author == "operator":
        authority.require(conn, grant)
        _require_seen_render(conn, render_id)
    if scope_type == "chain":
        if author != "operator":
            raise db.Refusal("a class-level expectation is the operator's to set")
        tags = parse_scope(scope)
        if not tags or not all(_TAG.match(t) for t in tags):
            raise db.Refusal("a chain is comma-separated classification tags")
        norm = ex.normalize_scope(tags)
        if norm is None:
            raise db.Refusal("those tags select different rows of the mapping; name one chain")
        rows, okey = norm
        key = ", ".join(sorted(tags))
        # A chain override replaces any existing one whose normalized (rows, key)
        # is the same, regardless of the literal scope text it was set under
        # (carried ruling, Task 4 review): "refund" and "income, refund" both
        # normalize to the same (rows, key), and derive() refuses to guess
        # between two overrides that collide there — so the store never lets
        # two such overrides coexist; the operator's later ruling wins.
        for r in conn.execute("SELECT scope, rows_json, key_json FROM chain_overrides"):
            if (frozenset(json.loads(r["rows_json"])), frozenset(json.loads(r["key_json"]))) \
                    == (rows, okey):
                conn.execute("DELETE FROM chain_overrides WHERE scope=?", (r["scope"],))
        if kind != "default":
            conn.execute("INSERT INTO chain_overrides(scope, kind, tier, rows_json,"
                         " key_json, author, set_at) VALUES (?,?,?,?,?,?,?)",
                         (key, kind, tier, json.dumps(sorted(rows)), json.dumps(sorted(okey)),
                          author, db.now()))
    else:
        e = _entry(conn, scope) or counterparty_for(conn, scope)
        if e is None:
            conn.execute("INSERT INTO counterparties(name, patterns_json, updated_at)"
                         " VALUES (?, '[]', ?)", (scope.strip(), db.now()))
            e = _entry(conn, scope)
        if author == "specialist" and e["exp_author"] == "operator":
            # the operator's ruling on this payee ("no invoices ever for X") is theirs
            # to change; a specialist's write never silently replaces it (fix wave F)
            import views
            raise db.Refusal(f"the operator set what {views.field(e['name'])} needs; only "
                             "the operator changes it — nothing was changed")
        if kind == "default":
            conn.execute("UPDATE counterparties SET exp_kind=NULL, exp_tier=NULL,"
                         " exp_author=NULL, updated_at=? WHERE cp_id=?", (db.now(), e["cp_id"]))
        else:
            conn.execute("UPDATE counterparties SET exp_kind=?, exp_tier=?, exp_author=?,"
                         " updated_at=? WHERE cp_id=?", (kind, tier, author, db.now(),
                                                         e["cp_id"]))
    lineage.settle_all(conn)
    return {"scope_type": scope_type, "scope": scope, "kind": kind, "tier": tier}
