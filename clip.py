"""Web-clip handling for voice-inbox.

A clip is a POST of a web page (or selection). The browser captures the
*rendered* DOM (the hard part: paywalled / logged-in / JS content the server
could never fetch itself), and we extract clean markdown from those bytes and
drop the file into ~/vault/links/ — the vault's "browser-extension drop" zone.

From there the existing pipeline takes over, untouched: collect.sh scans
links/*.md and emits a `[links]` inbox item; the inbox-watcher (~7.5s) runs
/triage, which dispatches /process-link; /process-link reads the LOCAL file
(it knows not to refetch a paywalled URL), wiki-merges it, and files the
immutable source into links/reference/. The server never fetches anything.

Conversion is server-side (a bookmarklet can't load a converter in-page under
strict CSP): readability finds the main content, markdownify makes markdown.

Configurable: TROVE_CLIP_DEST sets the destination subdir under ~/vault
(default "links"). It must stay "links" for the auto-pipeline to pick it up —
collect.sh only scans links/, and /process-link only treats links/ as a
no-refetch local source.
"""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import markdownify as md
from readability import Document

JUNK_TAGS = ["script", "style", "nav", "footer", "aside", "form", "noscript", "iframe", "svg"]

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_HTML_TAG = re.compile(r"<[a-z][\s\S]*>", re.IGNORECASE)
_BLANK_RUN = re.compile(r"\n{3,}")
_FM_URL = re.compile(r'^url:\s*["\']?([^"\'\n]+)["\']?\s*$', re.MULTILINE)


def _dest_dir() -> Path:
    # Resolved at call time so it honours the real HOME at runtime and a
    # monkeypatched Path.home() in tests. TROVE_CLIP_DEST overrides the subdir,
    # but "links" is what the auto-pipeline (collect.sh + /process-link) scans.
    sub = os.environ.get("TROVE_CLIP_DEST", "links").strip()
    p = Path(sub)
    return p if p.is_absolute() else Path.home() / "vault" / sub


def _slugify(title: str) -> str:
    base = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    base = _SLUG_STRIP.sub("-", base.lower()).strip("-")[:80].strip("-")
    return base or "clipping"


def _url_hash(url: str) -> str:
    h = 5381
    for ch in url:
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    return format(h, "x").rjust(7, "0")[:7]


def _yamlq(value) -> str:
    # Single-quoted YAML, matching save-article.sh's yamlq (doubles internal ').
    return "'" + str(value).replace("'", "''") + "'"


# Emitted bare (no quotes), matching save-article.sh: a date and an enum.
_RAW_KEYS = {"saved", "status"}


def _frontmatter(fields: dict) -> str:
    lines = ["---"]
    for key, val in fields.items():
        if val is None or val == "" or val == []:
            continue
        if isinstance(val, list):
            lines.append(f"{key}: [{', '.join(_yamlq(v) for v in val)}]")
        elif key in _RAW_KEYS:
            lines.append(f"{key}: {val}")
        else:
            lines.append(f"{key}: {_yamlq(val)}")
    lines.append("---")
    return "\n".join(lines)


def _html_to_markdown(content_html: str) -> str:
    soup = BeautifulSoup(content_html, "html.parser")
    for tag in soup(JUNK_TAGS):
        tag.decompose()
    text = md(str(soup), heading_style="ATX", bullets="-")
    return _BLANK_RUN.sub("\n\n", text).strip()


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
        "published": m(('meta[property="article:published_time"]', "content"), ('meta[name="date"]', "content"), ("time[datetime]", "datetime")),
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
        if not html:
            # By design the server never fetches a URL itself — the browser
            # captures the rendered (possibly logged-in) DOM and sends it. No
            # html means there's nothing to extract (and no SSRF surface).
            raise ValueError("no page content — send 'html' or 'selection' (the server does not fetch URLs)")
        try:
            doc = Document(html)
            body = _html_to_markdown(doc.summary(html_partial=True))
            doc_title = doc.short_title()
        except Exception:
            body, doc_title = "", ""
        if not body:
            body = _html_to_markdown(html)
        meta = _parse_meta(html)
        if doc_title:
            meta["title"] = doc_title

    def pick(key, *alts):
        for a in (payload.get(key), *alts):
            if a:
                return str(a).strip()
        return ""

    return {
        "markdown": body or "",
        "title": pick("title", meta.get("title")) or url,
        "author": pick("author", meta.get("author")),
        "published": pick("published", meta.get("published")),
        "is_selection": is_selection,
    }


