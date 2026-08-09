# Speaker Transcript

A small, self-hosted web tool that turns a **YouTube URL** or an **uploaded
audio/video file** into a **speaker-labelled transcript** using
[AssemblyAI](https://www.assemblyai.com/) (Universal-3.5 Pro with speaker
diarization).

**Live demo:** https://speaker-transcript.onrender.com

```
YouTube URL ─┐
             ├─► FastAPI server (yt-dlp / stream) ─► AssemblyAI upload ─► transcript
upload file ─┘              ▲                                                 │
                     No files kept on disk                                   │
                          ◄────── speaker-wise transcript ◄──────────────────┘
```

## Features

- **YouTube → transcript** — paste a YouTube (or any yt-dlp-supported) URL; the
  server downloads just the audio with `yt-dlp`, transcribes it, and deletes the
  temp file. Nothing is persisted past the request.
- **File upload → transcript** — drag-and-drop an mp3/mp4 (or any supported
  format, see below). The file is streamed straight through to AssemblyAI's
  temporary storage, never written to the server's disk.
- **Speaker diarization** — each utterance is attributed to a speaker (`A`,
  `B`, `C`, …) and rendered color-coded, backed by AssemblyAI `speaker_labels`
  with optional `speakers_expected` hint (2–10).
- **Privacy-first** — uploaded media lives only in AssemblyAI's temp storage
  (auto-expires ~24h), and the app issues a `DELETE` as soon as the transcript
  is delivered. No database, no file system, no cookies.

## Supported input formats

AssemblyAI accepts most common **audio formats natively** and extracts the audio
track from **video formats** server-side:

| Category | Extensions |
|---|---|
| Audio | `.3ga .8svx .aac .ac3 .aif .aiff .alac .amr .ape .au .dss .flac .flv .m4a .m4b .m4p .m4r .mp3 .mpga .ogg .oga .mogg .opus .qcp .tta .voc .wav .wma .wv` |
| Video (audio extracted) | `.webm .mts .m2ts .ts .mov .mp2 .mp4 .m4v .mxf` |

So yes — **`.mp4` works fine** (and `.mov`, `.webm`, `.m4v`, even `.ts`/.mxf
recordings). You can also pass **any public audio URL** via the YouTube tab.
Limits: files ≤ 500 MB via the tool (AssemblyAI supports up to ~5 GB directly);
durations 160 ms – 10 h.

## Architecture

| Layer | Tech | Why |
|---|---|---|
| API | FastAPI (Python 3.12) | Async, lightweight, serves the UI + proxies AssemblyAI |
| Frontend | Vanilla HTML/JS (no build step) | Zero toolchain; drop-in static file |
| YouTube download | `yt-dlp` + `ffmpeg` | Reliable audio extraction |
| Speech-to-text | `assemblyai` SDK (`universal-3-5-pro` → `universal-2`) | Current flagship with native multi-language + fallback; `speaker_labels` for diarization |
| Hosting | Docker on Render (free) | `Dockerfile` + `render.yaml` blueprint |

### Data flow

1. **Client** submits a URL (`POST /api/transcribe/url`) or a file
   (`POST /api/transcribe/upload`). The browser never holds an API key.
2. **Server** downloads/streams the audio into memory, submits it via the
   AssemblyAI SDK (non-blocking `submit` → returns `transcript_id`).
3. **Client polls** `GET /api/transcript/{id}` every ~2.5 s.
4. On **completed**, the server shapes `utterances[]` (`{speaker, text}`) into
   JSON; the client renders the color-coded transcript.
5. The client fires `DELETE /api/transcript/{id}` to purge AssemblyAI's copy.

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `POST` | `/api/transcribe/url` | `{url, speakers_expected?, language_code?}` → downloads with yt-dlp, returns `{transcript_id, status: "queued"}` (202) |
| `POST` | `/api/transcribe/upload` | multipart `file` (+ optional `speakers_expected`, `language_code`) → returns `{transcript_id, status: "queued"}` (202) |
| `GET` | `/api/transcript/{id}` | Poll. Returns `{status, id}`; adds `utterances` when completed |
| `DELETE` | `/api/transcript/{id}` | Purges the transcript from AssemblyAI (204) |

Optional query/body params: `speakers_expected` (int 2–10, diarization hint),
`language_code` (e.g. `"en"`, `"hi"`, `"es"`).

## Local development

```bash
# 1. Requires Python 3.10+ and ffmpeg (brew install ffmpeg)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. API key (get one at https://www.assemblyai.com/)
export ASSEMBLYAI_API_KEY=your_key

# 3. Run
uvicorn app.main:app --reload --port 8080
```

Open http://localhost:8080. No `.env` loader is imported by the app — export the
variable (or use `dotenv`/your shell).

## Deployment (Render)

The repo ships everything needed:

- **`Dockerfile`** — Python 3.12 + `ffmpeg` + app.
- **`render.yaml`** — Blueprint for a free web service (Oregon, health check `/`).

**Via dashboard:** Render → New → Blueprint → select
`rohitkaulcoder/speaker-transcript` → set the `ASSEMBLYAI_API_KEY` env var.

**Via API** (`POST /v1/services`, key from your Render profile):

```json
{
  "type": "web_service",
  "name": "speaker-transcript",
  "ownerId": "<your_owner_id>",
  "repo": "https://github.com/rohitkaulcoder/speaker-transcript",
  "branch": "main",
  "serviceDetails": {
    "env": "docker",
    "runtime": "docker",
    "plan": "free",
    "region": "oregon",
    "healthCheckPath": "/",
    "envSpecificDetails": { "dockerfilePath": "./Dockerfile", "dockerContext": "." }
  },
  "envVars": [{ "key": "ASSEMBLYAI_API_KEY", "value": "<key>" }]
}
```

**Free-tier caveats:** the service sleeps after ~15 min idle and takes
~30–60 s to cold-start on the first request. Builds auto-deploy on every push to
`main`.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ASSEMBLYAI_API_KEY` | yes | AssemblyAI API key (server-side only, never in the browser) |
| `ASSEMBLYAI_BASE_URL` | no | Region override, e.g. `https://api.eu.assemblyai.com` for EU residency |

## AssemblyAI notes (verified against live API docs)

- **Auth:** `Authorization: <raw key>` — no `Bearer` prefix (server-side only).
- **Model:** `speech_models: ["universal-3-5-pro", "universal-2"]` — flagship
  first, stable fallback. 18 languages natively, auto-falls back for the rest.
- **Diarization:** `speaker_labels: true` (+ optional `speakers_expected`).
- **Deprecated params avoided:** no `summarization`/`auto_chapters`/`speech_model`
  (singular). Transcript cleanup via `Transcript.delete_by_id`.
- **Billing:** unless your AssemblyAI balance is positive, transcription is
  rejected with `Your current account balance is negative` — top up at
  assemblyai.com.

## Repo layout

```
app/
  main.py          # FastAPI: routes, yt-dlp download, upload handling
  assembly.py      # assemblyai SDK wrapper (submit / get / delete)
  static/
    index.html     # single-page web UI
Dockerfile         # container for Render/host
render.yaml        # Render Blueprint
requirements.txt
```
