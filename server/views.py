# server/views.py
"""The views (spec §"Pull only", §"What the operator can ask for"). The server
renders; Ellen relays the text verbatim. Every count, sum, date and ordering
is computed here.

Membership is fixed first: every managed, un-ended lineage from the
watermark through the end of the view's quarter, by effective date, whatever
its expectation or state. Only then does a view filter and cap what it
prints. The two coverage dates are printed together or not at all.

Rendering is not showing. build_review persists an UNSHOWN rendering;
mark_rendering_delivered, called after the send succeeded, promotes it and
advances the shown-revision pointers that corrections bind to."""
from __future__ import annotations

import json
import textwrap

import amounts
import binding
import dates
import db
import lineage
import work

WIDTH = 64
CAP = 8
TELEGRAM_LIMIT = 4096
VIEWS = ("status", "missing", "check", "rest", "older", "all", "item", "quarter")
KIND_WORD = {"invoice": "invoice", "sales-invoice": "sales invoice", "credit-note": "credit note",
             "payslip": "payslip", "statement": "statement", "receipt": "receipt",
             "other": "document"}
# Machinery the operator never has to learn (spec §"The reversibility ladder").
FORBIDDEN = ("proposed", "conflicted", "revision", "projection", "CAS", "no-ref",
             "partial-search", "recipient?", "acct::", "pid", "match_id",
             "render_id")


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _wrap(line: str) -> list:
    if len(line) <= WIDTH:
        return [line]
    out, cur = [], ""
    for part in line.split(" · "):
        cand = part if not cur else cur + " · " + part
        if len(cand) <= WIDTH:
            cur = cand
            continue
        if cur:
            out.append(cur)
        pieces = textwrap.wrap(part, WIDTH, break_long_words=False, break_on_hyphens=False) or [""]
        out.extend(pieces[:-1])
        cur = pieces[-1]
    if cur:
        out.append(cur)
    return out


def _day(d):
    return dates.short_day(d) if d else "no date"


def _money(d) -> str:
    return amounts.fmt(d["amount_minor"], d["currency"]) if d.get("amount_minor") is not None else "?"


def headline(d: dict, view_quarter=None) -> str:
    parts = [d["counterparty"], _money(d), _day(d["date"])]
    kind = d["expectation"]["kind"]
    if kind and kind not in ("invoice", "none"):
        parts.append(KIND_WORD[kind])
    if d.get("pending"):
        parts.append("pending")
    if view_quarter and d.get("quarter") and d["quarter"] != view_quarter:
        parts.append(dates.quarter_label(d["quarter"]))
    return " · ".join(parts)


def _docname(doc: dict) -> str:
    w = KIND_WORD.get(doc["kind"], "document")
    return f"{w} {doc['number']}" if doc.get("number") else w


def evidence(d: dict) -> list:
    out = []
    cur = d["current"]
    if cur is not None:
        doc = cur["document"]
        name = _docname(doc)
        if "facts-changed" in d["reasons"]:
            out.append("The bank changed this payment after it was paired — still right?")
        if "kind-mismatch" in d["reasons"]:
            need = KIND_WORD.get(d["expectation"]["kind"] or "", "different document")
            out.append(f"Paired with a {KIND_WORD.get(doc['kind'], 'document')}, but this "
                       f"payment now needs a {need}.")
        elif "kind-changed" in d["reasons"]:
            out.append(f"Its category changed since it was paired — still {name}?")
        labels = cur["labels"]
        if "guessed" not in labels:
            # a line that asks for a verdict names what it is asking about (round p7:
            # a no-ref line never named its invoice, yet "all good" confirmed it)
            out.insert(0, f"Paired with {name} ({_day(doc['date'])}).")
        if "guessed" in labels:
            others = "; ".join(cur["runners_up"])
            out.append(f"Picked {name} ({_day(doc['date'])}); {others} also fits." if others
                       else f"Picked {name} among several that fit.")
        if "no-ref" in labels:
            out.append("Repeating equal charges, and no invoice number on both sides.")
        if "partial-search" in labels:
            out.append("The search was cut short, so this may not be the only fit.")
        if "recipient?" in labels:
            out.append(f"{name[0].upper() + name[1:]} names "
                       f"{doc.get('recipient') or 'someone else'}, not the business.")
        if d["status"] == "proposed" and len(out) == 1:
            out.append("Not sure — say if it's wrong.")
    if d["candidates"]:
        out.append("Could be: " + ", ".join(f"{_docname(c['document'])} "
                                            f"({_day(c['document']['date'])})"
                                            for c in d["candidates"]) + ".")
    return out


