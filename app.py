import streamlit as st
import cv2
import numpy as np
import yt_dlp
import io
import os
import glob
import tempfile
from PIL import Image
from PIL import ImageFilter
from PIL import ImageEnhance
from PIL import ImageOps

# ==========================================
# 核心處理功能區
# ==========================================

# 固定參數：高解析來源 + 穩定幀挑選 + 高品質壓縮（避免檔案爆炸）
MAX_SOURCE_HEIGHT = 2160
FRAME_SAMPLE_SECONDS = 0.8
LOOKAHEAD_SECONDS = 0.8
TARGET_LONG_SIDE = 2800
PDF_RESOLUTION = 220.0
PDF_QUALITY = 93
SHARPEN_FACTOR = 1.18
UNSHARP_RADIUS = 0.5
UNSHARP_PERCENT = 145

def get_video_stream_url(youtube_url, prefer_highest=False):
    """
    使用 yt-dlp 取得可直接串流的影片 URL（不下載檔案）。
    若無法取得，回傳 None。
    """
    base_opts = {
        'format': 'bestvideo[ext=mp4][vcodec!=none]/bestvideo[vcodec!=none]/best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'retries': 10,
        'fragment_retries': 10,
        'socket_timeout': 30,
        'force_ipv4': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Referer': 'https://www.youtube.com/'
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'web']
            }
        }
    }
    attempts = [{}]

    last_error = None
    for extra_opts in attempts:
        ydl_opts = {**base_opts, **extra_opts}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(youtube_url, download=False)
        except Exception as exc:
            last_error = str(exc)
            continue

        if not isinstance(info, dict):
            continue

        if info.get('url'):
            return info.get('url'), None

        formats = info.get('formats') or []
        candidates = [
            f for f in formats
            if f.get('url') and f.get('protocol') in ('https', 'http') and f.get('ext') == 'mp4'
        ]
        if not candidates:
            candidates = [f for f in formats if f.get('url') and f.get('protocol') in ('https', 'http')]

        if not candidates:
            continue

        # 優先較高但合理的解析度，提升輸出清晰度
        def rank(f):
            h = f.get('height') or 0
            fps = f.get('fps') or 0
            if prefer_highest:
                # 固定上限到 2160p，兼顧畫質與速度
                return (-min(h, MAX_SOURCE_HEIGHT), -fps)
            # 預設：偏好 2160p 以內高解析度，過高會降權
            over_penalty = 1 if h > MAX_SOURCE_HEIGHT else 0
            return (over_penalty, -min(h, MAX_SOURCE_HEIGHT), -fps)

        selected = min(candidates, key=rank)
        return selected.get('url'), None

    return None, (last_error or '無法取得可用串流網址')

def download_video_to_temp(youtube_url, temp_dir, prefer_highest=False):
    """下載影片到暫存資料夾，回傳實際檔案路徑。"""
    output_template = os.path.join(temp_dir, "temp_video.%(ext)s")
    # 當 prefer_highest=True 時，優先下載最高畫質的 video+audio 組合
    if prefer_highest:
        format_pref = f'bestvideo[height<={MAX_SOURCE_HEIGHT}][vcodec!=none]+bestaudio/best[height<={MAX_SOURCE_HEIGHT}]/best'
    else:
        format_pref = 'bestvideo[ext=mp4][vcodec!=none]/bestvideo[vcodec!=none]/best[ext=mp4]/best'

    base_opts = {
        'format': format_pref,
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'retries': 10,
        'fragment_retries': 10,
        'socket_timeout': 30,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Referer': 'https://www.youtube.com/'
        },
    }

    # 不同雲端出口對 YouTube 會有不同行為，依序嘗試多組策略
    strategies = [
        {
            'force_ipv4': True,
            'extractor_args': {'youtube': {'player_client': ['android', 'web']}},
        },
        {
            'force_ipv4': False,
            'extractor_args': {'youtube': {'player_client': ['tv', 'web']}},
        },
        {
            'force_ipv4': True,
            'extractor_args': {'youtube': {'player_client': ['web_creator', 'android']}},
        },
    ]

    last_error = None
    for extra in strategies:
        opts = {**base_opts, **extra}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([youtube_url])
            break
        except Exception as exc:
            last_error = str(exc)
            continue
    else:
        raise RuntimeError(last_error or '下載失敗')

    # 下載後實際副檔名不一定，掃描常見格式
    patterns = [
        os.path.join(temp_dir, 'temp_video.*'),
        os.path.join(temp_dir, '*.mp4'),
        os.path.join(temp_dir, '*.mkv'),
        os.path.join(temp_dir, '*.webm'),
    ]
    for pattern in patterns:
        files = sorted(glob.glob(pattern))
        if files:
            return files[0]
    return None

