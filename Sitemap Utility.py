#!/usr/bin/env python3
"""
Sitemap URL Fetcher WebUI (single-file)

Features:
- Settings tab: add/remove sitemap URLs (persisted to sitemaps.json next to this script)
- Fetch tab: checkbox-select sitemaps to fetch; resolves sitemap indexes recursively (e.g., Blogger)
- Displays deduplicated list of URLs and counts

Run:
  python sitemap_webui.py

Env:
  Requires: Flask, requests

Security note: This is a simple utility for local use; do not expose publicly without adding auth/rate limiting.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests
from flask import (
    Flask,
    flash,
    redirect,
    render_template_string,
    request,
    url_for,
)


APP_TITLE = "Sitemap URL Fetcher"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sitemaps.json")
DEFAULT_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


@dataclass
class SitemapEntry:
    id: str
    url: str
    label: str


def _load_sitemaps() -> List[SitemapEntry]:
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        result: List[SitemapEntry] = []
        for item in raw:
            # Backward/forwards tolerant deserialization
            result.append(
                SitemapEntry(
                    id=item.get("id") or str(uuid.uuid4()),
                    url=item.get("url") or item.get("sitemap") or "",
                    label=item.get("label") or item.get("name") or item.get("url") or "",
                )
            )
        # Filter empties
        return [s for s in result if s.url]
    except Exception:
        return []


def _save_sitemaps(entries: List[SitemapEntry]) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump([asdict(e) for e in entries], f, indent=2)


def _normalize_url(u: str) -> str:
    u = u.strip()
    if not u:
        return u
    # Prepend scheme if missing
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
        u = "https://" + u
    return u


def _is_probably_sitemap(url: str) -> bool:
    return bool(re.search(r"sitemap(\.xml|$|\?)", url, re.IGNORECASE))


def _fetch(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (text, error)"""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        # Try to get text as UTF-8; fall back to apparent
        resp.encoding = resp.apparent_encoding or resp.encoding or "utf-8"
        return resp.text, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _parse_sitemap_xml(xml_text: str) -> Tuple[Set[str], Set[str]]:
    """
    Parse sitemap XML, returning (urls, nested_sitemaps).
    Uses a light-weight namespace-agnostic approach.
    """
    import xml.etree.ElementTree as ET

    urls: Set[str] = set()
    nested: Set[str] = set()

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return urls, nested

    tag = root.tag.lower()
    # Strip namespace if present
    if "}" in tag:
        tag = tag.split("}", 1)[1]

    # urlset: contains <url><loc>...</loc></url>
    # sitemapindex: contains <sitemap><loc>...</loc></sitemap>
    # Namespace-agnostic search using wildcard
    if tag == "urlset":
        for loc in root.findall(".//{*}loc"):
            if loc.text:
                urls.add(loc.text.strip())
    elif tag == "sitemapindex":
        for loc in root.findall(".//{*}loc"):
            if loc.text:
                nested.add(loc.text.strip())
    else:
        # Try best-effort: collect all <loc>
        for loc in root.findall(".//{*}loc"):
            if loc.text:
                val = loc.text.strip()
                # Heuristic: if it looks like a sitemap, treat as nested, else as url
                if _is_probably_sitemap(val):
                    nested.add(val)
                else:
                    urls.add(val)

    return urls, nested


