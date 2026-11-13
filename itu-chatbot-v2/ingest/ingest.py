#!/usr/bin/env python3
"""
ITU knowledge ingest for Open WebUI.

Crawls approved public pages (HTML, PDF, DOCX) plus an optional folder of approved local
files, and keeps one Open WebUI knowledge collection in sync with them:

  * new document      -> upload, wait for embedding, add to the collection
  * changed document  -> upload new version, swap it in, delete the old file
  * unchanged         -> nothing (content SHA-256 is tracked in a local SQLite file)
  * gone from site    -> removed from the collection (with a safety cap, see PRUNE_MAX_FRACTION)

The crawler stays on CRAWL_ALLOWED_HOSTS, honours robots.txt and its crawl-delay, drops query
strings and fragments, and never sends credentials or cookies to the site.

Usage (inside the compose stack):
  docker compose run --rm ingest              # normal sync
  docker compose run --rm ingest --check      # verify Open WebUI URL / API key / collection only
  docker compose run --rm ingest --dry-run    # crawl and report, change nothing in Open WebUI
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("ingest")

HTML_TYPES = {"text/html", "application/xhtml+xml"}
BINARY_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}
LOCAL_SUFFIXES = {".pdf", ".docx", ".md", ".txt"}
SKIP_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif", ".tiff",
    ".css", ".js", ".json", ".xml", ".rss", ".zip", ".rar", ".7z", ".gz", ".tar",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".webm", ".wav",
    ".ppt", ".pptx", ".xls", ".xlsx", ".doc", ".exe", ".apk", ".dmg", ".iso",
}
STRIP_TAGS = ["script", "style", "noscript", "nav", "footer", "header", "aside",
              "form", "iframe", "svg", "button", "template"]
MIN_TEXT_CHARS = 200


# --------------------------------------------------------------------------- config

def _env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


@dataclass
class Config:
    owui_url: str
    api_key: str
    knowledge_name: str
    seeds: list[str]
    allowed_hosts: set[str]
    exclude_prefixes: list[str]
    exclude_patterns: list[str]
    max_pages: int
    delay: float
    max_bytes: int
    user_agent: str
    state_dir: Path
    local_docs_dir: Path | None
    prune_max_fraction: float
    process_timeout: int = 900

    @classmethod
    def from_env(cls) -> "Config":
        local = os.environ.get("LOCAL_DOCS_DIR", "").strip()
        return cls(
            owui_url=os.environ.get("OWUI_URL", "http://open-webui:8080").rstrip("/"),
            api_key=os.environ.get("OWUI_API_KEY", "").strip(),
            knowledge_name=os.environ.get("KNOWLEDGE_NAME", "ITU Public Website").strip(),
            seeds=_env_list("CRAWL_SEEDS"),
            allowed_hosts={h.lower() for h in _env_list("CRAWL_ALLOWED_HOSTS")},
            exclude_prefixes=_env_list("CRAWL_EXCLUDE_PREFIXES"),
            exclude_patterns=_env_list("CRAWL_EXCLUDE_PATTERNS"),
            max_pages=int(os.environ.get("CRAWL_MAX_PAGES", "1500")),
            delay=float(os.environ.get("CRAWL_DELAY_SECONDS", "1.0")),
            max_bytes=int(float(os.environ.get("CRAWL_MAX_FILE_MB", "25")) * 1024 * 1024),
            user_agent=os.environ.get("CRAWL_USER_AGENT", "ITU-Chatbot-Ingest/1.0"),
            state_dir=Path(os.environ.get("STATE_DIR", "./state")),
            local_docs_dir=Path(local) if local else None,
            prune_max_fraction=float(os.environ.get("PRUNE_MAX_FRACTION", "0.2")),
            process_timeout=int(os.environ.get("PROCESS_TIMEOUT_SECONDS", "900")),
        )

    def validate(self, need_api: bool) -> list[str]:
        problems = []
        if need_api and not self.api_key:
            problems.append("OWUI_API_KEY is empty")
        if not self.seeds and not self.local_docs_dir:
            problems.append("set CRAWL_SEEDS and/or LOCAL_DOCS_DIR")
        if self.seeds and not self.allowed_hosts:
            problems.append("CRAWL_ALLOWED_HOSTS is empty")
        for s in self.seeds:
            host = (urlsplit(s).hostname or "").lower()
            if host not in self.allowed_hosts:
                problems.append(f"seed {s} is not on CRAWL_ALLOWED_HOSTS")
        return problems


# --------------------------------------------------------------------------- documents

@dataclass
class Doc:
    key: str            # canonical URL, or "local:<relative path>"
    filename: str       # name shown in Open WebUI citations
    content: bytes
    content_type: str
    body_text: str | None = None   # for HTML: text without the Source line, used to spot duplicates
    sha256: str = field(init=False)
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        self.sha256 = hashlib.sha256(self.content).hexdigest()
        self.fingerprint = (hashlib.sha256(self.body_text.encode()).hexdigest()
                            if self.body_text is not None else self.sha256)


def canonical(url: str) -> str:
    """Drop query string and fragment, lowercase host, give an empty path a '/'."""
    p = urlsplit(url)
    path = re.sub(r"/{2,}", "/", p.path or "/")
    return urlunsplit((p.scheme.lower(), (p.hostname or "").lower() + (f":{p.port}" if p.port else ""),
                       path, "", ""))


def filename_for(url: str, suffix: str) -> str:
    p = urlsplit(url)
    stem = (p.hostname or "site") + "_" + p.path.strip("/").replace("/", "_")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-_.") or "index"
    if stem.lower().endswith(suffix):
        stem = stem[: -len(suffix)]
    short = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"{stem[:120]}_{short}{suffix}"


def html_to_markdown(html: bytes, url: str) -> tuple[str, str]:
    """Return (title, markdown body). The source URL is written into the body so it survives
    into every chunk-level citation."""
    soup = BeautifulSoup(html, "lxml")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "") or url
    for tag in soup(STRIP_TAGS):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.find(id="content") or soup.body or soup
    for level in range(1, 5):
        for h in root.find_all(f"h{level}"):
            h.insert_before("\n\n" + "#" * level + " ")
            h.insert_after("\n\n")
    for li in root.find_all("li"):
        li.insert_before("\n- ")
    for br in root.find_all("br"):
        br.replace_with("\n")
    text = root.get_text(" ", strip=False)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    body, blank = [], False
    for ln in lines:
        if ln:
            body.append(ln)
            blank = False
        elif not blank:
            body.append("")
            blank = True
    return title, "\n".join(body).strip()


# --------------------------------------------------------------------------- crawler

class Gone(Exception):
    """404/410: the page no longer exists. Not a crawl-health error; it will simply be pruned."""

class Crawler:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.http = requests.Session()
        self.http.headers["User-Agent"] = cfg.user_agent
        self.robots: dict[str, RobotFileParser | None] = {}
        self.last_fetch: dict[str, float] = {}
        self.fetched = 0
        self.errors: list[str] = []
        self.seed_failures = 0

    # robots.txt: 4xx -> allow all, network/5xx -> disallow host (conservative)
    def _robots(self, url: str) -> RobotFileParser | None:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.robots:
            rp = RobotFileParser()
            try:
                r = self.http.get(origin + "/robots.txt", timeout=20)
                if r.status_code >= 500:
                    raise requests.HTTPError(f"robots.txt HTTP {r.status_code}")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                self.robots[origin] = rp
            except requests.RequestException as e:
                self.errors.append(f"robots {origin}: {e}")
                log.warning("cannot read robots.txt for %s (%s); skipping host this run", origin, e)
                self.robots[origin] = None
        return self.robots[origin]

    def allowed(self, url: str) -> bool:
        p = urlsplit(url)
        if p.scheme not in ("http", "https"):
            return False
        if (p.hostname or "").lower() not in self.cfg.allowed_hosts:
            return False
        if any(p.path.startswith(x) for x in self.cfg.exclude_prefixes):
            return False
        if any(pat.lower() in url.lower() for pat in self.cfg.exclude_patterns):
            return False
        path_lower = p.path.lower()
        if Path(path_lower).suffix in SKIP_SUFFIXES or path_lower.endswith(".pdf"):
            return False
        blocked_keywords = (
            "rfp", "tender", "procurement", "auction", "budget-estimates", "peeda",
            "merit-list", "laptop-scheme", "itu-latest-news", "press-clippings",
            "convocation", "robotics-expo", "library-week", "tedxitu",
            "/wp-content/uploads/", "/category/", "/tag/", "/author/", "/page/",
            "2014", "2015", "2016", "2017", "2018", "2019", "2020", "2021", "2022", "2023", "2024"
        )
        if any(x in path_lower for x in blocked_keywords):
            return False
        rp = self._robots(url)
        return bool(rp and rp.can_fetch(self.cfg.user_agent, url))
    def _wait(self, url: str) -> None:
        host = urlsplit(url).netloc
        rp = self._robots(url)
        delay = max(self.cfg.delay, (rp.crawl_delay(self.cfg.user_agent) or 0) if rp else 0)
        gap = time.monotonic() - self.last_fetch.get(host, 0)
        if gap < delay:
            time.sleep(delay - gap)
        self.last_fetch[host] = time.monotonic()

    def _get(self, url: str) -> tuple[str, str, bytes] | None:
        """Returns (final_url, content_type, body) or None if skipped."""
        self._wait(url)
        with self.http.get(url, timeout=60, stream=True, allow_redirects=True) as r:
            final = canonical(r.url)
            if final != url and not self.allowed(final):
                log.info("skip %s: redirected off-limits to %s", url, final)
                return None
            if r.status_code in (404, 410):
                raise Gone(f"HTTP {r.status_code}")
            if r.status_code != 200:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype not in HTML_TYPES and ctype not in BINARY_TYPES:
                return None
            declared = int(r.headers.get("content-length") or 0)
            if declared > self.cfg.max_bytes:
                log.info("skip %s: %d bytes exceeds cap", url, declared)
                return None
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf.extend(chunk)
                if len(buf) > self.cfg.max_bytes:
                    log.info("skip %s: exceeds size cap", url)
                    return None
            return final, ctype, bytes(buf)

    def crawl(self) -> Iterator[Doc]:
        queue = deque(canonical(s) for s in self.cfg.seeds)
        seeds = set(queue)
        seen: set[str] = set(queue)
        while queue and self.fetched < self.cfg.max_pages:
            url = queue.popleft()
            if not self.allowed(url):
                continue
            try:
                got = self._get(url)
            except Gone as e:
                log.info("gone %s (%s)", url, e)
                if url in seeds:
                    self.seed_failures += 1
                continue
            except requests.RequestException as e:
                self.errors.append(f"{url}: {e}")
                log.warning("fetch failed %s: %s", url, e)
                if url in seeds:
                    self.seed_failures += 1
                continue
            self.fetched += 1
            if not got:
                continue
            final, ctype, body = got
            seen.add(final)

            if ctype in HTML_TYPES:
                soup = BeautifulSoup(body, "lxml")
                for a in soup.find_all("a", href=True):
                    nxt = canonical(urljoin(final, a["href"]))
                    if nxt not in seen and urlsplit(nxt).scheme in ("http", "https"):
                        seen.add(nxt)
                        queue.append(nxt)
                title, md = html_to_markdown(body, final)
                if len(md) < MIN_TEXT_CHARS:
                    continue
                text = f"# {title}\n\nSource: {final}\n\n{md}\n"
                yield Doc(final, filename_for(final, ".md"), text.encode("utf-8"), "text/markdown",
                          body_text=f"{title}\n{md}")
            else:
                yield Doc(final, filename_for(final, BINARY_TYPES[ctype]), body, ctype)
        if queue:
            log.warning("stopped at CRAWL_MAX_PAGES=%d with %d URLs still queued",
                        self.cfg.max_pages, len(queue))


def local_docs(root: Path) -> Iterator[Doc]:
    types = {".pdf": "application/pdf", ".md": "text/markdown", ".txt": "text/plain",
             ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in LOCAL_SUFFIXES and not path.name.startswith("."):
            rel = path.relative_to(root).as_posix()
            yield Doc(f"local:{rel}", re.sub(r"[^A-Za-z0-9._-]+", "-", rel.replace("/", "_")),
                      path.read_bytes(), types[path.suffix.lower()])


# --------------------------------------------------------------------------- Open WebUI API

class OpenWebUI:
    def __init__(self, base: str, api_key: str, timeout: int) -> None:
        self.base = base
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {api_key}", "Accept": "application/json"})

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        r = self.s.request(method, self.base + path, timeout=kw.pop("timeout", 120), **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:300]}")
        return r

    def knowledge_id(self, name: str, create: bool) -> str | None:
        data = self._req("GET", "/api/v1/knowledge/").json()
        items = data.get("items", data.get("data", [])) if isinstance(data, dict) else data
        for k in items or []:
            if k.get("name") == name:
                return k["id"]
        if not create:
            return None
        k = self._req("POST", "/api/v1/knowledge/create", json={
            "name": name,
            "description": "Approved public ITU content, synced automatically by the ingest job.",
        }).json()
        log.info("created knowledge collection %r (%s)", name, k["id"])
        return k["id"]

    def upload(self, doc: Doc) -> str:
        r = self._req("POST", "/api/v1/files/", timeout=300,
                      files={"file": (doc.filename, doc.content, doc.content_type)})
        return r.json()["id"]

    def wait_processed(self, file_id: str) -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            r = self.s.get(f"{self.base}/api/v1/files/{file_id}/process/status", timeout=30)
            if r.status_code == 404:          # older builds without the status route
                time.sleep(5)
                return
            r.raise_for_status()
            status = (r.json().get("status") or "").lower()
            if status == "completed":
                return
            if status == "failed":
                raise RuntimeError(f"processing failed: {r.json().get('error', 'no detail')}")
            time.sleep(3)
        raise TimeoutError(f"file {file_id} not processed after {self.timeout}s")

    def add(self, kid: str, file_id: str) -> None:
        self._req("POST", f"/api/v1/knowledge/{kid}/file/add", json={"file_id": file_id}, timeout=600)

    def remove(self, kid: str, file_id: str) -> None:
        try:
            self._req("POST", f"/api/v1/knowledge/{kid}/file/remove", json={"file_id": file_id})
        except RuntimeError as e:
            log.warning("remove from collection failed for %s: %s", file_id, e)
        try:
            self._req("DELETE", f"/api/v1/files/{file_id}")
        except RuntimeError as e:
            log.warning("delete file failed for %s: %s", file_id, e)


# --------------------------------------------------------------------------- state

class State:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("""create table if not exists docs(
            key text primary key, sha256 text, file_id text, filename text,
            knowledge_id text, updated_at text, last_seen text)""")
        self.db.commit()

    def get(self, key: str):
        return self.db.execute("select sha256, file_id from docs where key=?", (key,)).fetchone()

    def seen(self, key: str, run: str) -> None:
        self.db.execute("update docs set last_seen=? where key=?", (run, key))

    def put(self, doc: Doc, file_id: str, kid: str, run: str) -> None:
        self.db.execute("""insert into docs values(?,?,?,?,?,?,?) on conflict(key) do update set
            sha256=excluded.sha256, file_id=excluded.file_id, filename=excluded.filename,
            knowledge_id=excluded.knowledge_id, updated_at=excluded.updated_at,
            last_seen=excluded.last_seen""",
            (doc.key, doc.sha256, file_id, doc.filename, kid, run, run))
        self.db.commit()

    def stale(self, run: str, prefix: str):
        return self.db.execute("select key, file_id from docs where last_seen<>? and key like ?",
                               (run, prefix + "%")).fetchall()

    def count(self, prefix: str) -> int:
        return self.db.execute("select count(*) from docs where key like ?", (prefix + "%",)).fetchone()[0]

    def delete(self, key: str) -> None:
        self.db.execute("delete from docs where key=?", (key,))
        self.db.commit()


# --------------------------------------------------------------------------- main

def sync(cfg: Config, dry_run: bool, force_prune: bool) -> dict:
    run = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")  # unique per run
    state = State(cfg.state_dir / "state.sqlite")
    api = None if dry_run else OpenWebUI(cfg.owui_url, cfg.api_key, cfg.process_timeout)
    kid = None if dry_run else api.knowledge_id(cfg.knowledge_name, create=True)
    stats = {"run": run, "added": 0, "updated": 0, "unchanged": 0, "duplicates": 0,
             "removed": 0, "failed": 0, "dry_run": dry_run}
    hashes_this_run: set[str] = set()

    def handle(doc: Doc) -> None:
        if doc.fingerprint in hashes_this_run:     # same content under another URL
            stats["duplicates"] += 1
            log.info("duplicate content, skipped %s", doc.key)
            return
        hashes_this_run.add(doc.fingerprint)
        prev = state.get(doc.key)
        if prev and prev[0] == doc.sha256:
            state.seen(doc.key, run)
            stats["unchanged"] += 1
            return
        if dry_run:
            stats["updated" if prev else "added"] += 1
            log.info("[dry-run] would %s %s", "update" if prev else "add", doc.key)
            return
        new_id = None
        try:
            new_id = api.upload(doc)
            api.wait_processed(new_id)
            api.add(kid, new_id)
        except Exception as e:  # noqa: BLE001 — one bad document must not stop the run
            stats["failed"] += 1
            log.error("failed %s: %s", doc.key, e)
            if new_id:
                api.remove(kid, new_id)
            if prev:
                state.seen(doc.key, run)           # keep serving the old version
            return
        if prev and prev[1]:
            api.remove(kid, prev[1])
        state.put(doc, new_id, kid, run)
        stats["updated" if prev else "added"] += 1
        log.info("%s %s", "updated" if prev else "added", doc.key)

    crawler = None
    if cfg.seeds:
        crawler = Crawler(cfg)
        for doc in crawler.crawl():
            handle(doc)
        stats.update(fetched=crawler.fetched, fetch_errors=len(crawler.errors))
    if cfg.local_docs_dir and cfg.local_docs_dir.is_dir():
        for doc in local_docs(cfg.local_docs_dir):
            handle(doc)

    # Prune only sources that were fully enumerated this run.
    prune_sets = []
    if crawler:
        healthy = (crawler.seed_failures == 0 and crawler.fetched > 0
                   and len(crawler.errors) <= 0.25 * max(crawler.fetched, 1))
        if healthy:
            prune_sets.append("http")
        else:
            log.warning("crawl unhealthy (seed failures=%d, errors=%d, fetched=%d): not pruning web docs",
                        crawler.seed_failures, len(crawler.errors), crawler.fetched)
    if cfg.local_docs_dir and cfg.local_docs_dir.is_dir():
        prune_sets.append("local:")
    for prefix in prune_sets:
        stale = state.stale(run, prefix)
        known = state.count(prefix)
        if stale and not force_prune and len(stale) > cfg.prune_max_fraction * known:
            log.error("refusing to remove %d of %d %s docs (> PRUNE_MAX_FRACTION=%.2f); "
                      "re-run with --force-prune if this is intended", len(stale), known,
                      prefix, cfg.prune_max_fraction)
            stats["prune_blocked"] = True
            continue
        for key, file_id in stale:
            if dry_run:
                log.info("[dry-run] would remove %s", key)
            else:
                api.remove(kid, file_id)
                state.delete(key)
                log.info("removed %s", key)
            stats["removed"] += 1
    state.db.commit()
    return stats


def reindex_url(cfg: Config, url: str) -> dict:
    run = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    state = State(cfg.state_dir / "state.sqlite")
    api = OpenWebUI(cfg.owui_url, cfg.api_key, cfg.process_timeout)
    kid = api.knowledge_id(cfg.knowledge_name, create=True)

    url = canonical(url)
    crawler = Crawler(cfg)
    if not crawler.allowed(url):
        log.error("URL %s is not allowed by crawler rules", url)
        return {"status": "error", "reason": "not_allowed"}

    res = crawler._get(url)
    if not res:
        log.error("Failed to fetch %s", url)
        return {"status": "error", "reason": "fetch_failed"}

    final_url, ctype, body = res
    if ctype in HTML_TYPES:
        title, md = html_to_markdown(body, final_url)
        content = f"# {title}\n\nSource: {final_url}\n\n{md}".encode("utf-8")
        doc = Doc(key=final_url, filename=filename_for(final_url, ".md"),
                  content=content, content_type="text/markdown", body_text=md)
    else:
        doc = Doc(key=final_url, filename=filename_for(final_url, BINARY_TYPES.get(ctype, ".bin")),
                  content=body, content_type=ctype)

    prev = state.get(doc.key)
    log.info("Uploading %s (previous file_id: %s)...", doc.filename, prev[1] if prev else "none")
    new_id = api.upload(doc)
    api.wait_processed(new_id)
    if prev:
        log.info("Removing previous file_id: %s before adding new version", prev[1])
        api.remove(kid, prev[1])
    api.add(kid, new_id)
    state.put(doc, new_id, kid, run)
    state.db.commit()
    log.info("Successfully replaced %s with new file_id %s", doc.key, new_id)
    return {"status": "success", "key": doc.key, "new_id": new_id, "prev_id": prev[1] if prev else None}


def remove_key(cfg: Config, key_or_pattern: str) -> dict:
    state = State(cfg.state_dir / "state.sqlite")
    api = OpenWebUI(cfg.owui_url, cfg.api_key, cfg.process_timeout)
    kid = api.knowledge_id(cfg.knowledge_name, create=False)

    c = state.db.execute("SELECT key, file_id, filename FROM docs WHERE key = ? OR key LIKE ? OR filename LIKE ?",
                         (key_or_pattern, f"%{key_or_pattern}%", f"%{key_or_pattern}%")).fetchall()
    if not c:
        log.warning("No rows found matching %s", key_or_pattern)
        return {"status": "not_found", "pattern": key_or_pattern}
    removed = []
    for k, fid, fn in c:
        log.info("Removing %s (file_id: %s, filename: %s)...", k, fid, fn)
        if kid and fid:
            api.remove(kid, fid)
        state.delete(k)
        removed.append({"key": k, "file_id": fid, "filename": fn})
    state.db.commit()
    log.info("Successfully removed %d file(s)", len(removed))
    return {"status": "success", "removed": removed}


def check(cfg: Config) -> int:
    try:
        v = requests.get(cfg.owui_url + "/api/version", timeout=15).json()
        log.info("Open WebUI reachable, version %s", v.get("version", "?"))
        api = OpenWebUI(cfg.owui_url, cfg.api_key, cfg.process_timeout)
        kid = api.knowledge_id(cfg.knowledge_name, create=False)
        log.info("API key OK; collection %r %s", cfg.knowledge_name,
                 f"exists ({kid})" if kid else "does not exist yet (created on first sync)")
        return 0
    except Exception as e:  # noqa: BLE001
        log.error("check failed: %s", e)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="verify connectivity and API key, then exit")
    ap.add_argument("--dry-run", action="store_true", help="crawl and report; change nothing")
    ap.add_argument("--force-prune", action="store_true", help="allow removals above PRUNE_MAX_FRACTION")
    ap.add_argument("--reindex-url", help="Fetch and re-ingest a single URL through the replace path")
    ap.add_argument("--remove-key", help="Remove document(s) matching key or filename from knowledge base and state")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = Config.from_env()
    problems = cfg.validate(need_api=not args.dry_run)
    if problems:
        for p in problems:
            log.error("config: %s", p)
        return 2
    if args.check:
        return check(cfg)
    if args.remove_key:
        res = remove_key(cfg, args.remove_key)
        print(json.dumps(res, indent=2))
        return 0 if res.get("status") == "success" else 1
    if args.reindex_url:
        res = reindex_url(cfg, args.reindex_url)
        print(json.dumps(res, indent=2))
        return 0 if res.get("status") == "success" else 1
    stats = sync(cfg, args.dry_run, args.force_prune)
    print(json.dumps(stats, indent=2))
    return 1 if stats["failed"] or stats.get("prune_blocked") else 0


if __name__ == "__main__":
    sys.exit(main())