def extract_unique_slides(video_path, threshold=15.0):
    """
    透過影像差異分析 (MSE)，自動抓取不重複的簡報畫面。
    """
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    
    # 防呆機制：如果抓不到 FPS，預設為 30
    if fps == 0 or fps is None or np.isnan(fps):
        fps = 30
        
    frame_interval = max(int(fps * FRAME_SAMPLE_SECONDS), 1)
    lookahead_frames = max(int(fps * LOOKAHEAD_SECONDS), 1)

    def sharpness_score(frame_bgr):
        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        return cv2.Laplacian(gray, cv2.CV_64F).var()

    def pick_sharpest_after_change(current_frame):
        best_frame = current_frame
        best_score = sharpness_score(current_frame)

        for _ in range(lookahead_frames):
            ok, candidate = cap.read()
            if not ok:
                break
            score = sharpness_score(candidate)
            if score > best_score:
                best_score = score
                best_frame = candidate
        return best_frame

    success, frame = cap.read()
    if not success:
        cap.release()
        return []

    # 初始化第一張畫面（轉灰階用於運算，轉 RGB 用於儲存）
    prev_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    slides_images = [Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))]

    count = 0

    while success:
        success, frame = cap.read()
        if not success:
            break
        
        count += 1
        
        # 略過不需要比對的幀數，節省運算資源
        if count % frame_interval != 0:
            continue 

        curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        
        # 計算 Mean Squared Error (MSE)，判斷畫面是否發生大幅度切換
        mse = np.mean((prev_gray.astype("float") - curr_gray.astype("float")) ** 2)
        
        # 若差異大於設定的閾值，判定為換頁
        if mse > threshold:
            # 避免轉場模糊：在換頁後挑最清晰的一幀存入 PDF
            best_frame = pick_sharpest_after_change(frame)
            best_gray = cv2.cvtColor(best_frame, cv2.COLOR_BGR2GRAY)
            slides_images.append(Image.fromarray(cv2.cvtColor(best_frame, cv2.COLOR_BGR2RGB)))
            prev_gray = best_gray # 更新對比基準

    cap.release()
    return slides_images

def generate_pdf(images):
    """將提取的圖片轉換為 PDF 二進位資料"""
    if not images:
        return None

    def crop_letterbox(img):
        rgb_np = np.array(img.convert("RGB"))
        gray = cv2.cvtColor(rgb_np, cv2.COLOR_RGB2GRAY)
        mask = gray > 16
        coords = np.argwhere(mask)
        if coords.size == 0:
            return img

        y0, x0 = coords.min(axis=0)
        y1, x1 = coords.max(axis=0)
        return img.crop((int(x0), int(y0), int(x1) + 1, int(y1) + 1))

    processed_images = []
    for img in images:
        rgb = crop_letterbox(img).convert("RGB")

        # 固定輸出尺寸，平衡清晰度與檔案大小
        long_side = max(rgb.size)
        scale = max(1.0, TARGET_LONG_SIDE / float(long_side))
        if scale > 1.0:
            new_w = int(round(rgb.width * scale))
            new_h = int(round(rgb.height * scale))
            rgb = rgb.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # 先做局部對比增強，讓細字與線條更容易被看見
        rgb_np = np.array(rgb)
        lab = cv2.cvtColor(rgb_np, cv2.COLOR_RGB2LAB)
        l_channel, a_channel, b_channel = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=1.6, tileGridSize=(8, 8))
        l_channel = clahe.apply(l_channel)
        rgb_np = cv2.cvtColor(cv2.merge((l_channel, a_channel, b_channel)), cv2.COLOR_LAB2RGB)

        rgb = Image.fromarray(rgb_np)

        # 文字優先的細節強化，避免把整張圖過度銳化
        rgb = ImageOps.autocontrast(rgb, cutoff=0.5)
        rgb = ImageEnhance.Sharpness(rgb).enhance(SHARPEN_FACTOR)
        rgb = rgb.filter(ImageFilter.UnsharpMask(radius=UNSHARP_RADIUS, percent=UNSHARP_PERCENT, threshold=0))
        processed_images.append(rgb)

    # 高品質壓縮 PDF：在檔案大小與清晰度間取得平衡
    pdf_bytes = io.BytesIO()
    processed_images[0].save(
        pdf_bytes,
        format="PDF",
        save_all=True,
        append_images=processed_images[1:],
        resolution=PDF_RESOLUTION,
        quality=PDF_QUALITY,
        subsampling=0,
        optimize=True,
    )
    return pdf_bytes.getvalue()

