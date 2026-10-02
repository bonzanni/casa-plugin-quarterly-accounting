# tests/legacy_tools.py
"""The five tools S2 removed from the server (spec §8): begin_pass, record_step,
continue_pass, end_pass and more_work — kept here for the TESTS ONLY.

The delegation-protocol machinery behind them (passes.begin_pass, steps.start/finish/
claim, passes.end_pass, steps.more_work) is still in the server: the job reuses its
parts, and the pre-S2 tests pin its behaviour through these wrappers, exactly as the
0.8.0 tool layer drove it (argument validation, refusals rendered as `refused: …`, the
`clock` on a call carrying a pass_token). `handle` answers these five names itself and
passes every other call to qa_server.handle unchanged; nothing is ever added to
qa_server.TOOLS, so the server's tool list stays the one plugin.json declares."""
from __future__ import annotations

from tests import _base  # noqa: F401  (puts server/ on sys.path)
import db
import passes
import steps
from tools import _bool, _deliverable, _int, _need, _quarter, conn


def t_begin(args):
    _need(args, "trigger")
    return passes.begin_pass(conn(), args["trigger"], args.get("reply"),
                             quarter=_quarter(args), channel=args.get("channel"))


def t_step(args):
    _need(args, "pass_token", "step", "action")
    token, step, action = _int(args, "pass_token"), args["step"], args["action"]
    carry = {"quarter": _quarter(args), "channel": args.get("channel"),
             "doc_ids": args.get("doc_ids"), "report": args.get("report")}
    fin = {"remaining_in_cycle": _int(args, "remaining_in_cycle"),
           "triage_remaining": _int(args, "triage_remaining")}
    if action == "delegated":
        return steps.delegated(conn(), token, step, args.get("delegation_id"))
    if args.get("delegation_id") is not None:
        raise db.Refusal('delegation_id goes with action="delegated"')
    if action == "start":
        for k in ("remaining_in_cycle", "triage_remaining", "stopped", "stopped_by_refusal",
                  "out_of_time", "failed"):
            if args.get(k) is not None:
                raise db.Refusal(f'{k} goes with action="finish"')
        return steps.start(conn(), token, step, carry)
    if action == "finish":
        for k, v in carry.items():
            if v is not None:
                raise db.Refusal({"doc_ids": "doc_ids go with a handover start",
                                  "report": "report goes with a judge start",
                                  "quarter": "a package's quarter goes with begin_pass",
                                  "channel": "a package's channel goes with begin_pass"}[k])
        return steps.finish(conn(), token, step, counts=fin, stopped=args.get("stopped"),
                            failed=_bool(args, "failed", False),
                            by_refusal=_bool(args, "stopped_by_refusal", False),
                            out_of_time=_bool(args, "out_of_time", False))
    raise db.Refusal('action is "start", "delegated" or "finish"')


def t_continue(args):
    return _deliverable("continue_pass", steps.claim(
        conn(), delegation_id=args.get("delegation_id"),
        delegation_status=args.get("delegation_status")))


def t_end(args):
    _need(args, "pass_token", "outcome")
    return _deliverable("end_pass", passes.end_pass(conn(), _int(args, "pass_token"),
                                                     args["outcome"], args.get("report") or {}))


def t_more_work(args):
    _need(args, "pass_token", "calls_made")
    return steps.more_work(conn(), _int(args, "pass_token"), _int(args, "calls_made"))


LEGACY = {"begin_pass": t_begin, "record_step": t_step, "continue_pass": t_continue,
          "end_pass": t_end, "more_work": t_more_work}


def _with_clock(fn, args):
    """tools.register's clock (issue #2), as the removed tools had it."""
    out = fn(args)
    token = args.get("pass_token")
    if isinstance(token, str) and token.strip().isdigit():
        token = int(token)
    if isinstance(out, dict) and isinstance(token, int) and not isinstance(token, bool):
        c = steps.clock(conn(), token)
        if c is not None:
            out["clock"] = c
    return out


def handle(req: dict):
    """qa_server.handle, answering the five removed tools the way it answered them."""
    import qa_server
    params = req.get("params") or {}
    fn = LEGACY.get(params.get("name")) if req.get("method") == "tools/call" else None
    if fn is None:
        return qa_server.handle(req)
    try:
        out, is_error = _with_clock(fn, params.get("arguments") or {}), False
        content = [{"type": "text", "text": qa_server._render(out)}]
    except db.Refusal as exc:
        content, is_error = [{"type": "text", "text": f"refused: {exc}"}], False
    except Exception as exc:                       # surfaced, never swallowed
        content = [{"type": "text", "text": f"error: {type(exc).__name__}: {exc}"}]
        is_error = True
    payload = {"content": content}
    if is_error:
        payload["isError"] = True
    return {"jsonrpc": "2.0", "id": req.get("id"), "result": payload}

