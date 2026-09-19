import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/info/health":
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
        elif self.headers.get("Authorization") != "Bearer stubtoken123":
            body = json.dumps({"detail": "Invalid API token"}).encode()
            self.send_response(401)
        elif self.path.startswith("/api/v1/system"):
            body = json.dumps({"status": "ok", "version": "0.4.7", "api_version": "v1"}).encode()
            self.send_response(200)
        elif self.path.startswith("/api/v1/candidates"):
            body = json.dumps({"items": [], "total": 0, "page": 1, "per_page": 200, "total_pages": 0}).encode()
            self.send_response(200)
        else:
            body = b"{}"
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