def _existing_url(path: Path) -> str:
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:2048]
    except OSError:
        return ""
    if not head.startswith("---"):
        return ""
    m = _FM_URL.search(head)
    return m.group(1).strip() if m else ""


def _resolve_path(dest_dir: Path, slug: str, url: str) -> tuple[Path, bool]:
    """Pick the output file, house-style: slug.md, then slug-2.md … slug-9.md.
    Re-clipping the SAME url overwrites its file (returns existed=True); a
    different page with the same slug gets the next free disambiguator."""
    for i in range(1, 10):
        name = f"{slug}.md" if i == 1 else f"{slug}-{i}.md"
        p = dest_dir / name
        if not p.exists():
            return p, False
        if _existing_url(p) == url:
            return p, True
    # 9 different pages already share this slug — fall back to a url-hashed name.
    p = dest_dir / f"{slug}-{_url_hash(url)}.md"
    return p, p.exists()


def save_clip(payload: dict) -> dict:
    """Turn a clip payload into a markdown file under the configured dest dir.
    Returns {path, title, updated, bytes}."""
    url = (payload.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError("a valid http(s) url is required")

    data = _extract(payload)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    tags = payload.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    tags = [t for t in (str(t).strip() for t in tags) if t]

    # A links/ drop for /process-link to read locally. It pulls `url` from the
    # frontmatter for attribution and uses the body as the article (no refetch).
    fields = {
        "title": data["title"],
        "url": url,
        "author": data["author"],
        "published": data["published"],
        "source": "web clip" + (" (selection)" if data["is_selection"] else ""),
        "saved": today,
        "tags": tags,
    }

    body = data["markdown"] or "_(no extractable content)_"
    contents = f"{_frontmatter(fields)}\n\n{body}\n"

    dest_dir = _dest_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest, existed = _resolve_path(dest_dir, _slugify(data["title"]), url)
    dest.write_text(contents, encoding="utf-8")

    try:
        rel = str(dest.relative_to(Path.home() / "vault"))
    except ValueError:
        rel = str(dest)
    return {
        "path": rel,
        "title": data["title"],
        "updated": existed,
        "bytes": len(contents.encode("utf-8")),
    }


# Bookmarklet body with __ENDPOINT__/__TOKEN__ placeholders (the setup page
# substitutes them in the browser, so the token never round-trips to the server).
#
# It POSTs via a hidden <form> in a new tab, NOT fetch(). A form submission rides
# the `form-action` CSP directive (rarely set) instead of `connect-src` (often
# 'self'), so it gets the captured bytes out of pages where a fetch would be
# blocked. The bytes never leave the request body; nothing huge goes in a URL.
BOOKMARKLET_TEMPLATE = (
    "(function(){try{var s=window.getSelection&&window.getSelection(),h='';"
    "if(s&&s.rangeCount&&!s.isCollapsed){var d=document.createElement('div');"
    "for(var i=0;i<s.rangeCount;i++)d.appendChild(s.getRangeAt(i).cloneContents());h=d.innerHTML;}"
    "var m=function(q){var e=document.querySelector(q);return e?(e.getAttribute('content')||e.getAttribute('datetime')||''):'';};"
    "var f={token:'__TOKEN__',url:location.href,title:document.title,"
    "html:h?'':document.documentElement.outerHTML,selection:h,"
    "author:m('meta[name=\"author\"]'),published:m('meta[property=\"article:published_time\"]')||m('time[datetime]')};"
    "var form=document.createElement('form');form.method='POST';form.action='__ENDPOINT__';"
    "form.target='_blank';form.style.display='none';"
    "for(var k in f){var ta=document.createElement('textarea');ta.name=k;ta.value=f[k]==null?'':f[k];form.appendChild(ta);}"
    "document.body.appendChild(form);form.submit();"
    "setTimeout(function(){form.remove();},1000);}catch(e){alert('Clip error: '+e.message);}})();"
)


def install_page() -> str:
    import json as _json

    # Plain string with a __TMPL__ placeholder (avoids f-string brace-escaping
    # for all the CSS/JS). The bookmarklet is shown in a selectable box with a
    # Copy button — copying a `javascript:` link's href is near-impossible on
    # iOS, so we hand the user the literal code to paste into a bookmark.
    page = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Clip to vault — setup</title>
<style>
 body{font:16px/1.6 system-ui,-apple-system,sans-serif;max-width:42rem;margin:1.4rem auto;padding:0 1rem;color:#1d2129}
 h1{font-size:1.4rem;margin-bottom:.2rem}
 input,textarea{width:100%;padding:.6rem;font-size:1rem;box-sizing:border-box;border:1px solid #cbd2d9;border-radius:8px}
 textarea{font-family:ui-monospace,Menlo,monospace;font-size:12px;height:6.5rem;white-space:pre-wrap;word-break:break-all;background:#f7f8fa;color:#333}
 button{margin-top:.5rem;padding:.7rem 1.3rem;font-size:1rem;font-weight:600;background:#0645ad;color:#fff;border:0;border-radius:8px;cursor:pointer}
 button:disabled{background:#9aa5b1;cursor:default}
 .muted{color:#6b7280}.step{background:#eef6ff;border-radius:8px;padding:.3rem 1rem;margin:1rem 0}
 ol{padding-left:1.2rem}li{margin:.35rem 0}code{font-family:ui-monospace,monospace;background:#eef;padding:1px 5px;border-radius:4px}
 a.drag{color:#0645ad;font-weight:600;text-decoration:none}
</style></head><body>
<h1>Clip to vault — setup</h1>
<p class="muted">Saves an article into <code>~/vault/links/</code>.</p>

<p><b>1.</b> Paste your clip token (stays in this browser):</p>
<input id="tok" type="password" placeholder="VOICE_BEARER_TOKEN" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">

<p><b>2.</b> Copy the bookmarklet:</p>
<textarea id="out" readonly placeholder="(enter your token above first)"></textarea>
<button id="copy" disabled>Copy bookmarklet</button>

<div class="step">
<p><b>3. Install on iOS Safari:</b></p>
<ol>
 <li>Tap <b>Copy bookmarklet</b> above.</li>
 <li>Bookmark <i>this</i> page — Share → <b>Add Bookmark</b> → Save.</li>
 <li>Open <b>Bookmarks</b> → <b>Edit</b> → tap that bookmark.</li>
 <li>Select the whole address, delete it, <b>paste</b>, and rename it “Clip to vault”. Done.</li>
</ol>
<p class="muted">Desktop: drag this to your bookmarks bar instead → <a id="drag" class="drag" href="#">📎 Clip to vault</a></p>
</div>

<p class="muted">Then tapping the bookmark on any article clips it (or your current text selection) and opens a “Clipped ✓” tab.</p>

<script>
 var TMPL=__TMPL__, EP=location.origin+'/clip';
 var tok=document.getElementById('tok'),out=document.getElementById('out'),copy=document.getElementById('copy'),drag=document.getElementById('drag');
 function build(){
   var t=tok.value.trim();
   if(!t){out.value='';copy.disabled=true;drag.removeAttribute('href');return;}
   var code='javascript:'+encodeURIComponent(TMPL.replace('__ENDPOINT__',EP).replace('__TOKEN__',t));
   out.value=code;copy.disabled=false;drag.href=code;
 }
 tok.addEventListener('input',build);
 copy.addEventListener('click',function(){
   if(!out.value)return;
   var done=function(){copy.textContent='Copied ✓';setTimeout(function(){copy.textContent='Copy bookmarklet';},1600);};
   if(navigator.clipboard&&navigator.clipboard.writeText){
     navigator.clipboard.writeText(out.value).then(done,function(){out.focus();out.select();document.execCommand('copy');done();});
   }else{out.focus();out.select();document.execCommand('copy');done();}
 });
 drag.addEventListener('click',function(e){if(!drag.getAttribute('href'))e.preventDefault();});
</script></body></html>"""
    return page.replace("__TMPL__", _json.dumps(BOOKMARKLET_TEMPLATE))
