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


def _reference(home):
    return home / "vault" / "links" / "reference"


def test_requires_auth(client):
    c, _, _ = client
    assert c.post("/clip", json={"url": "https://x.com"}).status_code == 401
    assert c.post("/clip", json={"url": "https://x.com"}, headers=_auth("wrong")).status_code == 403


def test_requires_valid_url(client):
    c, _, _ = client
    assert c.post("/clip", json={"title": "no url"}, headers=_auth()).status_code == 422


def test_clips_html_to_markdown(client):
    c, home, _ = client
    r = c.post("/clip", json={"url": "https://example.com/post", "html": ARTICLE_HTML, "tags": ["ai"]}, headers=_auth())
    assert r.status_code == 200 and r.json()["ok"] and not r.json()["updated"]

    files = list(_reference(home).glob("*.md"))
    assert len(files) == 1
    assert files[0].name == "real-title.md"  # slug filename, house style
    text = files[0].read_text()
    assert "title: 'Real Title'" in text  # single-quoted, house style
    assert "status: to-read" in text  # lands in the read queue
    assert "source: 'web clip'" in text
    assert "type: clipping" not in text  # NOT the foreign shape
    assert "**bold**" in text and "[link](https://l.com)" in text  # formatting kept
    assert "Home About" not in text and "junk" not in text  # nav/footer stripped


def test_clips_selection_marks_source(client):
    c, home, _ = client
    r = c.post(
        "/clip",
        json={"url": "https://s.com/y", "title": "Sel", "selection": "<p>just <em>this</em></p>"},
        headers=_auth(),
    )
    assert r.status_code == 200
    text = next(_reference(home).glob("*.md")).read_text()
    assert "source: 'web clip (selection)'" in text
    assert "just *this*" in text


def test_reclip_same_url_overwrites(client):
    c, home, _ = client
    p = {"url": "https://example.com/post", "html": ARTICLE_HTML}
    c.post("/clip", json=p, headers=_auth())
    assert c.post("/clip", json=p, headers=_auth()).json()["updated"] is True
    assert len(list(_reference(home).glob("*.md"))) == 1


def test_same_slug_different_url_disambiguates(client):
    c, home, _ = client
    c.post("/clip", json={"url": "https://a.com/x", "title": "Same Title", "selection": "one"}, headers=_auth())
    c.post("/clip", json={"url": "https://b.com/y", "title": "Same Title", "selection": "two"}, headers=_auth())
    names = sorted(p.name for p in _reference(home).glob("*.md"))
    assert names == ["same-title-2.md", "same-title.md"]


def test_dest_is_configurable(client):
    c, home, monkeypatch = client
    monkeypatch.setenv("TROVE_CLIP_DEST", "clippings")
    c.post("/clip", json={"url": "https://example.com/post", "html": ARTICLE_HTML}, headers=_auth())
    assert list((home / "vault" / "clippings").glob("*.md"))
    assert not _reference(home).exists()


def test_clip_does_not_touch_voice_pipeline(client):
    c, home, _ = client
    c.post("/clip", json={"url": "https://example.com/post", "html": ARTICLE_HTML}, headers=_auth())
    rec = home / "vault" / "audio" / "recordings"
    assert not rec.exists() or not list(rec.glob("*"))
