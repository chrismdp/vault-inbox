# voice-inbox

A tiny FastAPI endpoint that accepts audio POSTs from iOS Shortcuts (or anything) and drops the file onto disk. A bearer token gates the endpoint. Optionally fires a hook script after saving, which is where you wire up transcription.

## Why this exists

iOS doesn't give apps programmatic access to iCloud Drive, and the Telegram Shortcuts integration won't attach audio. Recording a voice note, uploading it to your own server, and taking it from there via whatever pipeline you like turns out to be the simplest reliable path — and running it yourself means no third-party sees the audio.

This is the server side. On the phone: an iOS Shortcut bound to the Action Button that records audio and POSTs it here. The handoff script is a single `Get Contents of URL` action.

## How it works

```
iOS Shortcut (Action Button)
  → POST https://your.host/voice  (Authorization: Bearer <token>)
    → voice-inbox (FastAPI on 127.0.0.1:8790)
      → writes ~/vault/audio/recordings/voice-YYYY-MM-DD_HH-MM-SS[-label].<ext>
      → fires $HOME/vault/scripts/transcribe-voice-inbox.sh if it exists (fire-and-forget)
```

The hook script is optional. Without it you just get an archive of recordings on disk. With it you can wire up transcription, indexing, or anything else you like — the endpoint just saves the file and spawns the script with `<path> <label>`.

## Install

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/chrismdp/voice-inbox.git
cd voice-inbox
uv sync
```

Create `.env` (gitignored) with a bearer token — generate one with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`:

```
VOICE_BEARER_TOKEN=<random-token>
```

Run it:

```bash
uv run uvicorn main:app --host 127.0.0.1 --port 8790
```

Put it behind nginx or Caddy with TLS — see `nginx-voice.snippet.conf` for a sample location block (`client_max_body_size 50M`, `proxy_request_buffering off`).

## systemd

Sample user unit — copy to `~/.config/systemd/user/voice-inbox.service`:

```ini
[Unit]
Description=voice-inbox
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/voice-inbox
EnvironmentFile=/path/to/voice-inbox/.env
ExecStart=/usr/local/bin/uv run --no-sync uvicorn main:app --host 127.0.0.1 --port 8790
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Then `systemctl --user enable --now voice-inbox` (and `loginctl enable-linger $USER` so it survives logout).

## iOS Shortcut recipe

1. **Record Audio** — Audio Quality: Normal, Start Recording: On Tap
2. **Get Contents of URL**
   - URL: `https://your.host/voice?label=note`
   - Method: `POST`
   - Headers: `Authorization: Bearer <your-token>`
   - Request Body: **File** — pick the Recorded Audio variable
3. Bind the shortcut to the Action Button: Settings → Action Button → Shortcut.

The optional `?label=note` query parameter is sanitised server-side and appended to the filename.

## Endpoint

`POST /voice` — accepts either a raw audio body or `multipart/form-data` with a `file` field.

Request headers:
- `Authorization: Bearer <VOICE_BEARER_TOKEN>` (required)
- `Content-Type` — used to pick the file extension

Optional query param:
- `?label=<string>` — sanitised (`[^A-Za-z0-9._-]` → `-`, truncated to 40 chars), appended to the saved filename

Response:
```json
{ "ok": true, "path": "...", "bytes": 100140, "transcribe_pid": 12345 }
```

`GET /health` — unauthenticated liveness check, returns `{"ok": true}`.

## Security

- Bearer-token auth with timing-safe comparison (`hmac.compare_digest`)
- Filename extension restricted to a known audio-type allow-list; anything else falls back to `.bin`
- Labels sanitised before being used in a filename or passed to the hook script
- The hook script is invoked via `subprocess.Popen` with a list argv (no shell), and the companion `transcribe-voice-inbox.sh` uses a quoted heredoc and env-passed variables to block shell injection
- Auth and sanitisation are covered by `tests/test_security.py`

## Tests

```bash
uv run pytest
```

Covers: missing/wrong/malformed bearer, empty body, path-traversal via label, shell-metacharacter labels, extension restrictions, and multipart filename spoofing.

## Hook script

The hook is not part of this repo — it's whatever you want to do after an audio file lands. I run a companion Bash script (`~/vault/scripts/transcribe-voice-inbox.sh`) that calls OpenAI's transcription API, drops a markdown transcript into my Obsidian vault, and appends an entry to my inbox file. Yours could do anything — forward to S3, push to a webhook, index into a search engine — or nothing at all.

If `$HOME/vault/scripts/transcribe-voice-inbox.sh` doesn't exist, the endpoint just saves the file and returns.

## Web clipping (`/clip`)

The same endpoint also takes web clips — grab an article and get it into the
vault as clean markdown. Deliberately separate from `/voice`: it does **not**
fire the transcribe hook or create a project, so clips don't bloat the inbox.

```
bookmarklet / share → POST /clip (Authorization: Bearer <token>)
  → readability extracts the article + markdownify → markdown
  → writes ~/vault/links/reference/<slug>.md
```

Clips are written in the **same house style as `save-article.sh`** (the
`/process-link` fetcher): slug filenames with `-2..-9` collision disambiguation,
single-quoted YAML, and `title / url / source / saved / status: to-read` — so a
clip is indistinguishable from any other saved article and lands in the read
queue. The destination is configurable with `TROVE_CLIP_DEST` (a subdir under
`~/vault`, default `links/reference`; an absolute path is used as-is).

Conversion is **server-side** on purpose: a bookmarklet can't load a converter
in-page under a strict Content-Security-Policy, so it just sends the rendered
HTML (or a text selection, or a bare URL) and the server does the extraction.

`POST /clip` takes JSON (all optional except `url`):

| field | meaning |
|---|---|
| `url` | **required** — the article URL |
| `html` | rendered page HTML — server runs readability over it |
| `selection` | clip just this (html or text); tags the note `clip_kind: selection` |
| `title`, `author`, `site`, `published`, `excerpt` | metadata overrides |
| `tags` | array of strings |

If only `url` is sent, the server fetches and extracts it. Re-clipping the same
URL overwrites its file; a different page with the same title-slug gets the next
free `-N` suffix. Returns `{ok, path, title, updated, bytes}`.

```bash
curl -X POST https://your.host/clip -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"url":"https://example.com/article"}'
```

### Bookmarklet setup

Visit `https://your.host/clip/setup`, paste your token, and it builds a
bookmarklet with your endpoint + token baked in (the token stays in your
browser). On iOS: bookmark that page, then edit the bookmark's URL and paste the
generated `javascript:` link. Tapping it on any article clips it (or your current
text selection). `clip-bookmarklet.src.js` is the readable source.

## License

MIT.
