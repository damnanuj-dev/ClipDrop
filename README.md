# ClipDrop — YouTube Creator Toolkit & Media Downloader

ClipDrop is a high-performance, dark-aesthetic YouTube creator utility and downloader built with **Flask**, **yt-dlp**, and **FFmpeg**, specifically engineered to run natively on **Vercel Serverless Functions** and traditional environments.

---

## ⚡ Key Highlights & Architecture

- **Serverless-Native Streaming Architecture**: Eliminates fragile daemon threads and in-memory job dictionaries. Media is synthesized in an isolated temporary directory and streamed directly to the client via HTTP chunked transfer, guaranteeing immediate cleanup of `/tmp` files upon completion and bypassing serverless memory limits.
- **YouTube JS Challenge Solver (Deno >= 2.3 + yt-dlp-ejs)**: Resolves YouTube extraction challenges using `yt-dlp[default]` with `yt-dlp-ejs` and external JavaScript runtimes (Deno primary, Node.js fallback).
- **Static FFmpeg Bundling**: Automatically utilizes standalone FFmpeg binaries via `imageio-ffmpeg` for lossless MP3 audio transcoding and multi-stream MP4 merging.
- **Zero-Config Vercel Compatibility**: Uses `api/index.py` standard entrypoint with automatic framework detection and configurable `maxDuration`.
- **SSRF & Security Defense**: Strictly validates YouTube hostnames (`youtube.com`, `youtu.be`, `youtube-nocookie.com`), blocks loopback/internal IP addresses, and sanitizes filenames against path traversal.
- **Diagnostic Health Monitoring**: Exposes `/health` to verify runtime engines, JS solvers, and writable storage without exposing sensitive internal paths.

---

## 📂 Project Structure

```
ClipDrop/
├── api/
│   └── index.py            # Standard Vercel Serverless entrypoint
├── scripts/
│   └── setup_deno.py       # Deterministic Deno binary installer for build/deploy
├── tests/
│   └── test_clipdrop.py    # Automated test suite (SSRF, URL normalizer, endpoints)
├── app.py                  # Core Flask application, resolvers, & streaming logic
├── index.html              # Frontend user interface with real-time stream feedback
├── requirements.txt        # Pinned, compatible dependencies (Flask, yt-dlp[default], etc.)
├── vercel.json             # Modern Vercel deployment configuration
├── .gitignore              # Ignores temp downloads, caches, and local binaries
├── .env.example            # Environment configuration template
└── README.md               # Project documentation
```

---

## 🚀 Local Development Setup

### 1. Prerequisites
- **Python 3.10+** (Tested up to Python 3.14)
- **Node.js** (v18+) or **Deno** (v2.3+) installed locally for JavaScript challenge execution.

### 2. Installation
Clone the repository and install dependencies:
```bash
pip install -r requirements.txt
```

### 3. Optional: Install Deno locally
```bash
python scripts/setup_deno.py
```

### 4. Run the Server
```bash
python app.py
```
The server will start at `http://127.0.0.1:5000`.

### 5. Run Automated Tests
```bash
python -m unittest tests/test_clipdrop.py -v
```

---

## 🌐 Vercel Deployment

Deploying ClipDrop to Vercel requires zero complex setup.

### Option 1: Vercel CLI
```bash
vercel
```
Select the default settings. Vercel automatically discovers `api/index.py` and `requirements.txt`.

### Option 2: Git Integration
1. Push your repository to GitHub, GitLab, or Bitbucket.
2. Import the repository into the Vercel Dashboard.
3. Framework Preset: **Other** (Vercel automatically detects Python serverless functions).
4. Click **Deploy**.

### Vercel Runtime Details
- `vercel.json` defines a `buildCommand` that pre-fetches the verified Linux x86_64 Deno binary into `bin/` during the cloud build step.
- On cold start, `app.py` safely permissions the binary in `/tmp/bin` to prevent `[Errno 30] Read-only file system` errors.
- `maxDuration: 60` is configured for Hobby plans (can be adjusted up to 300s/900s for Pro/Enterprise accounts).

---

## 🩺 Health & Diagnostics Endpoint

`GET /health` returns live health status for all subsystem dependencies:

```json
{
  "status": "ok",
  "runtime": "python",
  "python_version": "3.14.7",
  "yt_dlp": true,
  "yt_dlp_version": "2026.08.19",
  "yt_dlp_ejs": true,
  "deno": true,
  "deno_version": "2.9.7",
  "deno_supported": true,
  "node": true,
  "node_version": "24.19.0",
  "ffmpeg": true,
  "tmp_writable": true
}
```

If a required component is missing or degraded, the endpoint returns HTTP `503` with a diagnostic explanation.

---

## 📡 API Reference

### 1. `POST /api/info`
Inspects video metadata, channel details, duration, view counts, and available video/audio formats.

**Request Body:**
```json
{
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
}
```

**Response (HTTP 200):**
```json
{
  "success": true,
  "id": "dQw4w9WgXcQ",
  "canonical_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "title": "Rick Astley - Never Gonna Give You Up (Official Video)",
  "channel": "Rick Astley",
  "duration": 213,
  "duration_str": "3:33",
  "views": 1600000000,
  "views_str": "1.6B views",
  "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
  "video_qualities": [
    {"height": 1080, "label": "1080p (Full HD)", "short": "1080p", "size_str": "64.2 MB"}
  ],
  "audio_bitrates": [
    {"bitrate": "320", "label": "320 kbps (Ultra High Fidelity)", "size_str": "8.5 MB"}
  ]
}
```

### 2. `POST /api/download`
Downloads and streams media directly in real time.

**Request Body:**
```json
{
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "type": "video",
  "quality": "1080p"
}
```
*Note: Set `type: "audio"` and `quality: "320"` to extract MP3.*

**Response:** Binary octet-stream with `Content-Disposition: attachment; filename="..."`.

---

## ⚠️ Important Limitations & Upstream Disclaimer

1. **Serverless Function Duration**: Vercel functions are bounded by execution timeouts (60 seconds on Hobby, 300+ on Pro). Videos longer than ~15-20 minutes or demanding 4K stream merges may exceed Hobby timeouts.
2. **YouTube Anti-Bot Changes**: YouTube frequently updates its player algorithms, challenge mechanisms, and bot checks. While ClipDrop implements current best practices (Deno >= 2.3 + yt-dlp-ejs), upstream changes may require running `pip install --upgrade "yt-dlp[default]"` when YouTube alters its challenge scripts.
3. **No Account Login / DRM**: ClipDrop does not circumvent DRM or require user cookies. Age-restricted and private videos requiring user authentication will fail with clear, descriptive error messages.
