"""A fake signal-cli-rest-api, plugged into SignalSink as its HTTP transport.

Answers the way version 0.100 of the real API did when probed: JSON bodies,
{"error": ...} with status 400, a plain-text 404 for unknown paths, and a
timestamp (as a string) for each message sent.
"""
import json
import urllib.error

URL = "http://signal-api:8080"
SENDER = "+15550000000"
FIRST_TS = 1700000000001  # timestamp of the first message the fake "sends"


class FakeSignalAPI:
    def __init__(self):
        self.accounts = [{"number": SENDER, "aci": "aci-1"}]
        self.groups = [{"id": "group.AAAA", "name": "Copy"}]
        self.sent = []        # /v2/send payloads that went through, decoded
        self.requests = []    # (method, path) of every request
        self.down = False     # nothing listening
        self.fail = []        # (status, body) answers for the next sends, in order
        self._ts = FIRST_TS - 1

    def __call__(self, method, url, body, headers, timeout):
        path = url[len(URL):]
        self.requests.append((method, path))
        if self.down:
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        if method == "GET" and path == "/v1/accounts":
            return 200, json.dumps(self.accounts).encode()
        if method == "GET" and path.startswith("/v1/groups/"):
            return 200, json.dumps(self.groups).encode()
        if method == "POST" and path == "/v2/send":
            raw = body.read()
            assert int(headers["Content-Length"]) == len(raw)
            assert headers["Content-Type"] == "application/json"
            if self.fail:
                status, answer = self.fail.pop(0)
                return status, answer if isinstance(answer, bytes) else json.dumps(answer).encode()
            self.sent.append(json.loads(raw))
            self._ts += 1
            return 201, json.dumps({"timestamp": str(self._ts)}).encode()
        return 404, b"404 page not found"


def make_sink(api=None):
    """(SignalSink talking to a fake API, that API). Retries don't wait."""
    from src.adapters.signal.sink import SignalSink
    api = api or FakeSignalAPI()
    sink = SignalSink(URL, SENDER, transport=api)
    sink.retry_delay = 0
    return sink, api
