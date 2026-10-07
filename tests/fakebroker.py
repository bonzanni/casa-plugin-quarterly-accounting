"""An in-process stand-in for Casa's broker deposit route: a Unix-socket HTTP server on a
thread. It records every deposit body and answers a fresh reference, or the error code set
in `refuse`. It does NOT validate: Casa's validators run in the Task 14 gate.
`honour_keys` (Casa #1312): a deposit whose `key` was delivered before sends nothing —
the original reference comes back; `sent` lists only what was sent."""
import http.server, json, os, secrets, socketserver, tempfile, threading


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class FakeBroker:
    def __init__(self):
        self.deposits, self.refuse = [], None
        self.honour_keys, self.sent, self._keys = False, [], {}

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        path = os.path.join(self._dir.name, "broker.sock")
        broker = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                broker.deposits.append(body)
                key = body.get("key") if broker.honour_keys else None
                if broker.refuse:
                    answer = {"error": broker.refuse}
                elif key in broker._keys:
                    answer = {"reference": broker._keys[key], "repeat": True}
                else:
                    answer = {"reference": "casa-cap-" + secrets.token_hex(16)}
                    broker.sent.append(body)
                    if key is not None:
                        broker._keys[key] = answer["reference"]
                raw = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

            def address_string(self):
                return "unix"
        self._srv = _Server(path, H)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()
        self._env = {k: os.environ.get(k) for k in ("CASA_BROKER_SOCKET", "CASA_BROKER_CLIENT")}
        os.environ["CASA_BROKER_SOCKET"], os.environ["CASA_BROKER_CLIENT"] = path, "client-1"
        return self

    def __exit__(self, *exc):
        self._srv.shutdown()
        self._srv.server_close()
        self._dir.cleanup()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def proposal(self, i=-1) -> dict:
        """The i-th deposit's value, parsed as a proposal."""
        return json.loads(self.deposits[i]["value"])


# casa:stored_calls.py arguments_ok at bcebd66b, stdlib only (the `is_reference` check is
# the broker's _REF_RE) — Task 14's gate asserts it agrees with Casa's on every recorded call
import re
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_REF_RE = re.compile(r"^casa-cap-[0-9a-f]{32}$")


def _value_ok(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return None if -(2 ** 53 - 1) <= value <= 2 ** 53 - 1 else "unsafe_integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        if len(value) > 2000:
            return "string_length"
        if "<" in value or ">" in value:
            return "angle_bracket"
        if _REF_RE.match(value):
            return "reference"
        return None
    if isinstance(value, list):
        for item in value:
            why = _value_ok(item)
            if why is not None:
                return why
        return None
    if isinstance(value, dict):
        return _object_ok(value)
    return "type"


def _object_ok(obj):
    for key, value in obj.items():
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            return "key"
        why = _value_ok(value)
        if why is not None:
            return why
    return None


def arguments_ok(arguments):
    if not isinstance(arguments, dict):
        return "not_object"
    why = _object_ok(arguments)
    if why is not None:
        return why
    canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(canonical.encode("utf-8")) > 4096:
        return "size"
    if json.loads(canonical) != arguments or \
            json.dumps(json.loads(canonical), sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) != canonical:
        return "round_trip"
    return None