def fetch_all_urls_from_sitemaps(sitemaps: Iterable[str], max_depth: int = 5, limit: Optional[int] = None) -> Tuple[List[str], List[str]]:
    """
    Crawl one or more sitemap URLs, following nested sitemap indexes up to max_depth.
    Returns (urls, errors) where urls is de-duplicated in a stable order.
    Optionally stops after collecting `limit` urls.
    """
    visited_sitemaps: Set[str] = set()
    collected_urls: List[str] = []
    seen_urls: Set[str] = set()
    errors: List[str] = []

    frontier: List[Tuple[str, int]] = [(u, 0) for u in sitemaps]

    while frontier:
        current, depth = frontier.pop()
        current = _normalize_url(current)
        if current in visited_sitemaps:
            continue
        visited_sitemaps.add(current)

        text, err = _fetch(current)
        if err:
            errors.append(f"Failed {current}: {err}")
            continue
        if not text:
            errors.append(f"Empty response from {current}")
            continue

        urls, nested = _parse_sitemap_xml(text)

        # Add URLs
        for u in urls:
            if u not in seen_urls:
                seen_urls.add(u)
                collected_urls.append(u)
                if limit is not None and len(collected_urls) >= limit:
                    return collected_urls, errors

        # Recurse into nested sitemaps if depth allows
        if depth < max_depth:
            for n in nested:
                if n not in visited_sitemaps:
                    frontier.append((n, depth + 1))

    return collected_urls, errors


app = Flask(__name__)
app.secret_key = os.environ.get("SITEMAP_WEBUI_SECRET", "dev-secret-key-change-me")


