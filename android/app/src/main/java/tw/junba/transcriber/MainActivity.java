package tw.junba.transcriber;

import android.app.Activity;
import android.app.ActivityManager;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.OpenableColumns;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.BufferedReader;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public class MainActivity extends Activity {
    private static final int REQ_AUDIO = 1001;
    private static final int REQ_SAVE_TXT = 1002;
    private static final int REQ_SAVE_MD = 1003;
    private static final String PREFS = "junba_android_v382";
    private static final String TRANSCRIBE_MODEL = "gemini-3.5-transcribe";
    private static final String AI_STUDIO_URL = "https://aistudio.google.com/app/apikey";
    private static final String MODEL_BASE_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/";

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final AtomicBoolean cancelFlag = new AtomicBoolean(false);
    private final AtomicBoolean failoverUsed = new AtomicBoolean(false);

    private Uri audioUri;
    private String audioName = "";
    private String lastTranscript = "";
    private String lastRawResponse = "";
    private String lastEngineLabel = "—";

    private TextView audioLabel;
    private EditText apiKey;
    private Spinner engineSpinner;
    private Spinner modeSpinner;
    private Spinner languageSpinner;
    private Spinner localModelSpinner;
    private CheckBox diarization;
    private CheckBox timestamps;
    private ProgressBar progress;
    private TextView stage;
    private TextView modelStatus;
    private TextView deviceStatus;
    private EditText result;
    private Button startButton;
    private Button cancelButton;
    private Button modelButton;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        migrateLegacyApiKey();
        setContentView(buildUi());
        updateModelStatus();
    }

    private void migrateLegacyApiKey() {
        if (!SecureKeyStore.load(this).isEmpty()) return;
        try {
            SharedPreferences old = getSharedPreferences("junba_android", MODE_PRIVATE);
            String legacy = old.getString("api_key", "");
            if (legacy != null && !legacy.trim().isEmpty()) {
                SecureKeyStore.save(this, legacy.trim());
                old.edit().remove("api_key").apply();
            }
        } catch (Exception ignored) {}
    }

    private View buildUi() {
        int pad = dp(14);
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(pad, pad, pad, dp(28));
        scroll.addView(root, new ScrollView.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        root.addView(text("峻爸 AI Transcriber v3.8.2｜Android 自適應混合版", 22, true));
        TextView note = text("自動混合會依手機、檔案格式、是否已下載 Whisper 模型與進階需求，自動選擇本機 Whisper 或 Gemini。API Key 只儲存在這一台手機。", 13, false);
        note.setPadding(0, dp(4), 0, dp(10));
        root.addView(note);

        Button pick = button("選擇錄音檔");
        pick.setOnClickListener(v -> chooseAudio());
        root.addView(pick, lpMatch());
        audioLabel = text("尚未選擇錄音檔", 14, false);
        audioLabel.setPadding(0, dp(6), 0, dp(10));
        root.addView(audioLabel);

        root.addView(sectionLabel("辨識引擎"));
        engineSpinner = new Spinner(this);
        String[] engines = {
                "自動混合（建議）",
                "本機 Whisper（離線）",
                "Google Gemini（雲端）"
        };
        engineSpinner.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item, engines));
        engineSpinner.setSelection(getSharedPreferences(PREFS, MODE_PRIVATE).getInt("engine", 0));
        root.addView(engineSpinner, lpMatch());

        deviceStatus = text(deviceSummary(), 12, false);
        deviceStatus.setPadding(0, dp(4), 0, dp(8));
        root.addView(deviceStatus);

        root.addView(sectionLabel("本機 Whisper 模型"));
        localModelSpinner = new Spinner(this);
        String[] models = {
                "自動（依手機記憶體）",
                "tiny｜約 75 MB｜最快",
                "base｜約 142 MB｜建議",
                "small｜約 466 MB｜較準但較慢"
        };
        localModelSpinner.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item, models));
        localModelSpinner.setSelection(getSharedPreferences(PREFS, MODE_PRIVATE).getInt("model", 0));
        localModelSpinner.setOnItemSelectedListener(new android.widget.AdapterView.OnItemSelectedListener() {
            @Override public void onItemSelected(android.widget.AdapterView<?> parent, View view, int position, long id) {
                getSharedPreferences(PREFS, MODE_PRIVATE).edit().putInt("model", position).apply();
                updateModelStatus();
            }
            @Override public void onNothingSelected(android.widget.AdapterView<?> parent) {}
        });
        root.addView(localModelSpinner, lpMatch());
        modelStatus = text("檢查模型中…", 12, false);
        modelStatus.setPadding(0, dp(4), 0, dp(4));
        root.addView(modelStatus);
        modelButton = button("下載／準備本機 Whisper 模型");
        modelButton.setOnClickListener(v -> downloadSelectedModel());
        root.addView(modelButton, lpMatch());
        TextView localNote = text("本機 Whisper 完全離線，免費 AAR 目前支援 arm64-v8a；App 會先用本機 FFmpeg 將 M4A／AAC／MP3／WAV／FLAC／OGG 轉成 Whisper 所需 16 kHz 單聲道 WAV，再離線辨識。", 12, false);
        localNote.setPadding(0, dp(2), 0, dp(10));
        root.addView(localNote);

        root.addView(sectionLabel("Gemini API Key（每台手機獨立）"));
        apiKey = new EditText(this);
        apiKey.setSingleLine(true);
        apiKey.setHint("先到 AI Studio 建立 Key，再貼回這裡");
        apiKey.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        apiKey.setText(SecureKeyStore.load(this));
        root.addView(apiKey, lpMatch());

        CheckBox showKey = new CheckBox(this);
        showKey.setText("顯示 API Key");
        showKey.setOnCheckedChangeListener((buttonView, checked) -> {
            apiKey.setInputType(InputType.TYPE_CLASS_TEXT | (checked ? InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD : InputType.TYPE_TEXT_VARIATION_PASSWORD));
            apiKey.setSelection(apiKey.length());
        });
        root.addView(showKey);

        Button aiStudio = button("前往 AI Studio 取得 API Key");
        aiStudio.setOnClickListener(v -> openAiStudio());
        Button pasteKey = button("從剪貼簿貼上並儲存");
        pasteKey.setOnClickListener(v -> pasteAndSaveKey());
        Button saveKey = button("儲存 Key");
        saveKey.setOnClickListener(v -> saveApiKey());
        Button testKey = button("測試 Key");
        testKey.setOnClickListener(v -> testApiKey());
        root.addView(adaptiveButtonRow(aiStudio, pasteKey));
        root.addView(adaptiveButtonRow(saveKey, testKey));
        TextView keyNote = text("基於帳號安全，App 不會也不能自動讀取你 Google 帳號裡的 API Key；按上方按鈕可一鍵前往 AI Studio 建立，每台手機貼上後會各自加密記憶。", 12, false);
        keyNote.setPadding(0, dp(2), 0, dp(8));
        root.addView(keyNote);

        root.addView(sectionLabel("轉錄方式"));
        modeSpinner = new Spinner(this);
        String[] modes = {"逐字稿 verbatim", "智慧逐字稿 smart（Gemini 閱讀優先）"};
        modeSpinner.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item, modes));
        root.addView(modeSpinner, lpMatch());

        languageSpinner = new Spinner(this);
        String[] langs = {"自動偵測語言", "繁體中文／華語・台語", "英文", "日文"};
        languageSpinner.setAdapter(new ArrayAdapter<>(this, android.R.layout.simple_spinner_dropdown_item, langs));
        root.addView(languageSpinner, lpMatch());

        diarization = new CheckBox(this);
        diarization.setText("多人講者辨識（需要 Gemini）");
        diarization.setChecked(false);
        root.addView(diarization);
        timestamps = new CheckBox(this);
        timestamps.setText("字詞級時間戳（需要 Gemini；Whisper 仍會提供段落時間）");
        timestamps.setChecked(false);
        root.addView(timestamps);

        modeSpinner.setOnItemSelectedListener(new android.widget.AdapterView.OnItemSelectedListener() {
            @Override public void onItemSelected(android.widget.AdapterView<?> parent, View view, int position, long id) {
                boolean smart = position == 1;
                diarization.setEnabled(!smart);
                timestamps.setEnabled(!smart);
                if (smart) {
                    diarization.setChecked(false);
                    timestamps.setChecked(false);
                }
            }
            @Override public void onNothingSelected(android.widget.AdapterView<?> parent) {}
        });

        startButton = button("開始自動辨識");
        cancelButton = button("取消");
        cancelButton.setEnabled(false);
        startButton.setOnClickListener(v -> startTranscription());
        cancelButton.setOnClickListener(v -> {
            cancelFlag.set(true);
            setStage("已要求取消；目前工作安全結束後停止。", true);
        });
        root.addView(adaptiveButtonRow(startButton, cancelButton));

        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        progress.setIndeterminate(true);
        progress.setVisibility(View.GONE);
        root.addView(progress, lpMatch());
        stage = text("待命", 14, true);
        stage.setPadding(0, dp(4), 0, dp(8));
        root.addView(stage);

        root.addView(sectionLabel("轉錄結果"));
        result = new EditText(this);
        result.setGravity(Gravity.TOP | Gravity.START);
        result.setTextSize(16);
        result.setMinLines(12);
        result.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE | InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
        result.setHint("完成後會顯示逐字稿，可直接修正，再另存 TXT 或 Markdown。\n\n自動混合若使用 Whisper，音訊不會上傳；若切換到 Gemini，才會上傳該音訊。 ");
        root.addView(result, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(330)));

        Button saveTxt = button("另存 TXT");
        Button saveMd = button("另存 Markdown");
        saveTxt.setOnClickListener(v -> createDocument(false));
        saveMd.setOnClickListener(v -> createDocument(true));
        root.addView(adaptiveButtonRow(saveTxt, saveMd));

        TextView limits = text("自動規則：已下載本機模型＋不要求 Gemini 專屬功能 → 優先離線 Whisper；M4A/AAC/MP3/WAV/FLAC/OGG 會在手機本機先轉成 16 kHz WAV。多人講者、字詞級時間戳、smart 模式或本機條件不足 → 使用 Gemini（需 Key）。", 12, false);
        limits.setPadding(0, dp(10), 0, 0);
        root.addView(limits);
        return scroll;
    }

    private void chooseAudio() {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("audio/*");
        startActivityForResult(intent, REQ_AUDIO);
    }

    private void openAiStudio() {
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(AI_STUDIO_URL)));
            toast("在 AI Studio 建立／複製 Key 後，回到 App 按「從剪貼簿貼上並儲存」");
        } catch (Exception e) {
            toast("無法開啟瀏覽器：" + e.getMessage());
        }
    }

    private void pasteAndSaveKey() {
        ClipboardManager cm = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
        ClipData clip = cm == null ? null : cm.getPrimaryClip();
        if (clip == null || clip.getItemCount() == 0) { toast("剪貼簿目前沒有內容"); return; }
        CharSequence cs = clip.getItemAt(0).coerceToText(this);
        String key = cs == null ? "" : cs.toString().trim();
        if (key.isEmpty()) { toast("剪貼簿內容是空的"); return; }
        apiKey.setText(key);
        saveApiKey();
    }

    private void saveApiKey() {
        try {
            SecureKeyStore.save(this, apiKey.getText().toString().trim());
            toast("API Key 已加密儲存在這一台手機");
        } catch (Exception e) {
            toast("Key 儲存失敗：" + e.getMessage());
        }
    }

    private void testApiKey() {
        String key = apiKey.getText().toString().trim();
        if (key.isEmpty()) { toast("請先輸入 API Key"); return; }
        setBusy(true);
        setStage("測試 Gemini API Key…", false);
        executor.submit(() -> {
            try {
                HttpURLConnection c = (HttpURLConnection) new URL("https://generativelanguage.googleapis.com/v1beta/models").openConnection();
                c.setRequestMethod("GET");
                c.setConnectTimeout(15_000);
                c.setReadTimeout(20_000);
                c.setRequestProperty("x-goog-api-key", key);
                int code = c.getResponseCode();
                String body = readResponse(c);
                c.disconnect();
                if (code >= 200 && code < 300) {
                    SecureKeyStore.save(this, key);
                    setStage("Gemini API Key 測試成功，已記憶在此手機。", false);
                } else {
                    setStage("API Key 測試失敗 HTTP " + code + "：" + shortText(body, 180), true);
                }
            } catch (Exception e) {
                setStage("API Key 測試失敗：" + e.getMessage(), true);
            } finally {
                setBusy(false);
            }
        });
    }

    private void createDocument(boolean markdown) {
        String text = result.getText().toString().trim();
        if (text.isEmpty()) { toast("目前沒有可儲存的逐字稿"); return; }
        lastTranscript = text;
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType(markdown ? "text/markdown" : "text/plain");
        String stem = audioName.isEmpty() ? "逐字稿" : audioName.replaceFirst("\\.[^.]+$", "");
        intent.putExtra(Intent.EXTRA_TITLE, stem + (markdown ? "_逐字稿.md" : "_逐字稿.txt"));
        startActivityForResult(intent, markdown ? REQ_SAVE_MD : REQ_SAVE_TXT);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        if (requestCode == REQ_AUDIO) {
            audioUri = uri;
            audioName = displayName(uri);
            audioLabel.setText(audioName + "\n" + safeMime(uri));
            try { getContentResolver().takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION); } catch (Exception ignored) {}
        } else if (requestCode == REQ_SAVE_TXT || requestCode == REQ_SAVE_MD) {
            boolean md = requestCode == REQ_SAVE_MD;
            String payload = md ? markdownText(result.getText().toString().trim()) : result.getText().toString().trim() + "\n";
            try (OutputStream out = getContentResolver().openOutputStream(uri, "wt")) {
                if (out == null) throw new Exception("無法開啟輸出檔");
                out.write(payload.getBytes(StandardCharsets.UTF_8));
                toast(md ? "Markdown 已儲存" : "TXT 已儲存");
            } catch (Exception e) {
                toast("儲存失敗：" + e.getMessage());
            }
        }
    }

    private void startTranscription() {
        if (audioUri == null) { toast("請先選擇錄音檔"); return; }
        getSharedPreferences(PREFS, MODE_PRIVATE).edit()
                .putInt("engine", engineSpinner.getSelectedItemPosition())
                .putInt("model", localModelSpinner.getSelectedItemPosition())
                .apply();
        String key = apiKey.getText().toString().trim();
        int engine = resolveEngine(key);
        if (engine == 0) return;
        cancelFlag.set(false);
        failoverUsed.set(false);
        setBusy(true);
        result.setText("");
        if (engine == 1) startLocalWhisper(); else startGemini(key);
    }

    // 0=不能開始，1=Whisper，2=Gemini
    private int resolveEngine(String key) {
        int choice = engineSpinner.getSelectedItemPosition();
        boolean localReady = supportsLocalWhisper() && localAudioSupported(audioName) && selectedModelFile().isFile();
        boolean geminiNeeded = modeSpinner.getSelectedItemPosition() == 1 || diarization.isChecked() || timestamps.isChecked();

        if (choice == 1) {
            if (!supportsLocalWhisper()) { setStage("此手機 ABI 不支援目前的本機 Whisper AAR；請改用自動或 Gemini。", true); return 0; }
            if (!localAudioSupported(audioName)) { setStage("目前音訊格式不在本機 Whisper 支援清單；請改用自動或 Gemini。", true); return 0; }
            if (!selectedModelFile().isFile()) { setStage("尚未下載本機 Whisper 模型，請先按「下載／準備本機 Whisper 模型」。", true); return 0; }
            if (geminiNeeded) setStage("提示：本機 Whisper 不提供多人講者／字詞級時間戳／smart；將以段落時間逐字稿執行。", true);
            return 1;
        }
        if (choice == 2) {
            if (key.isEmpty()) { setStage("Gemini 模式需要 API Key。請按「前往 AI Studio 取得 API Key」。", true); return 0; }
            return 2;
        }

        // 自動混合：Gemini 專屬功能優先；其餘優先離線 Whisper。
        if (geminiNeeded) {
            if (!key.isEmpty()) return 2;
            if (localReady) {
                setStage("沒有 Gemini Key，已自動改用本機 Whisper；多人講者／字詞級時間戳將略過。", true);
                return 1;
            }
            setStage("目前設定需要 Gemini，但尚未設定 Key；也沒有可用的本機 Whisper 模型。", true);
            return 0;
        }
        if (localReady) return 1;
        if (!key.isEmpty()) {
            if (!localAudioSupported(audioName)) setStage("目前音檔格式不在本機轉碼支援清單，已自動改用 Gemini。", false);
            else if (!selectedModelFile().isFile()) setStage("尚未下載本機 Whisper 模型，已自動改用 Gemini。", false);
            return 2;
        }
        if (!supportsLocalWhisper()) setStage("此手機本機 Whisper 不支援，且尚未設定 Gemini API Key。", true);
        else if (!localAudioSupported(audioName)) setStage("目前格式無法走本機 Whisper；請先設定 Gemini API Key。", true);
        else setStage("請先下載本機 Whisper 模型，或設定 Gemini API Key。", true);
        return 0;
    }

    private void startLocalWhisper() {
        setStage("本機 Whisper：準備音檔…", false);
        runOnUiThread(() -> progress.setIndeterminate(true));
        executor.submit(() -> {
            File temp = null;
            try {
                temp = copyUriToCache(audioUri);
                if (cancelFlag.get()) throw new InterruptedException("已取消");
                File finalTemp = temp;
                setStage("本機 Whisper 辨識中（音訊不會上傳）…", false);
                LocalWhisperEngine.transcribeAsync(this, selectedModelFile().getAbsolutePath(), temp.getAbsolutePath(), whisperLanguageCode(), new WhisperCallback() {
                    @Override public void onSuccess(String text) {
                        if (!cancelFlag.get()) {
                            lastEngineLabel = "Whisper.cpp / " + selectedModelId();
                            lastTranscript = text == null ? "" : text;
                            result.setText(lastTranscript);
                            setStage("本機 Whisper 轉錄完成", false);
                        } else {
                            setStage("已取消", true);
                        }
                        finalTemp.delete();
                        setBusy(false);
                    }
                    @Override public void onError(String message) {
                        finalTemp.delete();
                        String key = apiKey.getText().toString().trim();
                        if (!cancelFlag.get() && engineSpinner.getSelectedItemPosition() == 0 && !key.isEmpty() && failoverUsed.compareAndSet(false, true)) {
                            setStage("本機 Whisper 失敗，已自動改用 Gemini：" + message, true);
                            startGemini(key);
                        } else {
                            setStage("本機 Whisper 失敗：" + message, true);
                            setBusy(false);
                        }
                    }
                });
                temp = null; // callback 負責刪除
            } catch (InterruptedException e) {
                setStage("已取消", true);
                setBusy(false);
            } catch (Exception e) {
                setStage("本機 Whisper 失敗：" + e.getMessage(), true);
                setBusy(false);
            } finally {
                if (temp != null) temp.delete();
            }
        });
    }

    private void startGemini(String key) {
        if (key.isEmpty()) { setStage("Gemini 需要 API Key。", true); setBusy(false); return; }
        try { SecureKeyStore.save(this, key); } catch (Exception ignored) {}
        setStage("Gemini：準備音檔…", false);
        runOnUiThread(() -> progress.setIndeterminate(true));
        executor.submit(() -> {
            File temp = null;
            String uploadedName = null;
            boolean handedOff = false;
            try {
                temp = copyUriToCache(audioUri);
                if (cancelFlag.get()) throw new InterruptedException("已取消");
                String mime = safeMime(audioUri);
                setStage("上傳音訊至 Gemini…", false);
                JSONObject uploaded = uploadFile(key, temp, mime);
                JSONObject fileObj = uploaded.optJSONObject("file");
                if (fileObj == null) throw new Exception("Files API 沒有回傳 file 資訊：" + uploaded);
                String fileUri = fileObj.optString("uri", "");
                uploadedName = fileObj.optString("name", "");
                if (fileUri.isEmpty()) throw new Exception("Files API 沒有回傳 file URI");
                if (cancelFlag.get()) throw new InterruptedException("已取消");

                setStage("Gemini 3.5 Transcribe 辨識中…", false);
                JSONObject response = transcribe(key, fileUri, mime);
                lastRawResponse = response.toString(2);
                String text = extractOutputText(response).trim();
                if (text.isEmpty()) text = "【未能從回應中解析出文字】\n\n" + lastRawResponse;
                lastTranscript = text;
                lastEngineLabel = TRANSCRIBE_MODEL;
                String finalText = text;
                runOnUiThread(() -> result.setText(finalText));
                setStage("Gemini 轉錄完成", false);
            } catch (InterruptedException e) {
                setStage("已取消", true);
            } catch (Exception e) {
                boolean canFallbackLocal = !cancelFlag.get() && engineSpinner.getSelectedItemPosition() == 0 &&
                        supportsLocalWhisper() && localAudioSupported(audioName) && selectedModelFile().isFile() &&
                        failoverUsed.compareAndSet(false, true);
                if (canFallbackLocal) {
                    setStage("Gemini 失敗，已自動改用本機 Whisper（進階 Gemini 功能將略過）：" + e.getMessage(), true);
                    final boolean hadUpload = uploadedName != null && !uploadedName.isEmpty();
                    if (hadUpload) { try { deleteUploaded(key, uploadedName); } catch (Exception ignored) {} }
                    if (temp != null) temp.delete();
                    handedOff = true;
                    startLocalWhisper();
                    return;
                } else {
                    setStage("Gemini 失敗：" + e.getClass().getSimpleName() + "：" + e.getMessage(), true);
                }
            } finally {
                if (uploadedName != null && !uploadedName.isEmpty()) {
                    try { deleteUploaded(key, uploadedName); } catch (Exception ignored) {}
                }
                if (temp != null) temp.delete();
                if (!handedOff) setBusy(false);
            }
        });
    }

    private void downloadSelectedModel() {
        if (!supportsLocalWhisper()) {
            setStage("此手機不是 arm64-v8a，本版免費 Whisper AAR 無法使用。", true);
            return;
        }
        String id = selectedModelId();
        File target = modelFile(id);
        if (target.isFile() && target.length() > 20_000_000L) {
            toast("模型已存在：" + target.getName());
            updateModelStatus();
            return;
        }
        cancelFlag.set(false);
        setBusy(true);
        modelButton.setEnabled(false);
        progress.setIndeterminate(false);
        progress.setProgress(0);
        setStage("下載 Whisper " + id + " 模型…", false);
        executor.submit(() -> {
            File part = new File(target.getParentFile(), target.getName() + ".part");
            try {
                if (!target.getParentFile().exists() && !target.getParentFile().mkdirs()) throw new Exception("無法建立模型資料夾");
                URL u = new URL(MODEL_BASE_URL + target.getName());
                HttpURLConnection c = (HttpURLConnection) u.openConnection();
                c.setInstanceFollowRedirects(true);
                c.setConnectTimeout(30_000);
                c.setReadTimeout(120_000);
                int code = c.getResponseCode();
                if (code < 200 || code >= 300) throw new Exception("下載失敗 HTTP " + code);
                long total = c.getContentLengthLong();
                try (InputStream in = new BufferedInputStream(c.getInputStream()); OutputStream out = new BufferedOutputStream(new FileOutputStream(part))) {
                    byte[] buf = new byte[256 * 1024];
                    long done = 0;
                    int n;
                    while ((n = in.read(buf)) != -1) {
                        if (cancelFlag.get()) throw new InterruptedException("已取消");
                        out.write(buf, 0, n);
                        done += n;
                        if (total > 0) {
                            int pct = (int) Math.min(100, done * 100L / total);
                            runOnUiThread(() -> progress.setProgress(pct));
                        }
                    }
                } finally { c.disconnect(); }
                if (!part.renameTo(target)) {
                    try (InputStream in = new FileInputStream(part); OutputStream out = new FileOutputStream(target)) {
                        byte[] b = new byte[128 * 1024]; int n; while ((n = in.read(b)) > 0) out.write(b, 0, n);
                    }
                    part.delete();
                }
                setStage("Whisper " + id + " 模型下載完成，可離線使用。", false);
            } catch (InterruptedException e) {
                part.delete();
                setStage("模型下載已取消", true);
            } catch (Exception e) {
                part.delete();
                setStage("模型下載失敗：" + e.getMessage(), true);
            } finally {
                runOnUiThread(() -> {
                    progress.setIndeterminate(true);
                    modelButton.setEnabled(true);
                    updateModelStatus();
                });
                setBusy(false);
            }
        });
    }

    private boolean supportsLocalWhisper() {
        return Arrays.asList(Build.SUPPORTED_ABIS).contains("arm64-v8a");
    }

    private boolean localAudioSupported(String name) {
        String n = name == null ? "" : name.toLowerCase(Locale.ROOT);
        return n.endsWith(".wav") || n.endsWith(".mp3") || n.endsWith(".flac") ||
                n.endsWith(".m4a") || n.endsWith(".aac") || n.endsWith(".ogg") ||
                n.endsWith(".opus") || n.endsWith(".mp4");
    }

    private String selectedModelId() {
        int p = localModelSpinner == null ? 0 : localModelSpinner.getSelectedItemPosition();
        if (p == 1) return "tiny";
        if (p == 2) return "base";
        if (p == 3) return "small";
        return recommendedModelId();
    }

    private String recommendedModelId() {
        try {
            ActivityManager am = (ActivityManager) getSystemService(ACTIVITY_SERVICE);
            ActivityManager.MemoryInfo mi = new ActivityManager.MemoryInfo();
            am.getMemoryInfo(mi);
            long gb = mi.totalMem / (1024L * 1024L * 1024L);
            return gb < 5 ? "tiny" : "base";
        } catch (Exception e) {
            return "base";
        }
    }

    private File modelFile(String id) {
        File dir = getExternalFilesDir("models");
        if (dir == null) dir = new File(getFilesDir(), "models");
        return new File(dir, "ggml-" + id + ".bin");
    }

    private File selectedModelFile() { return modelFile(selectedModelId()); }

    private void updateModelStatus() {
        if (modelStatus == null) return;
        String id = selectedModelId();
        File f = modelFile(id);
        String state = f.isFile() ? String.format(Locale.ROOT, "已下載 %.0f MB", f.length() / 1024.0 / 1024.0) : "尚未下載";
        modelStatus.setText("自動建議：" + recommendedModelId() + "｜目前：" + id + "｜" + state);
    }

    private String deviceSummary() {
        String ram = "?";
        try {
            ActivityManager am = (ActivityManager) getSystemService(ACTIVITY_SERVICE);
            ActivityManager.MemoryInfo mi = new ActivityManager.MemoryInfo();
            am.getMemoryInfo(mi);
            ram = String.format(Locale.ROOT, "%.1f GB", mi.totalMem / 1024.0 / 1024.0 / 1024.0);
        } catch (Exception ignored) {}
        return "裝置：" + Build.MODEL + "｜RAM " + ram + "｜ABI " + String.join(",", Build.SUPPORTED_ABIS) + "｜本機 Whisper " + (supportsLocalWhisper() ? "可用" : "不可用");
    }

    private File copyUriToCache(Uri uri) throws Exception {
        String ext = ".audio";
        String n = displayName(uri);
        int dot = n.lastIndexOf('.');
        if (dot >= 0 && dot < n.length() - 1) ext = n.substring(dot);
        File dst = File.createTempFile("j_", ext, getCacheDir());
        try (InputStream in = new BufferedInputStream(getContentResolver().openInputStream(uri));
             OutputStream out = new BufferedOutputStream(new FileOutputStream(dst))) {
            if (in == null) throw new Exception("無法讀取選取的音檔");
            byte[] buf = new byte[256 * 1024];
            int r;
            while ((r = in.read(buf)) != -1) {
                if (cancelFlag.get()) throw new InterruptedException("已取消");
                out.write(buf, 0, r);
            }
        }
        return dst;
    }

    private JSONObject uploadFile(String key, File file, String mime) throws Exception {
        URL startUrl = new URL("https://generativelanguage.googleapis.com/upload/v1beta/files");
        HttpURLConnection start = (HttpURLConnection) startUrl.openConnection();
        start.setRequestMethod("POST");
        start.setConnectTimeout(30_000);
        start.setReadTimeout(60_000);
        start.setDoOutput(true);
        start.setRequestProperty("x-goog-api-key", key);
        start.setRequestProperty("X-Goog-Upload-Protocol", "resumable");
        start.setRequestProperty("X-Goog-Upload-Command", "start");
        start.setRequestProperty("X-Goog-Upload-Header-Content-Length", String.valueOf(file.length()));
        start.setRequestProperty("X-Goog-Upload-Header-Content-Type", mime);
        start.setRequestProperty("Content-Type", "application/json; charset=utf-8");
        JSONObject meta = new JSONObject().put("file", new JSONObject().put("display_name", audioName.isEmpty() ? "audio" : audioName));
        writeUtf8(start, meta.toString());
        int sc = start.getResponseCode();
        if (sc < 200 || sc >= 300) throw new Exception("建立上傳工作失敗 HTTP " + sc + "：" + readResponse(start));
        String uploadUrl = start.getHeaderField("X-Goog-Upload-URL");
        if (uploadUrl == null || uploadUrl.isEmpty()) uploadUrl = start.getHeaderField("x-goog-upload-url");
        start.disconnect();
        if (uploadUrl == null || uploadUrl.isEmpty()) throw new Exception("Gemini 未回傳 upload URL");

        HttpURLConnection up = (HttpURLConnection) new URL(uploadUrl).openConnection();
        up.setRequestMethod("POST");
        up.setConnectTimeout(30_000);
        up.setReadTimeout(180_000);
        up.setDoOutput(true);
        up.setFixedLengthStreamingMode(file.length());
        up.setRequestProperty("Content-Length", String.valueOf(file.length()));
        up.setRequestProperty("X-Goog-Upload-Offset", "0");
        up.setRequestProperty("X-Goog-Upload-Command", "upload, finalize");
        up.setRequestProperty("Content-Type", mime);
        try (InputStream in = new BufferedInputStream(new FileInputStream(file)); OutputStream out = new BufferedOutputStream(up.getOutputStream())) {
            byte[] buf = new byte[256 * 1024];
            int r;
            while ((r = in.read(buf)) != -1) {
                if (cancelFlag.get()) throw new InterruptedException("已取消");
                out.write(buf, 0, r);
            }
        }
        int code = up.getResponseCode();
        String body = readResponse(up);
        up.disconnect();
        if (code < 200 || code >= 300) throw new Exception("音訊上傳失敗 HTTP " + code + "：" + body);
        return new JSONObject(body);
    }

    private JSONObject transcribe(String key, String fileUri, String mime) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL("https://generativelanguage.googleapis.com/v1beta/interactions").openConnection();
        conn.setRequestMethod("POST");
        conn.setConnectTimeout(30_000);
        conn.setReadTimeout(240_000);
        conn.setDoOutput(true);
        conn.setRequestProperty("x-goog-api-key", key);
        conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");

        JSONObject audio = new JSONObject().put("type", "audio").put("uri", fileUri).put("mime_type", mime);
        JSONArray input = new JSONArray().put(audio);
        JSONObject tc = new JSONObject();
        if (modeSpinner.getSelectedItemPosition() == 1) {
            tc.put("mode", "smart");
        } else {
            JSONObject mode = new JSONObject().put("type", "verbatim");
            if (diarization.isChecked()) mode.put("diarization_mode", "speaker");
            if (timestamps.isChecked()) mode.put("timestamp_granularities", new JSONArray().put("word"));
            tc.put("mode", mode);
        }
        String lang = geminiLanguageCode();
        if (!lang.isEmpty()) tc.put("language_codes", new JSONArray().put(lang));

        JSONObject body = new JSONObject()
                .put("model", TRANSCRIBE_MODEL)
                .put("input", input)
                .put("generation_config", new JSONObject().put("transcription_config", tc));
        writeUtf8(conn, body.toString());
        int code = conn.getResponseCode();
        String response = readResponse(conn);
        conn.disconnect();
        if (code < 200 || code >= 300) throw new Exception("Gemini 轉錄失敗 HTTP " + code + "：" + response);
        return new JSONObject(response);
    }

    private void deleteUploaded(String key, String name) throws Exception {
        String path = name.startsWith("files/") ? name : "files/" + name;
        HttpURLConnection c = (HttpURLConnection) new URL("https://generativelanguage.googleapis.com/v1beta/" + path).openConnection();
        c.setRequestMethod("DELETE");
        c.setConnectTimeout(10_000);
        c.setReadTimeout(20_000);
        c.setRequestProperty("x-goog-api-key", key);
        try { c.getResponseCode(); } finally { c.disconnect(); }
    }

    private static void writeUtf8(HttpURLConnection c, String s) throws Exception {
        byte[] bytes = s.getBytes(StandardCharsets.UTF_8);
        c.setFixedLengthStreamingMode(bytes.length);
        try (OutputStream out = c.getOutputStream()) { out.write(bytes); }
    }

    private static String readResponse(HttpURLConnection c) throws Exception {
        InputStream in = c.getResponseCode() >= 400 ? c.getErrorStream() : c.getInputStream();
        if (in == null) return "";
        StringBuilder b = new StringBuilder();
        try (BufferedReader r = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))) {
            String line;
            while ((line = r.readLine()) != null) b.append(line).append('\n');
        }
        return b.toString().trim();
    }

    private String extractOutputText(JSONObject root) {
        String v = root.optString("output_text", "");
        if (!v.isEmpty()) return v;
        JSONArray outputs = root.optJSONArray("outputs");
        if (outputs != null) {
            StringBuilder b = new StringBuilder();
            for (int i = 0; i < outputs.length(); i++) {
                JSONObject o = outputs.optJSONObject(i);
                if (o != null && "text".equalsIgnoreCase(o.optString("type"))) {
                    String t = o.optString("text", "");
                    if (!t.isEmpty()) b.append(t).append('\n');
                }
            }
            if (b.length() > 0) return b.toString();
        }
        JSONArray steps = root.optJSONArray("steps");
        if (steps != null) {
            for (int i = steps.length() - 1; i >= 0; i--) {
                JSONObject step = steps.optJSONObject(i);
                if (step == null) continue;
                JSONArray content = step.optJSONArray("content");
                if (content == null) continue;
                StringBuilder b = new StringBuilder();
                for (int j = 0; j < content.length(); j++) {
                    JSONObject part = content.optJSONObject(j);
                    if (part != null) {
                        String t = part.optString("text", "");
                        if (!t.isEmpty()) b.append(t).append('\n');
                    }
                }
                if (b.length() > 0) return b.toString();
            }
        }
        return findTextRecursive(root, 0);
    }

    private String findTextRecursive(Object obj, int depth) {
        if (obj == null || depth > 8) return "";
        if (obj instanceof JSONObject) {
            JSONObject o = (JSONObject) obj;
            String direct = o.optString("text", "");
            if (!direct.isEmpty()) return direct;
            JSONArray names = o.names();
            if (names != null) {
                for (int i = 0; i < names.length(); i++) {
                    String key = names.optString(i);
                    String found = findTextRecursive(o.opt(key), depth + 1);
                    if (!found.isEmpty()) return found;
                }
            }
        } else if (obj instanceof JSONArray) {
            JSONArray a = (JSONArray) obj;
            for (int i = 0; i < a.length(); i++) {
                String found = findTextRecursive(a.opt(i), depth + 1);
                if (!found.isEmpty()) return found;
            }
        }
        return "";
    }

    private String markdownText(String transcript) {
        String stem = audioName.isEmpty() ? "逐字稿" : audioName.replaceFirst("\\.[^.]+$", "");
        return "# " + stem + "_逐字稿\n\n" +
                "- 來源音檔：" + (audioName.isEmpty() ? "—" : audioName) + "\n" +
                "- 辨識引擎：" + lastEngineLabel + "\n\n" +
                "## 逐字稿\n\n" + transcript + "\n";
    }

    private String geminiLanguageCode() {
        switch (languageSpinner.getSelectedItemPosition()) {
            case 1: return "zh-TW";
            case 2: return "en-US";
            case 3: return "ja-JP";
            default: return "";
        }
    }

    private String whisperLanguageCode() {
        switch (languageSpinner.getSelectedItemPosition()) {
            case 1: return "zh";
            case 2: return "en";
            case 3: return "ja";
            default: return "";
        }
    }

    private String safeMime(Uri uri) {
        String m = getContentResolver().getType(uri);
        if (m == null || m.trim().isEmpty()) {
            String n = displayName(uri).toLowerCase(Locale.ROOT);
            if (n.endsWith(".m4a")) return "audio/mp4";
            if (n.endsWith(".mp3")) return "audio/mpeg";
            if (n.endsWith(".wav")) return "audio/wav";
            if (n.endsWith(".aac")) return "audio/aac";
            if (n.endsWith(".flac")) return "audio/flac";
            if (n.endsWith(".ogg")) return "audio/ogg";
            return "audio/mp4";
        }
        return m;
    }

    private String displayName(Uri uri) {
        String name = "audio";
        try (android.database.Cursor c = getContentResolver().query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (c != null && c.moveToFirst()) {
                int idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (idx >= 0) name = c.getString(idx);
            }
        } catch (Exception ignored) {}
        return name == null ? "audio" : name;
    }

    private void setBusy(boolean busy) {
        runOnUiThread(() -> {
            progress.setVisibility(busy ? View.VISIBLE : View.GONE);
            startButton.setEnabled(!busy);
            cancelButton.setEnabled(busy);
            modelButton.setEnabled(!busy);
        });
    }

    private void setStage(String message, boolean warn) {
        runOnUiThread(() -> {
            stage.setText(message);
            stage.setTextColor(warn ? 0xFFB71C1C : 0xFF1565C0);
        });
    }

    private TextView sectionLabel(String s) {
        TextView t = text(s, 16, true);
        t.setPadding(0, dp(10), 0, dp(4));
        return t;
    }

    private TextView text(String s, int sp, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(0xFF17202A);
        if (bold) t.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        return t;
    }

    private Button button(String s) {
        Button b = new Button(this);
        b.setText(s);
        b.setAllCaps(false);
        b.setMinHeight(dp(46));
        return b;
    }

    private View adaptiveButtonRow(Button... buttons) {
        boolean narrow = getResources().getConfiguration().screenWidthDp < 600;
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(narrow ? LinearLayout.VERTICAL : LinearLayout.HORIZONTAL);
        row.setPadding(0, dp(4), 0, dp(2));
        for (Button b : buttons) row.addView(b, narrow ? lpMatch() : lpWeight());
        return row;
    }

    private LinearLayout.LayoutParams lpMatch() {
        return new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private LinearLayout.LayoutParams lpWeight() {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        p.setMargins(dp(3), 0, dp(3), 0);
        return p;
    }

    private int dp(int x) { return Math.round(x * getResources().getDisplayMetrics().density); }
    private void toast(String s) { runOnUiThread(() -> Toast.makeText(this, s, Toast.LENGTH_SHORT).show()); }
    private static String shortText(String s, int max) { return s == null ? "" : (s.length() <= max ? s : s.substring(0, max) + "…"); }

    @Override
    protected void onDestroy() {
        cancelFlag.set(true);
        executor.shutdownNow();
        super.onDestroy();
    }
}
