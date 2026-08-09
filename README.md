# Speaker Transcript

A tiny web tool that turns a **YouTube URL** or an **uploaded mp3/mp4** into a
**speaker-labelled transcript** using [AssemblyAI](https://www.assemblyai.com/)
(Universal-2 with speaker diarization).

No lock-in to Groq and no files stored on your server: uploads stream straight
through to AssemblyAI's temporary storage, and the transcript is deleted as soon
as it has been fetched.

## How it works

```
YouTube URL ─┐
             ├─► FastAPI server (yt-dlp / stream) ─► AssemblyAI upload ─► transcript
Upload file ─┘              ▲                                                 │
                      No files kept on disk                                   │
                            ◄──────── speaker-wise transcript ◄───────────────┘
```

- `POST /api/transcribe/url` — takes `{"url": "..."}`, downloads audio with yt-dlp, returns `transcript_id`
- `POST /api/transcribe/upload` — multipart file upload, returns `transcript_id`
- `GET /api/transcript/{id}` — poll status; returns `utterances` with `speaker` + `text` when done
- `DELETE /api/transcript/{id}` — deletes the transcript from AssemblyAI

## Run locally

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ASSEMBLYAI_API_KEY=your_key
uvicorn app.main:app --reload --port 8080
```

Open http://localhost:8080. You must have **ffmpeg** installed for YouTube
downloads (yt-dlp audio extraction); uploads don't need it.

## Deploy

The repo ships a `Dockerfile` and `render.yaml`. On [Render](https://render.com)
you can create a **Blueprint** from the repo and set the `ASSEMBLYAI_API_KEY`
secret — no other config needed. Any Docker-capable host (Railway, Fly.io, etc.)
works the same way.

## AssemblyAI

- Get a free API key at https://www.assemblyai.com/ (free tier ≈ 10 hours/month)
- Speaker diarization is enabled via `speaker_labels: true`
- Uploaded media is temporary: AssemblyAI auto-expires it and the app also
  issues a `DELETE` after the transcript is rendered