def get_video_title(youtube_url):
    """取得影片標題，失敗時回傳預設名稱。"""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
        if isinstance(info, dict) and info.get('title'):
            return str(info.get('title'))
    except Exception:
        pass
    return 'YouTube_Slides'

def sanitize_filename(name):
    invalid = '<>:"/\\|?*'
    cleaned = ''.join('_' if ch in invalid else ch for ch in name).strip().strip('.')
    return cleaned[:120] or 'YouTube_Slides'

def image_to_png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()

# ==========================================
# Streamlit 網頁使用者介面 (UI)
# ==========================================

st.set_page_config(page_title="YT 簡報自動擷取神器", page_icon="📊", layout="centered")

st.markdown(
        """
        <style>
            @import url('https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;700&family=Noto+Sans+TC:wght@400;600;700&display=swap');

            :root {
                --bg-1: #fff7fb;
                --bg-2: #ffe8f3;
                --panel: rgba(255, 255, 255, 0.86);
                --line: rgba(255, 117, 182, 0.38);
                --ink: #3a2342;
                --ink-soft: #684b74;
                --pink-neon: #ff4fa2;
                --pink-glow: #ff8cc3;
                --violet-glow: #c28bff;
            }

            .stApp {
                background:
                    radial-gradient(circle at 10% 8%, rgba(255, 154, 201, 0.45), transparent 30%),
                    radial-gradient(circle at 88% 12%, rgba(255, 206, 234, 0.62), transparent 34%),
                    linear-gradient(140deg, var(--bg-1), var(--bg-2) 48%, #fff2fa 100%);
                color: var(--ink);
                font-family: 'Noto Sans TC', sans-serif;
                animation: hue-drift 18s linear infinite;
            }

            @keyframes hue-drift {
                0% { filter: hue-rotate(0deg); }
                50% { filter: hue-rotate(8deg); }
                100% { filter: hue-rotate(0deg); }
            }

            header[data-testid="stHeader"],
            div[data-testid="stToolbar"],
            div[data-testid="stDecoration"],
            div[data-testid="stStatusWidget"],
            section[data-testid="stSidebar"],
            [data-testid="collapsedControl"] {
                display: none !important;
            }

            #MainMenu,
            footer {
                visibility: hidden;
            }

            .esports-bg {
                position: fixed;
                inset: 0;
                pointer-events: none;
                z-index: 0;
                background:
                    linear-gradient(rgba(255, 79, 162, 0.06) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(255, 79, 162, 0.06) 1px, transparent 1px);
                background-size: 38px 38px;
                mask-image: radial-gradient(circle at center, black 32%, transparent 90%);
                opacity: 0.34;
                animation: grid-flow 8s linear infinite;
            }

            @keyframes grid-flow {
                0% { transform: translate3d(0, 0, 0); }
                100% { transform: translate3d(38px, 38px, 0); }
            }

            .esports-scan {
                position: fixed;
                left: 0;
                right: 0;
                top: -20%;
                height: 22%;
                z-index: 1;
                pointer-events: none;
                background: linear-gradient(to bottom, transparent, rgba(255, 79, 162, 0.18), transparent);
                animation: scanline 7s linear infinite;
            }

            .neon-orb {
                position: fixed;
                border-radius: 999px;
                pointer-events: none;
                z-index: 0;
                filter: blur(4px);
                mix-blend-mode: screen;
            }

            .orb-a {
                width: 220px;
                height: 220px;
                left: 7%;
                top: 24%;
                background: radial-gradient(circle at 30% 30%, rgba(255, 85, 170, 0.76), rgba(255, 85, 170, 0.02));
                animation: orb-float-a 9s ease-in-out infinite;
            }

            .orb-b {
                width: 280px;
                height: 280px;
                right: 8%;
                top: 56%;
                background: radial-gradient(circle at 35% 35%, rgba(194, 139, 255, 0.7), rgba(194, 139, 255, 0.02));
                animation: orb-float-b 11s ease-in-out infinite;
            }

            .orb-c {
                width: 160px;
                height: 160px;
                left: 52%;
                top: 10%;
                background: radial-gradient(circle at 35% 35%, rgba(255, 190, 222, 0.72), rgba(255, 190, 222, 0.02));
                animation: orb-float-c 7.5s ease-in-out infinite;
            }

            @keyframes orb-float-a {
                0%, 100% { transform: translateY(0) translateX(0); }
                50% { transform: translateY(-26px) translateX(12px); }
            }

            @keyframes orb-float-b {
                0%, 100% { transform: translateY(0) translateX(0); }
                50% { transform: translateY(20px) translateX(-16px); }
            }

            @keyframes orb-float-c {
                0%, 100% { transform: translateY(0) scale(1); }
                50% { transform: translateY(18px) scale(1.1); }
            }

            @keyframes scanline {
                0% { transform: translateY(-25vh); }
                100% { transform: translateY(140vh); }
            }

            [data-testid="stAppViewContainer"] > .main {
                position: relative;
                z-index: 2;
                padding-top: 2rem;
            }

            [data-testid="stAppViewContainer"] .block-container {
                max-width: 760px;
                padding-top: 0.5rem;
                padding-bottom: 2rem;
            }

            .main-shell {
                border: 1px solid rgba(255, 127, 188, 0.34);
                border-radius: 20px;
                padding: 20px 20px 12px;
                background: rgba(255, 255, 255, 0.8);
                box-shadow: 0 10px 30px rgba(255, 132, 190, 0.18);
                backdrop-filter: blur(4px);
                margin-bottom: 14px;
                overflow: hidden;
                position: relative;
                isolation: isolate;
            }

            .main-shell::before {
                content: "";
                position: absolute;
                inset: -120% -20% auto;
                height: 220%;
                background: linear-gradient(110deg, transparent 36%, rgba(255, 255, 255, 0.55) 50%, transparent 64%);
                transform: translateX(-45%) rotate(8deg);
                animation: shell-sheen 5.5s ease-in-out infinite;
                pointer-events: none;
            }

            .main-shell::after {
                content: "";
                position: absolute;
                width: 54px;
                height: 7px;
                border-radius: 999px;
                background: linear-gradient(
                    90deg,
                    rgba(255, 255, 255, 0.15) 0%,
                    rgba(255, 255, 255, 0.95) 35%,
                    rgba(255, 117, 186, 0.95) 72%,
                    rgba(194, 139, 255, 0.86) 100%
                );
                box-shadow:
                    0 0 9px rgba(255, 110, 184, 0.78),
                    0 0 20px rgba(194, 139, 255, 0.5),
                    0 0 30px rgba(255, 110, 184, 0.34);
                offset-path: inset(-1px round 20px);
                offset-rotate: auto;
                animation: border-run 4.2s linear infinite;
                pointer-events: none;
                z-index: 2;
            }

            @keyframes border-run {
                0% {
                    offset-distance: 0%;
                    transform: scale(1);
                    opacity: 0.95;
                }
                50% {
                    transform: scale(1.12);
                    opacity: 1;
                }
                100% {
                    offset-distance: 100%;
                    transform: scale(1);
                    opacity: 0.95;
                }
            }

            @keyframes shell-sheen {
                0% { transform: translateX(-65%) rotate(8deg); }
                55% { transform: translateX(72%) rotate(8deg); }
                100% { transform: translateX(72%) rotate(8deg); }
            }

            .hero {
                border: 1px solid var(--line);
                border-radius: 18px;
                padding: 20px 22px;
                background: linear-gradient(160deg, rgba(255, 255, 255, 0.9), rgba(255, 241, 248, 0.92));
                box-shadow: 0 0 0 1px rgba(255, 140, 195, 0.2) inset, 0 8px 26px rgba(255, 128, 186, 0.18);
                margin-bottom: 16px;
            }

            .hero h1 {
                margin: 0;
                font-family: 'Chakra Petch', sans-serif;
                font-weight: 700;
                letter-spacing: 0.04em;
                color: #40214d;
                text-shadow: 0 0 10px rgba(255, 120, 190, 0.28);
                font-size: clamp(1.45rem, 2.1vw, 2.25rem);
                animation: neon-flicker 2.8s ease-in-out infinite;
            }

            .hero p {
                margin: 10px 0 0;
                color: var(--ink-soft);
                font-size: 0.98rem;
            }

            @keyframes neon-flicker {
                0%, 100% { text-shadow: 0 0 8px rgba(255, 120, 190, 0.32), 0 0 16px rgba(194, 139, 255, 0.2); }
                50% { text-shadow: 0 0 16px rgba(255, 79, 162, 0.45), 0 0 24px rgba(194, 139, 255, 0.35); }
            }

            [data-testid="stMarkdownContainer"],
            label,
            p,
            span,
            .st-emotion-cache-10trblm,
            .st-emotion-cache-1xarl3l {
                color: var(--ink) !important;
            }

            [data-testid="stTextInput"] > div > div > input {
                background: rgba(255, 255, 255, 0.96);
                border: 1px solid rgba(255, 117, 182, 0.45);
                color: #2f1e39;
                border-radius: 12px;
                font-weight: 600;
                transition: transform 0.18s ease, box-shadow 0.18s ease;
            }

            [data-testid="stTextInput"] > div > div > input:focus {
                border-color: var(--pink-neon);
                box-shadow: 0 0 0 1px rgba(255, 79, 162, 0.45), 0 0 18px rgba(255, 121, 188, 0.28);
                transform: translateY(-1px);
            }

            .stButton > button,
            .stDownloadButton > button {
                border-radius: 12px;
                border: 1px solid rgba(255, 79, 162, 0.52);
                background: linear-gradient(95deg, rgba(255, 98, 173, 0.95), rgba(255, 160, 206, 0.95), rgba(194, 139, 255, 0.9));
                background-size: 220% 220%;
                color: #fff;
                font-weight: 700;
                box-shadow: 0 0 12px rgba(255, 128, 186, 0.34);
                transition: transform 0.16s ease, box-shadow 0.16s ease;
                animation: btn-energy 2.4s linear infinite;
            }

            @keyframes btn-energy {
                0% { background-position: 0% 50%; }
                100% { background-position: 100% 50%; }
            }

            .stButton > button:hover,
            .stDownloadButton > button:hover {
                transform: translateY(-1px);
                box-shadow: 0 0 18px rgba(255, 98, 173, 0.45), 0 0 24px rgba(255, 170, 213, 0.32);
            }

            [data-testid="stAlert"] {
                background: rgba(255, 255, 255, 0.88);
                border: 1px solid rgba(255, 132, 190, 0.4);
                color: #3a2342;
            }

            [data-testid="stImage"] img {
                border-radius: 14px;
                border: 1px solid rgba(255, 132, 190, 0.38);
                box-shadow: 0 8px 24px rgba(255, 154, 201, 0.3);
            }

            [data-testid="stSpinner"] {
                filter: drop-shadow(0 0 8px rgba(255, 95, 174, 0.45));
            }
        </style>
        <div class="esports-bg"></div>
        <div class="esports-scan"></div>
        <div class="neon-orb orb-a"></div>
        <div class="neon-orb orb-b"></div>
        <div class="neon-orb orb-c"></div>
                <div class="main-shell">
                    <div class="hero">
            <h1>YT 幻燈片提取器</h1>
            <p>貼上 YouTube 網址，系統會自動抓取畫面並輸出高品質 PDF。</p>
                    </div>
        </div>
        """,
        unsafe_allow_html=True,
)

