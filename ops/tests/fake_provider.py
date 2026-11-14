"""A scripted OpenAI-compatible server on 127.0.0.1 for tests; never touches the network."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

Reply = Tuple[int, Dict[str, str], Any]   # status, headers, JSON-serialisable body (or bytes)

# Built at runtime so no key-shaped literal sits in a tracked file.
FAKE_GOOGLE_KEY = "AIza" + "Fake0Test1Key2Only3Not4A5Real6Key7X"[:35]


def completion(content: Optional[str] = "", *, finish_reason: str = "stop", tool_calls: Optional[list] = None,
               usage: Optional[dict] = None) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    body: Dict[str, Any] = {"choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]}
    body["usage"] = usage if usage is not None else {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return body


class FakeProvider:
    """Replies come from `script` (a list consumed in order) or, when it is empty, from `responder`."""

    def __init__(self, responder: Optional[Callable[[str, str, Dict[str, Any]], Reply]] = None):
        self.script: List[Reply] = []
        self.responder = responder
        self.requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else {}
                with owner._lock:
                    owner.requests.append({"method": self.command, "path": self.path,
                                           "authorization": self.headers.get("Authorization"), "body": body})
                    reply = owner.script.pop(0) if owner.script else None
                if reply is None:
                    reply = owner.responder(self.command, self.path, body) if owner.responder \
                        else (500, {}, {"error": {"message": "no scripted reply"}})
                status, headers, payload = reply
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = _handle
            do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1beta/openai/"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeProvider":
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()

    def chat_requests(self) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["path"].endswith("/chat/completions")]
