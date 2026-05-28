"""Tests for the /clip endpoint — auth, extraction, selection, routing, dest."""

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-abc"

ARTICLE_HTML = """<html><head><title>Real Title</title>
<meta name="author" content="Jane Doe">
<meta property="og:site_name" content="Example News">
<meta name="description" content="A short summary."></head>
<body><header><nav>Home About</nav></header>
<article><h1>Real Title</h1>
<p>First <b>bold</b> paragraph with a <a href="https://l.com">link</a> and enough text to keep.</p>
<h2>Sub</h2>
<p>Closing paragraph with plenty of substantive text so readability keeps the block intact.</p>
</article><footer>(c) 2026 junk</footer></body></html>"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_BEARER_TOKEN", TOKEN)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    for mod in ("main", "clip"):
        sys.modules.pop(mod, None)
    main = importlib.import_module("main")
    monkeypatch.setattr(main, "TRANSCRIBE_SCRIPT", tmp_path / "nope.sh")
    return TestClient(main.app), tmp_path, monkeypatch


def _auth(h=TOKEN):
    return {"authorization": f"Bearer {h}"}


def _links(home):
    return home / "vault" / "links"  # the browser-extension drop zone


def test_requires_auth(client):
    c, _, _ = client
    assert c.post("/clip", json={"url": "https://x.com"}).status_code == 401
    assert c.post("/clip", json={"url": "https://x.com"}, headers=_auth("wrong")).status_code == 401


def test_requires_valid_url(client):
    c, _, _ = client
    assert c.post("/clip", json={"title": "no url"}, headers=_auth()).status_code == 422


def test_url_without_content_rejected(client):
    """The server never fetches — a url with no html/selection is a 422, not an
    SSRF-prone server-side fetch."""
    c, home, _ = client
    r = c.post("/clip", json={"url": "https://example.com/x"}, headers=_auth())
    assert r.status_code == 422
    assert not (home / "vault" / "links").exists()


def test_clips_html_to_markdown_into_links(client):
    c, home, _ = client
    r = c.post("/clip", json={"url": "https://example.com/post", "html": ARTICLE_HTML, "tags": ["ai"]}, headers=_auth())
    assert r.status_code == 200 and r.json()["ok"] and not r.json()["updated"]

    files = list(_links(home).glob("*.md"))  # root, where collect.sh scans
    assert len(files) == 1 and files[0].name == "real-title.md"
    text = files[0].read_text()
    assert "title: 'Real Title'" in text and "url: 'https://example.com/post'" in text
    assert "source: 'web clip'" in text
    assert "type: clipping" not in text
    assert "**bold**" in text and "[link](https://l.com)" in text  # formatting kept
    assert "Home About" not in text and "junk" not in text  # nav/footer stripped


def test_form_post_returns_html_and_takes_token_field(client):
    """The bookmarklet path: form-encoded, token as a field, HTML response."""
    c, home, _ = client
    r = c.post(
        "/clip",
        data={"token": TOKEN, "url": "https://example.com/post", "html": ARTICLE_HTML},
    )
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Clipped to vault" in r.text
    assert list(_links(home).glob("*.md"))
    # bad token via form → HTML 401, nothing written
    r2 = c.post("/clip", data={"token": "nope", "url": "https://x.com/y", "html": ARTICLE_HTML})
    assert r2.status_code == 401 and "text/html" in r2.headers["content-type"]


def test_form_path_422_on_no_content(client):
    """The bookmarklet (form) path's 422 branch returns HTML, not a JSON 500."""
    c, _, _ = client
    r = c.post("/clip", data={"token": TOKEN, "url": "https://example.com/x"})
    assert r.status_code == 422 and "text/html" in r.headers["content-type"]


def test_non_ascii_token_is_rejected_not_500(client):
    """A non-ASCII form token must be a clean 401, not a hmac.compare_digest
    TypeError → 500. (The header path can't carry non-ASCII over the wire.)"""
    c, _, _ = client
    r = c.post("/clip", data={"token": "töken", "url": "https://x.com", "html": ARTICLE_HTML})
    assert r.status_code == 401


def test_tags_as_comma_string(client):
    c, home, _ = client
    c.post("/clip", json={"url": "https://example.com/post", "html": ARTICLE_HTML, "tags": "ai, reading ,"}, headers=_auth())
    text = next(_links(home).glob("*.md")).read_text()
    assert "tags: ['ai', 'reading']" in text  # split, trimmed, empties dropped


def test_title_with_quote_and_newline_stays_single_line(client):
    c, home, _ = client
    c.post("/clip", json={"url": "https://example.com/post", "html": ARTICLE_HTML,
                          "title": "It's a\nbroken title"}, headers=_auth())
    text = next(_links(home).glob("*.md")).read_text()
    # newline collapsed, quote doubled, frontmatter stays one line
    assert "title: 'It''s a broken title'" in text


def test_clips_selection_marks_source(client):
    c, home, _ = client
    r = c.post(
        "/clip",
        json={"url": "https://s.com/y", "title": "Sel", "selection": "<p>just <em>this</em></p>"},
        headers=_auth(),
    )
    assert r.status_code == 200
    text = next(_links(home).glob("*.md")).read_text()
    assert "source: 'web clip (selection)'" in text
    assert "just *this*" in text


def test_reclip_same_url_overwrites(client):
    c, home, _ = client
    p = {"url": "https://example.com/post", "html": ARTICLE_HTML}
    c.post("/clip", json=p, headers=_auth())
    assert c.post("/clip", json=p, headers=_auth()).json()["updated"] is True
    assert len(list(_links(home).glob("*.md"))) == 1


def test_same_slug_different_url_disambiguates(client):
    c, home, _ = client
    c.post("/clip", json={"url": "https://a.com/x", "title": "Same Title", "selection": "one"}, headers=_auth())
    c.post("/clip", json={"url": "https://b.com/y", "title": "Same Title", "selection": "two"}, headers=_auth())
    names = sorted(p.name for p in _links(home).glob("*.md"))
    assert names == ["same-title-2.md", "same-title.md"]


def test_dest_is_configurable(client):
    c, home, monkeypatch = client
    monkeypatch.setenv("TROVE_CLIP_DEST", "clippings")
    c.post("/clip", json={"url": "https://example.com/post", "html": ARTICLE_HTML}, headers=_auth())
    assert list((home / "vault" / "clippings").glob("*.md"))
    assert not _links(home).exists()


def test_clip_does_not_touch_voice_pipeline(client):
    c, home, _ = client
    c.post("/clip", json={"url": "https://example.com/post", "html": ARTICLE_HTML}, headers=_auth())
    rec = home / "vault" / "audio" / "recordings"
    assert not rec.exists() or not list(rec.glob("*"))
