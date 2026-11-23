import os
import sys
import re
import json
import shutil
import subprocess
import tempfile
import urllib.parse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI(title="Mailbot Video Bridge")

MAX_PART_BYTES = 18 * 1024 * 1024

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
    temp_dir = tempfile.mkdtemp(prefix="mailbot_vid_")
    try:
        out_tmpl = os.path.join(temp_dir, "%(title).40s.%(ext)s")
        # Try progressive 360p mp4 directly (no recode needed if ffmpeg is missing)
        cmd = [
            "yt-dlp",
            "--no-update",
            "--no-playlist",
            "--max-downloads", "1",
            "-f", "best[ext=mp4][height<=480]/best[height<=480]/best[ext=mp4]/best",
            "--extractor-args", "youtube:player_client=android,ios,web",
            "-o", out_tmpl,
            query_or_url
        ]
        print(f"Executing: {' '.join(cmd)}")
        res = subprocess.run(cmd, capture_output=True, text=True)
        print(f"yt-dlp exit: {res.returncode}")
        print(f"yt-dlp stdout: {res.stdout[:500]}")
        print(f"yt-dlp stderr: {res.stderr[:500]}")

        if res.returncode != 0:
            raise RuntimeError(f"yt-dlp failed (code {res.returncode}): {res.stderr.strip() or res.stdout.strip()}")

        files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.endswith((".mp4", ".mkv", ".webm"))]
        if not files:
            raise RuntimeError(f"No video file found in {os.listdir(temp_dir)}")

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
            print(f"ffmpeg split failed: {s_res.stderr}")
            # If split fails, upload full file anyway if close to limit
            url = upload_catbox(main_vid)
            return {"title": title, "single": True, "parts": [url]}

        segments = sorted([os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if f.startswith("segment_")])
        part_urls = []
        for seg in segments:
            u = upload_catbox(seg)
            if u:
                part_urls.append(u)

        if not part_urls:
            raise RuntimeError("Failed to upload video segments")

        return {
            "title": title,
            "single": False,
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
    import shutil
    return {
        "ffmpeg": shutil.which("ffmpeg"),
        "curl": shutil.which("curl"),
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
