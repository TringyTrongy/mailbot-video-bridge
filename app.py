import os
import sys
import re
import json
import shutil
import base64
import subprocess
import tempfile
import urllib.parse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI(title="Mailbot Video Bridge")

MAX_PART_BYTES = 18 * 1024 * 1024

def ensure_deno():
    repo_bin = os.path.abspath(os.path.join(os.path.dirname(__file__), "bin"))
    if os.path.isdir(repo_bin) and repo_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{repo_bin}:{os.environ.get('PATH', '')}"
    if shutil.which("deno"):
        return shutil.which("deno")
    bin_dir = "/tmp/bin"
    deno_path = os.path.join(bin_dir, "deno")
    if os.path.exists(deno_path) and os.access(deno_path, os.X_OK):
        if bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
        return deno_path
    
    print("Downloading standalone Deno binary for yt-dlp EJS challenge solver...")
    os.makedirs(bin_dir, exist_ok=True)
    zip_path = os.path.join(bin_dir, "deno.zip")
    url = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"
    try:
        import urllib.request
        import zipfile
        urllib.request.urlretrieve(url, zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(bin_dir)
        os.chmod(deno_path, 0o755)
        if os.path.exists(zip_path):
            os.remove(zip_path)
        if bin_dir not in os.environ.get("PATH", ""):
            os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
        print("Deno configured successfully at:", deno_path)
        return deno_path
    except Exception as e:
        print(f"Failed to auto-download Deno: {e}")
        return None

# Ensure Deno is preloaded at module import
ensure_deno()

def upload_catbox(file_path):
    try:
        cmd = [
            "curl", "-s", "-m", "120",
            "-F", "reqtype=fileupload",
            "-F", f"fileToUpload=@{file_path}",
            "https://catbox.moe/user/api.php"
        ]
        res = subprocess.check_output(cmd, text=True).strip()
        if res.startswith("http"):
            return res
        print(f"Catbox error response: {res}")
    except Exception as e:
        print(f"Catbox upload failed: {e}")
    return None

def process_video(query_or_url):
    ensure_deno()
    temp_dir = tempfile.mkdtemp(prefix="mailbot_vid_")
    try:
        out_tmpl = os.path.join(temp_dir, "%(title).40s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-update",
            "--no-playlist",
            "--remote-components", "ejs:github",
            "-S", "res:360,ext:mp4:m4a",
            "--recode-video", "mp4",
            "-o", out_tmpl
        ]

        # Use Render secret file or env var cookies if available
        secret_cookies = "/etc/secrets/cookies.txt"
        env_b64 = os.environ.get("YOUTUBE_COOKIES_B64", "")
        if os.path.exists(secret_cookies) and os.path.getsize(secret_cookies) > 20:
            print("Using /etc/secrets/cookies.txt")
            cmd.extend(["--cookies", secret_cookies])
        elif env_b64:
            c_path = os.path.join(temp_dir, "cookies.txt")
            with open(c_path, "wb") as cf:
                cf.write(base64.b64decode(env_b64))
            print("Using env YOUTUBE_COOKIES_B64")
            cmd.extend(["--cookies", c_path])

        cmd.append(query_or_url)

        print(f"Executing: {' '.join(cmd[:8])} ...")
        res = subprocess.run(cmd, capture_output=True, text=True)
        print(f"yt-dlp exit: {res.returncode}")
        if res.stdout:
            print(f"yt-dlp stdout: {res.stdout[-300:]}")
        if res.stderr:
            print(f"yt-dlp stderr: {res.stderr[-300:]}")

        files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.endswith(".mp4")]
        if not files:
            raise RuntimeError(f"yt-dlp failed to download video: {res.stderr.strip() or res.stdout.strip() or 'No media file generated'}")

        main_vid = files[0]
        file_size = os.path.getsize(main_vid)
        filename = os.path.basename(main_vid)
        title = os.path.splitext(filename)[0]
        print(f"Downloaded {filename} ({file_size / (1024*1024):.2f} MB)")

        if file_size <= MAX_PART_BYTES:
            url = upload_catbox(main_vid)
            if not url:
                raise RuntimeError("Failed to upload MP4 to hosting storage")
            return {
                "title": title,
                "single": True,
                "parts": [url]
            }

        # If larger than 18MB, segment with ffmpeg (240s chunks)
        part_tmpl = os.path.join(temp_dir, "segment_%03d.mp4")
        split_cmd = [
            "ffmpeg", "-y", "-i", main_vid,
            "-c", "copy",
            "-map", "0",
            "-segment_time", "240",
            "-f", "segment",
            "-reset_timestamps", "1",
            part_tmpl
        ]
        s_res = subprocess.run(split_cmd, capture_output=True, text=True)
        if s_res.returncode != 0:
            print(f"ffmpeg split warning: {s_res.stderr}")

        segments = sorted([os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.startswith("segment_") and f.endswith(".mp4")])
        part_urls = []
        if segments:
            for seg in segments:
                u = upload_catbox(seg)
                if u:
                    part_urls.append(u)

        if not part_urls:
            u = upload_catbox(main_vid)
            if u:
                part_urls.append(u)

        if not part_urls:
            raise RuntimeError("Failed to upload MP4 parts to hosting storage")

        return {
            "title": title,
            "single": len(part_urls) == 1,
            "parts": part_urls
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

@app.get("/", response_class=HTMLResponse)
def index():
    return "<html><body style='font-family:sans-serif;text-align:center;padding:50px;'><h1>🎬 Mailbot Video Bridge</h1><p>Online and serving autonomous 24/7 video downloads for Mailbot.</p></body></html>"

@app.get("/health")
def health():
    return {"status": "ok", "service": "mailbot-video-bridge"}

@app.get("/diag")
def diag():
    secret_exists = os.path.exists("/etc/secrets/cookies.txt")
    secret_size = os.path.getsize("/etc/secrets/cookies.txt") if secret_exists else 0
    return {
        "ffmpeg": shutil.which("ffmpeg"),
        "curl": shutil.which("curl"),
        "deno": shutil.which("deno"),
        "has_secret_cookies": secret_exists,
        "secret_cookies_bytes": secret_size,
        "has_env_cookies": bool(os.environ.get("YOUTUBE_COOKIES_B64")),
        "python": sys.version
    }

@app.get("/video")
def video_endpoint(query: str = "", url: str = "", id: str = ""):
    if id and id != "dQw4w9WgXcQ" and re.match(r'^[a-zA-Z0-9_-]{11}$', id):
        target = f"https://www.youtube.com/watch?v={id}"
    elif url and ("watch?v=" in url or "youtu.be/" in url):
        target = url
    elif query:
        target = f"ytsearch1:{query}"
    elif url and "results?search_query=" in url:
        sub_qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        sq = sub_qs.get("search_query", [""])[0]
        target = f"ytsearch1:{sq}" if sq else f"ytsearch1:{url}"
    else:
        target = url or id
        if not target.startswith("http") and not target.startswith("ytsearch"):
            target = f"ytsearch1:{target}"

    if not target:
        return JSONResponse(status_code=400, content={"error": "Missing ?url, ?id, or ?query"})

    try:
        data = process_video(target)
        return JSONResponse(status_code=200, content=data)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