TEMPLATE = """
<!doctype html>
<html lang="en" data-bs-theme="dark">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{{ title }}</title>
  <!-- Dark sci-fi theme using Bootswatch Cyborg + custom neon accents -->
  <link href="https://cdn.jsdelivr.net/npm/bootswatch@5.3.3/dist/cyborg/bootstrap.min.css" rel="stylesheet" />
  <style>
    :root {
      --neon-primary: #00e5ff;
      --neon-secondary: #7c4dff;
      --glow: 0 0 10px rgba(0, 229, 255, 0.6), 0 0 20px rgba(124, 77, 255, 0.3);
    }
    body {
      padding-top: 1.25rem;
      background: radial-gradient(1200px 500px at 10% -20%, rgba(0,229,255,0.12), transparent 60%),
                  radial-gradient(800px 400px at 110% 20%, rgba(124,77,255,0.12), transparent 60%),
                  linear-gradient(180deg, #0a0f17 0%, #0b111b 60%, #0a0f17 100%);
    }
    .url-list {
      white-space: pre-wrap;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \"Liberation Mono\", \"Courier New\", monospace;
    }
    .smallmuted { font-size: .875rem; color: #9aa5b1; }
    .card {
      border: 1px solid rgba(0,229,255,0.2);
      box-shadow: var(--glow);
      background: rgba(13, 18, 28, 0.9);
      backdrop-filter: blur(6px);
    }
    .nav-tabs .nav-link.active {
      color: #fff;
      border-color: var(--neon-primary);
      box-shadow: inset 0 -2px 0 var(--neon-primary), 0 0 8px rgba(0,229,255,.35);
    }
    .nav-tabs .nav-link {
      color: #cbd5e1;
    }
    .btn-primary {
      background: linear-gradient(90deg, var(--neon-secondary), var(--neon-primary));
      border: none;
      box-shadow: 0 0 12px rgba(124,77,255,.4);
    }
    .btn-outline-primary {
      border-color: var(--neon-primary);
      color: var(--neon-primary);
    }
    .form-control, .form-check-input {
      background-color: #121826;
      border-color: rgba(0,229,255,0.25);
      color: #e2e8f0;
    }
    .form-check-input:checked {
      background-color: var(--neon-primary);
      border-color: var(--neon-primary);
      box-shadow: 0 0 10px rgba(0,229,255,.5);
    }
    .list-group-item {
      background-color: rgba(13, 18, 28, 0.85);
      border-color: rgba(124,77,255,0.2);
    }
  </style>
  <script>
    async function copyToClipboard(id) {
      const el = document.getElementById(id);
      const btn = document.getElementById('copyBtn');
      if (!el) return;
      const text = el.innerText || el.textContent || '';
      if (btn) btn.disabled = true;
      const setLabel = (t)=>{ if (btn) { btn.innerText = t; setTimeout(()=>{ btn.innerText = 'Copy'; btn.disabled = false; }, 1200);} };

      try {
        // Prefer modern API in secure contexts
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
          setLabel('Copied!');
          return;
        }
      } catch (e) {
        // fall through to legacy method
      }

      // Legacy fallback using a hidden textarea
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'absolute';
        ta.style.left = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        setLabel('Copied!');
      } catch (e) {
        setLabel('Failed');
      }
    }
  </script>
  <link rel="icon" href="data:," />
  <meta name="robots" content="noindex, nofollow" />
  <meta http-equiv="Content-Security-Policy" content="default-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net data:; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; connect-src 'self';" />
  <meta name="referrer" content="no-referrer" />
  <meta name="color-scheme" content="light dark" />
  <meta name="theme-color" content="#0d6efd" />
  <meta name="description" content="Fetch URLs from one or more sitemap.xml files, including nested sitemap indexes (Blogger)." />
  <meta name="og:title" content="Sitemap URL Fetcher" />
  <meta name="og:type" content="website" />
  <meta name="og:description" content="Fetch URLs from sitemap(s)." />
  <meta name="og:url" content="" />
  <meta name="og:image" content="" />
  <meta name="twitter:card" content="summary" />
  <meta name="twitter:title" content="Sitemap URL Fetcher" />
  <meta name="twitter:description" content="Fetch URLs from sitemap(s)." />
  <meta name="twitter:image" content="" />
</head>
<body>
<div class="container">
  <h1 class="mb-3">{{ title }}</h1>

  {% with messages = get_flashed_messages() %}
    {% if messages %}
      <div class="alert alert-info" role="alert">
        {% for m in messages %}{{ m }}<br/>{% endfor %}
      </div>
    {% endif %}
  {% endwith %}

  <ul class="nav nav-tabs" id="mainTabs" role="tablist">
    <li class="nav-item" role="presentation">
      <button class="nav-link {% if active_tab == 'fetch' %}active{% endif %}" id="fetch-tab" data-bs-toggle="tab" data-bs-target="#fetch" type="button" role="tab" aria-controls="fetch" aria-selected="{{ 'true' if active_tab == 'fetch' else 'false' }}">Fetch URLs</button>
    </li>
    <li class="nav-item" role="presentation">
      <button class="nav-link {% if active_tab == 'settings' %}active{% endif %}" id="settings-tab" data-bs-toggle="tab" data-bs-target="#settings" type="button" role="tab" aria-controls="settings" aria-selected="{{ 'true' if active_tab == 'settings' else 'false' }}">Settings</button>
    </li>
  </ul>

  <div class="tab-content mt-3" id="mainTabsContent">
    <!-- Fetch Tab -->
    <div class="tab-pane fade {% if active_tab == 'fetch' %}show active{% endif %}" id="fetch" role="tabpanel" aria-labelledby="fetch-tab" tabindex="0">
      <form method="post" action="{{ url_for('fetch_urls') }}">
        <div class="mb-3">
          <label class="form-label">Select sitemaps to fetch</label>
          <div class="form-text">Add or edit sitemaps in the Settings tab.</div>
          <div class="mt-2">
            {% if sitemaps %}
              {% for s in sitemaps %}
                <div class="form-check">
                  <input class="form-check-input" type="checkbox" name="sitemap_ids" value="{{ s.id }}" id="sm_{{ s.id }}">
                  <label class="form-check-label" for="sm_{{ s.id }}">
                    <strong>{{ s.label }}</strong> <span class="smallmuted">({{ s.url }})</span>
                  </label>
                </div>
              {% endfor %}
            {% else %}
              <div class="text-muted">No sitemaps yet. Add some in Settings.</div>
            {% endif %}
          </div>
        </div>

        <div class="row g-3">
          <div class="col-md-3">
            <label for="limit" class="form-label">Optional limit</label>
            <input type="number" class="form-control" id="limit" name="limit" placeholder="e.g. 500" min="1">
            <div class="form-text">Stop after N URLs (leave blank for all).</div>
          </div>
          <div class="col-md-3">
            <label for="depth" class="form-label">Max depth</label>
            <input type="number" class="form-control" id="depth" name="depth" value="5" min="1" max="20">
            <div class="form-text">How deep to follow nested sitemap indexes.</div>
          </div>
        </div>

        <div class="mt-3"><button class="btn btn-primary" type="submit">Fetch</button></div>
      </form>

      {% if results is defined %}
        <hr/>
        <h5>Results</h5>
        <div class="smallmuted">Collected {{ results.total }} URL{{ '' if results.total == 1 else 's' }} from {{ results.sources }} sitemap{{ '' if results.sources == 1 else 's' }}. Time: {{ results.elapsed_ms }} ms</div>
        {% if results.errors %}
          <div class="alert alert-warning mt-2">
            <strong>Some errors occurred:</strong><br/>
            <ul class="mb-0">
              {% for e in results.errors %}<li>{{ e }}</li>{% endfor %}
            </ul>
          </div>
        {% endif %}
        <div class="d-flex gap-2 my-2">
          <button id="copyBtn" class="btn btn-outline-secondary btn-sm" onclick="copyToClipboard('urlList')">Copy</button>
          <a class="btn btn-outline-primary btn-sm" href="{{ url_for('download_as_text') }}?q={{ results.token }}">Download .txt</a>
        </div>
        <div class="card">
          <div class="card-body">
            <div id="urlList" class="url-list">{% for u in results.urls %}{{ u }}
{% endfor %}</div>
          </div>
        </div>
      {% endif %}
    </div>

    <!-- Settings Tab -->
    <div class="tab-pane fade {% if active_tab == 'settings' %}show active{% endif %}" id="settings" role="tabpanel" aria-labelledby="settings-tab" tabindex="0">
      <form method="post" action="{{ url_for('add_sitemap') }}" class="row g-3">
        <div class="col-md-6">
          <label for="url" class="form-label">Sitemap URL</label>
          <input type="text" class="form-control" id="url" name="url" placeholder="https://example.com/sitemap.xml" required>
        </div>
        <div class="col-md-6">
          <label for="label" class="form-label">Label</label>
          <input type="text" class="form-control" id="label" name="label" placeholder="Example Site" required>
        </div>
        <div class="col-12">
          <button type="submit" class="btn btn-success">Add Sitemap</button>
        </div>
      </form>

      <hr/>
      <h5>Saved Sitemaps</h5>
      {% if sitemaps %}
        <div class="list-group">
          {% for s in sitemaps %}
            <div class="list-group-item d-flex justify-content-between align-items-center">
              <div>
                <div><strong>{{ s.label }}</strong></div>
                <div class="smallmuted">{{ s.url }}</div>
              </div>
              <div>
                <form method="post" action="{{ url_for('delete_sitemap') }}" onsubmit="return confirm('Delete this sitemap?');">
                  <input type="hidden" name="id" value="{{ s.id }}" />
                  <button type="submit" class="btn btn-outline-danger btn-sm">Delete</button>
                </form>
              </div>
            </div>
          {% endfor %}
        </div>
      {% else %}
        <div class="text-muted">No sitemaps saved yet.</div>
      {% endif %}
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
{% if active_tab %}
<script>
  // Ensure the active tab is shown on load when server-side rendered
  const triggerTabList = [].slice.call(document.querySelectorAll('#mainTabs button'))
  triggerTabList.forEach(function (triggerEl) {
    const tabTrigger = new bootstrap.Tab(triggerEl)
    if (triggerEl.classList.contains('active')) tabTrigger.show()
  })
</script>
{% endif %}
</body>
<!--
  Tip: For Blogger, add https://<your-blog>.blogspot.com/sitemap.xml and the app
  will follow nested indexes (sitemap.xml?page=1, etc.) automatically.
-->
</html>
"""