if 'preview_images' not in st.session_state:
    st.session_state.preview_images = None
if 'slide_count' not in st.session_state:
    st.session_state.slide_count = 0
if 'pdf_data' not in st.session_state:
    st.session_state.pdf_data = None
if 'pdf_name' not in st.session_state:
    st.session_state.pdf_name = 'YouTube_Slides.pdf'

high_quality = True

url_input = st.text_input("🔗 YouTube 影片網址：", placeholder="https://www.youtube.com/watch?v=...")

if st.button("🚀 開始執行自動化擷取", type="primary"):
    if url_input:
        try:
            sensitivity = 12.0
            title = sanitize_filename(get_video_title(url_input))
            slides = []

            def extract_with_retry(video_source, source_name):
                with st.spinner(f"🔍 正在分析{source_name}..."):
                    found_slides = extract_unique_slides(video_source, threshold=sensitivity)

                if len(found_slides) <= 1 and sensitivity > 5.0:
                    retry_threshold = max(5.0, round(sensitivity * 0.6, 1))
                    with st.spinner(f"🔁 {source_name} 首輪擷取較少，改用較低閾值 {retry_threshold} 再試一次..."):
                        retry_slides = extract_unique_slides(video_source, threshold=retry_threshold)
                    if len(retry_slides) > len(found_slides):
                        found_slides = retry_slides
                return found_slides

            # 直接採用最高畫質下載分析，避免串流來源限制解析度
            with tempfile.TemporaryDirectory() as temp_dir:
                with st.spinner("📥 正在下載最高畫質影片..."):
                    downloaded_video = download_video_to_temp(url_input, temp_dir, prefer_highest=high_quality)

                if not downloaded_video:
                    st.error("❌ 下載完成但找不到影片檔，請稍後再試。")
                    st.stop()

                slides = extract_with_retry(downloaded_video, "下載後影片")
                
            if slides:
                if len(slides) <= 1:
                    st.warning("⚠️ 本次只偵測到少量畫面。系統已自動重試，若結果仍不理想，請換另一支影片再試。")
                pdf_data = generate_pdf(slides)
                if not pdf_data:
                    st.error("❌ PDF 產生失敗，請換一支影片再試。")
                    st.stop()

                st.session_state.preview_images = [image_to_png_bytes(img) for img in slides[:3]]
                st.session_state.slide_count = len(slides)
                st.session_state.pdf_data = pdf_data
                st.session_state.pdf_name = f"{title}.pdf"
            else:
                st.warning("⚠️ 未偵測到可用畫面，請確認影片網址有效，或換另一支公開影片再試。")
                    
        except Exception as e:
            err = str(e)
            if 'HTTP Error 403' in err or 'unable to download video data' in err:
                st.error("❌ YouTube 拒絕下載（403）。此狀況即使公開影片也可能發生（雲端 IP 被限制）。請換影片或改在本機執行。")
            else:
                st.error(f"❌ 程式執行發生錯誤：{err}")
    else:
        st.error("⚠️ 請先輸入有效的 YouTube 網址！")

if st.session_state.pdf_data:
    st.success(f"🎉 成功擷取了 **{st.session_state.slide_count}** 張不重複的簡報畫面！")

    st.subheader("👀 擷取結果預覽")
    preview_images = st.session_state.preview_images or []
    preview_count = min(len(preview_images), 3)
    for i in range(preview_count):
        st.image(preview_images[i], caption=f"第 {i+1} 頁", width=700)
    if st.session_state.slide_count > 3:
        st.caption(f"（還有 {st.session_state.slide_count - 3} 張畫面未顯示於預覽中...）")

    st.markdown("---")
    st.subheader("📥 下載 PDF")
    st.download_button(
        label="📄 下載 PDF 檔",
        data=st.session_state.pdf_data,
        file_name=st.session_state.pdf_name,
        mime="application/pdf",
        use_container_width=True,
        key="pdf_download_btn",
    )