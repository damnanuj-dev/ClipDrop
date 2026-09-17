"""
ClipDrop - YouTube Creator Toolkit & Media Downloader Backend
Production-quality Flask application optimized for Vercel Serverless and local execution.
Features yt-dlp EJS challenge support via Deno (with Node fallback), FFmpeg merging, and serverless-safe streaming.
"""

import os
import sys
import re
import io
import time
import uuid
import shutil
import zipfile
import logging
import tempfile
import platform
import subprocess
import urllib.parse
from typing import Dict, Any, Optional, Tuple, Generator

from flask import Flask, request, jsonify, send_file, Response, stream_with_context
import yt_dlp
import imageio_ffmpeg

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s"
)
logger = logging.getLogger("clipdrop")

app = Flask(__name__, static_folder='.', static_url_path='')
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class VercelPathMiddleware:
    """
    WSGI Middleware to restore original request paths on Vercel Serverless.
    Clears SCRIPT_NAME and resolves PATH_INFO from Vercel routing headers or query params,
    ensuring routes match properly whether invoked via rewrites or direct serverless endpoints.
    """
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        environ['SCRIPT_NAME'] = ''
        qs = environ.get('QUERY_STRING', '')
        if '__path=' in qs:
            params = urllib.parse.parse_qs(qs)
            if '__path' in params and params['__path']:
                target = params['__path'][0]
                environ['PATH_INFO'] = target
                params.pop('__path', None)
                environ['QUERY_STRING'] = urllib.parse.urlencode(params, doseq=True)
        else:
            matched = (
                environ.get('HTTP_X_FORWARDED_URI') or
                environ.get('HTTP_X_MATCHED_PATH') or
                environ.get('REQUEST_URI') or
                environ.get('RAW_URI')
            )
            if matched:
                environ['PATH_INFO'] = matched.split('?')[0]
            elif environ.get('PATH_INFO') in ('/api/index', '/api/index.py', '/api', '/api/'):
                environ['PATH_INFO'] = '/'
        return self.wsgi_app(environ, start_response)

app.wsgi_app = VercelPathMiddleware(app.wsgi_app)

# -----------------------------------------------------------------------------
# Runtime Verification & Caching (Deno, Node, & FFmpeg)
# -----------------------------------------------------------------------------

_DENO_CACHE: Optional[Dict[str, Any]] = None
_NODE_CACHE: Optional[Dict[str, Any]] = None
_FFMPEG_CACHE: Optional[Dict[str, Any]] = None

def get_temp_bin_dir() -> str:
    """Returns a writable directory in /tmp for binaries and runtime execution."""
    tmp_bin = os.path.join(tempfile.gettempdir(), "clipdrop_bin")
    os.makedirs(tmp_bin, exist_ok=True)
    return tmp_bin

