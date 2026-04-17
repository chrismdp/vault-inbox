"""Accept audio POSTs from iOS Shortcuts, save to vault, fire transcribe."""

import hmac
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

VAULT_AUDIO = Path.home() / "vault" / "audio" / "recordings"
VAULT_AUDIO.mkdir(parents=True, exist_ok=True)

TRANSCRIBE_SCRIPT = Path.home() / "vault" / "scripts" / "transcribe-voice-inbox.sh"
TRANSCRIBE_LOG = Path("/tmp/transcribe-voice-inbox.log")

TOKEN = os.environ.get("VOICE_BEARER_TOKEN")
if not TOKEN:
    raise RuntimeError("VOICE_BEARER_TOKEN not set")

CONTENT_TYPE_EXT = {
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
}
ALLOWED_EXTS = set(CONTENT_TYPE_EXT.values())

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

app = FastAPI()


def check_auth(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(presented, TOKEN):
        raise HTTPException(status_code=403, detail="invalid token")


def pick_ext(content_type: str | None, filename: str | None) -> str:
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix in ALLOWED_EXTS:
            return suffix
    if content_type:
        base = content_type.split(";", 1)[0].strip().lower()
        if base in CONTENT_TYPE_EXT:
            return CONTENT_TYPE_EXT[base]
    return ".bin"


def safe_label(raw: str | None) -> str:
    if not raw:
        return ""
    return SAFE_NAME.sub("-", raw).strip("-")[:40]


def timestamped_path(ext: str, label: str) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    parts = ["voice", stamp]
    if label:
        parts.append(label)
    return VAULT_AUDIO / ("-".join(parts) + ext)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/voice")
async def voice(
    request: Request,
    authorization: str | None = Header(default=None),
    file: UploadFile | None = File(default=None),
):
    check_auth(authorization)
    label = safe_label(request.query_params.get("label"))

    if file is not None:
        ext = pick_ext(file.content_type, file.filename)
        data = await file.read()
    else:
        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="empty body")
        ext = pick_ext(request.headers.get("content-type"), None)

    dest = timestamped_path(ext, label)
    dest.write_bytes(data)

    transcribe_pid = None
    if TRANSCRIBE_SCRIPT.exists():
        with TRANSCRIBE_LOG.open("a") as log:
            proc = subprocess.Popen(
                [str(TRANSCRIBE_SCRIPT), str(dest), label],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            transcribe_pid = proc.pid

    return JSONResponse(
        {"ok": True, "path": str(dest), "bytes": len(data), "transcribe_pid": transcribe_pid}
    )
