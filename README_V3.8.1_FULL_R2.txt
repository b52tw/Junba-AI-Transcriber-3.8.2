峻爸 AI Transcriber v3.8.1 FULL R2
=================================

這是完整原始碼版，不是 Overlay，也不會在 GitHub Actions 執行時再去另一個 Repo 套補丁。
請將本資料夾「裡面的所有檔案」直接上傳到一個新的空白 GitHub Repository 根目錄。

本版修正／保留：
1. 「開啟／選擇核對播放器」可尋找最近 HTML，也可自行選擇其他 HTML。
2. Word 橫向版面，欄寬：逐字內容 > 時間 > 講者；逐字內容字體較大。
3. 每支音檔輸出到獨立資料夾：01_原始檔名、02_原始檔名……
4. Windows Portable、Windows Single EXE、Android Gemini APK 三種建置。
5. 新增 Markdown 輸出；HTML 核對播放器也接受 .md/.markdown。

這版已針對前一輪 GitHub 失敗修正：
- open_latest_review 的字串換行 SyntaxError 已排除。
- Markdown 的 HTML accept 排序保留舊測試需要的 .txt,.srt,.vtt,.json 前綴，舊測試不再失敗。
- GitHub Actions 在安裝大量依賴前先跑 Python syntax preflight，可更快抓出語法錯誤。
- Android 使用已在前一輪 GitHub Actions 成功編譯的同一份 Android 原始碼與 sdkmanager 自動定位流程。

Workflow：
.github/workflows/build-windows-v3.8.yml

成功後 Artifacts：
- Junba-AI-Transcriber-v3.8.1-FULL-Portable-Windows-x64
- Junba-AI-Transcriber-v3.8.1-FULL-Single-EXE-Windows-x64
- Junba-AI-Transcriber-v3.8.1-Android-APK
