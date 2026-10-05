"""S7 §7.6/§12/§6.1: every deposit this plugin makes, judged by Casa's REAL deposit
(ReferenceStore.deposit: proposal_ok, _message_ok, _file_caption_ok, the S7a filename
predicate), Casa's real message plan (render_paged ≤ MAX_MESSAGE_PAGES) and Casa's real
renderer (tg_richtext.render: the displayed text equals the unescaped composition; no
entity). Runs under Casa's interpreter; never imported by the stdlib suite.

The file's first line is the generator's header; every deposit case it names must be judged
and accepted, the accepted deposits per kind must equal its counts, every kind in
REQUIRED_KINDS must be present, and every deposit must carry either a display expectation or
an explicit `display_skip` reason — the checked count must reach the header's. An empty,
truncated or thinned file fails.

    python3 tests/gen_casa_shapes.py OUT.jsonl && CASA_TREE=… CASA_TESTS=… \\
        <Casa's python> scripts/check_casa_shapes.py OUT.jsonl"""
import importlib.util, json, os, pathlib, subprocess, sys

CASA, CTESTS = os.environ["CASA_TREE"], os.environ["CASA_TESTS"]
sys.path[:0] = [CASA, CTESTS]
ROOT = pathlib.Path(__file__).resolve().parents[1]

import result_broker as rb                                   # noqa: E402
import stored_calls as sc                                    # noqa: E402
import tools as casa_tools                                   # noqa: E402
from channels.tg_richtext import render, render_paged        # noqa: E402
from plugin_grants import result_contract_map                # noqa: E402
from plugin_registry import ResolutionResult, ResolvedPlugin # noqa: E402
from plugin_store import validate_manifest                   # noqa: E402
from specialist_desk import clip, DESK_QUOTE_CHARS          # noqa: E402
from test_proposal_slot import _identity                     # noqa: E402

# the plugin's stdlib copy of the argument grammar, loaded by path: putting the plugin's
# tests/ on sys.path could shadow a module Casa imports lazily
_spec = importlib.util.spec_from_file_location("qa_fakebroker", ROOT / "tests" / "fakebroker.py")
_fb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fb)
our_arguments_ok = _fb.arguments_ok

NAME = "quarterly-accounting"
DISPLAY = "F" * 40                                           # a long display name
casa_tools._display_name_for_role = lambda role: DISPLAY
# the kinds that must be judged at least once; and every capability tool the manifest
# declares must have a deposit judged (read from the manifest: a new one is covered or fails)
REQUIRED_KINDS = ("operator_proposal", "operator_message", "operator_file")


def _display(rec):
    """("check", expected) | ("skip", reason) | (None, None) for a record with neither, or
    with both."""
    has_e, has_s = "display_expect" in rec, "display_skip" in rec
    if has_e and not has_s and isinstance(rec["display_expect"], str):
        return "check", rec["display_expect"]
    if has_s and not has_e and isinstance(rec["display_skip"], str) and rec["display_skip"]:
        return "skip", rec["display_skip"]
    return None, None


