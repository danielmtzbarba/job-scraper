"""Serve the throwaway scored-jobs prototype without starting app workers."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env", override=False)
os.environ["JOB_SCRAPER_STORAGE"] = "cloudsql"

from job_scraper.storage.repository import create_repository  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    repository = None

    def do_GET(self) -> None:
        route = urlsplit(self.path)
        if route.path in {"/", "/prototype/scored-jobs"}:
            body = (ROOT / "job_scraper/prototypes/scored_jobs.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif route.path == "/jobs":
            params = parse_qs(route.query)
            limit = min(500, max(1, int(params.get("limit", ["500"])[0])))
            jobs = self.repository.list_jobs(
                limit=limit, offset=0, fit_status="Scored"
            )
            body = json.dumps({"count": len(jobs), "jobs": jobs}, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
        else:
            self.send_error(404)
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[prototype] {format % args}")


def main() -> None:
    repository = create_repository(ROOT)
    repository.initialize()
    Handler.repository = repository
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Scored jobs prototype: http://127.0.0.1:8765/prototype/scored-jobs")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if hasattr(repository, "close"):
            repository.close()


if __name__ == "__main__":
    main()
