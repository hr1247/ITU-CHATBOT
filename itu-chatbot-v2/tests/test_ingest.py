"""End-to-end test of ingest.py against a fake website and a fake Open WebUI API.

Run:  python -m pytest -q tests/   (or python tests/test_ingest.py)
"""
import json
import os
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingest"))
import ingest  # noqa: E402

FILLER = " ".join(["ITU offers programmes in engineering and computing."] * 10)
SITE = {
    "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /private/\n"),
    "/": ("text/html", f"<html><head><title>Home</title></head><body><nav>menu</nav><main>"
                       f"<h1>Welcome</h1><p>{FILLER}</p><a href='/admissions?x=1#top'>Adm</a>"
                       f"<a href='/private/hr'>HR</a><a href='https://evil.example/x'>x</a>"
                       f"<a href='/prospectus.pdf'>PDF</a><a href='/logo.png'>logo</a>"
                       f"<a href='/copy'>copy</a></main></body></html>"),
    "/admissions": ("text/html", f"<html><head><title>Admissions</title></head><body><main>"
                                 f"<h2>Apply</h2><ul><li>Step one</li><li>Step two</li></ul>"
                                 f"<p>{FILLER}</p></main></body></html>"),
    "/copy": None,  # filled below: identical bytes to /admissions -> duplicate
    "/private/hr": ("text/html", "<html><body>SECRET</body></html>"),
    "/prospectus.pdf": ("application/pdf", "%PDF-1.4 fake pdf bytes"),
}
SITE["/copy"] = SITE["/admissions"]
FETCHED: list[str] = []


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        FETCHED.append(path)
        page = SITE.get(path)
        if page is None:
            self.send_response(404); self.end_headers(); return
        body = page[1].encode()
        self.send_response(200)
        self.send_header("content-type", page[0] + "; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeOWUI:
    def __init__(self):
        self.files: dict[str, dict] = {}
        self.knowledge: dict[str, dict] = {}
        self.calls: list[str] = []


OW = FakeOWUI()


class Api(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def _auth(self):
        if self.headers.get("Authorization") != "Bearer test-key":
            self._json(401, {"detail": "unauthorized"}); return False
        return True

    def do_GET(self):
        OW.calls.append("GET " + self.path)
        if self.path == "/api/version":
            return self._json(200, {"version": "fake"})
        if not self._auth():
            return
        if self.path == "/api/v1/knowledge/":
            return self._json(200, list(OW.knowledge.values()))
        if self.path.startswith("/api/v1/files/") and self.path.endswith("/process/status"):
            fid = self.path.split("/")[4]
            return self._json(200, {"status": "completed" if fid in OW.files else "failed"})
        self._json(404, {})

    def do_POST(self):
        OW.calls.append("POST " + self.path)
        if not self._auth():
            return
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n)
        if self.path == "/api/v1/files/":
            fid = str(uuid.uuid4())
            name = raw.split(b'filename="')[1].split(b'"')[0].decode()
            OW.files[fid] = {"id": fid, "name": name}
            return self._json(200, {"id": fid})
        if self.path == "/api/v1/knowledge/create":
            kid = str(uuid.uuid4())
            OW.knowledge[kid] = {"id": kid, **json.loads(raw), "files": []}
            return self._json(200, OW.knowledge[kid])
        parts = self.path.split("/")
        if self.path.startswith("/api/v1/knowledge/") and parts[-2] == "file":
            kid, fid = parts[4], json.loads(raw)["file_id"]
            files = OW.knowledge[kid]["files"]
            if parts[-1] == "add":
                files.append(fid)
            elif fid in files:
                files.remove(fid)
            return self._json(200, OW.knowledge[kid])
        self._json(404, {})

    def do_DELETE(self):
        OW.calls.append("DELETE " + self.path)
        if not self._auth():
            return
        OW.files.pop(self.path.split("/")[4], None)
        self._json(200, {})


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def collection_files():
    (k,) = OW.knowledge.values()
    return sorted(OW.files[f]["name"] for f in k["files"])


def test_end_to_end():
    site, api = serve(Site), serve(Api)
    base = f"http://127.0.0.1:{site.server_port}"
    state = tempfile.mkdtemp()
    local = Path(tempfile.mkdtemp())
    (local / "policies").mkdir()
    (local / "policies" / "fee-structure.md").write_text("# Fees\n" + FILLER)
    os.environ.update({
        "OWUI_URL": f"http://127.0.0.1:{api.server_port}", "OWUI_API_KEY": "test-key",
        "KNOWLEDGE_NAME": "ITU Test", "CRAWL_SEEDS": base + "/", "CRAWL_ALLOWED_HOSTS": "127.0.0.1",
        "CRAWL_DELAY_SECONDS": "0", "STATE_DIR": state, "LOCAL_DOCS_DIR": str(local),
        "PRUNE_MAX_FRACTION": "0.5",
    })

    assert ingest.main(["--check"]) == 0

    # Run 1: home, admissions, pdf, local file. /copy is a duplicate, /private blocked, png/off-site skipped.
    assert ingest.main([]) == 0
    names = collection_files()
    assert len(names) == 4, names
    assert any(n.endswith(".pdf") for n in names)
    assert any("fee-structure" in n for n in names)
    assert "/private/hr" not in FETCHED and "/logo.png" not in FETCHED
    home = next(f for f in OW.files.values() if f["name"].startswith("127.0.0.1_") and "admissions" not in f["name"]
                and f["name"].endswith(".md"))
    assert home  # markdown file created for the home page

    # Run 2: nothing changed -> no uploads.
    uploads_before = sum(c == "POST /api/v1/files/" for c in OW.calls)
    assert ingest.main([]) == 0
    assert sum(c == "POST /api/v1/files/" for c in OW.calls) == uploads_before
    assert len(collection_files()) == 4

    # Run 3: admissions page edited -> swapped; old file deleted.
    SITE["/admissions"] = ("text/html", SITE["/admissions"][1].replace("Step two", "Step two (updated)"))
    SITE["/copy"] = SITE["/admissions"]
    old_ids = set(OW.files)
    assert ingest.main([]) == 0
    assert len(collection_files()) == 4
    assert len(set(OW.files) - old_ids) == 1 and len(old_ids - set(OW.files)) == 1

    # Run 4: pdf removed from site (1 of 3 web docs, under the 0.5 cap) -> pruned.
    del SITE["/prospectus.pdf"]
    assert ingest.main([]) == 0
    assert not any(n.endswith(".pdf") for n in collection_files())

    # Run 5: site down -> unhealthy crawl, nothing pruned.
    site.shutdown(); site.server_close()
    ingest.main([])
    assert len(collection_files()) == 3

    # Markdown content carries the source URL for citations.
    md = ingest.html_to_markdown(SITE["/admissions"][1].encode(), base + "/admissions")[1]
    assert "## Apply" in md and "- Step one" in md
    print("all ingest checks passed")


if __name__ == "__main__":
    test_end_to_end()
