import streamlit as st
import cv2
import numpy as np
import yt_dlp
import io
import os
import glob
import tempfile
from PIL import Image

# ==========================================
# 核心處理功能區
# ==========================================

def get_video_stream_url(youtube_url):
    """
    使用 yt-dlp 取得可直接串流的影片 URL（不下載檔案）。
    若無法取得，回傳 None。
    """
    base_opts = {
        'format': 'best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
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

    # 先嘗試無 cookie，失敗再嘗試帶入常見瀏覽器 cookie（本機可用時）
    attempts = [
        {},
        {'cookiesfrombrowser': ('chrome',)},
        {'cookiesfrombrowser': ('edge',)}
    ]

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
            # 先選不超過 1080p 的最高解析度，避免 4K 造成處理過慢
            over_penalty = 1 if h > 1080 else 0
            return (over_penalty, -min(h, 1080), -fps)

        selected = min(candidates, key=rank)
        return selected.get('url'), None

    return None, (last_error or '無法取得可用串流網址')

def download_video_to_temp(youtube_url, temp_dir):
    """下載影片到暫存資料夾，回傳實際檔案路徑。"""
    output_template = os.path.join(temp_dir, "temp_video.%(ext)s")
    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([youtube_url])

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
        
    frame_interval = max(int(fps), 1) # 每秒抽樣一次
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
            slides_images.append(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
            prev_gray = curr_gray # 更新對比基準

    cap.release()
    return slides_images

def generate_pdf(images):
    """將提取的圖片轉換為 PDF 二進位資料"""
    if not images: return None
    pdf_bytes = io.BytesIO()
    rgb_images = [img.convert("RGB") for img in images]
    rgb_images[0].save(
        pdf_bytes,
        format="PDF",
        save_all=True,
        append_images=rgb_images[1:],
        resolution=300.0,
    )
    return pdf_bytes.getvalue()

# ==========================================
# Streamlit 網頁使用者介面 (UI)
# ==========================================

st.set_page_config(page_title="YT 簡報自動擷取神器", page_icon="📊", layout="centered")

st.title("📊 YT 簡報自動擷取神器")
st.markdown("貼上 YouTube 網址，系統會自動辨識影片中的投影片切換，並幫你打包成 **PDF**。")

with st.sidebar:
    st.header("⚙️ 偵測參數設定")
    sensitivity = st.slider(
        "畫面變動閾值 (Threshold)", 
        min_value=5.0, max_value=50.0, value=15.0, step=1.0,
        help="數值越小：越容易觸發截圖（適合有小動畫的簡報）；數值越大：只有整頁大翻頁才會截圖。"
    )
    st.info("💡 提示：如果抓出來的簡報太多重複頁面，請將閾值「調高」。如果漏掉很多頁，請將閾值「調低」。")

url_input = st.text_input("🔗 YouTube 影片網址：", placeholder="https://www.youtube.com/watch?v=...")

if st.button("🚀 開始執行自動化擷取", type="primary"):
    if url_input:
        try:
            # 只走串流流程，不下載整片影片
            with st.spinner("🔗 正在取得可串流的影片網址..."):
                stream_url, stream_error = get_video_stream_url(url_input)

            if not stream_url:
                st.error(f"❌ 無法取得可用串流網址：{stream_error}")
                st.info("💡 可能是影片受限（年齡/地區/登入），可嘗試改用可公開播放的影片網址。")
                st.stop()

            with st.spinner("🔍 正在串流解析簡報畫面..."):
                slides = extract_unique_slides(stream_url, threshold=sensitivity)

            # 若僅抓到單張畫面，代表未偵測到明顯切頁，嘗試自動降閾值重跑一次
            if len(slides) <= 1 and sensitivity > 5.0:
                retry_threshold = max(5.0, round(sensitivity * 0.6, 1))
                with st.spinner(f"🔁 首輪擷取較少，改用較低閾值 {retry_threshold} 再試一次..."):
                    retry_slides = extract_unique_slides(stream_url, threshold=retry_threshold)
                if len(retry_slides) > len(slides):
                    slides = retry_slides

            # 串流仍抓不到有效畫面時，改用暫存下載流程做備援
            if len(slides) <= 1:
                with tempfile.TemporaryDirectory() as temp_dir:
                    with st.spinner("📥 串流偵測不足，改用下載模式重試..."):
                        downloaded_video = download_video_to_temp(url_input, temp_dir)
                    if downloaded_video:
                        with st.spinner("🔍 正在分析下載後影片..."):
                            download_slides = extract_unique_slides(downloaded_video, threshold=sensitivity)
                        if len(download_slides) > len(slides):
                            slides = download_slides
                
            if slides:
                if len(slides) <= 1:
                    st.warning("⚠️ 未觸發到峰值的波動，請嘗試在左側選單調低「波動閾值」。")

                st.success(f"🎉 成功擷取了 **{len(slides)}** 張不重複的簡報畫面！")
                
                # 畫面預覽區塊（使用較穩定的單欄渲染，避免前端節點錯誤）
                st.subheader("👀 擷取結果預覽")
                preview_count = min(len(slides), 3)
                for i in range(preview_count):
                    st.image(slides[i], caption=f"第 {i+1} 頁", width=700)
                if len(slides) > 3:
                    st.caption(f"（還有 {len(slides) - 3} 張畫面未顯示於預覽中...）")
                
                st.markdown("---")
                st.subheader("📥 下載 PDF")

                pdf_data = generate_pdf(slides)
                st.download_button(
                    label="📄 下載 PDF 檔",
                    data=pdf_data,
                    file_name="YouTube_Slides.pdf",
                    mime="application/pdf",
                    use_container_width=True
                )
            else:
                st.warning("⚠️ 未偵測到可用畫面，請確認影片網址有效，或嘗試調低「波動閾值」。")
                    
        except Exception as e:
            st.error(f"❌ 程式執行發生錯誤：{str(e)}")
    else:
        st.error("⚠️ 請先輸入有效的 YouTube 網址！")