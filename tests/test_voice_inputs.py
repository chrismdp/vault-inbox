"""Action Button uploads follow the Today input queue contract."""

import importlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

TOKEN = "voice-input-test-token"
AUDIO = b"recorded words"
AUTH = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "audio/mp4"}


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("VAULT_INBOX_BEARER_TOKEN", TOKEN)
    recordings = tmp_path / "recordings"
    queue = tmp_path / "inputs" / "queue"
    queue.mkdir(mode=0o700, parents=True)
    monkeypatch.setenv("VOICE_INBOX_AUDIO", str(recordings))
    monkeypatch.setenv("CHRIS_INPUTS_QUEUE", str(queue))
    # Even an installed legacy hook must never run.
    hook = tmp_path / "vault/scripts/transcribe-voice-inbox.sh"
    hook.parent.mkdir(parents=True)
    hook.touch()
    popen = Mock(side_effect=AssertionError("Unexpected subprocess"))
    monkeypatch.setattr("subprocess.Popen", popen)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    if hasattr(main, "TRANSCRIBE_LOG"):
        monkeypatch.setattr(main, "TRANSCRIBE_LOG", tmp_path / "legacy.log")
    yield TestClient(main.app), recordings, queue, main
    popen.assert_not_called()
    assert not (tmp_path / "vault/transcripts").exists()


@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("label", [None, "Planning café"])
@pytest.mark.parametrize("mask", [0o077, 0o022])
def test_queue_contract_and_atomic_ready_signal(inbox, monkeypatch, multipart, label, mask):
    client, recordings, queue, _ = inbox
    published = []
    replace = Path.replace

    def observe_publish(path, target):
        target = Path(target)
        if target.parent == queue:
            assert path.parent == queue
            assert path.name.startswith(".") and path.suffix == ".tmp"
            assert not list(queue.glob("*.json"))
            assert path.stat().st_mode & 0o777 == 0o666 & ~mask
            if target.suffix == ".json":
                record = json.loads(path.read_text())
                assert (queue / record["clip"]).read_bytes() == AUDIO
                assert len(published) == 1
            published.append(target)
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", observe_publish)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    old_mask = os.umask(mask)
    try:
        params = {"label": label} if label is not None else {}
        if multipart:
            response = client.post("/voice", params=params,
                                   headers={"Authorization": AUTH["Authorization"]},
                                   files={"file": ("note.m4a", AUDIO, "audio/mp4")})
        else:
            response = client.post("/voice", params=params, headers=AUTH, content=AUDIO)
    finally:
        os.umask(old_mask)

    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is True and result["bytes"] == len(AUDIO)
    assert "transcribe_pid" not in result
    saved = Path(result["path"])
    assert saved.parent == recordings and saved.read_bytes() == AUDIO
    assert recordings.stat().st_mode & 0o777 == 0o700
    assert queue.stat().st_mode & 0o777 == 0o700
    record_path, = queue.glob("*.json")
    record = json.loads(record_path.read_text())
    input_id = record_path.stem
    assert re.fullmatch(r"[0-9]{8}T[0-9]{6}-[0-9a-f]{8}", input_id)
    at = datetime.fromisoformat(record["at"])
    assert before <= at <= datetime.now(timezone.utc)
    assert record["at"] == at.isoformat(timespec="seconds")
    assert at.utcoffset().total_seconds() == 0
    assert input_id.startswith(at.strftime("%Y%m%dT%H%M%S"))
    expected = {"id": input_id, "at": record["at"], "kind": "update", "ref": None,
                "text": None, "picks": [], "clip": f"{input_id}.mp4",
                "source": "action-button"}
    if label is not None:
        expected["label"] = label
    assert record == expected
    assert result["input_id"] == input_id
    assert published == [queue / record["clip"], record_path]
    assert set(queue.iterdir()) == set(published)


@pytest.mark.parametrize("failure", ["missing", "clip-write", "clip-rename", "json-write", "json-rename"])
def test_queue_failure_retains_recording_without_ready_json(inbox, monkeypatch, failure):
    client, recordings, queue, _ = inbox
    if failure == "missing":
        queue.rmdir()
    else:
        stage, operation = failure.split("-")
        method = "replace" if operation == "rename" else ("write_bytes" if stage == "clip" else "write_text")
        original = getattr(Path, method)

        def fail(path, *args, **kwargs):
            if path.parent == queue and ((".json." in path.name) == (stage == "json")):
                if operation == "write":
                    original(path, *args, **kwargs)
                raise OSError("Queue write failed")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, method, fail)
    response = client.post("/voice", headers=AUTH, content=AUDIO)
    assert response.status_code == 503
    result = response.json()
    assert result["ok"] is False
    assert result["error"] == "Recording saved, but could not queue it for Today."
    saved, = recordings.iterdir()
    assert result["path"] == str(saved)
    assert saved.read_bytes() == AUDIO
    assert not list(queue.glob("*"))
    if failure == "missing":
        assert not queue.exists()


@pytest.mark.parametrize("authorization,status", [(None, 401), ("Bearer wrong", 403), ("Basic x", 401)])
def test_auth_prevents_recording_and_queue_writes(inbox, authorization, status):
    client, recordings, queue, _ = inbox
    headers = {"Content-Type": "audio/mp4"}
    if authorization:
        headers["Authorization"] = authorization
    response = client.post("/voice", headers=headers, content=AUDIO)
    assert response.status_code == status
    assert not list(recordings.iterdir())
    assert not list(queue.iterdir())


def test_default_queue_location(inbox, monkeypatch, tmp_path):
    monkeypatch.delenv("CHRIS_INPUTS_QUEUE")
    queue = tmp_path / ".local/state/chris-inputs/queue"
    queue.mkdir(mode=0o700, parents=True)
    main = importlib.reload(sys.modules["main"])
    if hasattr(main, "TRANSCRIBE_LOG"):
        monkeypatch.setattr(main, "TRANSCRIBE_LOG", tmp_path / "legacy.log")
    response = TestClient(main.app).post("/voice", headers=AUTH, content=AUDIO)
    assert response.status_code == 200
    assert len(list(queue.glob("*.json"))) == 1