def _tracked(d):
    # the reducer emits `unclassified`/`conflicted` even for lineages it then
    # reports ended or ineligible: those reasons are not the operator's to act on
    return d["status"] not in ("ended", "ineligible")


def _is_missing(d):
    return d["status"] == "open" and d["expectation"]["kind"] is not None


def _is_unclassified(d):
    return (_tracked(d) and d["expectation"]["kind"] is None
            and "classification-conflict" not in d["reasons"])


def _is_conflict(d):
    return _tracked(d) and "classification-conflict" in d["reasons"]


def _needs_check(d):
    if not _tracked(d):
        return False
    if d["candidates"] or d["status"] == "proposed":
        return True
    return d["status"] == "matched" and d["current"] is not None and d["current"]["labels"] != ["clean"]


def _missing_detail(d) -> list:
    if d["identity_question"]:
        return ["Who was this payment to?"]
    out = []
    if not d["search"].get("last_searched_at"):
        out.append("Not searched yet.")
    elif d["search"].get("incomplete"):
        out.append("Search incomplete — resumes next pass.")
    if d["link"]:
        out.append(d["link"])
    if d["search_state"] == "accepted-missing":
        out.append("No longer chased.")
    return out


def membership(conn, view: str, quarter: str, pid=None) -> list:
    if view == "item":
        return [lineage.resolve_pid(conn, pid)]
    b = binding.get(conn)
    if b is None:
        return []
    end = dates.quarter_bounds(quarter)[1]
    out = []
    for p in lineage.live_pids(conn):
        proj = lineage.projection(conn, p)
        if proj["ended"]:
            continue
        row = lineage.live_row(conn, proj)
        eff = dates.effective_date(row) if row else None
        if eff is not None and b["watermark"] <= eff < end:
            out.append(p)
    return out


def coverage(conn, members) -> str:
    if not members:
        return "No transactions yet."
    snap = conn.execute("SELECT bank_through FROM snapshots ORDER BY snapshot_id DESC"
                        " LIMIT 1").fetchone()
    if snap is None or snap["bank_through"] is None:
        return "Not checked yet."
    obs = [lineage.projection(conn, p)["class_observed_at"] for p in members]
    seen = [o for o in obs if o]
    never = len(obs) - len(seen)
    cls = (f"classification through {dates.short_day(min(seen))}" if seen
           else "classification not checked yet")
    line = f"Bank checked through {dates.short_day(snap['bank_through'])} · {cls}"
    if never and seen:
        line += f" · {never} never checked"
    return line


def _lead(conn):
    setup = binding.check_setup(conn)
    if not setup["can_run"]:
        return [setup["header"], *setup["conditions"], "Nothing else to do until then."], True
    out = []
    gmail = setup["probes"].get("gmail")
    if gmail is not None and not gmail["ok"]:
        out.append("Review incomplete - Gmail unavailable.")
    last = conn.execute("SELECT * FROM passes WHERE ended_at IS NOT NULL ORDER BY ended_at DESC,"
                        " generation DESC LIMIT 1").fetchone()
    if last is not None and last["outcome"] == "interrupted":
        rep = json.loads(last["report_json"] or "{}")
        total, checked = int(rep.get("total", 0)), int(rep.get("checked", 0))
        out += ["Review interrupted.", f"{checked} of {total} new payments checked.",
                f"{total - checked} not checked yet. Saved."]
    return out, False


