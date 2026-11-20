#!/usr/bin/env python3
"""Throwaway stub of the llama-server API, used to test the harness loop quickly."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COUNT = {"n": 0}
REPLIES = [
    '{"tool":"list_dir","path":"."}',
    '{"tool":"write_file","path":"hello.txt","content":"hello from the stub\\nsecond line\\n"}',
    '{"tool":"finish","answer":"Wrote hello.txt with two greeting lines."}',
]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({
            "model_path": "/stub/model.gguf",
            "default_generation_settings": {"n_ctx": 4096},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        i = min(COUNT["n"], len(REPLIES) - 1)
        COUNT["n"] += 1
        text = REPLIES[i]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for k in range(0, len(text), 12):
            chunk = {"choices": [{"delta": {"content": text[k:k + 12]}}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


ThreadingHTTPServer(("127.0.0.1", 8099), H).serve_forever()
