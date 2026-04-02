# Yapper - Discord YouTube Music Bot

## 功能
- 加入/離開語音頻道
- 播放 YouTube 連結或關鍵字搜尋
- 佇列管理（自動播放下一首）
- 佇列安全上限（預設 100 首，避免資源被大量佔用）
- 暫停、繼續、跳過、停止
- 查看目前播放與待播清單
- 抽籤隨機標註目前頻道中的一位使用者
- 播放控制限制：必須和機器人在同一個語音頻道
- Opus 直通播放管線（移除音量控制以降低 CPU/記憶體負載）
- 閒置自動離線（預設 5 分鐘）
- 分類錯誤訊息（參數、下載、語音客戶端）
- 同時支援前綴指令 `!` 與斜線指令 `/`
- 自動刪除使用者前綴指令訊息（僅保留機器人回覆）

## 安裝
1. 安裝 Python 3.10+
2. 安裝 FFmpeg 並確保 ffmpeg 在 PATH 中
3. 安裝套件：

   pip install -r requirements.txt

## 設定 Token
Windows PowerShell：

$env:DISCORD_BOT_TOKEN = "你的機器人Token"
python yapper.py

Windows cmd：

set DISCORD_BOT_TOKEN=你的新Token
python yapper.py

## QNAP NAS 前置作業（Container Station）
1. 在 QNAP 安裝 `Container Station`，並確認 NAS 可連線外網。
2. 建立專案資料夾（例如 `Public/yapper`），把以下檔案放進同一層：
   - `yapper.py`
   - `requirements.txt`
   - `Dockerfile`
   - `docker-compose.yml`
   - `.env.example`
3. 若使用純 GUI 建立 Compose，請在 Container Station 的 Environment 欄位填入：

   DISCORD_BOT_TOKEN=你的機器人Token
   TZ=Asia/Taipei

4. 若你使用 SSH 啟動 Compose，仍可複製 `.env.example` 為 `.env` 並填入相同內容。

5. 透過 SSH 進入該資料夾後執行：

   docker compose up -d --build

6. 查看執行狀態與日誌：

   docker ps
   docker logs -f yapper-bot

7. 更新版本（重新 build）時執行：

   docker compose pull
   docker compose up -d --build

8. 停止服務：

   docker compose down

## 指令
- 斜線指令（建議）：
   - /join
   - /play <YouTube網址或關鍵字>
   - /queue
   - /now
   - /roll
   - /pause
   - /resume
   - /skip
   - /stop
   - /leave
- !join
- !play <YouTube網址或關鍵字>
- !queue
- !now
- !roll
- !pause
- !resume
- !skip
- !stop
- !leave

## 注意
- 請勿將 Token 寫死在程式碼中。
- 斜線指令採全域同步，首次啟動或更新後可能需要幾分鐘到一小時才會在所有伺服器顯示。
- 本專案的容器預設啟用唯讀 root filesystem 與記憶體限制，目標是穩定且低資源占用。
- 若機器人能加入但無聲音，請先確認 FFmpeg 與語音權限設定。
- 目前未提供音量調整指令；音訊採 Opus 直通優先策略。
- skip、stop、leave 需在和機器人同一語音頻道中執行。
- 若要啟用自動刪除使用者指令訊息，機器人需要 `Manage Messages` 權限。
- 若缺少 `Manage Messages` 權限，機器人會降級為不刪訊，但播放功能仍可使用。