# Simple in-memory store of last results for download by token
_LAST_RESULTS: Dict[str, List[str]] = {}
_LAST_RESULTS_LOCK = threading.Lock()


def _put_results(urls: List[str]) -> str:
    token = str(uuid.uuid4())
    with _LAST_RESULTS_LOCK:
        # Trim map to avoid unbounded growth
        if len(_LAST_RESULTS) > 32:
            _LAST_RESULTS.clear()
        _LAST_RESULTS[token] = urls
    return token


def _get_results(token: str) -> Optional[List[str]]:
    with _LAST_RESULTS_LOCK:
        return _LAST_RESULTS.get(token)


@app.route("/")
def index():
    sitemaps = _load_sitemaps()
    return render_template_string(
        TEMPLATE,
        title=APP_TITLE,
        sitemaps=sitemaps,
        active_tab="fetch",
    )


@app.post("/add")
def add_sitemap():
    url = request.form.get("url", "").strip()
    label = request.form.get("label", "").strip() or url
    if not url:
        flash("URL is required")
        return redirect(url_for("settings"))
    url = _normalize_url(url)
    entries = _load_sitemaps()
    # Prevent duplicates by URL
    if any(e.url == url for e in entries):
        flash("Sitemap already exists")
        return redirect(url_for("settings"))
    entries.append(SitemapEntry(id=str(uuid.uuid4()), url=url, label=label))
    _save_sitemaps(entries)
    flash("Sitemap added")
    return redirect(url_for("settings"))


