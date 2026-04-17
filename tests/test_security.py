"""Auth, path-traversal, and label-sanitisation tests for voice-inbox."""

import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


TOKEN = "test-token-abc"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_BEARER_TOKEN", TOKEN)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    for mod in [m for m in list(sys.modules) if m == "main"]:
        del sys.modules[mod]
    main = importlib.import_module("main")
    # Neutralise subprocess to keep tests hermetic.
    monkeypatch.setattr(main, "TRANSCRIBE_SCRIPT", tmp_path / "does-not-exist.sh")
    return TestClient(main.app), tmp_path


def test_missing_bearer_rejected(client):
    c, _ = client
    r = c.post("/voice", content=b"x", headers={"Content-Type": "audio/mp4"})
    assert r.status_code == 401


def test_wrong_bearer_rejected(client):
    c, _ = client
    r = c.post(
        "/voice",
        content=b"x",
        headers={"Authorization": "Bearer wrong", "Content-Type": "audio/mp4"},
    )
    assert r.status_code == 403


def test_malformed_auth_header_rejected(client):
    c, _ = client
    r = c.post(
        "/voice",
        content=b"x",
        headers={"Authorization": "Basic foo", "Content-Type": "audio/mp4"},
    )
    assert r.status_code == 401


def test_good_bearer_accepts_raw_body(client):
    c, tmp = client
    r = c.post(
        "/voice",
        content=b"fake-audio",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "audio/mp4"},
    )
    assert r.status_code == 200
    saved = Path(r.json()["path"])
    assert saved.exists()
    assert saved.suffix == ".m4a"
    assert saved.is_relative_to(tmp / "vault" / "audio" / "recordings")


def test_empty_body_rejected(client):
    c, _ = client
    r = c.post(
        "/voice",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "audio/mp4"},
    )
    assert r.status_code == 400


def test_label_cannot_escape_directory(client):
    c, tmp = client
    r = c.post(
        "/voice?label=../../etc/passwd",
        content=b"fake",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "audio/mp4"},
    )
    assert r.status_code == 200
    saved = Path(r.json()["path"])
    assert saved.is_relative_to(tmp / "vault" / "audio" / "recordings")
    assert "/" not in saved.name
    assert ".." not in saved.name.split("-")[-1].replace(".m4a", "")


def test_label_strips_shell_metacharacters(client):
    c, _ = client
    r = c.post(
        "/voice?label=$(touch /tmp/pwned)%22evil",
        content=b"fake",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "audio/mp4"},
    )
    assert r.status_code == 200
    name = Path(r.json()["path"]).name
    for bad in ["$", "(", ")", '"', "'", "`", ";", "|", "&", "\\", " "]:
        assert bad not in name


def test_extension_restricted_to_known_types(client):
    c, _ = client
    r = c.post(
        "/voice",
        content=b"fake",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/x-sh",
        },
    )
    assert r.status_code == 200
    assert Path(r.json()["path"]).suffix == ".bin"


def test_uploaded_filename_cannot_set_arbitrary_suffix(client):
    c, _ = client
    # multipart with a dangerous filename; the server should ignore the suffix.
    r = c.post(
        "/voice",
        headers={"Authorization": f"Bearer {TOKEN}"},
        files={"file": ("malicious.sh", b"echo hi", "audio/mp4")},
    )
    assert r.status_code == 200
    assert Path(r.json()["path"]).suffix == ".m4a"


def test_health_requires_no_auth(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
