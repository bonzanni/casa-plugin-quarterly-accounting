# server/package.py
"""The quarterly package — built only when the operator asks (spec
§Packaging). The build freezes its inputs in ONE read transaction and renders
from them deterministically, so the same frozen inputs give the same bytes
and "identical to the package from 14 Oct" is a computed fact. Membership is
the transaction's effective-date quarter, never where a file is stored. Only
`matched` feeds a folder, and routing over (kind, tier) is total and exclusive.
A build never overwrites: its filename is reserved by exclusive create,
widening from the date to minutes, seconds and a short suffix."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets

import amounts
import binding
import dates
import db
import lineage
import reducer as R
import work
import xlsx

MAX_ZIP_BYTES = 20_000_000
COLUMNS = ("date", "amount", "currency", "direction", "counterparty", "vendor", "status",
           "confidence", "expectation_kind", "expectation_tier", "document", "link", "notes")
STATUS = {"matched": "MATCHED", "proposed": "UNCONFIRMED", "open": "MISSING",
          "optional": "OPTIONAL-MISSING", "no-document": "NO-DOCUMENT", "exempt": "NO-DOCUMENT",
          "ineligible": "UNTRACKED"}
xml_safe = xlsx.xml_safe
deterministic_zip = xlsx.zip_files


def route(kind: str, tier: str | None) -> str:
    if tier == "required" and kind in ("invoice", "sales-invoice", "credit-note"):
        return {"invoice": "invoices", "sales-invoice": "sales-invoices",
                "credit-note": "credit-notes"}[kind]
    return "documents"


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s or "").strip("-")[:40] or "unknown"


def doc_filename(doc: dict, used: set, fallback_date: str) -> str:
    amount = f"{doc['amount_minor'] // 100}.{doc['amount_minor'] % 100:02d}" \
        if doc.get("amount_minor") is not None else "0.00"
    base = f"{doc.get('document_date') or fallback_date}_{_slug(doc.get('issuer') or doc.get('counterparty'))}_{amount}"
    # `used` holds casefolded names: Adobe_… and ADOBE_… are one file on a
    # case-insensitive filesystem (Windows/macOS extraction), and one would
    # silently replace the other there.
    name = f"{base}.{doc['ext']}"
    n = 1
    while name.casefold() in used:
        n += 1
        name = f"{base}_{doc['sha256'][:8]}{'' if n == 2 else f'-{n - 1}'}.{doc['ext']}"
    used.add(name.casefold())
    return name


def _place(folder: str, doc: dict, used: set, named: dict, fallback_date: str) -> str:
    """One document gets ONE name per folder, however many lines carry it."""
    key = (folder, doc["doc_id"])
    if key not in named:
        named[key] = f"{folder}/{doc_filename(doc, used, fallback_date)}"
    return named[key]


def _freeze(conn, quarter: str) -> dict:
    start, end = dates.quarter_bounds(quarter)
    conn.execute("BEGIN")                 # one consistent WAL read snapshot for the whole build
    try:
        b = binding.get(conn)
        if b is None:
            raise db.Refusal("no account is bound yet")
        rows = [dict(r) for r in conn.execute("SELECT * FROM bank_rows WHERE account_id=?"
                                              " ORDER BY row_id", (b["account_id"],))]
        in_q = [r for r in rows if start <= (dates.effective_date(r) or "") < end]
        lines = []
        for r in sorted((r for r in in_q if r["state"] == "active"),
                        key=lambda r: (dates.effective_date(r), r["row_id"])):
            a = conn.execute("SELECT pid FROM aliases WHERE row_id=?", (r["row_id"],)).fetchone()
            d = work.describe(conn, lineage.resolve_pid(conn, a[0])) if a else None
            docs = {}
            if d is not None:
                for c in ([d["current"]] if d["current"] else []) + d["candidates"]:
                    row = conn.execute("SELECT * FROM documents WHERE doc_id=?",
                                       (c["document"]["doc_id"],)).fetchone()
                    docs[c["match_id"]] = dict(row)
            lines.append({"row": r, "d": d, "docs": docs})
        history = [r for r in in_q if r["state"] != "active"]
        unmatched = [dict(x) for x in conn.execute(
            "SELECT d.* FROM documents d JOIN document_status s ON s.doc_id=d.doc_id"
            " WHERE s.status='unmatched' AND d.ingest_quarter=? ORDER BY d.doc_id", (quarter,))]
        snap = conn.execute("SELECT bank_through FROM snapshots ORDER BY snapshot_id DESC"
                            " LIMIT 1").fetchone()
        prev = conn.execute(
            "SELECT p.* , d.settled_at FROM packages p JOIN deliveries d ON d.package_id="
            "p.package_id WHERE p.quarter=? AND d.status='delivered' ORDER BY d.settled_at DESC,"
            " p.package_id DESC LIMIT 1", (quarter,)).fetchone()
        # the import every line's freshness was judged against (round E3, Terra S1)
        # every row's date and amount: notes.md names a successor by them, never by id
        facts = {r["row_id"]: {"date": dates.effective_date(r), "amount_minor": r["amount_minor"],
                               "currency": r["currency"]} for r in rows}
        return {"snapshot_id": lineage.latest_import(conn), "facts": facts,
                "binding": dict(b), "lines": lines, "history": history, "unmatched": unmatched,
                "bank_through": snap["bank_through"] if snap else None,
                "prev": dict(prev) if prev else None}
    finally:
        conn.execute("COMMIT")


def _render(frozen: dict, quarter: str, today: str, oversize_note=None) -> tuple:
    files, used, named, manifest_rows, matched_docs = {}, set(), {}, [], []
    missing, unclassified, nice, unresolved_lines, anomalies = [], [], [], [], []
    unread = []
    table = [list(COLUMNS)]
    for ln in frozen["lines"]:
        r, d = ln["row"], ln["d"]
        status = "UNTRACKED" if d is None else STATUS.get(d["status"], "UNTRACKED")
        exp = d["expectation"] if d else {"kind": None, "tier": None}
        # the kind the accountant's copy stands for (fix wave F): the one it ships under,
        # else — shipped unclassified — the last one known. The delivered kind check
        # compares against this, so a row shipped unread is not "categorised differently"
        # when its next read finds the kind it already had.
        known_kind = exp["kind"] or (d["last_known_kind"] if d else None)
        # Classification is reported from the expectation alone (fix wave D, Astra S1):
        # an unknown expectation is UNCLASSIFIED whether or not a pairing is retained
        # (round-42 ruling keeps the pairing and its last known kind verdict; spec
        # ~2742-2744: `UNCLASSIFIED` for a row whose expectation is not yet known). The
        # retained document still ships in its folder and is named in `document`.
        unknown = d is not None and exp["kind"] is None \
            and d["status"] in ("open", "matched", "proposed")
        if unknown:
            status = "UNCLASSIFIED"
        # fix E2: a row not observed at the latest import ships no classification and
        # no document as its own — its kind may have changed (spec §Error handling:
        # packaging ships rather than blocking; the caption says how many)
        stale = d is not None and not d["fresh"] and d["status"] not in ("ineligible", "exempt")
        if stale:
            status, exp = "UNCLASSIFIED", {"kind": None, "tier": None}
        docname, confidence, link, notes, set_aside = "", "", "", [], []
        if not stale and d is not None and d["status"] == "matched" and d["current"]:
            doc = ln["docs"][d["current"]["match_id"]]
            folder = route(doc["kind"], exp["tier"] or "required")
            docname = _place(folder, doc, used, named, dates.effective_date(r))
            files[docname] = documents_bytes(doc)
            matched_docs.append(doc["sha256"])
            confidence = "; ".join(x for x in d["current"]["labels"] if x != "clean")
            if d["current"]["author"] == "operator":
                notes.append("confirmed by the operator")
        elif d is not None and ln["docs"]:
            if unknown and d["status"] == "proposed":
                notes.append("pairing not yet confirmed")
            for mid, doc in sorted(ln["docs"].items()):
                name = _place("unresolved", doc, used, named, dates.effective_date(r))
                files[name] = documents_bytes(doc)
                set_aside.append(name)
                # a row not observed ships its documents set aside, named in its own
                # section: they are not candidates that failed to match (fix wave F)
                if not stale:
                    unresolved_lines.append((d, name))
        if d is not None:
            link = d["link"] or ""
            if stale:
                unread.append((d, set_aside))
            elif status == "MISSING":
                missing.append((d, link))
            elif status == "UNCLASSIFIED":
                unclassified.append((d, docname))
            elif status == "OPTIONAL-MISSING":
                nice.append(d)
            if d["broken_floor"]:
                why = ("its replacements loop back on themselves"
                       if d["broken_floor"].endswith("(a cycle)")
                       else "the row that replaces it is missing")
                anomalies.append(f"{_head(d)}: bank-feed's history is broken ({why}).")
            if d["unprojectable"]:
                anomalies.append(f"{_head(d)}: the bank ledger could not take its tag.")
        vendor = d["counterparty"] if d else (r["counterparty"] or "")
        table.append([dates.effective_date(r) or "", f"{r['amount_minor'] // 100}.{r['amount_minor'] % 100:02d}",
                      r["currency"], r["direction"], r["counterparty"] or "", vendor, status,
                      confidence, exp["kind"] or "", exp["tier"] or "", docname, link,
                      "; ".join(notes)])
        manifest_rows.append({"row_id": r["row_id"], "pid": d["pid"] if d else None,
                              "facts_fp": db.canonical(R.facts_of(r)), "kind": exp["kind"],
                              "last_known_kind": known_kind, "not_reread": bool(stale)})
    buf = io.StringIO(newline="")
    csv.writer(buf, lineterminator="\n").writerows(table)
    files["ledger.csv"] = buf.getvalue().encode("utf-8")
    files["ledger.xlsx"] = xlsx.workbook(table)
    partial = dates.is_partial(quarter, today)
    notes = [f"# {dates.quarter_label(quarter)}", ""]
    notes += ["## Missing required documents", ""]
    for kind in ("invoice", "sales-invoice", "credit-note", "payslip", "statement", "receipt"):
        for d, link in [(d, lk) for d, lk in missing if d["expectation"]["kind"] == kind]:
            notes.append(f"- {_head(d)} — {kind}" + (f" — {link}" if link else ""))
    if not missing:
        notes.append("- none")
    notes += ["", "## Not yet classified", ""]
    notes += [f"- {_head(d)}" + (f" — holds {name}" if name else "")
              for d, name in unclassified] or ["- none"]
    if unread:
        notes += ["", "## Not seen in the last bank check", ""]
        notes += [f"- {_head(d)}" + (f" — holds {', '.join(names)}, set aside until it is "
                                     "seen again" if names else "") for d, names in unread]
    notes += ["", "## Nice to have, not found", ""]
    notes += [f"- {_head(d)} — {d['expectation']['kind']}" for d in nice] or ["- none"]
    notes += ["", "## Unresolved candidates", ""]
    notes += [f"- {_head(d)}: {name}" for d, name in unresolved_lines] or ["- none"]
    notes += ["", "## Documents filed but not matched", ""]
    notes += [f"- {u.get('issuer') or u.get('counterparty') or 'unknown'} "
              f"{u.get('document_number') or ''} ({u['kind']})" for u in frozen["unmatched"]] or ["- none"]
    notes += ["", "## Anomalies", ""]
    notes += [f"- {a}" for a in anomalies] or ["- none"]
    if frozen["history"]:
        notes += ["", "## Bank rows kept as history (not summed)", ""]
        # rows named by date and amount, never by bank-feed's row id (fix wave F)
        notes += [f"- {_row_words(h)} — {h['state']}"
                  + (_successor(frozen["facts"].get(h["superseded_by"]))
                     if h["superseded_by"] else "")
                  for h in frozen["history"]]
    if oversize_note:
        notes += ["", "## Too large to send", ""] + [f"- {x}" for x in oversize_note]
    # The digest covers every file INCLUDING notes.md (round p6, Astra S2: a change
    # visible only in notes.md was captioned "identical"); only the closing line,
    # which carries the build date and the digest itself, is left out.
    # The opening "Partial quarter" line carries the build date too, so it is
    # left out of the digest as well; `partial` itself enters it, so a partial
    # and a closed build of the same rows never compare identical. (Otherwise
    # two unchanged partial builds on different days were captioned "Changed".)
    body = ("\n".join(notes) + "\n").encode("utf-8")
    digest = hashlib.sha256(b"".join(n.encode() + b"\0" + files[n] for n in sorted(files))
                            + b"notes.md\0" + (b"partial\0" if partial else b"closed\0")
                            + body).hexdigest()
    period = "{} to {}".format(*dates.quarter_bounds(quarter))
    notes += ["", f"built {today}, covers {period}, bank data through "
                  f"{frozen['bank_through'] or 'not checked'}, digest {digest[:16]}"]
    if partial:
        notes = [f"Partial quarter — built {today}, before {dates.quarter_label(quarter)} "
                 "ended. Not a filing set.", ""] + notes
    files["notes.md"] = ("\n".join(notes) + "\n").encode("utf-8")
    counts = {"payments": len(frozen["lines"]), "with_documents": len(matched_docs),
              "missing": len(missing), "unclassified": len(unclassified), "unread": len(unread)}
    return (deterministic_zip(files), digest, partial,
            {"rows": manifest_rows, "documents": sorted(matched_docs), "counts": counts})


def _row_words(r) -> str:
    day = dates.effective_date(r)
    return (f"{dates.short_day(day) if day else 'no date'} · "
            f"{amounts.fmt(r['amount_minor'], r['currency'])}")


def _successor(f) -> str:
    if f is None:
        return ", replaced by a row the bank no longer shows"
    day = dates.short_day(f["date"]) if f["date"] else "undated"
    return f", replaced by the {day} {amounts.fmt(f['amount_minor'], f['currency'])} row"


def _head(d) -> str:
    return (f"{d['counterparty']} · {amounts.fmt(d['amount_minor'], d['currency'])} · "
            f"{dates.short_day(d['date']) if d['date'] else 'no date'}")


def documents_bytes(doc: dict) -> bytes:
    return (db.data_dir() / "documents" / doc["sha256"][:2]
            / f"{doc['sha256']}.{doc['ext']}").read_bytes()


def _reserve(stem: str, stamp: str) -> tuple:
    d = db.data_dir() / "packages"
    d.mkdir(parents=True, exist_ok=True)
    hhmm, hhmmss = stamp[11:13] + stamp[14:16], stamp[11:13] + stamp[14:16] + stamp[17:19]
    tries = [stem, f"{stem}-{hhmm}", f"{stem}-{hhmmss}"]
    while True:
        name = (tries.pop(0) if tries else f"{stem}-{hhmmss}-{secrets.token_hex(2)}") + ".zip"
        try:
            fd = os.open(d / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o640)
            return fd, d / name
        except FileExistsError:
            continue


def _caption(quarter, manifest, prev, digest, partial, b, filename, oversize, size,
             check=None) -> str:
    c = manifest["counts"]
    out = [f"Accounting {dates.quarter_label(quarter)} · {c['payments']} payments · "
           f"{c['with_documents']} with documents"]
    if prev is not None:
        when = dates.short_day(prev["settled_at"])
        if prev["digest"] == digest:
            out.append(f"Identical to the package from {when}.")
        else:
            added = len(set(manifest["documents"]) - set(json.loads(prev["manifest_json"])
                                                         .get("documents", [])))
            out.append(f"{added} document{'s' if added != 1 else ''} added since the package "
                       f"from {when}." if added else f"Changed since the package from {when}.")
    tail = [f"{c['missing']} still missing"] if c["missing"] else []
    if c["unclassified"]:
        tail.append(f"{c['unclassified']} not yet classified")
    if tail:
        out.append(", ".join(tail) + " — listed in notes.md.")
    if c.get("unread"):
        out.append(f"{c['unread']} not seen in the last bank check, so shipped unclassified "
                   "— say \"go and check now\", then rebuild.")
    check = check or {}
    if check.get("gmail") == "down":
        out.append("The email search couldn't run, so documents emailed since the last check "
                   "may be missing.")
    if check.get("unfinished"):
        n = check["unfinished"]
        out.append(f"The check couldn't get through {n} payment{'s' if n != 1 else ''} — say "
                   "\"rebuild it\" to try again.")
    if partial:
        out.append("The quarter isn't over yet.")
    if oversize:
        out.append(f"Too large for Telegram ({size / 1e6:.1f} MB; the limit is 20 MB) — kept "
                   "here; notes.md names the largest files.")
    if not b["package_name_announced"]:
        out.append(f'Files are named "{filename}" — say "call the zips <name>" to change that.')
    return "\n".join(out)


def _request_for_build(conn, quarter: str, package_token) -> int:
    """The package request this build is for: the one holding `package_token`,
    open and not yet staged, for this quarter. Refuses otherwise. Returns its id."""
    import passes
    if package_token is None:
        raise db.Refusal("a package is built for a package request: pass the package_token "
                         "end_pass or continue_pass gave you")
    req = conn.execute("SELECT * FROM package_requests WHERE token=?",
                       (int(package_token),)).fetchone()
    if req is None:
        raise db.Refusal("this package request has been taken over by a later turn — stop, "
                         "nothing was written")
    req = passes.check_package_token(conn, req["request_id"], package_token)
    if req["quarter"] != quarter:
        raise db.Refusal(f"this package_token is for the {dates.quarter_label(req['quarter'])} "
                         "package")
    # one request builds one package: a second build would unlink the first from its
    # request, and that package could then be sent outside the send-once rule
    if req["state"] != "snapshot-done":
        raise db.Refusal("this package request already built its package — stage it, or ask "
                         "for the package again for a new one")
    return req["request_id"]


RECHECK = ("the bank was re-read since the check — the check runs again, and the package "
           "follows it; call continue_pass")


def stale_check(conn, request_id) -> bool:
    """Issue #15 (design D2): a request's check describes the import it ran on. Once a
    newer import landed, the request goes back to `queued` (committed on its own) and
    the caller refuses with RECHECK. True when that happened."""
    import passes
    with db.tx(conn):
        req = conn.execute("SELECT * FROM package_requests WHERE request_id=?",
                           (request_id,)).fetchone()
        if req is None or req["state"] not in ("snapshot-done", "built"):
            return False
        if req["checked_snapshot"] is not None and \
                req["checked_snapshot"] == lineage.latest_import(conn):
            return False
        passes.requeue(conn, request_id)
        return True


class _Recheck(Exception):
    def __init__(self, request_id):
        self.request_id = request_id


def build_quarterly_package(conn, quarter: str, package_token=None, *, bound=True) -> dict:
    """Build the quarter's zip for the package request holding `package_token`.
    The token is checked before the custody lock (an early refusal) and again in
    the transaction that registers the zip and links it to the request, so a
    holder rotated while it waited registers nothing and leaves no zip behind.
    bound=False builds outside any request (in-process callers and tests only;
    the tool always binds)."""
    dates.parse_quarter(quarter)
    if conn.in_transaction:
        raise RuntimeError("build_quarterly_package opens its own transactions")
    if bound:
        rid = _request_for_build(conn, quarter, package_token)
        if stale_check(conn, rid):
            raise db.Refusal(RECHECK)
    # The custody lock over documents/ and packages/ (db.custody_lock): a build
    # reads held documents' bytes and writes into packages/, which reset_store
    # erases under that lock. Taken BEFORE the freeze transaction, never inside
    # one (lock order: custody, then SQLite).
    with db.custody_lock():
        return _build(conn, quarter, package_token if bound else None, bound)


def _build(conn, quarter: str, package_token=None, bound=False) -> dict:
    stamp = db.now()
    today = stamp[:10]
    frozen = _freeze(conn, quarter)
    data, digest, partial, manifest = _render(frozen, quarter, today)
    oversize = len(data) > MAX_ZIP_BYTES
    if oversize:
        sizes = sorted(((len(documents_bytes(ln["docs"][ln["d"]["current"]["match_id"]])),
                         _head(ln["d"])) for ln in frozen["lines"]
                        if ln["d"] and ln["d"]["status"] == "matched" and ln["d"]["current"]),
                       reverse=True)[:10]
        data, digest, partial, manifest = _render(
            frozen, quarter, today, [f"{h}: {n / 1e6:.1f} MB" for n, h in sizes])
    b = frozen["binding"]
    check = None
    if bound and package_token is not None:
        row = conn.execute("SELECT check_json FROM package_requests WHERE token=?",
                           (int(package_token),)).fetchone()
        check = json.loads(row["check_json"]) if row is not None and row["check_json"] else None
    stem = f"{b['package_name']}-{quarter}{'-partial' if partial else ''}-{today}"
    fd, path = _reserve(stem, stamp)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    caption = _caption(quarter, manifest, frozen["prev"], digest, partial, b, path.name,
                       oversize, len(data), check)
    try:
        with db.tx(conn):
            # the binding check: in the transaction that registers and links the zip
            request_id = _request_for_build(conn, quarter, package_token) if bound else None
            if request_id is not None and conn.execute(
                    "SELECT checked_snapshot FROM package_requests WHERE request_id=?",
                    (request_id,)).fetchone()[0] != lineage.latest_import(conn):
                raise _Recheck(request_id)
            if lineage.latest_import(conn) != frozen["snapshot_id"]:
                # round E3 (Terra S1): an import landed between the freeze and this
                # commit, so rows judged fresh may no longer be; never register or hand
                # out a zip built from a superseded snapshot
                raise db.Refusal("the bank was re-read while building — build again")
            pkg_id = conn.execute(
                "INSERT INTO packages(quarter, filename, path, built_at, partial, digest, size,"
                " oversize, caption, manifest_json, snapshot_id, request_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (quarter, path.name, str(path), stamp, int(partial), digest, len(data),
                 int(oversize), caption, db.canonical(manifest),
                 frozen["snapshot_id"], request_id)).lastrowid
            if request_id is not None:
                conn.execute("UPDATE package_requests SET package_id=?, state='built',"
                             " updated_at=? WHERE request_id=?", (pkg_id, db.now(), request_id))
    except _Recheck as exc:
        path.unlink(missing_ok=True)
        stale_check(conn, exc.request_id)
        raise db.Refusal(RECHECK)
    except BaseException:
        path.unlink(missing_ok=True)       # an unregistered zip is never left to hand out
        raise
    return {"package_id": pkg_id, "filename": path.name, "path": str(path), "caption": caption,
            "oversize": oversize, "size": len(data), "digest": digest, "partial": partial}
