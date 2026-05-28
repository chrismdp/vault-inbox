"""Tests for the /clip endpoint — auth, extraction, selection, routing."""

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
    return TestClient(main.app), tmp_path


def _auth(h=TOKEN):
    return {"authorization": f"Bearer {h}"}


def test_requires_auth(client):
    c, _ = client
    assert c.post("/clip", json={"url": "https://x.com"}).status_code == 401
    assert c.post("/clip", json={"url": "https://x.com"}, headers=_auth("wrong")).status_code == 403


def test_requires_valid_url(client):
    c, _ = client
    r = c.post("/clip", json={"title": "no url"}, headers=_auth())
    assert r.status_code == 422


def test_clips_html_to_markdown(client):
    c, home = client
    r = c.post("/clip", json={"url": "https://example.com/post", "html": ARTICLE_HTML, "tags": ["ai"]}, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and not body["updated"]

    files = list((home / "vault" / "clippings").glob("*.md"))
    assert len(files) == 1
    text = files[0].read_text()
    assert "type: clipping" in text
    assert "title: Real Title" in text
    assert "author: Jane Doe" in text
    assert "**bold**" in text  # formatting preserved
    assert "[link](https://l.com)" in text  # links preserved
    assert "Home About" not in text  # nav stripped
    assert "junk" not in text  # footer stripped


def test_clips_selection(client):
    c, home = client
    r = c.post(
        "/clip",
        json={"url": "https://s.com/y", "title": "Sel", "selection": "<p>just <em>this</em></p>"},
        headers=_auth(),
    )
    assert r.status_code == 200
    text = next((home / "vault" / "clippings").glob("*.md")).read_text()
    assert "clip_kind: selection" in text
    assert "just *this*" in text


def test_reclip_same_url_overwrites(client):
    c, home = client
    p = {"url": "https://example.com/post", "html": ARTICLE_HTML}
    c.post("/clip", json=p, headers=_auth())
    r2 = c.post("/clip", json=p, headers=_auth())
    assert r2.json()["updated"] is True
    assert len(list((home / "vault" / "clippings").glob("*.md"))) == 1


def test_clip_does_not_touch_voice_pipeline(client):
    """A clip must not write into audio/recordings or fire the transcribe hook."""
    c, home = client
    c.post("/clip", json={"url": "https://example.com/post", "html": ARTICLE_HTML}, headers=_auth())
    assert not (home / "vault" / "audio" / "recordings").exists() or not list(
        (home / "vault" / "audio" / "recordings").glob("*")
    )