def _residue_lines(conn) -> tuple:
    lines, ids = [], []
    for r in conn.execute("SELECT * FROM residue WHERE shown_render IS NULL ORDER BY id"):
        d = work.describe(conn, r["pid"]) if r["pid"] else None
        if d is None or d.get("amount_minor") is None:
            continue
        head = headline(d)
        if r["reason"] == "ended":
            freed = conn.execute("SELECT COUNT(*) FROM log WHERE pid=? AND kind='retire' AND"
                                 " cause='row-ended'", (r["pid"],)).fetchone()[0]
            tail = "its document is free again" if freed else "nothing was paired to it"
            lines.append(f"{head} left the bank ledger ({r['detail']}) — {tail}")
        elif r["reason"] == "occupied":
            lines.append(f"{head} — that document is already on another payment")
        elif r["reason"] == "kind-mismatch":
            lines.append(f"{head} — its category changed; its document no longer fits")
        elif r["reason"] == "exempt-doc":
            lines.append(f"{head} — a document turned up for a payment you said needs none")
        elif r["reason"] == "broken-floor":
            lines.append(f"{head} — bank-feed's history for it is broken; left as it was")
        else:
            continue
        ids.append(r["id"])
    return lines, ids


def _shown_pairings(d) -> set:
    ids = {c["match_id"] for c in d["candidates"]}
    if d["current"] is not None:
        ids.add(d["current"]["match_id"])
    return ids


def _section(out, printed, title, ds, detail, cap, q, shows_pairings=False):
    """`printed` maps pid -> the match ids whose proposition the text displays.
    Only those are bound for a later correction (round p1, Astra S1: a missing
    view that bound candidates it never showed let "Adobe is wrong" reject them)."""
    if not ds:
        return
    if len(ds) > cap:
        chosen = sorted(ds, key=lambda d: (-(d["amount_minor"] or 0), d["pid"]))[:cap]
    else:
        chosen = sorted(ds, key=lambda d: (d["date"] or "", d["pid"]))
    out.append("")
    if title:
        out.append(title)
    for i, d in enumerate(chosen):
        if i and title == "MISSING":
            out.append("")
        out.append(headline(d, q))
        out.extend(detail(d))
        printed.setdefault(d["pid"], set())
        if shows_pairings:
            printed[d["pid"]] |= _shown_pairings(d)
    if len(ds) > cap:
        out.append(f'+{len(ds) - cap} more — say "all of them"')


