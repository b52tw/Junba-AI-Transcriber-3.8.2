from __future__ import annotations
import math
import os
import struct
import tempfile
import wave
from pathlib import Path
from PySide6.QtCore import Qt, Signal, QThread, QUrl, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QFileDialog, QComboBox, QSpinBox, QCheckBox, QLineEdit, QProgressBar,
    QMessageBox, QGroupBox, QFormLayout, QTabWidget, QAbstractItemView, QDialog,
    QDialogButtonBox, QPlainTextEdit, QScrollArea, QSizePolicy, QFrame, QGridLayout
)
from app.core.settings import settings, load_api_key, save_api_key
from app.core.worker import TranscribeWorker
from app.core.audio_tools import (
    merge_audio, audio_duration_seconds, quick_duration_seconds, ensure_split_audio, split_output_dir
)
from app.core.diagnostics import environment_report
from app.core.hardware import (
    accelerator_options, hardware_summary, recommended_model, adaptive_plan, clear_hardware_profile
)

AUDIO_EXTS = {'.m4a','.mp3','.wav','.aac','.flac','.ogg','.mp4','.webm','.aiff','.opus'}
API_KEY_URL = 'https://aistudio.google.com/app/apikey'


class AudioListWidget(QListWidget):
    filesDropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setToolTip('可一次多選，也可從檔案總管拖曳多個音檔到這裡。')

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
        else: super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
        else: super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            paths=[]
            for u in event.mimeData().urls():
                p=u.toLocalFile()
                if p and Path(p).suffix.lower() in AUDIO_EXTS: paths.append(p)
            if paths:
                self.filesDropped.emit(paths); event.acceptProposedAction(); return
        super().dropEvent(event)


class ApiKeyTestWorker(QThread):
    done = Signal(bool, str)
    def __init__(self, key: str):
        super().__init__(); self.key = key

    def run(self):
        uploaded = None
        try:
            from google import genai
            client = genai.Client(api_key=self.key)
            # Real end-to-end smoke test: upload a tiny ASCII-named WAV and invoke
            # gemini-3.5-transcribe. This catches failures that a /models check misses.
            with tempfile.TemporaryDirectory(prefix='junba_api_test_') as td:
                wav_path = Path(td) / 'junba_api_test.wav'
                rate = 16000
                frames = bytearray()
                # Quiet 440 Hz tone; no personal/user audio is used for the test.
                for i in range(rate // 2):
                    sample = int(600 * math.sin(2 * math.pi * 440 * i / rate))
                    frames.extend(struct.pack('<h', sample))
                with wave.open(str(wav_path), 'wb') as wf:
                    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate); wf.writeframes(bytes(frames))
                uploaded = client.files.upload(file=str(wav_path))
                client.interactions.create(
                    model='gemini-3.5-transcribe',
                    input=[{'type':'audio','uri':uploaded.uri,'mime_type':uploaded.mime_type or 'audio/wav'}],
                )
            self.done.emit(True, 'API Key、音訊上傳與 gemini-3.5-transcribe 測試成功。')
        except Exception as e:
            self.done.emit(False, f'Gemini 完整測試失敗：{type(e).__name__}: {e}')
        finally:
            try:
                if uploaded is not None and getattr(uploaded, 'name', None):
                    client.files.delete(name=uploaded.name)
            except Exception:
                pass


class DiagnosticsWorker(QThread):
    done = Signal(bool, str)
    def __init__(self, output_dir: str, model_dir: str):
        super().__init__(); self.output_dir=output_dir; self.model_dir=model_dir
    def run(self):
        self.done.emit(*environment_report(self.output_dir, self.model_dir))


class SplitOnlyWorker(QThread):
    progress = Signal(int)
    stage = Signal(str)
    log = Signal(str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, files: list[str], output_dir: str, minutes: int):
        super().__init__(); self.files=files; self.output_dir=output_dir; self.minutes=minutes

    def run(self):
        try:
            if self.minutes <= 0:
                raise ValueError('「只切割音檔」需要先設定大於 0 的切割分鐘數。')
            roots=[]; n=max(1,len(self.files))
            for i, src in enumerate(self.files):
                dur=audio_duration_seconds(src)
                if dur > 0 and dur <= self.minutes*60:
                    self.log.emit(f'{Path(src).name} 未超過 {self.minutes} 分鐘，不需切割。')
                    self.progress.emit(int((i+1)/n*100)); continue
                out=split_output_dir(self.output_dir, src, self.minutes)
                self.stage.emit(f'切割 {Path(src).name}')
                chunks=ensure_split_audio(
                    src, out, self.minutes,
                    progress_cb=lambda p,_i=i: self.progress.emit(int(((_i+p/100)/n)*100)),
                    event_cb=self.log.emit,
                )
                roots.append(out)
                self.log.emit(f'切割完成：{len(chunks)} 段｜{out}')
            self.progress.emit(100)
            self.done.emit('\n'.join(roots) if roots else str(Path(self.output_dir)/'切割音檔'))
        except Exception as e:
            self.failed.emit(f'{type(e).__name__}: {e}')