@app.post("/delete")
def delete_sitemap():
    sid = request.form.get("id", "")
    entries = _load_sitemaps()
    new_entries = [e for e in entries if e.id != sid]
    if len(new_entries) != len(entries):
        _save_sitemaps(new_entries)
        flash("Deleted sitemap")
    else:
        flash("Sitemap not found")
    return redirect(url_for("settings"))


@app.get("/settings")
def settings():
    sitemaps = _load_sitemaps()
    return render_template_string(
        TEMPLATE,
        title=APP_TITLE,
        sitemaps=sitemaps,
        active_tab="settings",
    )


@app.post("/fetch")
def fetch_urls():
    ids = request.form.getlist("sitemap_ids")
    if not ids:
        flash("Please select at least one sitemap")
        return redirect(url_for("index"))

    limit_s = request.form.get("limit")
    depth_s = request.form.get("depth")
    limit = int(limit_s) if limit_s and limit_s.isdigit() else None
    depth = int(depth_s) if depth_s and depth_s.isdigit() else 5

    entries = _load_sitemaps()
    id_to_entry = {e.id: e for e in entries}
    selected = [id_to_entry[i] for i in ids if i in id_to_entry]
    if not selected:
        flash("Selected sitemaps not found")
        return redirect(url_for("index"))

    start = time.perf_counter()
    urls, errors = fetch_all_urls_from_sitemaps([e.url for e in selected], max_depth=depth, limit=limit)
    elapsed_ms = int((time.perf_counter() - start) * 1000)

    token = _put_results(urls)

    sitemaps = entries  # populate for template
    results = {
        "urls": urls,
        "total": len(urls),
        "errors": errors,
        "elapsed_ms": elapsed_ms,
        "sources": len(selected),
        "token": token,
    }

    return render_template_string(
        TEMPLATE,
        title=APP_TITLE,
        sitemaps=sitemaps,
        results=results,
        active_tab="fetch",
    )


@app.get("/download")
def download_as_text():
    token = request.args.get("q", "")
    urls = _get_results(token) or []
    # Return as a simple text file
    from flask import Response

    body = "\n".join(urls) + ("\n" if urls else "")
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Disposition": "attachment; filename=urls.txt",
        "X-Content-Type-Options": "nosniff",
    }
    return Response(body, headers=headers)


def main():
    # Ensure data file exists
    if not os.path.exists(DATA_FILE):
        _save_sitemaps([])

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    debug = os.environ.get("DEBUG", "false").lower() in {"1", "true", "yes"}
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
