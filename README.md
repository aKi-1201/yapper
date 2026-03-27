# Yapper - Discord YouTube Music Bot

## 功能
- 加入/離開語音頻道
- 播放 YouTube 連結或關鍵字搜尋
- 佇列管理（自動播放下一首）
- 暫停、繼續、跳過、停止
- 查看目前播放與待播清單
- 播放控制限制：必須和機器人在同一個語音頻道
- 閒置自動離線（預設 5 分鐘）
- 分類錯誤訊息（參數、下載、語音客戶端）

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

## 指令
- !join
- !play <YouTube網址或關鍵字>
- !queue
- !now
- !pause
- !resume
- !skip
- !stop
- !leave

## 注意
- 請勿將 Token 寫死在程式碼中。
- 若機器人能加入但無聲音，請先確認 FFmpeg 與語音權限設定。
- skip、stop、leave 需在和機器人同一語音頻道中執行。
