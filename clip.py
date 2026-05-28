"""Web-clip handling for voice-inbox.

A clip is a POST of a web page (or selection) that we turn into clean markdown
and drop into the vault's clippings/ directory — deliberately *separate* from the
voice/transcribe pipeline so clips never spawn transcript-processing projects.

Conversion is server-side (the bookmarklet can't load a converter in-page under
strict CSP). The pipeline is the same one Obsidian's clipper uses, in Python:
readability finds the main content as HTML, markdownify turns it into markdown.
The url-only path fetches the page server-side first.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import markdownify as md
from readability import Document

MAX_FETCH_BYTES = 5 * 1024 * 1024
FETCH_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 trove-clipper"
)
# Elements readability sometimes leaves in that are never article content.
JUNK_TAGS = ["script", "style", "nav", "footer", "aside", "form", "noscript", "iframe", "svg"]

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_HTML_TAG = re.compile(r"<[a-z][\s\S]*>", re.IGNORECASE)
_BLANK_RUN = re.compile(r"\n{3,}")


# Bookmarklet body with __ENDPOINT__/__TOKEN__ placeholders. The setup page
# substitutes them in the browser, so the token never round-trips to the server.
BOOKMARKLET_TEMPLATE = (
    "(function(){try{var s=window.getSelection&&window.getSelection(),h='';"
    "if(s&&s.rangeCount&&!s.isCollapsed){var d=document.createElement('div');"
    "for(var i=0;i<s.rangeCount;i++)d.appendChild(s.getRangeAt(i).cloneContents());h=d.innerHTML;}"
    "var m=function(q){var e=document.querySelector(q);return e?(e.getAttribute('content')||e.getAttribute('datetime')||''):'';};"
    "var p={url:location.href,title:document.title,html:h?'':document.documentElement.outerHTML,selection:h,"
    "excerpt:m('meta[name=\\\"description\\\"]')||m('meta[property=\\\"og:description\\\"]'),"
    "author:m('meta[name=\\\"author\\\"]'),siteName:m('meta[property=\\\"og:site_name\\\"]'),"
    "published:m('meta[property=\\\"article:published_time\\\"]')||m('time[datetime]')};"
    "var t=function(x,o){var e=document.createElement('div');e.textContent=x;"
    "e.style.cssText='position:fixed;z-index:2147483647;left:50%;top:24px;transform:translateX(-50%);background:'+(o?'#0a7f3f':'#b00020')+';color:#fff;font:600 14px system-ui;padding:10px 16px;border-radius:8px;box-shadow:0 4px 16px rgba(0,0,0,.3)';"
    "document.body.appendChild(e);setTimeout(function(){e.remove();},2800);};t('Clipping…',1);"
    "fetch('__ENDPOINT__',{method:'POST',headers:{'content-type':'application/json',authorization:'Bearer __TOKEN__'},body:JSON.stringify(p)})"
    ".then(function(r){return r.json().catch(function(){return{ok:r.ok};});})"
    ".then(function(j){t(j&&j.ok?((j.updated?'Updated ':'Clipped ')+(j.path||'')):'Error: '+((j&&j.detail)||'failed'),j&&j.ok);})"
    ".catch(function(e){t('Blocked by page? '+e.message,0);});}catch(e){alert('Clip error: '+e.message);}})();"
)


def install_page() -> str:
    import json as _json

    # The endpoint is computed in-browser as location.origin + '/clip', so it's
    # correct behind nginx without the server needing to know its public URL.
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Clip to vault — setup</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:42rem;margin:2rem auto;padding:0 1rem;color:#1d2129}}
h1{{font-size:1.4rem}}input{{width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box;border:1px solid #cbd2d9;border-radius:8px}}
.bm{{display:inline-block;margin:.8rem 0;padding:.7rem 1.2rem;background:#0645ad;color:#fff;border-radius:8px;text-decoration:none;font-weight:600}}
.bm.off{{background:#9aa5b1;pointer-events:none}}.muted{{color:#6b7280}}code{{font-family:ui-monospace,monospace}}</style></head><body>
<h1>Clip to vault — setup</h1>
<p class="muted">Clips an article into <code>~/vault/clippings/</code> as markdown.</p>
<p>1. Paste your clip token (it stays in this browser):</p>
<input id="tok" type="password" placeholder="VOICE_BEARER_TOKEN" autocomplete="off">
<p>2. Add this to your bookmarks (drag on desktop; on iOS bookmark this page, then edit the bookmark's URL and paste the generated link):</p>
<a id="bm" class="bm off" href="#">📎 Clip to vault</a>
<p class="muted">Tapping it on any page clips that article (or your current text selection).</p>
<script>
var TMPL={_json.dumps(BOOKMARKLET_TEMPLATE)},EP=location.origin+'/clip';
var tok=document.getElementById('tok'),bm=document.getElementById('bm');
tok.addEventListener('input',function(){{var t=tok.value.trim();if(!t){{bm.classList.add('off');bm.href='#';return;}}
bm.href='javascript:'+encodeURIComponent(TMPL.replace('__ENDPOINT__',EP).replace('__TOKEN__',t));bm.classList.remove('off');}});
bm.addEventListener('click',function(e){{if(bm.classList.contains('off'))e.preventDefault();}});
</script></body></html>"""


