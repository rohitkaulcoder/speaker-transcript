# Speaker Transcript

A small, self-hosted web tool that turns a **YouTube URL** or an **uploaded
audio/video file** into a **speaker-labelled, cloud-archived transcript** using
[AssemblyAI](https://www.assemblyai.com/) (Universal-3.5 Pro with speaker
diarization + speaker identification) and [Supabase](https://supabase.com/)
for storage.

**Live demo:** https://speaker-transcript.onrender.com

```
YouTube URL ─┐
             ├─► FastAPI server (yt-dlp / stream) ─► AssemblyAI upload ─► transcript
upload file ─┘              ▲                                   │         │
                     No files kept on disk                     │         ▼
                                                                │   Supabase archive
                          ◄────── speaker-wise transcript ◄────┘   (auto-saved)
```

## Features

- **YouTube → transcript** — paste a YouTube (or any yt-dlp-supported) URL; the
  server downloads just the audio with `yt-dlp`, transcribes it, and deletes the
  temp file. Runs in the background: you instantly get a job id and a progress
  bar + live elapsed timer while it works.
- **File upload → transcript** — drag-and-drop an mp3/mp4 (or any supported
  format, see below). The file is streamed straight through to AssemblyAI's
  temporary storage, never written to the server's disk.
- **Speaker identification** — AssemblyAI's `speaker_identification` add-on
  replaces generic `A`/`B` labels with **roles** (`Host`/`Guest`) by default,
  or **real names** when you supply them (comma-separated field on each tab).
  Verified outputs look like `Wes Kao: …` / `Lenny: …`.
- **Speakers panel** — after transcription, edit any speaker label in one
  place; the whole transcript re-renders instantly (a pure label remap — no
  re-transcription, no extra API cost).
- **Automatic archive** — every completed transcript auto-saves to Supabase
  (free tier). The **Archive** tab lists history (title, creator, duration,
  date) and reopens any transcript with its saved speaker names.
- **Export** — one-click **Copy**, **Download .md**, or **Download PDF**
  (print-to-PDF) of the full transcript with timestamps.
- **Timestamps** — each utterance shows a `[0:12 – 0:25]` range.
- **Privacy-first** — media lives only in AssemblyAI's temp storage
  (auto-expires ~24h) and the app issues a `DELETE` after rendering. Persisted
  data is just the transcript text + speaker labels in your own Supabase.

## Supported input formats

AssemblyAI accepts most common **audio formats natively** and extracts the audio
track from **video formats** server-side:

| Category | Extensions |
|---|---|
| Audio | `.3ga .8svx .aac .ac3 .aif .aiff .alac .amr .ape .au .dss .flac .flv .m4a .m4b .m4p .m4r .mp3 .mpga .ogg .oga .mogg .opus .qcp .tta .voc .wav .wma .wv` |
| Video (audio extracted) | `.webm .mts .m2ts .ts .mov .mp2 .mp4 .m4v .mxf` |

So yes — **`.mp4` works fine** (and `.mov`, `.webm`, `.m4v`, even `.ts`/.mxf
recordings). You can also pass **any public audio URL** via the YouTube tab.
Limits: files **≤ 2 GB** via the tool (AssemblyAI's `/upload` ceiling is
2.2 GB, `/transcript` ~5 GB); durations 160 ms – 10 h.

Oversized files are **rejected with a clear 413 error, never truncated**.
Uploads stream to a temp file in 1 MB chunks and the SDK re-streams them to
AssemblyAI, so a 1 GB file is never loaded into the server's RAM.

## Architecture

| Layer | Tech | Why |
|---|---|---|
| API | FastAPI (Python 3.12) | Async, lightweight, serves the UI + proxies AssemblyAI |
| Frontend | Vanilla HTML/JS (no build step) | Zero toolchain; drop-in static file |
| YouTube download | `yt-dlp` (native m4a, no transcode) | Fast audio fetch; resolves YouTube JS challenges with `deno` |
| Speech-to-text | `assemblyai` SDK (`universal-3-5-pro` → `universal-2`) | Flagship with native multi-language + fallback; `speaker_labels` diarization + `speaker_identification` |
| Archive | Supabase (PostgREST via httpx, free tier) | `transcripts` table, upsert by `external_key`, RLS on |
| Hosting | Docker on Render (free) | `Dockerfile` + `render.yaml` blueprint |

### Data flow

1. **Client** submits a YouTube URL (`POST /api/transcribe/url`) or uploads a
   file (`POST /api/transcribe/upload`). The browser never holds an API key.
2. **URL jobs** run in the background: yt-dlp grabs the best **native m4a**
   (no ffmpeg transcode — that was the free-tier CPU bottleneck), then the
   AssemblyAI SDK streams it up (`submit` → `transcript_id`). The API returns a
   `job_id` immediately.
3. **Client polls** `GET /api/job/{job_id}` until a `transcript_id` appears,
   then `GET /api/transcript/{id}` every ~2.5 s. The completed payload includes
   `utterances[]` (`{speaker, text, start, end}`) — where `speaker` is already
   the identified role/name — plus `speaker_mapping` (e.g. `{"A": "Wes Kao"}`).
4. The client renders the speaker panel + color-coded transcript, fires
   `DELETE /api/transcript/{id}` to purge AssemblyAI's copy, and auto-saves the
   finished transcript to the Supabase archive (`POST /api/archive`).
5. The **Archive** tab calls `GET /api/archive` to list history and
   `GET /api/archive/{id}` to reopen a transcript with its saved speaker names.

**Measured on Render free tier (end-to-end):** YouTube URL → completed
speaker-labelled transcript in **~60 s** (≈43 s download + submit, ≈17 s
AssemblyAI).

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI (single page: new transcript + Archive tabs) |
| `POST` | `/api/transcribe/url` | `{url, speakers_expected?, language_code?, speaker_type?, speakers?}` → yt-dlp download in background, returns `{job_id, status: "downloading"}` (202) |
| `GET` | `/api/job/{job_id}` | Poll URL-job; yields `transcript_id` + metadata when download+submit finish |
| `POST` | `/api/transcribe/upload` | multipart `file` (+ optional `speakers_expected`, `language_code`, `speaker_type`, `speakers`) → returns `{transcript_id, status: "queued"}` (202) |
| `GET` | `/api/transcript/{id}` | Poll. Returns `{status, id, utterances[], speaker_mapping}` when completed |
| `DELETE` | `/api/transcript/{id}` | Purges the transcript from AssemblyAI (204) |
| `POST` | `/api/archive` | Save a completed transcript to Supabase (upsert by `external_key`). Accepts `transcript_id` (server fetches) or `utterances` directly |
| `GET` | `/api/archive` | List archived transcripts, newest first (`?limit=`) |
| `GET` | `/api/archive/{id}` | Full archived transcript: `utterances`, `speaker_mapping`, metadata |
| `PATCH` | `/api/archive/{id}` | Update `speaker_mapping` on an archived row |

Speaker identification params (both transcribe endpoints):
- `speaker_type`: `"role"` (default, Host/Guest) or `"name"`
- `speakers`: e.g. `[{"role": "Host"}, {"role": "Guest"}]` or `[{"name": "Wes Kao"}, ...]`

Other optional params: `speakers_expected` (int 2–10, diarization hint),
`language_code` (e.g. `"en"`, `"hi"`, `"es"`).

## Local development

```bash
# 1. Requires Python 3.10+ and a JS runtime for yt-dlp's YouTube challenge
#    solver (deno 2.3+ or node 22+; brew install deno node). ffmpeg no longer
#    needed for YouTube — the app sends native m4a to AssemblyAI.
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. API keys (get at https://www.assemblyai.com/ and https://supabase.com/)
export ASSEMBLYAI_API_KEY=your_key
export SUPABASE_URL=https://xxxx.supabase.co
export SUPABASE_SERVICE_KEY=your_service_role_key

# 3. Optional: YouTube cookies to avoid bot-checks on DC IPs
#    export YTDLP_COOKIES="$(base64 -i cookies.txt | tr -d '\n')"

# 4. Run
uvicorn app.main:app --reload --port 8080
```

Open http://localhost:8080. No `.env` loader is imported by the app — export the
variable (or use `dotenv`/your shell).

## Deployment (Render)

The repo ships everything needed:

- **`Dockerfile`** — Python 3.12 + `ffmpeg` (optional, unused for YouTube now) +
  `deno` + node (for yt-dlp's YouTube JS-challenge solver).
- **`render.yaml`** — Blueprint for a free web service (Oregon, health check `/`).

**Via dashboard:** Render → New → Blueprint → select
`rohitkaulcoder/speaker-transcript` → set the `ASSEMBLYAI_API_KEY`,
`SUPABASE_URL`, and `SUPABASE_SERVICE_KEY` env vars. Run the archive migration
from `supabase/migrations/` in the Supabase SQL editor.

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
  "envVars": [
    { "key": "ASSEMBLYAI_API_KEY", "value": "<key>" },
    { "key": "SUPABASE_URL", "value": "https://xxxx.supabase.co" },
    { "key": "SUPABASE_SERVICE_KEY", "value": "<service_role_key>" }
  ]
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
| `SUPABASE_URL` | yes (for archive) | Your Supabase project URL, e.g. `https://xxxx.supabase.co` |
| `SUPABASE_SERVICE_KEY` | yes (for archive) | Supabase **service_role** key (secret). Never expose to the browser |
| `YTDLP_COOKIES` | no (for YT) | Base64-encoded Netscape-format cookies (YouTube session) — bypasses YouTube's bot check on datacenter IPs. Export with yt-dlp: `yt-dlp --cookies-from-browser chrome --cookies cookies.txt`, then `base64 -i cookies.txt \| tr -d '\n'`. Re-export when it stops working (cookies expire). |

The archive table is idempotent SQL in `supabase/migrations/20260809_archive.sql`
(run via the Supabase SQL editor or CLI; all statements are `if not exists`).

## AssemblyAI notes (verified against live API docs)

- **Auth:** `Authorization: <raw key>` — no `Bearer` prefix (server-side only).
- **Model:** `speech_models: ["universal-3-5-pro", "universal-2"]` — flagship
  first, stable fallback. 18 languages natively, auto-falls back for the rest.
- **Diarization:** `speaker_labels: true` (+ optional `speakers_expected`).
  Responses include `utterances[].start/end` (ms) for timestamps.
- **Speaker identification** (`speaker_identification`): infra type `"role"`
  uses roles you supply (Host/Guest); type `"name"` needs names you provide or
  names spoken in the audio. The completed payload includes
  `speaker_mapping` (`{A: name, B: name}`) and utterance `speaker` fields are
  already replaced with the identified labels.
- **Deprecated params avoided:** no `summarization`/`auto_chapters`/`speech_model`
  (singular). Transcript cleanup via `Transcript.delete_by_id`.
- **Billing:** per-hour rates: U3.5 Pro **$0.21**, Speaker Diarization **+$0.02**,
  Speaker Identification **+$0.02** → **≈ $0.25/video-hour** (~$0.02 per typical
  5-min clip). Supabase free tier = $0. If your AssemblyAI balance is negative,
  transcription is rejected with `Your current account balance is negative` —
  top up at assemblyai.com.

## Known limitations

- Speaker **names** can only be identified when you supply them or when they're
  spoken in the audio — AssemblyAI can't invent names from voice alone.
- YouTube auto-captions are intentionally not used (quality); we always run ASR
  so diarization works. YouTube cookie sessions expire — re-export if the bot
  check returns.
- Free-tier Render sleeps after ~15 min idle (cold start ~30–60 s).

## Repo layout

```
app/
  main.py          # FastAPI: transcribe/job/archive routes, yt-dlp download
  assembly.py      # assemblyai SDK wrapper (submit / poll / speaker identification)
  archive.py       # Supabase PostgREST client (upsert / list / get / patch)
  static/
    index.html     # single-page web UI (transcribe + archive + speakers panel)
supabase/
  migrations/
    20260809_archive.sql   # archive table + RLS
Dockerfile         # container for Render/host
render.yaml        # Render Blueprint
requirements.txt
```