def _compose(conn, view, q, items, members, lead, cap):
    out, printed = list(lead), {}
    extras = {"residue": [], "announce_watermark": False}
    cur = [d for d in items if d["quarter"] == q]
    older_missing = [d for d in items if d["quarter"] and d["quarter"] < q and _is_missing(d)]
    missing = [d for d in cur if _is_missing(d)]
    guessed = [d for d in items if _needs_check(d)]
    nice = [d for d in cur if d["status"] == "optional"]
    uncl = [d for d in cur if _is_unclassified(d)]
    conflicts = [d for d in cur if _is_conflict(d)]
    matched_clean = [d for d in cur if d["status"] == "matched" and not _needs_check(d)]
    delivered_before = conn.execute("SELECT COUNT(*) FROM renders WHERE delivered_at IS NOT NULL"
                                    ).fetchone()[0]
    b = binding.get(conn)

    if view == "item":
        d = items[0]
        out.append(headline(d))
        out.append(_item_sentence(d))
        out.extend(evidence(d))
        if _is_missing(d):
            out.extend(_missing_detail(d))
        printed[d["pid"]] = _shown_pairings(d)
        return out, printed, extras

    titles = {"status": f"Accounting · {dates.quarter_label(q)}",
              "all": f"Accounting · {dates.quarter_label(q)}",
              "missing": f"Missing · {dates.quarter_label(q)}",
              "check": f"To check · {dates.quarter_label(q)}",
              "rest": f"Nice to have · {dates.quarter_label(q)}",
              "older": "Older, still missing",
              "quarter": f"Accounting · {dates.quarter_label(q)}"}
    out.append(titles[view])
    if not delivered_before:
        out.append("First review.")
    cov = coverage(conn, members)
    if view in ("status", "all", "quarter") and members:
        cov += f" · {len(cur)} transactions, {len(missing)} missing a document."
    out.append(cov)
    if b is not None and not b["watermark_announced"] and view in ("status", "all", "missing"):
        start_q = dates.quarter_of(b["watermark"])
        n = dates.parse_quarter(start_q)[1]
        before = f"Q{n - 1}" if n > 1 else "Q4"
        out.append(f"Starting from {dates.quarter_label(start_q)} — say \"start from {before}\" "
                   "to go further back")
        extras["announce_watermark"] = True
    if view in ("status", "all"):
        res, ids = _residue_lines(conn)
        if res:
            out.append("")
            out.extend(res)
            extras["residue"] = ids

    if view in ("status", "all", "missing", "quarter"):
        _section(out, printed, "MISSING", missing, _missing_detail, cap, q)
    if view in ("status", "all"):
        _section(out, printed, "WHAT IS THIS?", conflicts,
                 lambda d: ["The categories on it disagree — which is it?"], cap, q)
        _section(out, printed, "I GUESSED THESE", guessed, evidence, cap, q, shows_pairings=True)
    if view == "check":
        if guessed:
            _section(out, printed, "", guessed, evidence, cap, q, shows_pairings=True)
        else:
            out.append("Nothing to check.")
    if view == "rest":
        _section(out, printed, "", nice, lambda d: [], cap, q)
        if not nice:
            out.append("Nothing else is missing.")
    if view == "older":
        _section(out, printed, "", older_missing, _missing_detail, cap, q)
        if not older_missing:
            out.append("Nothing older is missing.")
    if view == "quarter":
        for pk in conn.execute("SELECT p.filename, d.settled_at FROM packages p JOIN deliveries d"
                               " ON d.package_id=p.package_id WHERE p.quarter=? AND"
                               " d.status='delivered' ORDER BY d.settled_at", (q,)):
            out.append(f"Sent {pk['filename']} on {_day(pk['settled_at'])}.")
        out.append(f'Say "rebuild {dates.quarter_label(q).split()[0]}" for a fresh package.')

    if view in ("status", "all", "missing"):
        if uncl:
            out.append("")
            out.append(f"{len(uncl)} not yet classified — the categories aren't in yet.")
        if nice:
            out.append(f'+{len(nice)} nice-to-have — say "show the rest"')
        if older_missing:
            qs = sorted({dates.quarter_label(d["quarter"]).split()[0] for d in older_missing})
            out.append(f'+{len(older_missing)} older still missing ({", ".join(qs)}) — '
                       'say "show older"')
    if view in ("status", "all"):
        if guessed or matched_clean:
            out.append("")
        if matched_clean:
            out.append("Everything else matched cleanly." if (guessed or missing)
                       else "Everything matched cleanly.")
        if guessed:
            out.append(f'Tell me if one is wrong — "the {guessed[0]["counterparty"]} one is wrong".')
    if view in ("status", "all", "missing") and missing:
        out.append("Download the PDFs and email them to yourself, then")
        out.append('say "check emailed invoices" to file them now.')
    return out, printed, extras


def _item_sentence(d) -> str:
    cur, kind = d["current"], d["expectation"]["kind"]
    word = KIND_WORD.get(kind or "", "document")
    if d["ended"]:
        return "It has left the bank ledger."
    if d["status"] == "matched":
        return f"{_docname(cur['document'])[0].upper() + _docname(cur['document'])[1:]} is filed with it."
    if d["status"] == "proposed":
        return f"Paired with {_docname(cur['document'])}, not confirmed."
    if d["status"] in ("exempt", "no-document"):
        return "Needs no document."
    if d["status"] == "optional":
        return f"No {word} found (nice to have)."
    if d["status"] == "ineligible":
        return "Before the start date; not tracked."
    if kind is None:
        return "Not yet classified, so nothing was searched."
    n = len(d["search"].get("queries", []))
    return f"No {word} yet." + (f" Searched {n} ways." if n else "")


