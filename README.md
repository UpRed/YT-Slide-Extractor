# YT-Slide-Extractor

把 YouTube 影片中的簡報頁面自動擷取成圖片，並匯出為 PDF。

## 線上體驗

- https://yt-slide-extractor-kxnvssda6xezarl2gpvbxv.streamlit.app/

## 功能

- 貼上 YouTube 網址即可開始擷取
- 以 OpenCV 比對畫面差異，自動抓取不重複頁面
- 支援調整「波動閾值」控制擷取敏感度
- 輸出高畫質 PDF（300 DPI）

## 環境需求

- Python 3.10+
- Windows / macOS / Linux

## 安裝

```bash
python -m venv venv
```

Windows:

```bash
.\venv\Scripts\activate
```

macOS / Linux:

```bash
source venv/bin/activate
```

安裝套件：

```bash
pip install -r requirements.txt
```

## 啟動

```bash
streamlit run app.py
```

啟動後在瀏覽器開啟終端機顯示的網址（例如 `http://localhost:8501`）。

## 網頁版（貼網址直接執行）

如果你要使用「貼網址 -> 執行任務 -> 下載 PDF」的完整網頁介面，可啟動 Flask 版本：

```bash
python web_app.py
```

然後打開：`http://localhost:5000`

## 部署固定公開網址（Render）

本專案已內建 `render.yaml`，可直接用 Render 建立固定公開網址。

### 一鍵部署

打開以下連結並登入 Render：

`https://render.com/deploy?repo=https://github.com/UpRed/YT-Slide-Extractor`

### 手動重點

- Service 類型：`Web Service`
- Build Command：`pip install -r requirements.txt`
- Start Command：`gunicorn web_app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120`

### Render 重要環境變數（解決 YouTube bot 驗證）

在 Render 的 `Environment` 新增：

- `YTDLP_COOKIES_B64`：將 `cookies.txt` 內容轉成 base64 後貼上

Windows 產生 base64 範例：

```powershell
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-Content .\cookies.txt -Raw)))
```

把輸出的字串貼到 Render `YTDLP_COOKIES_B64`，再按 `Manual Deploy -> Deploy latest commit`。

部署完成後，Render 會提供固定網址（例如 `https://xxx.onrender.com`）。

## 使用方式

1. 貼上 YouTube 影片網址
2. 視需要調整左側「波動閾值」
3. 按下「開始執行自動化擷取」
4. 預覽擷取結果，下載 PDF

## 常見問題

### 1) 出現 403 / 無法取得串流網址

- 影片可能有年齡、地區或登入限制
- 先確認影片可公開播放
- 可嘗試更新 `yt-dlp`
- 雲端部署（Render）建議設定 `YTDLP_COOKIES_B64`，避免 `Sign in to confirm you’re not a bot`

```bash
pip install -U yt-dlp
```

### 2) 擷取頁數太少

- 請調低「波動閾值」
- 影片切頁速度慢時可多試幾次

### 3) PDF 看起來模糊

- 已預設高解析度輸出
- 若來源影片本身畫質較低，PDF 仍會受限制

## 專案結構

- `app.py`：主程式（Streamlit UI + 擷取邏輯）
- `requirements.txt`：Python 套件清單

## 授權

僅供學習與研究使用。請遵守 YouTube 與影片內容的使用規範。
