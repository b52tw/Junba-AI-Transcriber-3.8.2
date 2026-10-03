# 峻爸 AI Transcriber v3.8.2｜Windows 路徑瘦身＋Android Whisper/Gemini 自適應混合版

本版只延續 **Junba_AI_Transcriber_v3.8** 主程式，不修改另外那支「峻爸 KTV 多文字軌核對器」。

## 這版主要修正

### 1. Windows 路徑瘦身
- Single EXE 最終檔名縮成 `Junba.exe`。
- Portable 最外層縮成 `JunbaP`，依賴資料夾縮成 `_i`。
- GitHub Artifact 縮成：
  - `Junba-v382-Single`
  - `Junba-v382-Portable`
  - `Junba-v382-APK`
- Portable Artifact 裡只放 `JunbaP.zip`，避免 GitHub Artifact 外層名稱＋Portable 長資料夾名稱重複疊加。
- Workflow 會產生 `PATH_REPORT.txt`，若 Portable 內部相對路徑仍超過 205 字元就直接讓建置失敗，不把有長路徑風險的版本交給使用者。
- PySide6 不再使用 `collect_all()` 全量收集，改讓 PyInstaller 官方 Qt Hook 自動收必要資源，以降低體積與深層路徑。

建議使用者解壓到短路徑，例如：

```text
C:\Junba\
```

### 2. Android API Key：一鍵前往 AI Studio＋每台手機獨立記憶
Android 版新增：
- `前往 AI Studio 取得 API Key`
- `從剪貼簿貼上並儲存`
- `儲存 Key`
- `測試 Key`

API Key 使用 Android Keystore AES/GCM 加密後存在該手機本機，不寫死在 APK。

> Google AI Studio 不提供安全機制讓第三方 App 自動把你帳號裡的 API Key 抓回來，因此 App 會一鍵開啟官方 AI Studio；你建立／複製 Key 後回到 App，按「從剪貼簿貼上並儲存」即可。每台手機可各自使用不同 Key。

### 3. Android Whisper × Gemini 自適應混合
Android 新增三種引擎：
- `自動混合（建議）`
- `本機 Whisper（離線）`
- `Google Gemini（雲端）`

自動規則：
- 已下載本機模型＋arm64-v8a＋沒有要求 Gemini 專屬功能 → 優先使用離線 Whisper。
- M4A/AAC/MP3/WAV/FLAC/OGG 會先由手機本機 FFmpeg 轉成 16 kHz mono WAV，再送 Whisper；不會因 M4A 被迫上雲。
- 多人講者、字詞級時間戳、smart 模式，或本機條件不足 → 自動使用 Gemini。
- 沒有 Gemini Key，但本機 Whisper 可用 → 自動退回本機 Whisper。
- 沒有本機模型也沒有 API Key → 明確提示使用者先下載模型或設定 Key，不會卡住。

### 4. Android 本機模型自適應
可選：
- `自動`：RAM < 5 GB 建議 tiny，其餘建議 base。
- tiny：約 75 MB，最快。
- base：約 142 MB，手機預設建議。
- small：約 466 MB，較準但較慢、較吃記憶體。

模型在 App 內按一次「下載／準備本機 Whisper 模型」即可，儲存在該 App 的私人模型目錄，不塞進 APK。

### 5. Android 響應式
- 窄手機：主要按鈕自動改成直向堆疊，避免按鈕被擠出畫面。
- 平板／大螢幕：按鈕自動並排。
- 所有主要設定都在 ScrollView 內，直式／橫式都可使用。

## Android 本機 Whisper 說明
Android 本機 Whisper 使用 `dev.ffmpegkit-maintained:whisper-android:1.0.0`，底層為 whisper.cpp。免費 AAR 目前以 `arm64-v8a` 為主，Whisper 本身需要 16-bit PCM WAV / 16 kHz / mono。本 App 另用 `ffmpeg-kit-audio:8.1.7` 在手機本機先轉碼，因此可處理常見 M4A/AAC/MP3/WAV/FLAC/OGG。

詳見 `THIRD_PARTY_ANDROID.txt`。

## GitHub Actions
Workflow：

```text
.github/workflows/build-windows-v3.8.yml
```

成功後會有三個主要 Artifact：

```text
Junba-v382-Single
Junba-v382-Portable
Junba-v382-APK
```

內容：
- Single：`Junba.exe`＋`SHA256.txt`
- Portable：`JunbaP.zip`＋`SHA256.txt`＋`PATH_REPORT.txt`
- APK：`Junba.apk`＋`SHA256.txt`

## Windows v3.8.1 既有功能仍保留
- faster-whisper / Whisper large-v3
- Intel OpenVINO / Intel GPU/NPU / CPU 自適應
- NVIDIA CUDA 路徑
- Google Gemini
- 批次辨識與自動切段
- Word / TXT / Markdown / SRT / VTT
- Word 時間軸逐字稿
- 每支原始音檔獨立輸出資料夾
- KTV / HTML 錄音核對播放器
- 暫停、繼續、立即停止並輸出目前結果

## 安全與隱私
- 本機 Whisper：音訊不會上傳。
- Gemini：只有實際選用 Gemini 時才會上傳音訊。
- API Key：Android 端以 Android Keystore 加密後儲存在該手機。
- 本專案不使用任何規避 Chrome、SmartScreen 或 Defender 安全檢查的技巧。
