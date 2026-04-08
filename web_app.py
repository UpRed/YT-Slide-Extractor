import glob
import io
import os
import tempfile
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np
import yt_dlp
from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image

app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=2)
TASKS = {}
TASK_LOCK = threading.Lock()


def _build_ydl_opts(output_template):
    return {
        "format": "best[ext=mp4]/best",
        "outtmpl": output_template,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "force_ipv4": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.youtube.com/",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
            }
        },
    }


def _find_downloaded_video(temp_dir):
    patterns = [
        os.path.join(temp_dir, "temp_video.*"),
        os.path.join(temp_dir, "*.mp4"),
        os.path.join(temp_dir, "*.mkv"),
        os.path.join(temp_dir, "*.webm"),
    ]
    for pattern in patterns:
        files = sorted(glob.glob(pattern))
        if files:
            return files[0]
    return None


def _extract_unique_slides(video_path, threshold=12.0):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps == 0 or fps is None or np.isnan(fps):
        fps = 30

    frame_interval = max(int(fps), 1)
    success, frame = cap.read()
    if not success:
        cap.release()
        return []

    prev_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    slides = [Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))]

    count = 0
    while success:
        success, frame = cap.read()
        if not success:
            break

        count += 1
        if count % frame_interval != 0:
            continue

        curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mse = np.mean((prev_gray.astype("float") - curr_gray.astype("float")) ** 2)
        if mse > threshold:
            slides.append(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
            prev_gray = curr_gray

    cap.release()
    return slides


def _generate_pdf(images):
    if not images:
        return None

    pdf_buffer = io.BytesIO()
    rgb_images = [img.convert("RGB") for img in images]
    rgb_images[0].save(
        pdf_buffer,
        format="PDF",
        save_all=True,
        append_images=rgb_images[1:],
        resolution=300.0,
    )
    pdf_buffer.seek(0)
    return pdf_buffer


def _set_task(task_id, **updates):
    with TASK_LOCK:
        TASKS[task_id].update(updates)


def _process_task(task_id, youtube_url):
    try:
        _set_task(task_id, status="running", message="正在下載影片...", progress=10)

        with tempfile.TemporaryDirectory() as temp_dir:
            ydl_opts = _build_ydl_opts(os.path.join(temp_dir, "temp_video.%(ext)s"))
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([youtube_url])

            video_path = _find_downloaded_video(temp_dir)
            if not video_path:
                raise RuntimeError("下載完成但找不到影片檔")

            _set_task(task_id, message="正在分析影片畫面...", progress=50)
            slides = _extract_unique_slides(video_path)

            if len(slides) <= 1:
                retry_threshold = 7.0
                slides_retry = _extract_unique_slides(video_path, threshold=retry_threshold)
                if len(slides_retry) > len(slides):
                    slides = slides_retry

            if not slides:
                raise RuntimeError("未偵測到可用畫面")

            _set_task(task_id, message="正在產生 PDF...", progress=85)
            pdf_data = _generate_pdf(slides)
            if not pdf_data:
                raise RuntimeError("PDF 產生失敗")

            _set_task(
                task_id,
                status="done",
                message=f"完成，共擷取 {len(slides)} 張畫面",
                progress=100,
                slide_count=len(slides),
                pdf_bytes=pdf_data.getvalue(),
            )
    except Exception as exc:
        err = str(exc)
        if "HTTP Error 403" in err or "unable to download video data" in err:
            err = "YouTube 拒絕下載（403）。這通常是來源限制，請改用可公開播放的影片。"
        _set_task(task_id, status="error", message=err, progress=100)


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/tasks")
def create_task():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "請輸入有效的 YouTube 網址"}), 400

    task_id = uuid.uuid4().hex
    with TASK_LOCK:
        TASKS[task_id] = {
            "status": "queued",
            "message": "任務排隊中...",
            "progress": 0,
            "slide_count": 0,
            "pdf_bytes": None,
        }

    executor.submit(_process_task, task_id, url)
    return jsonify({"taskId": task_id})


@app.get("/api/tasks/<task_id>")
def get_task(task_id):
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return jsonify({"error": "任務不存在"}), 404
        return jsonify(
            {
                "status": task["status"],
                "message": task["message"],
                "progress": task["progress"],
                "slideCount": task["slide_count"],
                "hasPdf": task["pdf_bytes"] is not None,
            }
        )


@app.get("/api/tasks/<task_id>/download")
def download_task_pdf(task_id):
    with TASK_LOCK:
        task = TASKS.get(task_id)
        if not task:
            return jsonify({"error": "任務不存在"}), 404
        pdf_bytes = task.get("pdf_bytes")

    if not pdf_bytes:
        return jsonify({"error": "PDF 尚未可用"}), 400

    return send_file(
        io.BytesIO(pdf_bytes),
        as_attachment=True,
        download_name="YouTube_Slides.pdf",
        mimetype="application/pdf",
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