def main(path) -> int:
    manifest = validate_manifest(ROOT, NAME)                 # accepts "filename": true
    res = ResolutionResult(registry_valid=True, plugins=[ResolvedPlugin(
        name=NAME, artifact_id="f" * 64, path=str(ROOT), version=manifest["version"],
        manifest=manifest, manifest_name=NAME)])
    cmap = result_contract_map(res)
    by_wire = {e.wire_name: (rt, e) for rt, e in cmap.tools.items()}
    bad, n, kinds, judged, checked, skipped = [], 0, {}, [], 0, 0
    tools_judged = set()
    quotes = []                                              # (n, case, store, quote, rid, raw)
    quoted_kinds = set()
    lines = pathlib.Path(path).read_text().splitlines()
    head = json.loads(lines[0]) if lines else None
    if not isinstance(head, dict) or head.get("case") != "header":
        print("FAIL: no header line: an empty or foreign file")
        return 1
    for n, line in enumerate(lines[1:], 1):
        rec = json.loads(line)
        if rec["case"] == "header":
            bad.append((n, "a second header", ""))
            continue
        if rec["case"] == "stored_call":
            if sc.arguments_ok(rec["arguments"]) != our_arguments_ok(rec["arguments"]):
                bad.append((n, "grammar copy disagrees", rec["tool"]))
            continue
        rt, entry = by_wire[rec["tool"]]
        store = rb.ReferenceStore()
        store.open_call(client_id="c", artifact_id="f" * 64, tool_name=rt,
                        tool_use_id=f"t{n}", identity=_identity(enforcement_role="finance"),
                        provides=tuple(entry.provides), delivers=dict(entry.delivers),
                        contract_map=cmap, protected={}, entry=entry)
        b = rec["body"]
        ref, err = store.deposit(client_id="c", slot=b["slot"], value=b["value"],
                                 caption=b.get("caption"), label=b.get("label"),
                                 kind=b.get("kind"), filename=b.get("filename"))
        if err:
            bad.append((n, err, rec["case"]))
            continue
        dkind = entry.delivers[b["slot"]]
        kinds[dkind] = kinds.get(dkind, 0) + 1
        judged.append(rec["case"])
        tools_judged.add(rec["tool"])
        mode, expect = _display(rec)
        if mode is None:
            bad.append((n, "neither a display expectation nor an explicit skip", rec["case"]))
            continue
        label = rb.post_label("finance")
        if dkind == rb.OPERATOR_MESSAGE:
            pages = render_paged(rb.compose_operator_message(b["value"], label))
            if len(pages) > rb.MAX_MESSAGE_PAGES:
                bad.append((n, f"{len(pages)} pages", rec["case"]))
        text = (json.loads(b["value"])["text"] if dkind == rb.OPERATOR_PROPOSAL
                else b["value"] if dkind == rb.OPERATOR_MESSAGE else None)
        if mode == "skip":
            skipped += 1
        elif text is None:
            bad.append((n, f"a display expectation on a {dkind}, which has no text", rec["case"]))
        else:
            checked += 1
            shown, entities = render(text)
            if shown != expect or entities:
                bad.append((n, "display differs or an entity was produced", rec["case"]))
        if "bind" in rec and (mode == "check" or dkind == rb.OPERATOR_FILE):
            # binding r7 (#44: the package note and the file too): Casa's real quote of the
            # post, clipped as the desk context quotes it (specialist_desk.clip at
            # DESK_QUOTE_CHARS) — a proposal's or a message's text with the label line
            # composed onto it (_post_proposal, compose_operator_message), rendered; a file's
            # caption composed under the label (compose_file_caption), shown as plain text.
            # A post whose display is not promised (a legacy rendering) is not quoted
            if dkind == rb.OPERATOR_FILE:
                raw = rb.compose_file_caption(label, b.get("caption"))
            elif text is not None:
                raw = render(rb.compose_operator_message(text, label))[0]
            else:
                bad.append((n, f"a bind on a {dkind}", rec["case"]))
                continue
            quotes.append((n, rec["case"], str(pathlib.Path(path).parent / (
                pathlib.Path(path).name + ".stores") / f"{rec['bind']['store']}.sqlite"),
                           clip(raw, DESK_QUOTE_CHARS), rec["bind"]["render_id"], raw))
            quoted_kinds.add(dkind)
    # binding r7: every displayed view post's quote binds back to its own rendering, by the
    # plugin's own views.bound_rendering in a process of its own (scripts/bind_quotes.py)
    bound = 0
    if quotes:
        res = subprocess.run([sys.executable, str(ROOT / "scripts" / "bind_quotes.py")],
                             input=json.dumps([{"store": q[2], "quote": q[3]} for q in quotes]),
                             capture_output=True, text=True,
                             env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"})
        got = json.loads(res.stdout) if res.returncode == 0 else []
        if len(got) != len(quotes):
            bad.append((0, "the quote matcher failed", res.stderr[-300:]))
        for (n_, case, _, _, rid, _), g in zip(quotes, got):
            if g.get("render_id") != rid:
                bad.append((n_, f"Casa's quote binds {g} not {rid}", case))
            else:
                bound += 1
    if head.get("binds") != len(quotes) or not head.get("binds"):
        bad.append((0, f"{len(quotes)} quotes built, the header declares "
                       f"{head.get('binds')}", ""))
    for k in REQUIRED_KINDS:                                 # #44: a note and a file too
        if k not in quoted_kinds:
            bad.append((0, f"no {k} post was quoted and bound", ""))
    if not any(len(q[5]) > DESK_QUOTE_CHARS for q in quotes):
        bad.append((0, "no view post longer than Casa's quote cap was quoted", ""))
    # the header's promises: every case judged once, the kinds' counts, the display floor
    want = head.get("cases") or []
    if not want:
        bad.append((0, "the header names no deposit", ""))
    missing = [c for c in want if c not in judged]
    if missing:
        bad.append((0, f"{len(missing)} declared cases not accepted", ", ".join(missing[:5])))
    extra = sorted({c for c in judged if c not in want or judged.count(c) > 1})
    if extra:
        bad.append((0, "cases judged but not declared once", ", ".join(extra[:5])))
    if kinds != head.get("kinds"):
        bad.append((0, f"accepted per kind {kinds} != declared {head.get('kinds')}", ""))
    for k in REQUIRED_KINDS:
        if not kinds.get(k):
            bad.append((0, f"no {k} deposit was judged", ""))
    for tool in sorted(by_wire):
        if by_wire[tool][1].delivers and tool not in tools_judged:
            bad.append((0, f"no {tool} deposit was judged", ""))
    floor = head.get("display_checked")
    if not isinstance(floor, int) or floor < 1 or checked < floor:
        bad.append((0, f"{checked} display checks, the header declares {floor}", ""))
    for b_ in bad:
        print("FAIL", *b_)
    print(f"{'FAIL' if bad else 'OK'}: {n} records "
          f"({', '.join(f'{v} {k}' for k, v in sorted(kinds.items())) or 'no deposit accepted'}"
          f"; display checked {checked}, skipped {skipped}; quotes bound {bound}, clipped "
          f"{sum(1 for q in quotes if len(q[5]) > DESK_QUOTE_CHARS)})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