class SplitChoiceDialog(QDialog):
    def __init__(self, count: int, current: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle('確認音檔切割')
        root=QVBoxLayout(self)
        root.addWidget(QLabel(f'已加入 {count} 個音檔。辨識前是否先切割？'))
        self.choice=QComboBox(); self.choice.addItem('不切割',0)
        for m in (2,5,10,15,30,60): self.choice.addItem(f'每 {m} 分鐘切一段',m)
        self.choice.addItem('自訂分鐘數',-1)
        self.custom=QSpinBox(); self.custom.setRange(1,180); self.custom.setValue(current if current>0 else 10); self.custom.setSuffix(' 分鐘')
        row=QHBoxLayout(); row.addWidget(self.choice,1); row.addWidget(self.custom); root.addLayout(row)
        note=QLabel('v3.8 會優先採「無重編碼快速切割」，失敗才改用相容 WAV；再開始載入 Whisper 或上傳 Gemini。切割檔會保存在輸出位置\\切割音檔，可供其他辨識軟體使用。')
        note.setWordWrap(True); root.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); root.addWidget(buttons)
        idx=self.choice.findData(current)
        if idx>=0:self.choice.setCurrentIndex(idx)
        self.choice.currentIndexChanged.connect(self._sync); self._sync()
    def _sync(self): self.custom.setEnabled(self.choice.currentData()==-1)
    def minutes(self): return self.custom.value() if self.choice.currentData()==-1 else int(self.choice.currentData())


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('峻爸 AI Transcriber v3.8.1')
        self.resize(1280,900)
        self.setMinimumSize(960,700)
        self.worker=None; self.split_worker=None; self.api_test_worker=None; self.diag_worker=None; self._error_dialog_open=False
        self.qs=settings()
        tabs=QTabWidget(); tabs.addTab(self._build_workspace(),'工作區'); tabs.addTab(self._build_settings(),'設定')
        self.setCentralWidget(tabs); self.statusBar().showMessage('就緒'); self._update_mode_ui(); self._apply_profile(initial=True)
        self.activity_timer = QTimer(self)
        self.activity_timer.setInterval(1000)
        self.activity_timer.timeout.connect(self._refresh_activity_monitor)
        self.activity_timer.start()
        self._refresh_activity_monitor()

    def _build_workspace(self):
        content=QWidget(); root=QVBoxLayout(content)
        root.setSpacing(8); root.setContentsMargins(10,10,10,10)
        title=QLabel('峻爸 AI Transcriber v3.8.1｜最佳化優先 × 自適應硬體 × Whisper × Gemini')
        title.setStyleSheet('font-size:20px;font-weight:700;padding:6px;'); root.addWidget(title)
        hint=QLabel('預設使用「最佳化」：先檢查音檔、必要時相容轉換、自動切段、選擇較穩定的硬體與模型；需要時再切到進階自訂。')
        hint.setStyleSheet('color:#b8c7d9;'); root.addWidget(hint)
        self.files=AudioListWidget(); self.files.filesDropped.connect(self._add_paths)
        self.files.setMinimumHeight(105); self.files.setMaximumHeight(165)
        root.addWidget(self.files)
        row=QHBoxLayout()
        for text,fn in [('加入音檔',self.add_files),('移除選取',self.remove_selected),('清空',self.files.clear),('合併音檔',self.merge_selected)]:
            b=QPushButton(text); b.clicked.connect(fn); row.addWidget(b)
        root.addLayout(row)

        box=QGroupBox('工作流程'); box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum); box.setMinimumHeight(350)
        form=QFormLayout(box); form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow); form.setRowWrapPolicy(QFormLayout.WrapLongRows); form.setVerticalSpacing(7); form.setHorizontalSpacing(10)
        self.profile=QComboBox()
        self.profile.addItem('最佳化（推薦｜自動選模式、模型、硬體與切段）','optimized')
        self.profile.addItem('完全離線穩定','offline')
        self.profile.addItem('線上 Gemini 快速','google')
        self.profile.addItem('進階自訂','custom')
        self.profile.currentIndexChanged.connect(lambda _=0:self._apply_profile())
        self.optimize=QCheckBox('最佳化自動處理（相容檢查／自動切段／自適應硬體）'); self.optimize.setChecked(True)
        self.mode=QComboBox(); self.mode.addItems(['離線 Whisper','Google Gemini','混合模式']); self.mode.currentTextChanged.connect(self._update_mode_ui)
        self.model=QComboBox(); self.model.addItem('自動（依硬體／記憶體）','auto')
        for _m in ('large-v3','large-v3-turbo','medium','small','base'): self.model.addItem(_m,_m)
        self.local_model=QLineEdit(self.qs.value('local_model_dir',''))
        modelrow=QHBoxLayout(); modelrow.addWidget(self.local_model,1); mb=QPushButton('本機模型…'); mb.clicked.connect(self.choose_model_dir); modelrow.addWidget(mb)
        self.accel=QComboBox()
        self.accel.setToolTip('v3.8 自適應模式會依本機硬體、成功/失敗與實測 RTF 速度紀錄自動選擇；失敗會降級並記住結果。')
        self.hw_refresh=QPushButton('重新偵測硬體'); self.hw_refresh.clicked.connect(self.refresh_hardware)
        hwrow=QHBoxLayout(); hwrow.addWidget(self.accel,1); hwrow.addWidget(self.hw_refresh)
        self.hw_status=QLabel('正在偵測硬體…'); self.hw_status.setWordWrap(True); self.hw_status.setStyleSheet('color:#b8c7d9;')
        self.hw_note=QLabel('自適應硬體會依本機能力選擇 CUDA／Intel GPU／NPU／CPU；若加速器失敗，會在同一區段自動降級並記住較穩定路徑。完整硬體資訊可到「設定」查看。')
        self.hw_note.setWordWrap(True); self.hw_note.setStyleSheet('color:#b8c7d9;')
        self.refresh_hardware(initial=True)
        self.language=QComboBox(); self.language.addItem('自動偵測（輸出仍可轉台灣繁體）','auto'); self.language.addItem('繁體中文／華語・台語混合（台灣）','zh'); self.language.addItem('英文','en'); self.language.addItem('日文','ja')
        self.split=QSpinBox(); self.split.setRange(0,180); self.split.setValue(0); self.split.setSuffix(' 分鐘（最佳化時 0=自動）')
        splitrow=QHBoxLayout(); splitrow.addWidget(self.split,1)
        self.split_only_btn=QPushButton('只切割音檔'); self.split_only_btn.clicked.connect(self.split_only); splitrow.addWidget(self.split_only_btn)
        open_split=QPushButton('開啟切割資料夾'); open_split.clicked.connect(self.open_split_folder); splitrow.addWidget(open_split)
        self.diar=QCheckBox('多人講者（Gemini 音訊模式）'); self.diar.setChecked(True)
        self.timestamps=QCheckBox('字詞時間戳'); self.timestamps.setChecked(True)
        self.smart=QCheckBox('Gemini 智慧逐字稿（與多人講者／時間戳擇一）')
        self.traditional=QCheckBox('繁體中文（台灣用字）輸出'); self.traditional.setChecked(True)
        self.review=QCheckBox('錄音核對播放器（KTV 同步＋可匯入 TXT/MD/SRT/VTT）'); self.review.setChecked(True)
        self.diar.toggled.connect(lambda checked:self._sync_gemini_features('diar', checked))
        self.timestamps.toggled.connect(lambda checked:self._sync_gemini_features('timestamps', checked))
        self.smart.toggled.connect(lambda checked:self._sync_gemini_features('smart', checked))
        flags_widget=QWidget(); flags=QGridLayout(flags_widget); flags.setContentsMargins(0,0,0,0); flags.setHorizontalSpacing(18); flags.setVerticalSpacing(4)
        flags.addWidget(self.diar,0,0); flags.addWidget(self.timestamps,0,1); flags.addWidget(self.smart,1,0); flags.addWidget(self.traditional,1,1); flags.addWidget(self.review,2,0,1,2)
        self.privacy=QLabel(''); self.privacy.setWordWrap(True)
        splitnote=QLabel('最佳化開啟時，0 分鐘代表「自動切段」：舊款 Intel Iris/UHD、CPU 與長錄音會用較短區段，降低卡在 35% 的風險；手動分鐘數仍會優先。')
        splitnote.setWordWrap(True); splitnote.setStyleSheet('color:#b8c7d9;')
        form.addRow('使用方式',self.profile); form.addRow('',self.optimize); form.addRow('辨識引擎',self.mode); form.addRow('Whisper 模型',self.model); form.addRow('本機模型資料夾',modelrow); form.addRow('硬體加速',hwrow); form.addRow('',self.hw_status); form.addRow('',self.hw_note); form.addRow('語言',self.language); form.addRow('切割',splitrow); form.addRow('',splitnote); form.addRow('功能',flags_widget); form.addRow('',self.privacy)
        root.addWidget(box)

        outrow=QHBoxLayout(); self.output=QLineEdit(str(Path.home()/'Documents'/'JunbaTranscripts')); ob=QPushButton('選擇輸出位置'); ob.clicked.connect(self.choose_output); open_out=QPushButton('開啟輸出資料夾'); open_out.clicked.connect(self.open_output_folder); self.open_review_btn=QPushButton('開啟／選擇核對播放器'); self.open_review_btn.setEnabled(True); self.open_review_btn.clicked.connect(self.open_latest_review); outrow.addWidget(self.output,1); outrow.addWidget(ob); outrow.addWidget(open_out); outrow.addWidget(self.open_review_btn); root.addLayout(outrow)
        self.latest_review_path=''
        fmts=QHBoxLayout(); self.f_docx=QCheckBox('Word'); self.f_docx.setChecked(True); self.f_txt=QCheckBox('TXT'); self.f_txt.setChecked(True); self.f_md=QCheckBox('Markdown'); self.f_srt=QCheckBox('SRT'); self.f_srt.setChecked(True); self.f_vtt=QCheckBox('VTT')
        for x in (self.f_docx,self.f_txt,self.f_md,self.f_srt,self.f_vtt): fmts.addWidget(x)
        fmts.addStretch(); root.addLayout(fmts)

        self.stage_label=QLabel('目前階段：待命'); root.addWidget(self.stage_label)
        self.stage_progress=QProgressBar(); self.stage_progress.setRange(0,100); self.stage_progress.setValue(0); self.stage_progress.setFormat('目前階段 %p%')
        self.overall_progress=QProgressBar(); self.overall_progress.setRange(0,100); self.overall_progress.setValue(0); self.overall_progress.setFormat('整體進度 %p%')
        root.addWidget(self.stage_progress); root.addWidget(self.overall_progress)

        monitor=QGroupBox('執行監看｜v3.8.1'); monitor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum); monitor.setMinimumHeight(145)
        ml=QVBoxLayout(monitor)
        self.activity_state=QLabel('● 待命')
        self.activity_state.setStyleSheet('font-weight:700;color:#9aa0a6;')
        self.activity_detail=QLabel('實際裝置：—｜目前檔案：—｜區段：—')
        self.activity_detail.setWordWrap(True)
        self.activity_time=QLabel('總耗時：00:00｜本階段：00:00｜最後程式心跳：—')
        self.activity_time.setWordWrap(True)
        self.activity_hint=QLabel('開始後會每秒更新。OpenVINO 推論期間即使百分比暫時不動，也會以心跳文字顯示已運算多久。')
        self.activity_hint.setWordWrap(True); self.activity_hint.setStyleSheet('color:#b8c7d9;')
        ml.addWidget(self.activity_state); ml.addWidget(self.activity_detail); ml.addWidget(self.activity_time); ml.addWidget(self.activity_hint)
        root.addWidget(monitor)

        self.checkpoint_label=QLabel('進度快取：尚未建立'); self.checkpoint_label.setWordWrap(True); root.addWidget(self.checkpoint_label)
        controls=QHBoxLayout(); self.start_btn=QPushButton('▶ 開始'); self.pause_btn=QPushButton('⏸ 暫停'); self.resume_btn=QPushButton('▶ 繼續'); self.stop_btn=QPushButton('■ 立即停止並輸出目前結果')
        self.start_btn.clicked.connect(self.start); self.pause_btn.clicked.connect(self.pause); self.resume_btn.clicked.connect(self.resume); self.stop_btn.clicked.connect(self.stop)
        for b in (self.start_btn,self.pause_btn,self.resume_btn,self.stop_btn): controls.addWidget(b)
        root.addLayout(controls)
        self.logbox=QPlainTextEdit(); self.logbox.setReadOnly(True); self.logbox.setMaximumBlockCount(800); self.logbox.setMinimumHeight(115); self.logbox.setMaximumHeight(190); self.logbox.setPlaceholderText('切割、上傳、轉錄、雲端重試及錯誤都會顯示在這裡。')
        root.addWidget(self.logbox)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame); scroll.setWidget(content)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded); scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        return scroll

    def _build_settings(self):
        w=QWidget(); root=QVBoxLayout(w)
        g=QGroupBox('Google AI Studio / Gemini API'); f=QFormLayout(g)
        self.api_key=QLineEdit(load_api_key()); self.api_key.setEchoMode(QLineEdit.Password)
        show=QCheckBox('顯示 API Key'); show.toggled.connect(lambda on:self.api_key.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        save=QPushButton('儲存 API Key'); save.clicked.connect(self.save_key)
        getkey=QPushButton('前往取得 API Key'); getkey.clicked.connect(lambda:QDesktopServices.openUrl(QUrl(API_KEY_URL)))
        self.testkey=QPushButton('完整測試 API Key（含音訊上傳）'); self.testkey.clicked.connect(self.test_api_key)
        keybuttons=QHBoxLayout(); keybuttons.addWidget(save); keybuttons.addWidget(getkey); keybuttons.addWidget(self.testkey)
        self.api_status=QLabel('尚未測試'); self.api_status.setWordWrap(True)
        f.addRow('Gemini API Key',self.api_key); f.addRow('',show); f.addRow('',keybuttons); f.addRow('連線狀態',self.api_status); root.addWidget(g)
        dg=QGroupBox('程式環境檢查'); dl=QVBoxLayout(dg)
        self.diag_btn=QPushButton('執行環境檢查'); self.diag_btn.clicked.connect(self.run_diagnostics)
        self.diag_text=QPlainTextEdit(); self.diag_text.setReadOnly(True); self.diag_text.setMaximumHeight(240)
        dl.addWidget(self.diag_btn); dl.addWidget(self.diag_text); root.addWidget(dg)
        hg=QGroupBox('硬體自適應設定'); hl=QVBoxLayout(hg)
        self.hw_profile_text=QLabel(hardware_summary()); self.hw_profile_text.setWordWrap(True)
        clear_hw=QPushButton('清除本機硬體學習紀錄'); clear_hw.clicked.connect(self.clear_hw_profile)
        hl.addWidget(self.hw_profile_text); hl.addWidget(clear_hw); root.addWidget(hg)
        info=QLabel('v3.8 最佳化自適應版：同一份程式可在不同 Windows 電腦依硬體自適應。自動模式會辨識 NVIDIA CUDA、Intel OpenVINO GPU/NPU 與 Intel/AMD CPU，並使用本機成功／失敗紀錄調整優先順序；加速器失敗時同一區段會安全降級，不必重新選檔。\nWhisper 模型可選「自動」，依可用硬體與記憶體保守選擇 large-v3 / large-v3-turbo / medium / small。\n執行監看持續顯示程式心跳、實際裝置、總耗時、本階段耗時與目前檔案／區段。\nGemini 多人講者與字詞時間戳可同時使用；智慧逐字稿與這兩項互斥。中文／華台混合輸出可轉為台灣繁體中文。\n介面作者：峻爸。內部程式識別、快取與 GitHub/EXE 檔名仍保留 Junba 英文名稱，以相容舊版設定。')
        info.setWordWrap(True); root.addWidget(info); root.addStretch(); return w

    def _apply_profile(self, initial=False):
        if not hasattr(self, 'profile'):
            return
        profile = self.profile.currentData() or 'optimized'
        custom = profile == 'custom'
        if profile == 'optimized':
            has_key = bool(getattr(self, 'api_key', None) and self.api_key.text().strip())
            self.mode.setCurrentText('混合模式' if has_key else '離線 Whisper')
            self.model.setCurrentIndex(max(0, self.model.findData('auto')))
            idx=self.accel.findData('adaptive'); self.accel.setCurrentIndex(max(0,idx))
            self.optimize.setChecked(True); self.split.setValue(0); self.traditional.setChecked(True); self.review.setChecked(True)
        elif profile == 'offline':
            self.mode.setCurrentText('離線 Whisper'); self.model.setCurrentIndex(max(0,self.model.findData('auto')))
            idx=self.accel.findData('adaptive'); self.accel.setCurrentIndex(max(0,idx)); self.optimize.setChecked(True); self.split.setValue(0); self.review.setChecked(True)
        elif profile == 'google':
            self.mode.setCurrentText('Google Gemini'); self.optimize.setChecked(True); self.split.setValue(0); self.review.setChecked(True)
        # In one-click profiles the technical controls remain visible but locked,
        # so non-designers can see what the program chose without accidentally
        # breaking the optimized route. "進階自訂" unlocks them.
        for w in (self.mode, self.model, self.accel, self.local_model, self.hw_refresh, self.split):
            w.setEnabled(custom)
        self.optimize.setEnabled(custom)
        if profile == 'google':
            self.model.setEnabled(False); self.accel.setEnabled(False); self.local_model.setEnabled(False); self.hw_refresh.setEnabled(False)
        self._update_mode_ui()
        if not initial:
            self.statusBar().showMessage('已套用：'+self.profile.currentText(), 5000)

    def open_latest_review(self):
        output_root = Path(self.output.text()).expanduser()
        latest = Path(self.latest_review_path) if self.latest_review_path else None

        if (not latest or not latest.exists()) and output_root.exists():
            try:
                found = sorted(
                    output_root.rglob('*_錄音核對.html'),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                latest = found[0] if found else None
            except Exception:
                latest = None

        if latest and latest.exists():
            box = QMessageBox(self)
            box.setWindowTitle('錄音核對播放器')
            box.setText(f'最近的核對播放器：\n{latest.name}')
            open_latest = box.addButton('開啟最近', QMessageBox.AcceptRole)
            choose_other = box.addButton('選擇其他檔案…', QMessageBox.ActionRole)
            box.addButton('取消', QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is open_latest:
                self.latest_review_path = str(latest)
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(latest)))
                return
            if box.clickedButton() is not choose_other:
                return

        start_dir = str(
            latest.parent if latest and latest.exists()
            else (output_root if output_root.exists() else Path.home())
        )
        selected, _ = QFileDialog.getOpenFileName(
            self,
            '選擇錄音核對播放器',
            start_dir,
            '核對播放器 (*.html *.htm);;所有檔案 (*.*)',
        )
        if selected:
            self.latest_review_path = selected
            QDesktopServices.openUrl(QUrl.fromLocalFile(selected))

    def refresh_hardware(self, initial=False):
        try:
            previous = self.qs.value('acceleration','adaptive') if initial else (self.accel.currentData() or 'adaptive')
            self.accel.blockSignals(True)
            self.accel.clear()
            opts = accelerator_options()
            for o in opts:
                label = o.label if o.available else o.label + '（目前不可用）'
                self.accel.addItem(label, o.key)
                idx = self.accel.count()-1
                self.accel.setItemData(idx, bool(o.available), Qt.UserRole+1)
                self.accel.setItemData(idx, o.detail, Qt.ToolTipRole)
            idx = self.accel.findData(previous)
            if idx < 0 or self.accel.itemData(idx, Qt.UserRole+1) is False:
                idx = self.accel.findData('adaptive')
            self.accel.setCurrentIndex(max(0, idx))
            self.accel.blockSignals(False)
            summary = hardware_summary()
            self.hw_status.setText(summary.replace('\n','｜'))
            if hasattr(self, 'hw_profile_text'):
                self.hw_profile_text.setText(summary)
            if self.model.currentData() == 'auto':
                self.model.setToolTip('目前自動推薦：' + recommended_model(self.accel.currentData() or 'adaptive'))
            if not initial:
                self.statusBar().showMessage('硬體偵測已更新；自適應路徑：' + ' → '.join(adaptive_plan('adaptive')), 5000)
        except Exception as e:
            if hasattr(self, 'hw_status'):
                self.hw_status.setText(f'硬體偵測失敗：{type(e).__name__}: {e}')

    def clear_hw_profile(self):
        clear_hardware_profile()
        self.refresh_hardware(initial=False)
        QMessageBox.information(self, '已清除', '已清除這台電腦的硬體成功／失敗學習紀錄。下次會重新自適應。')

    def _all_files(self): return [self.files.item(i).data(Qt.UserRole) or self.files.item(i).text() for i in range(self.files.count())]
    def add_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,'選擇音檔','','音訊/影片 (*.m4a *.mp3 *.wav *.aac *.flac *.ogg *.aiff *.opus *.mp4 *.webm);;所有檔案 (*.*)'); self._add_paths(paths)
    def _add_paths(self,paths):
        existing=set(self._all_files()); added=[]
        for p in paths:
            p=str(Path(p))
            if p not in existing and Path(p).suffix.lower() in AUDIO_EXTS:
                dur=quick_duration_seconds(p)
                suffix=f'[{dur/60:.1f} 分]' if dur>0 else '[開始時自動檢查]'
                label=f'{p}   {suffix}'
                item=QListWidgetItem(label); item.setData(Qt.UserRole,p); self.files.addItem(item); existing.add(p); added.append(p)
        if added:
            self._append_log(f'已加入 {len(added)} 個音檔。')
            if self.optimize.isChecked():
                self._append_log('最佳化已啟用：程式會依音檔長度與此電腦硬體自動決定是否切割；不需要手動設定。')
            else:
                dlg=SplitChoiceDialog(len(added),self.split.value(),self)
                if dlg.exec()==QDialog.Accepted:
                    self.split.setValue(dlg.minutes()); self._append_log('切割設定：'+('不切割' if dlg.minutes()==0 else f'每 {dlg.minutes()} 分鐘；辨識前先切割'))
    def remove_selected(self):
        for item in self.files.selectedItems(): self.files.takeItem(self.files.row(item))
    def merge_selected(self):
        selected=[x.data(Qt.UserRole) or x.text() for x in self.files.selectedItems()]; paths=selected if len(selected)>=2 else self._all_files()
        if len(paths)<2: QMessageBox.information(self,'提示','至少加入兩個音檔才能合併。'); return
        out,_=QFileDialog.getSaveFileName(self,'合併輸出',str(Path(self.output.text())/'合併音檔.m4a'),'M4A (*.m4a)')
        if not out:return
        try:
            self.stage_label.setText('目前階段：合併音檔'); merge_audio(paths,out,progress_cb=self._set_stage_progress); QMessageBox.information(self,'完成',f'已合併：\n{out}')
        except Exception as e: QMessageBox.critical(self,'合併失敗',str(e))

    def split_only(self):
        if self.worker and self.worker.isRunning(): QMessageBox.warning(self,'忙碌中','目前正在辨識，請先完成或停止。'); return
        files=self._all_files()
        if not files: QMessageBox.warning(self,'缺少音檔','請先加入音檔。'); return
        if self.split.value()<=0: QMessageBox.warning(self,'未設定切割','請將切割分鐘數設為 1 以上。'); return
        Path(self.output.text()).mkdir(parents=True,exist_ok=True)
        self.split_only_btn.setEnabled(False); self.stage_label.setText('目前階段：只切割音檔'); self.overall_progress.setValue(0)
        self.split_worker=SplitOnlyWorker(files,self.output.text(),self.split.value())
        self.split_worker.progress.connect(self.overall_progress.setValue); self.split_worker.stage.connect(lambda t:self.stage_label.setText('目前階段：'+t)); self.split_worker.log.connect(self._append_log); self.split_worker.done.connect(self._split_done); self.split_worker.failed.connect(self._split_failed); self.split_worker.finished.connect(lambda:self.split_only_btn.setEnabled(True)); self.split_worker.start()
    def _split_done(self,path):
        self._append_log('音檔切割工作完成。'); QMessageBox.information(self,'切割完成',f'切割檔已保存於：\n{path}\n\n之後按「開始」會直接重用相同切割結果，不會再重切。')
    def _split_failed(self,text): self._append_log('切割錯誤：'+text); QMessageBox.critical(self,'切割失敗',text)
    def open_split_folder(self):
        p=Path(self.output.text())/'切割音檔'; p.mkdir(parents=True,exist_ok=True); QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def choose_model_dir(self):
        p=QFileDialog.getExistingDirectory(self,'選擇 faster-whisper 本機模型資料夾',self.local_model.text() or str(Path.cwd()/'models'))
        if p:self.local_model.setText(p); self.qs.setValue('local_model_dir',p)
    def choose_output(self):
        p=QFileDialog.getExistingDirectory(self,'選擇輸出資料夾',self.output.text())
        if p:self.output.setText(p)

    def open_output_folder(self):
        p=Path(self.output.text()).expanduser()
        p.mkdir(parents=True,exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
    def save_key(self):
        try:
            save_api_key(self.api_key.text()); self.api_status.setText('API Key 已儲存到 Windows 認證儲存區。')
            if hasattr(self,'profile') and self.profile.currentData()=='optimized': self._apply_profile()
        except Exception as e: QMessageBox.critical(self,'儲存失敗',str(e))
    def test_api_key(self):
        key=self.api_key.text().strip()
        if not key: QMessageBox.warning(self,'缺少 API Key','請先貼上 Gemini API Key。'); return
        self.testkey.setEnabled(False); self.api_status.setText('完整測試中：建立測試音訊 → 上傳 → Gemini 3.5 Transcribe…')
        self.api_test_worker=ApiKeyTestWorker(key); self.api_test_worker.done.connect(self._api_test_done); self.api_test_worker.start()
    def _api_test_done(self,ok,text): self.testkey.setEnabled(True); self.api_status.setText(('✓ ' if ok else '✗ ')+text)
    def run_diagnostics(self):
        self.diag_btn.setEnabled(False); self.diag_text.setPlainText('檢查中…'); self.diag_worker=DiagnosticsWorker(self.output.text(),self.local_model.text().strip()); self.diag_worker.done.connect(self._diag_done); self.diag_worker.start()
    def _diag_done(self,ok,text): self.diag_btn.setEnabled(True); self.diag_text.setPlainText(text+('\n\n整體：可開始測試。' if ok else '\n\n整體：有必要元件缺失，請先修正紅叉項目。'))
    def _update_mode_ui(self):
        if not hasattr(self, 'mode'):
            return
        mode = self.mode.currentText()
        is_google = mode == 'Google Gemini'
        self.smart.setEnabled(is_google)
        self.diar.setEnabled(is_google and not self.smart.isChecked())
        self.timestamps.setEnabled(is_google and not self.smart.isChecked())
        self.traditional.setEnabled(True)
        custom = not hasattr(self,'profile') or self.profile.currentData() == 'custom'
        self.model.setEnabled(custom and mode != 'Google Gemini')
        self.local_model.setEnabled(custom and mode != 'Google Gemini')
        self.accel.setEnabled(custom and mode != 'Google Gemini')
        self.hw_refresh.setEnabled(custom and mode != 'Google Gemini')
        if hasattr(self,'split'): self.split.setEnabled(custom)
        if mode == '離線 Whisper':
            self.privacy.setText('🔒 完全離線：音訊不會上傳。繁體中文輸出可套用台灣用字轉換。')
        elif mode == '混合模式':
            self.privacy.setText('🔀 Whisper 本機辨識；只把文字送至 Gemini 整理，音訊不會上傳。Gemini 高負載會自動重試／切換備援模型；若仍失敗，保留本機逐字稿並正常輸出。')
        else:
            self.privacy.setText('☁ 線上模式：音訊會上傳至 Google Gemini。多人講者與時間戳可同時使用；Smart 智慧逐字稿需單獨使用。')

    def _sync_gemini_features(self, source: str, checked: bool):
        """Keep Gemini feature combinations valid without blocking the user with a dialog."""
        if not checked:
            self._update_mode_ui()
            return
        if source == 'smart':
            # Smart transcription cannot be combined with diarization/timestamps.
            for w in (self.diar, self.timestamps):
                w.blockSignals(True); w.setChecked(False); w.blockSignals(False)
            self.statusBar().showMessage('Gemini 智慧逐字稿已啟用；多人講者與字詞時間戳已自動關閉。', 6000)
        elif source in ('diar', 'timestamps') and self.smart.isChecked():
            self.smart.blockSignals(True); self.smart.setChecked(False); self.smart.blockSignals(False)
            self.statusBar().showMessage('多人講者／時間戳需 verbatim 模式；已自動關閉智慧逐字稿。', 6000)
        self._update_mode_ui()

    def start(self):
        if self.split_worker and self.split_worker.isRunning(): QMessageBox.warning(self,'切割中','請先等待「只切割音檔」完成。'); return
        files=self._all_files()
        if not files: QMessageBox.warning(self,'缺少音檔','請先加入音檔。'); return
        mode=self.mode.currentText(); key=self.api_key.text().strip()
        if mode in ('Google Gemini','混合模式') and not key: QMessageBox.warning(self,'缺少 API Key','請到「設定」輸入 Gemini API Key。'); return
        if mode=='Google Gemini' and self.smart.isChecked() and (self.diar.isChecked() or self.timestamps.isChecked()):
            self.smart.setChecked(False)
            self._append_log('已自動關閉 Gemini 智慧逐字稿：多人講者／字詞時間戳需使用 verbatim 模式。')
        formats=[]
        if self.f_docx.isChecked():formats.append('docx')
        if self.f_txt.isChecked():formats.append('txt')
        if self.f_md.isChecked():formats.append('md')
        if self.f_srt.isChecked():formats.append('srt')
        if self.f_vtt.isChecked():formats.append('vtt')
        if not formats and not self.review.isChecked(): QMessageBox.warning(self,'缺少輸出格式','請至少選一種輸出格式或啟用「錄音核對播放器」。'); return
        try:Path(self.output.text()).mkdir(parents=True,exist_ok=True)
        except Exception as e: QMessageBox.critical(self,'輸出位置無法使用',str(e)); return
        if self.local_model.text().strip() and not Path(self.local_model.text().strip()).is_dir(): QMessageBox.warning(self,'本機模型路徑錯誤','指定的本機模型資料夾不存在。'); return
        self.qs.setValue('local_model_dir',self.local_model.text().strip())
        accel_key=self.accel.currentData() or 'adaptive'
        accel_available=self.accel.currentData(Qt.UserRole+1)
        if mode != 'Google Gemini' and accel_available is False:
            QMessageBox.warning(self,'硬體不可用',f'目前電腦未偵測到「{self.accel.currentText()}」。請改用自動或其他可用裝置。')
            return
        self.qs.setValue('acceleration',accel_key)
        self.worker=TranscribeWorker(files,self.output.text(),mode,self.split.value(),self.model.currentData() or self.model.currentText(),self.local_model.text().strip(),self.language.currentData(),key,self.diar.isChecked(),self.timestamps.isChecked(),self.smart.isChecked(),self.traditional.isChecked(),formats,acceleration=accel_key,optimize=self.optimize.isChecked(),review_player=self.review.isChecked())
        self.worker.status.connect(self.statusBar().showMessage); self.worker.progress.connect(self.overall_progress.setValue); self.worker.stage_progress.connect(self._set_stage_progress); self.worker.stage_text.connect(lambda t:self.stage_label.setText('目前階段：'+t)); self.worker.log.connect(self._append_log); self.worker.checkpoint.connect(self.checkpoint_label.setText); self.worker.file_done.connect(self._file_done); self.worker.failed.connect(self._failed); self.worker.finished_ok.connect(self._ok); self.worker.cancelled.connect(self._cancelled); self.worker.finished.connect(self._thread_finished)
        self._error_dialog_open=False; self._set_running(True); self.overall_progress.setValue(0); self.stage_progress.setValue(0); self._append_log(f'v3.8：開始工作；最佳化快速切割與安全停止已啟用；硬體策略={self.accel.currentText()}；模型={self.model.currentText()}。'); self.worker.start()
    def pause(self):
        if self.worker and self.worker.isRunning():self.worker.pause(); self._append_log('已要求暫停；會在下一個安全點停住。')
    def resume(self):
        if self.worker and self.worker.isRunning():self.worker.resume(); self._append_log('繼續處理。')
    def stop(self):
        if self.worker and self.worker.isRunning():
            accepted = self.worker.stop()
            if accepted:
                self.stop_btn.setEnabled(False)
                self.stop_btn.setText('■ 正在安全停止…')
                self._append_log('已要求立即停止；若正在切割會先中止 FFmpeg，再整理已取得內容／中止紀錄並輸出可開啟檔案。')
            else:
                self.statusBar().showMessage('停止要求已送出，正在處理中。', 3000)
    @staticmethod
    def _fmt_seconds(seconds):
        try:
            seconds=max(0,int(seconds))
        except Exception:
            seconds=0
        h,rem=divmod(seconds,3600); m,sec=divmod(rem,60)
        return f'{h:02d}:{m:02d}:{sec:02d}' if h else f'{m:02d}:{sec:02d}'

    def _refresh_activity_monitor(self):
        w=self.worker
        if not w:
            self.activity_state.setText('● 待命')
            self.activity_state.setStyleSheet('font-weight:700;color:#9aa0a6;')
            self.activity_detail.setText('實際裝置：—｜目前檔案：—｜區段：—')
            self.activity_time.setText('總耗時：00:00｜本階段：00:00｜最後程式心跳：—')
            return
        try:
            snap=w.activity_snapshot()
        except Exception:
            return
        alive=w.isRunning()
        since=float(snap.get('seconds_since_event',9999))
        since_progress=float(snap.get('seconds_since_progress',9999))
        stage=str(snap.get('stage') or '')
        detail=str(snap.get('detail') or '')
        paused=bool(snap.get('paused'))
        stop_requested=bool(snap.get('stop_requested'))

        openvino_infer = 'OPENVINO' in (stage+' '+detail).upper() and '推論' in (stage+detail)
        splitting = '切割' in (stage + ' ' + detail)
        if stop_requested:
            state='■ 正在安全停止並整理目前結果'
            color='#f6c344'
        elif paused:
            state='Ⅱ 已暫停／等待安全點'
            color='#f6c344'
        elif alive and openvino_infer and float(snap.get('stage_elapsed',0)) >= 300:
            state='◐ OpenVINO 已運算超過 5 分鐘｜程式仍有心跳，請觀察硬體使用率'
            color='#f6c344'
        elif alive and since <= 5:
            state='● 正在執行｜程式心跳正常'
            color='#54d17a'
        elif alive and since <= 20:
            state='● 背景工作仍在執行｜等待下一次工作回報'
            color='#8fd3ff'
        elif alive:
            state='◐ 工作執行緒仍存活，但工作階段較久未完成'
            color='#f6c344'
        else:
            state='○ 工作執行緒已結束'
            color='#9aa0a6'
        self.activity_state.setText(state)
        self.activity_state.setStyleSheet(f'font-weight:700;color:{color};')

        device=snap.get('device') or '自動／尚未回報'
        if splitting:
            device='CPU/磁碟（切割階段不使用 GPU/NPU）'
        fname=snap.get('file') or '—'
        chunk=snap.get('chunk') or 0; total=snap.get('chunk_count') or 0
        chunk_text=f'{chunk}/{total}' if total else '—'
        self.activity_detail.setText(f'實際裝置：{device}｜目前檔案：{fname}｜區段：{chunk_text}')
        last='剛剛' if since < 2 else f'{int(since)} 秒前'
        self.activity_time.setText(
            f'總耗時：{self._fmt_seconds(snap.get("job_elapsed",0))}｜'
            f'本階段：{self._fmt_seconds(snap.get("stage_elapsed",0))}｜最後程式心跳：{last}'
        )

        if splitting:
            hint='正在切割音檔：此階段主要使用 CPU／磁碟 I/O，GPU/NPU 沒有負載是正常的。v3.8 會先嘗試無重編碼快速切割；只有格式不相容才做 PCM 相容切割。'
        elif openvino_infer:
            if since <= 5:
                hint='OpenVINO 正在推論。這個引擎通常只在完成時給最終百分比；心跳持續更新就表示程式仍在等待引擎運算。'
            else:
                hint='OpenVINO 呼叫尚未返回。若「最後程式心跳」持續更新，程式沒有當機；若超過約 30 秒完全沒有心跳，再查看工作管理員 GPU/NPU 使用率。'
        elif since_progress > 60 and alive:
            hint='百分比超過 60 秒沒有改變，但工作執行緒仍存活。這可能是模型編譯、Gemini 等待或硬體推論；請以「最後程式心跳」判斷是否仍有活動。'
        else:
            hint=detail or '工作正常執行中。'
        self.activity_hint.setText(hint)

    def _set_stage_progress(self,pct):
        if pct<0:self.stage_progress.setRange(0,0); self.stage_progress.setFormat('引擎運算中…')
        else:
            if self.stage_progress.minimum()==0 and self.stage_progress.maximum()==0:self.stage_progress.setRange(0,100)
            self.stage_progress.setValue(max(0,min(100,pct))); self.stage_progress.setFormat('目前階段 %p%')
    def _append_log(self,text):self.logbox.appendPlainText(text)
    def _set_running(self,running):
        self.start_btn.setEnabled(not running); self.pause_btn.setEnabled(running); self.resume_btn.setEnabled(running)
        self.stop_btn.setEnabled(running); self.stop_btn.setText('■ 立即停止並輸出目前結果')
        self.split_only_btn.setEnabled(not running); self._refresh_activity_monitor()
    def _file_done(self,text):
        self._append_log('已輸出： '+text.replace('\n',' | ')); self.statusBar().showMessage('檔案輸出完成')
        for line in text.splitlines():
            if line.lower().endswith('.html') and Path(line).exists():
                self.latest_review_path=line; self.open_review_btn.setEnabled(True)
    def _failed(self,text):
        self._append_log('錯誤：'+text); self.statusBar().showMessage('處理失敗，詳細內容已寫入下方紀錄。')
        if not self._error_dialog_open:
            self._error_dialog_open=True
            QMessageBox.critical(self,'處理失敗',text)
            self._error_dialog_open=False
    def _ok(self):
        elapsed='—'; device='—'; warnings=[]
        try:
            snap=self.worker.activity_snapshot() if self.worker else {}
            elapsed=self._fmt_seconds(snap.get('job_elapsed',0)); device=snap.get('device') or '自動'
            warnings=self.worker.warnings_snapshot() if self.worker and hasattr(self.worker,'warnings_snapshot') else []
        except Exception:
            pass
        self._append_log(f'所有工作已完成。總耗時：{elapsed}｜最終裝置：{device}')
        if warnings:
            for w in warnings:self._append_log('提醒：'+w)
            QMessageBox.warning(self,'完成（有提醒）',f'所有工作已完成，檔案已正常輸出。\n\n總耗時：{elapsed}\n最終裝置：{device}\n\n提醒：{warnings[-1]}\n\n輸出位置：{self.output.text()}')
        else:
            QMessageBox.information(self,'完成',f'所有工作已完成。\n\n總耗時：{elapsed}\n最終裝置：{device}\n輸出位置：{self.output.text()}')

    def _cancelled(self):self._append_log('工作已安全停止。若已有辨識內容會輸出「中止版」；若停止時仍在切割階段，也會至少輸出一份可開啟的中止紀錄。完整完成的區段仍保留於快取。')
    def _thread_finished(self):
        self._set_running(False)
        if self.worker:self.worker.deleteLater(); self.worker=None