def _clippings_dir() -> Path:
    # Resolved at call time (not import) so it honours the real HOME at runtime
    # and a monkeypatched Path.home() in tests.
    return Path.home() / "vault" / "clippings"


def _slugify(title: str) -> str:
    base = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    base = _SLUG_STRIP.sub("-", base.lower()).strip("-")[:80].strip("-")
    return base or "clipping"


def _url_hash(url: str) -> str:
    # djb2 — stable, short; same URL → same file → an overwrite, not a duplicate.
    h = 5381
    for ch in url:
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    return format(h, "x").rjust(7, "0")[:7]


def _yaml_scalar(value) -> str:
    if value is None:
        return '""'
    s = str(value)
    if s == "" or re.search(r'[:#\[\]{}",&*!|>%@`]', s) or s.strip() != s:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _frontmatter(fields: dict) -> str:
    lines = ["---"]
    for key, val in fields.items():
        if val is None or val == "" or val == []:
            continue
        if isinstance(val, list):
            lines.append(f"{key}: [{', '.join(_yaml_scalar(v) for v in val)}]")
        else:
            lines.append(f"{key}: {_yaml_scalar(val)}")
    lines.append("---")
    return "\n".join(lines)


def _html_to_markdown(content_html: str) -> str:
    soup = BeautifulSoup(content_html, "html.parser")
    for tag in soup(JUNK_TAGS):
        tag.decompose()
    text = md(str(soup), heading_style="ATX", bullets="-")
    return _BLANK_RUN.sub("\n\n", text).strip()


def _fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": FETCH_UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            raise ValueError(f"unsupported content-type for clipping: {ctype or 'unknown'}")
        raw = resp.read(MAX_FETCH_BYTES + 1)
        if len(raw) > MAX_FETCH_BYTES:
            raise ValueError("page exceeds max fetch size")
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def _parse_meta(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    def m(*sels):
        for sel, attr in sels:
            el = soup.select_one(sel)
            if el and el.get(attr):
                return el.get(attr).strip()
        return ""

    return {
        "title": (soup.title.string.strip() if soup.title and soup.title.string else ""),
        "author": m(('meta[name="author"]', "content"), ('meta[property="article:author"]', "content")),
        "site": m(('meta[property="og:site_name"]', "content")),
        "published": m(('meta[property="article:published_time"]', "content"), ('meta[name="date"]', "content"), ("time[datetime]", "datetime")),
        "excerpt": m(('meta[name="description"]', "content"), ('meta[property="og:description"]', "content")),
    }


def _extract(payload: dict) -> dict:
    url = (payload.get("url") or "").strip()
    selection = (payload.get("selection") or "").strip()
    html = (payload.get("html") or "").strip()

    meta = {}
    is_selection = False

    if selection:
        body = _html_to_markdown(selection) if _HTML_TAG.search(selection) else selection.strip()
        is_selection = True
    else:
        if not html and url:
            html = _fetch(url)
        if not html:
            raise ValueError("no html, selection, or fetchable url to extract from")
        try:
            doc = Document(html)
            content_html = doc.summary(html_partial=True)
            body = _html_to_markdown(content_html)
            doc_title = doc.short_title()
        except Exception:
            body, doc_title = "", ""
        if not body:
            body = _html_to_markdown(html)  # last-resort: convert the whole thing
        meta = _parse_meta(html)
        if doc_title:
            meta["title"] = doc_title

    def pick(key, *alts):
        for a in (payload.get(key), payload.get("siteName") if key == "site" else None, *alts):
            if a:
                return str(a).strip()
        return ""

    return {
        "markdown": body or "",
        "title": pick("title", meta.get("title")) or url,
        "author": pick("author", meta.get("author")),
        "site": pick("site", meta.get("site")),
        "published": pick("published", meta.get("published")),
        "excerpt": pick("excerpt", meta.get("excerpt")),
        "is_selection": is_selection,
    }


def save_clip(payload: dict) -> dict:
    """Turn a clip payload into a markdown file in vault/clippings/. Returns
    {path, title, updated, bytes}."""
    url = (payload.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError("a valid http(s) url is required")

    data = _extract(payload)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    tags = payload.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    tags = [t for t in (str(t).strip() for t in tags) if t]

    fields = {
        "type": "clipping",
        "title": data["title"],
        "url": url,
        "clipped": now,
        "author": data["author"],
        "site": data["site"],
        "published": data["published"],
        "excerpt": data["excerpt"],
        "clip_kind": "selection" if data["is_selection"] else None,
        "tags": tags,
    }

    source = f"> Clipped from [{data['site'] or url}]({url})\n\n"
    body = data["markdown"] or "_(no extractable content)_"
    contents = f"{_frontmatter(fields)}\n\n{source}{body}\n"

    clippings = _clippings_dir()
    clippings.mkdir(parents=True, exist_ok=True)
    dest = clippings / f"{_slugify(data['title'])}-{_url_hash(url)}.md"
    existed = dest.exists()
    dest.write_text(contents, encoding="utf-8")

    return {
        "path": str(dest.relative_to(Path.home() / "vault")),
        "title": data["title"],
        "updated": existed,
        "bytes": len(contents.encode("utf-8")),
    }
