"""Deposit an operator-facing value with Casa's broker (S7 §2/§3).

A posting tool does not return its operator-facing text, proposal or file path.
During the call it DEPOSITS the value in one of its declared slots with Casa's
broker, over the internal Unix socket named by `$CASA_BROKER_SOCKET`, as the client
named by `$CASA_BROKER_CLIENT` (Casa puts both in this server's environment).
Casa answers with a reference, which the tool returns in the slot's field; after
the result passes Casa's structural check Casa posts the deposited value itself.

Protocol (Casa `result_broker.py`):

    POST /internal/broker/deposit
         {"client", "slot", "value", "caption"?, "label"?, "kind"?, "filename"?, "key"?}
      -> {"reference": "casa-cap-<32 hex>"} | {"error": "<code>"}

Only the members given are sent. `value` is a string (a proposal travels as a
JSON string; a file as its path). Nothing Casa sends back is echoed: a reference
must have Casa's exact shape, and an error must look like one of Casa's codes, or
it is reported by a fixed label. `key` (Casa #1312): a delivery Casa already made under
that key is not sent again — Casa answers the original receipt (an older Casa ignores the
member). Standard library only.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import socket

ENV_CLIENT = "CASA_BROKER_CLIENT"
ENV_SOCKET = "CASA_BROKER_SOCKET"
REFERENCE_RE = re.compile(r"^casa-cap-[0-9a-f]{32}$")
DEPOSIT_ROUTE = "/internal/broker/deposit"
TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 64 * 1024
_CODE_RE = re.compile(r"^[a-z][a-z_]{0,39}$")


class DepositFailed(Exception):
    """The value was not accepted for delivery. `code` is a Casa error code or
    one of this module's own fixed labels, never text Casa or the transport
    wrote."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=TIMEOUT_S)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT_S)
        try:
            sock.connect(self._path)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def deposit(slot: str, value: str, *, caption=None, label=None, kind=None,
            filename=None, key=None) -> str:
    """Deposit `value` in `slot` and return Casa's reference (casa:result_broker.py
    `build_broker_deposit_handler`; S3 `kind`, S5 proposals as a JSON string, S7a
    `filename`). Only the members given are sent. Raises DepositFailed with:
    `broker_env_missing`, `broker_unreachable:<Class>`, `broker_bad_response`, Casa's own
    code, or `unrecognized_error` — never text Casa or the transport wrote."""
    path = os.environ.get(ENV_SOCKET, "")
    client = os.environ.get(ENV_CLIENT, "")
    if not path or not client:
        raise DepositFailed("broker_env_missing")
    body = {"client": client, "slot": slot, "value": value}
    for k, v in (("caption", caption), ("label", label), ("kind", kind),
                 ("filename", filename), ("key", key)):
        if v is not None:
            body[k] = v
    conn = _UnixHTTP(path)
    try:
        conn.request("POST", DEPOSIT_ROUTE,
                     body=json.dumps(body).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        raw = conn.getresponse().read(MAX_RESPONSE_BYTES + 1)
    except Exception as exc:                     # noqa: BLE001 — the class only
        raise DepositFailed("broker_unreachable:%s" % type(exc).__name__) from None
    finally:
        conn.close()
    try:
        answer = json.loads(raw.decode("utf-8")) \
            if len(raw) <= MAX_RESPONSE_BYTES else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise DepositFailed("broker_bad_response")
    reference = answer.get("reference")
    if isinstance(reference, str) and REFERENCE_RE.fullmatch(reference):
        return reference
    error = answer.get("error")
    if isinstance(error, str) and _CODE_RE.fullmatch(error):
        raise DepositFailed(error)
    raise DepositFailed("unrecognized_error")