def get_deno_info(force_refresh: bool = False) -> Dict[str, Any]:
    """
    Locates, validates, and optionally bootstraps the Deno JS runtime.
    yt-dlp recommends Deno >= 2.3 for YouTube JS challenge solving.
    Returns: {"available": bool, "path": Optional[str], "version": Optional[str], "supported": bool}
    """
    global _DENO_CACHE
    if _DENO_CACHE is not None and not force_refresh:
        return _DENO_CACHE

    result: Dict[str, Any] = {
        "available": False,
        "path": None,
        "version": None,
        "supported": False,
        "source": "none"
    }

    candidate_paths = []

    # 1. Check explicit environment variable
    env_path = os.environ.get("DENO_PATH")
    if env_path:
        candidate_paths.append((env_path, "env"))

    # 2. Check system PATH
    which_deno = shutil.which("deno") or shutil.which("deno.exe")
    if which_deno:
        candidate_paths.append((which_deno, "path"))

    # 3. Check bundled bin directory in repository
    repo_bin_names = ["deno.exe", "deno"] if os.name == "nt" else ["deno"]
    for bname in repo_bin_names:
        p = os.path.join(BASE_DIR, "bin", bname)
        if os.path.isfile(p):
            candidate_paths.append((p, "bundled"))

    # 4. Check writable /tmp/clipdrop_bin
    tmp_bin = get_temp_bin_dir()
    for bname in repo_bin_names:
        p = os.path.join(tmp_bin, bname)
        if os.path.isfile(p):
            candidate_paths.append((p, "tmp"))

    for path, source in candidate_paths:
        active_path = path
        # On POSIX (Linux/macOS), if binary is not in /tmp and lacks execution permission,
        # copy it to /tmp to avoid "Read-only file system" error on Vercel /var/task.
        if os.name != "nt" and source == "bundled":
            try:
                dest = os.path.join(tmp_bin, "deno")
                if not os.path.exists(dest) or os.path.getsize(dest) != os.path.getsize(path):
                    shutil.copy2(path, dest)
                os.chmod(dest, 0o755)
                active_path = dest
                source = "bundled_copied_to_tmp"
            except Exception as e:
                logger.warning(f"Could not copy bundled deno to tmp: {e}")

        # Validate with deno --version
        try:
            res = subprocess.run(
                [active_path, "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if res.returncode == 0:
                first_line = res.stdout.strip().splitlines()[0]
                m = re.search(r'deno\s+([0-9]+\.[0-9]+\.[0-9]+)', first_line)
                ver_str = m.group(1) if m else "unknown"
                
                ver_parts = [int(p) for p in re.findall(r'\d+', ver_str)] if m else [0, 0, 0]
                is_supported = tuple(ver_parts[:3]) >= (2, 3, 0)

                result = {
                    "available": True,
                    "path": active_path,
                    "version": ver_str,
                    "supported": is_supported,
                    "source": source
                }
                logger.info(f"Deno runtime verified: {ver_str} from {source} at {active_path}")
                _DENO_CACHE = result
                return result
        except Exception as exc:
            logger.debug(f"Candidate Deno path {active_path} validation failed: {exc}")

    # 5. On-Demand Bootstrap for Vercel cold starts (Linux x86_64)
    if platform.system().lower() == "linux":
        try:
            logger.info("Attempting on-demand Deno bootstrap for Linux on Vercel...")
            import requests
            url = "https://github.com/denoland/deno/releases/download/v2.9.7/deno-x86_64-unknown-linux-gnu.zip"
            resp = requests.get(url, headers={"User-Agent": "ClipDrop/1.0"}, timeout=30)
            if resp.status_code == 200:
                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    target = os.path.join(tmp_bin, "deno")
                    with open(target, "wb") as f:
                        f.write(zf.read("deno"))
                os.chmod(target, 0o755)
                res = subprocess.run([target, "--version"], capture_output=True, text=True, timeout=5)
                if res.returncode == 0:
                    first_line = res.stdout.strip().splitlines()[0]
                    m = re.search(r'deno\s+([0-9]+\.[0-9]+\.[0-9]+)', first_line)
                    ver_str = m.group(1) if m else "2.9.7"
                    result = {
                        "available": True,
                        "path": target,
                        "version": ver_str,
                        "supported": True,
                        "source": "bootstrap_linux"
                    }
                    logger.info(f"Deno on-demand bootstrap succeeded: {ver_str}")
                    _DENO_CACHE = result
                    return result
        except Exception as b_err:
            logger.error(f"Deno on-demand bootstrap failed: {b_err}")

    logger.warning("No working Deno executable found.")
    _DENO_CACHE = result
    return result

def get_node_info(force_refresh: bool = False) -> Dict[str, Any]:
    """Locates and validates Node.js as a secondary JavaScript challenge runner."""
    global _NODE_CACHE
    if _NODE_CACHE is not None and not force_refresh:
        return _NODE_CACHE

    result: Dict[str, Any] = {
        "available": False,
        "path": None,
        "version": None
    }

    which_node = shutil.which("node") or shutil.which("node.exe")
    if which_node:
        try:
            res = subprocess.run([which_node, "-v"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                ver_str = res.stdout.strip().lstrip('v')
                result = {
                    "available": True,
                    "path": which_node,
                    "version": ver_str
                }
                logger.info(f"Node.js runtime verified: {ver_str} at {which_node}")
                _NODE_CACHE = result
                return result
        except Exception as e:
            logger.debug(f"Node validation check failed: {e}")

    _NODE_CACHE = result
    return result

def get_ffmpeg_info(force_refresh: bool = False) -> Dict[str, Any]:
    """Locates and validates the FFmpeg executable from imageio-ffmpeg or system PATH."""
    global _FFMPEG_CACHE
    if _FFMPEG_CACHE is not None and not force_refresh:
        return _FFMPEG_CACHE

    result: Dict[str, Any] = {
        "available": False,
        "path": None,
        "version": None
    }

    candidate_paths = []
    try:
        candidate_paths.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass

    which_ff = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if which_ff and which_ff not in candidate_paths:
        candidate_paths.append(which_ff)

    tmp_bin = get_temp_bin_dir()

    for path in candidate_paths:
        active_path = path
        if os.name != "nt" and os.path.isfile(path) and not os.access(path, os.X_OK):
            try:
                dest = os.path.join(tmp_bin, "ffmpeg")
                if not os.path.exists(dest):
                    shutil.copy2(path, dest)
                os.chmod(dest, 0o755)
                active_path = dest
            except Exception as e:
                logger.warning(f"Could not copy ffmpeg to tmp: {e}")

        try:
            res = subprocess.run([active_path, "-version"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                first_line = res.stdout.strip().splitlines()[0]
                m = re.search(r'ffmpeg\s+version\s+(\S+)', first_line)
                ver_str = m.group(1) if m else "installed"
                result = {
                    "available": True,
                    "path": active_path,
                    "version": ver_str
                }
                logger.info(f"FFmpeg verified: {ver_str} at {active_path}")
                _FFMPEG_CACHE = result
                return result
        except Exception as e:
            logger.debug(f"FFmpeg candidate {active_path} check failed: {e}")

    logger.warning("No functional FFmpeg executable found.")
    _FFMPEG_CACHE = result
    return result

def has_yt_dlp_ejs() -> bool:
    """Checks if yt-dlp-ejs package is available in the Python environment."""
    try:
        from yt_dlp.dependencies import yt_dlp_ejs
        return bool(yt_dlp_ejs)
    except Exception:
        try:
            import yt_dlp_ejs
            return True
        except ImportError:
            return False

# -----------------------------------------------------------------------------
# yt-dlp Configuration Factory
# -----------------------------------------------------------------------------

def build_ydl_opts(extra_opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Constructs a unified, safe yt-dlp options dictionary conforming to current standards.
    Configures JS runtimes via dict-of-dicts syntax, enables yt-dlp-ejs, and sets bounded timeouts.
    """
    deno_info = get_deno_info()
    node_info = get_node_info()
    ffmpeg_info = get_ffmpeg_info()

    opts: Dict[str, Any] = {
        'quiet': True,
        'no_warnings': True,
        'socket_timeout': 15,
        'retries': 3,
        'fragment_retries': 3,
        'file_access_retries': 3,
        'remote_components': ['ejs:npm'],
        'nocheckcertificate': False,
        'prefer_insecure': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-us,en;q=0.5',
        }
    }

    # Configure JavaScript challenge runtimes: Deno preferred, Node fallback
    js_runtimes: Dict[str, Dict[str, Any]] = {}
    if deno_info["available"] and deno_info["path"]:
        js_runtimes['deno'] = {'path': deno_info['path']}
    if node_info["available"] and node_info["path"]:
        js_runtimes['node'] = {'path': node_info['path']}

    if js_runtimes:
        opts['js_runtimes'] = js_runtimes

    # Set FFmpeg location
    if ffmpeg_info["available"] and ffmpeg_info["path"]:
        opts['ffmpeg_location'] = ffmpeg_info['path']

    if extra_opts:
        opts.update(extra_opts)

    return opts

# -----------------------------------------------------------------------------
# Security & URL Normalization
# -----------------------------------------------------------------------------

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "gaming.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com"
}

def normalize_youtube_url(raw_url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Validates and normalizes YouTube URLs against SSRF and malformed input.
    Returns: (canonical_url, video_id, error_code)
    """
    if not raw_url or not isinstance(raw_url, str):
        return None, None, "EMPTY_URL"

    clean_url = raw_url.strip()
    if not clean_url:
        return None, None, "EMPTY_URL"

    # Raw 11-character video ID
    if re.match(r'^[a-zA-Z0-9_-]{11}$', clean_url):
        return f"https://www.youtube.com/watch?v={clean_url}", clean_url, None

    # Check for explicit schemes like file://, ftp://, javascript://
    if re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', clean_url):
        if not clean_url.startswith(('http://', 'https://')):
            return None, None, "UNSUPPORTED_PROTOCOL"
    else:
        clean_url = 'https://' + clean_url

    try:
        parsed = urllib.parse.urlparse(clean_url)
    except Exception:
        return None, None, "MALFORMED_URL"

    if parsed.scheme not in ('http', 'https'):
        return None, None, "UNSUPPORTED_PROTOCOL"

    hostname = (parsed.hostname or '').lower()
    if not hostname:
        return None, None, "MISSING_HOSTNAME"

    # Block SSRF: localhost, IP addresses, private ranges, cloud metadata
    if hostname in ('localhost', '127.0.0.1', '0.0.0.0', '::1', '169.254.169.254'):
        return None, None, "SSRF_BLOCKED"

    if re.match(r'^\d+\.\d+\.\d+\.\d+$', hostname) or ':' in hostname:
        return None, None, "IP_DISALLOWED"

    # Strictly verify domain belongs to YouTube
    is_valid_domain = hostname in ALLOWED_HOSTS or any(hostname.endswith('.' + dom) for dom in ALLOWED_HOSTS)
    if not is_valid_domain:
        return None, None, "NOT_YOUTUBE_DOMAIN"

    video_id: Optional[str] = None

    # Handle youtu.be/ID
    if "youtu.be" in hostname:
        path_parts = parsed.path.strip('/').split('/')
        if path_parts and re.match(r'^[a-zA-Z0-9_-]{11}$', path_parts[0]):
            video_id = path_parts[0]

    # Handle youtube.com watch, shorts, live, embed, v
    if not video_id:
        path = parsed.path
        if "/watch" in path:
            qs = urllib.parse.parse_qs(parsed.query)
            v_param = qs.get('v')
            if v_param and re.match(r'^[a-zA-Z0-9_-]{11}$', v_param[0]):
                video_id = v_param[0]
        else:
            m = re.search(r'/(?:shorts|live|embed|v)/([a-zA-Z0-9_-]{11})', path)
            if m:
                video_id = m.group(1)

    if not video_id:
        m = re.search(r'(?:[?&]v=|/embed/|/shorts/|/live/|/v/|^/)([\w-]{11})', clean_url)
        if m:
            video_id = m.group(1)

    if video_id and re.match(r'^[a-zA-Z0-9_-]{11}$', video_id):
        return f"https://www.youtube.com/watch?v={video_id}", video_id, None

    return None, None, "INVALID_YOUTUBE_URL"

def sanitize_filename(title: str, ext: str) -> str:
    """Sanitizes user/YouTube title into a safe filename without path traversal risk."""
    clean = re.sub(r'[\\/*?:"<>|]', '', title).strip()
    clean = re.sub(r'\s+', ' ', clean)
    if not clean:
        clean = "ClipDrop_media"
    return f"{clean[:80]}.{ext}"

# -----------------------------------------------------------------------------
# Error Classification & Formatting
# -----------------------------------------------------------------------------

def map_ytdlp_error(exc: Exception) -> Tuple[str, str, int]:
    """
    Classifies yt-dlp and network exceptions into clear error codes and user-safe messages.
    Returns: (error_code, user_message, http_status_code)
    """
    msg = str(exc).lower()

    if "private" in msg:
        return "VIDEO_PRIVATE", "This video is private and cannot be downloaded.", 404
    elif "deleted" in msg or "does not exist" in msg or "removed" in msg:
        return "VIDEO_NOT_FOUND", "This video has been deleted or does not exist.", 404
    elif "sign in to confirm you're not a bot" in msg or "confirm your age" in msg or "sign in" in msg:
        return "BOT_CHALLENGE", "YouTube bot challenge detected. The challenge solver could not complete verification.", 403
    elif "age-restricted" in msg or "age restricted" in msg:
        return "AGE_RESTRICTED", "This video is age-restricted and requires YouTube login.", 403
    elif "geo" in msg or "country" in msg or "not available in your country" in msg:
        return "GEO_BLOCKED", "This video is geographically restricted.", 403
    elif "format" in msg and "not available" in msg:
        return "FORMAT_UNAVAILABLE", "The requested format or quality is not available for this video.", 400
    elif "timed out" in msg or "timeout" in msg:
        return "TIMEOUT", "Connection to YouTube timed out. Please try again.", 504
    elif "deno" in msg or "javascript runtime" in msg:
        return "DENO_RUNTIME_ERROR", "JavaScript challenge runtime error.", 500
    else:
        return "EXTRACTION_FAILED", "Unable to extract video information from YouTube.", 500

def json_error(code: str, message: str, status_code: int = 400, details: Optional[str] = None):
    """Returns a standardized JSON error response."""
    body: Dict[str, Any] = {
        "success": False,
        "error": {
            "code": code,
            "message": message
        },
        "request_id": str(uuid.uuid4())[:8]
    }
    if details and app.debug:
        body["error"]["details"] = details
    return jsonify(body), status_code

# -----------------------------------------------------------------------------
# Formatting Helpers
# -----------------------------------------------------------------------------

def format_duration(seconds: Optional[int]) -> str:
    if not seconds or seconds < 0:
        return "0:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def format_views(views: Optional[int]) -> str:
    if not views or views < 0:
        return "0 views"
    if views >= 1_000_000_000:
        return f"{views / 1_000_000_000:.1f}B views"
    if views >= 1_000_000:
        return f"{views / 1_000_000:.1f}M views"
    if views >= 1_000:
        return f"{views / 1_000:.1f}K views"
    return f"{views} views"

def format_size(bytes_val: Optional[float]) -> str:
    if not bytes_val or bytes_val <= 0:
        return "N/A"
    mb = bytes_val / (1024 * 1024)
    if mb >= 1000:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB"

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.route('/')
@app.route('/api/index')
@app.route('/api')
def index():
    """Serves the frontend homepage."""
    index_file = os.path.join(BASE_DIR, 'index.html')
    if os.path.isfile(index_file):
        return send_file(index_file)
    return jsonify({"error": "index.html not found"}), 404

@app.route('/health')
@app.route('/api/health')
def health():
    """
    Health and diagnostics endpoint.
    Verifies Deno, Node, FFmpeg, yt-dlp, and EJS without exposing sensitive filesystem paths.
    """
    deno_info = get_deno_info()
    node_info = get_node_info()
    ffmpeg_info = get_ffmpeg_info()
    ejs_ok = has_yt_dlp_ejs()

    # Test /tmp writeability
    tmp_writable = False
    try:
        test_dir = tempfile.mkdtemp(prefix="health_check_")
        tmp_writable = os.path.isdir(test_dir)
        shutil.rmtree(test_dir, ignore_errors=True)
    except Exception:
        tmp_writable = False

    has_js_solver = bool(deno_info["available"] or node_info["available"])
    is_healthy = bool(
        has_js_solver and
        ffmpeg_info["available"] and
        tmp_writable
    )

    return jsonify({
        "status": "ok" if is_healthy else "degraded",
        "runtime": "python",
        "python_version": sys.version.split()[0],
        "yt_dlp": True,
        "yt_dlp_version": getattr(yt_dlp.version, '__version__', 'unknown'),
        "yt_dlp_ejs": ejs_ok,
        "deno": deno_info["available"],
        "deno_version": deno_info.get("version"),
        "deno_supported": deno_info.get("supported", False),
        "node": node_info["available"],
        "node_version": node_info.get("version"),
        "ffmpeg": ffmpeg_info["available"],
        "tmp_writable": tmp_writable
    }), (200 if is_healthy else 503)

@app.route('/api/info', methods=['POST', 'OPTIONS'])
@app.route('/info', methods=['POST', 'OPTIONS'])
def get_video_info():
    """Extracts YouTube video metadata, thumbnails, and available qualities."""
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    data = request.get_json(silent=True) or {}
    raw_url = data.get('url', '').strip()

    if not raw_url:
        return json_error("EMPTY_URL", "Please enter a YouTube video link.", 400)

    canonical_url, video_id, err_code = normalize_youtube_url(raw_url)
    if err_code or not canonical_url:
        return json_error(err_code or "INVALID_URL", "Please provide a valid YouTube URL (video, shorts, or music).", 400)

    ydl_opts = build_ydl_opts({'skip_download': True, 'extract_flat': False})

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(canonical_url, download=False)
            if not info:
                return json_error("VIDEO_UNAVAILABLE", "This video could not be retrieved.", 404)
    except yt_dlp.utils.DownloadError as e:
        code, msg, status = map_ytdlp_error(e)
        logger.warning(f"DownloadError extracting metadata for {canonical_url}: {code} - {e}")
        return json_error(code, msg, status, details=str(e))
    except Exception as e:
        logger.exception(f"Unexpected error extracting metadata for {canonical_url}")
        return json_error("EXTRACTION_ERROR", "Unable to process video information.", 500, details=str(e))

    duration = info.get('duration', 0)
    formats = info.get('formats', [])

    quality_map = {
        2160: {"label": "2160p (4K UHD)", "short": "4k", "height": 2160},
        1440: {"label": "1440p (QHD)", "short": "1440p", "height": 1440},
        1080: {"label": "1080p (Full HD • Recommended)", "short": "1080p", "height": 1080},
        720: {"label": "720p (HD)", "short": "720p", "height": 720},
        480: {"label": "480p (Standard)", "short": "480p", "height": 480},
        360: {"label": "360p (Compact)", "short": "360p", "height": 360}
    }

    available_heights = set()
    format_sizes = {}

    for f in formats:
        h = f.get('height')
        w = f.get('width')
        if f.get('vcodec') != 'none':
            effective_h = h
            if w and h and w < h:
                effective_h = w

            if effective_h:
                for target_h in quality_map.keys():
                    if target_h - 40 <= effective_h <= target_h + 40:
                        available_heights.add(target_h)
                        sz = f.get('filesize') or f.get('filesize_approx')
                        if sz and (target_h not in format_sizes or sz > format_sizes[target_h]):
                            format_sizes[target_h] = sz

    if not available_heights:
        available_heights = {720, 360}

    video_qualities = []
    bitrate_table = {2160: 15_000_000, 1440: 8_000_000, 1080: 4_000_000, 720: 2_000_000, 480: 1_000_000, 360: 500_000}
    for h in sorted(quality_map.keys(), reverse=True):
        if h in available_heights:
            item = quality_map[h].copy()
            sz = format_sizes.get(h)
            if not sz and duration:
                est_bytes = (duration * bitrate_table.get(h, 2_000_000)) / 8
                sz = est_bytes
            item['size_str'] = format_size(sz)
            item['size_bytes'] = sz
            video_qualities.append(item)

    audio_bitrates = [
        {"bitrate": "320", "label": "320 kbps (Ultra High Fidelity)", "badge": "Ultra"},
        {"bitrate": "256", "label": "256 kbps (Studio Master)", "badge": "Studio"},
        {"bitrate": "192", "label": "192 kbps (Standard Podcast)", "badge": "High"},
        {"bitrate": "128", "label": "128 kbps (Compact Voice)", "badge": "Standard"}
    ]
    for ab in audio_bitrates:
        bps = int(ab['bitrate']) * 1000
        approx_bytes = (duration * bps) / 8 if duration else 0
        ab['size_str'] = format_size(approx_bytes) if approx_bytes else "Variable"

    thumbnails = info.get('thumbnails', [])
    thumbnail_url = info.get('thumbnail')
    if thumbnails:
        thumbnail_url = thumbnails[-1].get('url') or thumbnail_url

    response_data = {
        "success": True,
        "id": info.get('id', video_id),
        "canonical_url": canonical_url,
        "title": info.get('title', 'YouTube Video'),
        "channel": info.get('uploader') or info.get('channel') or 'Unknown Channel',
        "channel_url": info.get('uploader_url') or info.get('channel_url'),
        "duration": duration,
        "duration_str": format_duration(duration),
        "views": info.get('view_count', 0),
        "views_str": format_views(info.get('view_count', 0)),
        "thumbnail": thumbnail_url,
        "video_qualities": video_qualities,
        "audio_bitrates": audio_bitrates,
        "original_fps": info.get('fps') or 60,
    }

    return jsonify(response_data)

@app.route('/api/download', methods=['POST', 'GET', 'OPTIONS'])
@app.route('/download', methods=['POST', 'GET', 'OPTIONS'])
def download_media():
    """
    Serverless synchronous media download and streaming endpoint.
    Downloads media inside an isolated per-request temporary directory,
    streams the binary payload directly with Content-Disposition,
    and reliably cleans up all temporary files immediately upon stream completion.
    """
    if request.method == 'OPTIONS':
        return jsonify({}), 200

    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        raw_url = data.get('url', '').strip()
        dl_type = data.get('type', 'video').lower()
        quality = data.get('quality', '1080p')
    else:
        raw_url = request.args.get('url', '').strip()
        dl_type = request.args.get('type', 'video').lower()
        quality = request.args.get('quality', '1080p')

    canonical_url, video_id, err_code = normalize_youtube_url(raw_url)
    if err_code or not canonical_url:
        return json_error(err_code or "INVALID_URL", "Please provide a valid YouTube URL.", 400)

    if dl_type not in ('video', 'audio'):
        return json_error("INVALID_TYPE", "Download type must be 'video' or 'audio'.", 400)

    temp_dir = tempfile.mkdtemp(prefix="cdjob_", dir=tempfile.gettempdir())
    logger.info(f"Starting serverless download: {canonical_url} (type={dl_type}, quality={quality}) in {temp_dir}")

    try:
        # Pre-extract video title
        title = "ClipDrop_Media"
        ydl_opts_meta = build_ydl_opts({'skip_download': True})
        with yt_dlp.YoutubeDL(ydl_opts_meta) as ydl:
            pre_info = ydl.extract_info(canonical_url, download=False)
            if pre_info:
                title = pre_info.get('title', 'ClipDrop_Media')
                dur = pre_info.get('duration', 0)
                if dur > 1800: # 30 min cap on serverless
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    return json_error("DURATION_EXCEEDED", "This video exceeds the maximum allowable serverless duration (30 minutes).", 400)

        out_filepath: Optional[str] = None
        out_filename: Optional[str] = None
        content_type = "application/octet-stream"

        if dl_type == 'audio':
            bitrate = quality if quality in ['320', '256', '192', '128'] else '192'
            out_filename = sanitize_filename(f"{title}_{bitrate}kbps", 'mp3')
            out_filepath = os.path.join(temp_dir, out_filename)
            raw_tmpl = os.path.join(temp_dir, 'raw.%(ext)s')

            ydl_opts = build_ydl_opts({
                'format': 'bestaudio/best',
                'outtmpl': raw_tmpl,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': bitrate,
                }],
            })

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([canonical_url])

            extracted_mp3 = os.path.join(temp_dir, 'raw.mp3')
            if os.path.exists(extracted_mp3):
                os.replace(extracted_mp3, out_filepath)

            if not os.path.exists(out_filepath):
                for fname in os.listdir(temp_dir):
                    if fname.endswith('.mp3'):
                        out_filepath = os.path.join(temp_dir, fname)
                        out_filename = fname
                        break

            content_type = "audio/mpeg"

        else: # video
            height_map = {"4k": 2160, "2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
            target_h = height_map.get(str(quality).lower(), 1080)
            out_filename = sanitize_filename(f"{title}_{quality}", 'mp4')
            out_filepath = os.path.join(temp_dir, out_filename)
            raw_tmpl = os.path.join(temp_dir, 'video.%(ext)s')

            format_str = f"bestvideo[height<={target_h}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={target_h}]+bestaudio/best[height<={target_h}]/best"

            ydl_opts = build_ydl_opts({
                'format': format_str,
                'outtmpl': raw_tmpl,
                'merge_output_format': 'mp4',
            })

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([canonical_url])

            merged_mp4 = os.path.join(temp_dir, 'video.mp4')
            if os.path.exists(merged_mp4):
                os.replace(merged_mp4, out_filepath)

            if not os.path.exists(out_filepath):
                for fname in os.listdir(temp_dir):
                    if fname.endswith(('.mp4', '.mkv', '.webm')):
                        out_filepath = os.path.join(temp_dir, fname)
                        out_filename = fname
                        break

            content_type = "video/mp4"

        if not out_filepath or not os.path.exists(out_filepath):
            shutil.rmtree(temp_dir, ignore_errors=True)
            return json_error("DOWNLOAD_PRODUCE_FAILED", "Media processing did not produce an output file.", 500)

        file_size = os.path.getsize(out_filepath)
        logger.info(f"Download complete: {out_filename} ({file_size} bytes). Streaming response...")

        def stream_and_cleanup() -> Generator[bytes, None, None]:
            try:
                with open(out_filepath, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        yield chunk
            finally:
                logger.info(f"Cleaning up temporary download directory: {temp_dir}")
                shutil.rmtree(temp_dir, ignore_errors=True)

        quoted_filename = urllib.parse.quote(out_filename)
        headers = {
            "Content-Disposition": f"attachment; filename=\"{out_filename}\"; filename*=UTF-8''{quoted_filename}",
            "Content-Length": str(file_size),
            "X-ClipDrop-Filename": quoted_filename,
            "Cache-Control": "no-cache, no-store, must-revalidate"
        }

        return Response(
            stream_with_context(stream_and_cleanup()),
            mimetype=content_type,
            headers=headers
        )

    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        code, msg, status = map_ytdlp_error(e)
        logger.warning(f"DownloadError during media generation: {code} - {e}")
        return json_error(code, msg, status, details=str(e))
    except Exception as e:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.exception("Unexpected error during media download")
        return json_error("PROCESSING_FAILED", "Failed to process media file.", 500, details=str(e))

# -----------------------------------------------------------------------------
# Backwards-Compatibility Wrappers for Legacy Polling Endpoints
# -----------------------------------------------------------------------------

@app.route('/api/download/start', methods=['POST', 'OPTIONS'])
def legacy_download_start():
    """Legacy endpoint stub informing clients to use direct serverless /api/download."""
    if request.method == 'OPTIONS':
        return jsonify({}), 200
    return jsonify({
        "success": True,
        "message": "Direct download available at /api/download",
        "job_id": str(uuid.uuid4())
    })

@app.route('/api/download/progress/<job_id>', methods=['GET'])
def legacy_download_progress(job_id):
    """Legacy polling endpoint stub."""
    return jsonify({
        "job_id": job_id,
        "status": "ready",
        "progress": 100,
        "subtext": "Direct streaming available"
    })

@app.route('/api/download/file/<job_id>', methods=['GET'])
def legacy_download_file(job_id):
    """Legacy file retrieval stub."""
    return json_error("MIGRATED", "Downloads are now delivered directly via POST /api/download", 400)

# -----------------------------------------------------------------------------
# Static Asset Serving
# -----------------------------------------------------------------------------

@app.route('/<path:filename>')
def serve_static(filename):
    """Safely serves static assets (logos, icons, svg, etc.) from the application root."""
    safe_path = os.path.abspath(os.path.join(BASE_DIR, filename))
    if safe_path.startswith(BASE_DIR) and os.path.isfile(safe_path):
        return send_file(safe_path)
    return jsonify({"error": "File not found"}), 404

# -----------------------------------------------------------------------------
# Local Development Entrypoint
# -----------------------------------------------------------------------------

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"=== ClipDrop Server Starting ===")
    print(f"URL: http://127.0.0.1:{port}")
    
    deno_status = get_deno_info()
    node_status = get_node_info()
    ff_status = get_ffmpeg_info()
    print(f"Deno: {deno_status.get('version', 'NOT FOUND')} (Supported: {deno_status.get('supported')})")
    print(f"Node: {node_status.get('version', 'NOT FOUND')}")
    print(f"FFmpeg: {ff_status.get('version', 'NOT FOUND')}")
    print(f"yt-dlp EJS: {has_yt_dlp_ejs()}")
    print(f"================================")
    
    app.run(host='0.0.0.0', port=port, debug=False)