def build_review(conn, view="status", quarter=None, pid=None) -> dict:
    if view not in VIEWS:
        raise db.Refusal(f"view is one of {', '.join(VIEWS)}")
    if view == "item" and pid is None:
        raise db.Refusal("an item view names one transaction")
    q = quarter or dates.quarter_of(db.now()[:10])
    dates.parse_quarter(q)
    lead, stop = _lead(conn)            # may record the pass's gate: outside the read below
    # Compose and persist under ONE write lock, so the revisions recorded are
    # exactly those of the facts the text shows (round p1, Astra S1: a write
    # between composing and recording bound the operator to an unseen document).
    with db.tx(conn):
        members, printed, extras = [], {}, {}
        if stop:
            text = "\n".join(lead)
        else:
            members = membership(conn, view, q, pid)
            items = [work.describe(conn, p) for p in members]
            cap = 10 ** 6 if view in ("all", "item") else CAP
            while True:
                lines, printed, extras = _compose(conn, view, q, items, members, lead, cap)
                text = "\n".join(w for line in lines for w in (_wrap(line) if line else [""]))
                if utf16_len(text) <= TELEGRAM_LIMIT or cap <= 1:
                    break
                cap = CAP if cap > CAP else cap - 1
        rid = f"r{db.next_seq(conn)}"
        conn.execute("INSERT INTO renders(render_id, kind, scope_json, created_at, text,"
                     " membership_json) VALUES (?,?,?,?,?,?)",
                     (rid, view, db.canonical({"quarter": q, "pid": pid, **extras}), db.now(),
                      text, json.dumps(members)))
        for p, shown_ids in printed.items():
            prev = conn.execute("SELECT revision FROM projections WHERE pid=?", (p,)).fetchone()[0]
            mrevs = {str(r[0]): r[1] for r in conn.execute(
                "SELECT match_id, revision FROM match_state WHERE pid=?", (p,))
                if r[0] in shown_ids}
            conn.execute("INSERT INTO render_items(render_id, pid, projection_revision,"
                         " match_revisions_json) VALUES (?,?,?,?)",
                         (rid, p, prev, db.canonical(mrevs)))
    return {"render_id": rid, "text": text, "printed": len(printed)}


def render_items(conn, render_id) -> list:
    return [r[0] for r in conn.execute("SELECT pid FROM render_items WHERE render_id=?",
                                       (render_id,))]


def mark_rendering_delivered(conn, render_id: str) -> dict:
    with db.tx(conn):
        r = conn.execute("SELECT * FROM renders WHERE render_id=?", (render_id,)).fetchone()
        if r is None:
            raise db.Refusal(f"there is no rendering {render_id}")
        if r["delivered_at"] is not None:
            return {"render_id": render_id, "already": True}
        now = db.now()
        conn.execute("UPDATE renders SET delivered_at=? WHERE render_id=?", (now, render_id))
        for it in conn.execute("SELECT * FROM render_items WHERE render_id=?", (render_id,)):
            conn.execute("INSERT OR REPLACE INTO shown(pid, render_id, projection_revision,"
                         " match_revisions_json, delivered_at) VALUES (?,?,?,?,?)",
                         (it["pid"], render_id, it["projection_revision"],
                          it["match_revisions_json"], now))
        scope = json.loads(r["scope_json"])
        for rid_ in scope.get("residue", []):
            conn.execute("UPDATE residue SET shown_render=? WHERE id=?", (render_id, rid_))
        if scope.get("announce_watermark"):
            conn.execute("UPDATE binding SET watermark_announced=1 WHERE id=1")
        for a in scope.get("alerts", []):
            conn.execute("UPDATE alerts SET sent_at=?, render_id=? WHERE alert_id=?",
                         (now, render_id, a))
        if scope.get("announce_package_name"):
            conn.execute("UPDATE binding SET package_name_announced=1 WHERE id=1")
        return {"render_id": render_id, "delivered_at": now}
