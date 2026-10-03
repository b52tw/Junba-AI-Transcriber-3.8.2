from __future__ import annotations
import json
import hashlib
import threading
import time
from pathlib import Path
from PySide6.QtCore import QThread, Signal
from app.core.audio_tools import (
    ensure_split_audio, audio_duration_seconds, split_output_dir, probe_audio,
    prepare_compatible_audio, AudioOperationCancelled
)
from app.core.models import TranscriptResult, Segment
from app.providers.local_whisper import LocalWhisperProvider
from app.providers.openvino_whisper import OpenVINOWhisperProvider
from app.core.hardware import (
    resolve_acceleration, adaptive_plan, recommended_model, recommended_split_minutes, record_accelerator_result
)
from app.providers.gemini import GeminiProvider
from app.exporters.exporters import export_all
from app.core.text_normalize import normalize_result_traditional, to_traditional_taiwan


class TranscribeWorker(QThread):
    status = Signal(str)
    progress = Signal(int)
    stage_progress = Signal(int)
    stage_text = Signal(str)
    log = Signal(str)
    checkpoint = Signal(str)
    file_done = Signal(str)
    failed = Signal(str)
    finished_ok = Signal()
    cancelled = Signal()

    def __init__(self, files, output_dir, mode, split_minutes, model_name, local_model_dir, language,
                 api_key, diarization, timestamps, smart, traditional_output, formats, acceleration='auto',
                 optimize=True, review_player=True):
        super().__init__()
        self.files = files
        self.output_dir = Path(output_dir)
        self.mode = mode
        self.split_minutes = split_minutes
        self.model_name = model_name
        self.local_model_dir = local_model_dir or ''
        self.acceleration = acceleration or 'auto'
        self.language = language
        self.api_key = api_key
        self.diarization = diarization
        self.timestamps = timestamps
        self.smart = smart
        self.traditional_output = traditional_output
        self.formats = formats
        self.optimize = bool(optimize)
        self.review_player = bool(review_player)
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._pause.set()
        # v3.0 activity telemetry. These values are read by the GUI timer while
        # a provider call is blocking, so the user can tell the difference
        # between "still computing" and a frozen/crashed worker.
        now = time.monotonic()
        self._activity_lock = threading.Lock()
        self._job_started = now
        self._stage_started = now
        self._last_event = now
        self._last_progress = now
        self._current_stage = '待命'
        self._current_detail = ''
        self._current_device = ''
        self._current_file = ''
        self._current_chunk = 0
        self._chunk_count = 0
        self._last_pct = 0
        self._warnings = []
        self._current_source_path = ''
        self._partial_text: list[str] = []
        self._partial_segments: list[Segment] = []

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m, sec = divmod(rem, 60)
        return f'{h:02d}:{m:02d}:{sec:02d}' if h else f'{m:02d}:{sec:02d}'

    def _touch_activity(self, *, stage=None, detail=None, pct=None, device=None, new_stage=False):
        now = time.monotonic()
        with self._activity_lock:
            self._last_event = now
            if new_stage:
                self._stage_started = now
            if stage is not None:
                self._current_stage = str(stage)
            if detail is not None:
                self._current_detail = str(detail)
            if pct is not None and pct >= 0:
                self._last_progress = now
                self._last_pct = int(max(0, min(100, pct)))
            if device:
                self._current_device = str(device)

    def warnings_snapshot(self) -> list[str]:
        with self._activity_lock:
            return list(self._warnings)

    def activity_snapshot(self) -> dict:
        now = time.monotonic()
        with self._activity_lock:
            return {
                'job_elapsed': now - self._job_started,
                'stage_elapsed': now - self._stage_started,
                'seconds_since_event': now - self._last_event,
                'seconds_since_progress': now - self._last_progress,
                'stage': self._current_stage,
                'detail': self._current_detail,
                'device': self._current_device,
                'file': self._current_file,
                'chunk': self._current_chunk,
                'chunk_count': self._chunk_count,
                'pct': self._last_pct,
                'paused': not self._pause.is_set(),
                'stop_requested': self._stop.is_set(),
            }

    def pause(self):
        self._pause.clear()
        self._touch_activity(detail='使用者已要求暫停；等待安全點。')

    def resume(self):
        self._pause.set()
        self._touch_activity(detail='已繼續處理。')

    def stop(self):
        # Idempotent safe-stop. FFmpeg sees the flag immediately; providers stop
        # at their next safe point. Already recognized content is always exported.
        if self._stop.is_set():
            return False
        self._stop.set()
        self._pause.set()
        self._touch_activity(detail='已要求立即停止；正在中止目前子程序並整理可輸出結果。')
        return True

    def _wait(self):
        while not self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.1)

    def _set_stage(self, text: str, pct: int = -1):
        self._touch_activity(stage=text, detail=text, pct=pct if pct >= 0 else None, new_stage=True)
        self.stage_text.emit(text)
        self.stage_progress.emit(pct)
        self.status.emit(text)
        self.log.emit(text)

    def _provider_progress(self, p: int, detail: str = ''):
        # Provider heartbeats use a negative progress value while a blocking
        # inference/API call is alive. Keep the stage bar animated instead of
        # pretending a numeric percentage is available.
        device = None
        upper = (detail or '').upper()
        for d in ('NPU', 'GPU', 'CPU', 'CUDA'):
            if d in upper:
                device = d
                break
        self._touch_activity(detail=detail or self._current_detail, pct=p if p >= 0 else None, device=device)
        self.stage_progress.emit(p)
        if detail:
            self.stage_text.emit(detail)

    def _chunk_progress(self, fi: int, ci: int, chunk_count: int, total_files: int, pct: int, detail: str = ''):
        self._current_chunk = ci + 1
        self._chunk_count = chunk_count
        if pct < 0:
            # Indeterminate/heartbeat update from an engine that cannot expose
            # token-level progress (notably OpenVINO GenAI Whisper).
            self._provider_progress(pct, detail)
            return
        pct = max(0, min(100, int(pct)))
        self._touch_activity(detail=detail or self._current_detail, pct=pct)
        self.stage_progress.emit(pct)
        if detail:
            self.stage_text.emit(detail)
        # Reserve first 15% for preprocessing, 80% for recognition, 5% for export.
        within_file = (ci + pct / 100.0) / max(1, chunk_count)
        recognize_fraction = (fi + within_file) / max(1, total_files)
        overall = 15 + int(recognize_fraction * 80)
        self.progress.emit(max(15, min(95, overall)))

    def _effective_split(self, duration: float) -> int:
        effective_split = int(self.split_minutes)
        # User-selected minutes always win. In optimized mode, zero means
        # automatic chunking rather than "one giant chunk" for long audio.
        if effective_split <= 0 and self.optimize:
            policy = self.acceleration if self.acceleration in ('adaptive','performance','eco') else 'adaptive'
            effective_split = recommended_split_minutes(
                self.mode, duration, policy=policy, model=self.model_name
            )
            if effective_split > 0:
                self.log.emit(f'最佳化自動切割：依本機硬體與音檔長度採每 {effective_split} 分鐘一段。')
        if self.mode == 'Google Gemini':
            max_minutes = 30 if (self.diarization or self.timestamps) else 60
            if duration > max_minutes * 60 and (effective_split == 0 or effective_split > max_minutes):
                effective_split = max_minutes
                self.log.emit(f'依 Gemini 音訊上限自動切為每 {max_minutes} 分鐘。')
        return effective_split

    def _prepare_plans(self):
        """Validate/repair sources and split all files before model/API initialization."""
        plans = []
        total_files = max(1, len(self.files))
        prep_root = self.output_dir / '.junba_prepared'
        for fi, source in enumerate(self.files):
            if self._stop.is_set():
                break
            self._wait()
            srcp = Path(source)
            self._current_source_path = str(srcp)
            with self._activity_lock:
                self._current_file = srcp.name
                self._current_chunk = 0
                self._chunk_count = 0
            if not srcp.exists():
                raise FileNotFoundError(f'找不到音檔：{source}')

            self._set_stage(f'檢查音檔相容性：{srcp.name}', -1)
            probe = probe_audio(source)
            process_source = source
            if probe.get('decode_ok') and not probe.get('needs_prepare'):
                self.log.emit(f'音檔預檢通過：{srcp.name}｜{float(probe.get("duration") or 0)/60:.1f} 分鐘')
            if probe.get('needs_prepare'):
                self.log.emit(f'音檔需要相容轉換：{srcp.name}｜{probe.get("error") or "無法正常解碼/判讀"}')
                self._set_stage(f'建立相容音檔：{srcp.name}', 0)
                process_source = prepare_compatible_audio(
                    source, str(prep_root),
                    progress_cb=lambda p: self.stage_progress.emit(p),
                    cancel_cb=self._stop.is_set,
                )
                probe = probe_audio(process_source)
                if not probe.get('decode_ok'):
                    raise RuntimeError(
                        f'音檔無法讀取，即使轉為相容 WAV 仍失敗：{srcp.name}\n'
                        f'{probe.get("error") or "未知解碼錯誤"}\n'
                        '若檔案位於 OneDrive，請先設定「永遠保留在此裝置」後重試。'
                    )
                self.log.emit(f'相容轉換完成：{process_source}')

            duration = float(probe.get('duration') or audio_duration_seconds(process_source))
            if duration <= 0:
                raise RuntimeError(f'無法判讀音檔長度：{srcp.name}')
            effective_split = self._effective_split(duration)
            if effective_split > 0 and duration > effective_split * 60:
                split_dir = split_output_dir(str(self.output_dir), source, effective_split)
                self._set_stage(f'快速切割音檔（CPU/磁碟）：{srcp.name}', 0)
                split_started = time.monotonic()

                def split_progress(p, _fi=fi):
                    elapsed = max(0.01, time.monotonic() - split_started)
                    eta = (elapsed * (100 - p) / p) if p > 1 else 0
                    detail = (
                        f'快速切割｜{p}%｜已耗時 {self._fmt_elapsed(elapsed)}' +
                        (f'｜預估剩餘 {self._fmt_elapsed(eta)}' if eta > 0 else '') +
                        '｜此階段使用 CPU/磁碟，不使用 GPU/NPU'
                    )
                    self._touch_activity(detail=detail, pct=p, device='CPU/磁碟')
                    self.stage_text.emit(detail)
                    self.stage_progress.emit(p)
                    self.progress.emit(min(14, int(((_fi + p / 100.0) / total_files) * 15)))

                def split_event(text):
                    self.log.emit(text)
                    self._touch_activity(detail=text, device='CPU/磁碟')

                chunks = ensure_split_audio(
                    process_source, split_dir, effective_split,
                    progress_cb=split_progress,
                    cancel_cb=self._stop.is_set,
                    event_cb=split_event,
                )
                self.log.emit(
                    f'切割完成：{len(chunks)} 段｜{split_dir}｜耗時 {self._fmt_elapsed(time.monotonic()-split_started)}'
                )
            else:
                chunks = [process_source]
                self.progress.emit(min(14, int(((fi + 1) / total_files) * 15)))
                if effective_split > 0:
                    self.log.emit(f'{srcp.name} 未超過 {effective_split} 分鐘，不需實際切割。')
            plans.append({
                'source': source,
                'process_source': process_source,
                'duration': duration,
                'effective_split': effective_split,
                'chunks': chunks,
            })
        self.progress.emit(15)
        return plans

    def _compose_result(self, all_text, all_segments, engine_suffix='') -> TranscriptResult:
        final_text = '\n'.join(x for x in all_text if x)
        if self.traditional_output and self.language in ('zh', 'auto'):
            final_text = to_traditional_taiwan(final_text)
            for seg in all_segments:
                seg.text = to_traditional_taiwan(seg.text)
        engine = self.mode + engine_suffix
        return TranscriptResult(final_text, list(all_segments), engine=engine)

    def _export_current(self, srcp: Path, all_text, all_segments, partial=False, source_audio: str | None = None, allow_empty_status=False) -> list[str]:
        no_content = not all_text and not all_segments
        if no_content and not (partial and allow_empty_status):
            return []
        suffix = '_中止版_逐字稿' if partial else '_逐字稿'
        note = None
        export_text = list(all_text)
        if partial:
            if no_content:
                note = (
                    '【中止紀錄】使用者已按「立即停止並輸出目前結果」。'
                    '停止時仍在音檔預檢／切割階段，尚未產生可用的逐字稿文字。'
                    '本檔用來確認工作已安全結束；下次重新開始可重新切割或沿用已完成快取。'
                )
                export_text = ['尚未進入辨識階段，因此目前沒有逐字稿內容。']
            else:
                note = (
                    '【中止版】使用者已按「立即停止並輸出目前結果」。'
                    '本檔只包含停止前已完成或已取得的辨識內容；尚未完成的區段不會出現在本檔。'
                )
        result = self._compose_result(
            export_text,
            all_segments,
            engine_suffix='（中止版／部分結果）' if partial else '',
        )
        file_out_dir = Path(getattr(self, '_current_output_dir', self.output_dir / srcp.stem))
        file_out_dir.mkdir(parents=True, exist_ok=True)
        base = file_out_dir / f'{srcp.stem}{suffix}'
        formats = list(self.formats)
        if self.review_player and all_segments and 'review' not in formats:
            formats.append('review')
        paths = export_all(result, str(base), formats, note=note, source_audio=source_audio or str(srcp))
        if partial and no_content and not paths:
            status_path = base.with_suffix('.txt')
            status_path.write_text(
                (note or '') + '\n\n尚未進入辨識階段，因此目前沒有逐字稿內容。',
                encoding='utf-8-sig',
            )
            paths.append(str(status_path))
        if paths:
            self.file_done.emit('\n'.join(paths))
        return paths

    def _export_stop_placeholder(self) -> list[str]:
        src = Path(self._current_source_path) if self._current_source_path else Path('工作')
        try:
            return self._export_current(
                src, list(self._partial_text), list(self._partial_segments),
                partial=True, source_audio=self._current_source_path or None,
                allow_empty_status=True,
            )
        except Exception as exc:
            stamp = time.strftime('%Y%m%d_%H%M%S')
            p = self.output_dir / f'峻爸_工作中止紀錄_{stamp}.txt'
            p.write_text(
                '工作已停止。\n中止版匯出發生例外，但主工作已安全結束。\n'
                f'{type(exc).__name__}: {exc}',
                encoding='utf-8-sig',
            )
            self.file_done.emit(str(p))
            return [str(p)]

    def _make_local_provider(self):
        """Create the local Whisper provider with cross-PC adaptive fallback.

        Policies (adaptive/performance/eco/legacy auto) may try more than one
        backend during initialization. Manual selections are tried first and
        still fall back to CPU if the accelerator cannot initialize. This makes
        the same portable build usable on Intel/AMD CPU systems, NVIDIA CUDA
        systems and Intel OpenVINO GPU/NPU systems without editing config.
        """
        requested = self.acceleration or 'adaptive'
        policy = 'adaptive' if requested == 'auto' else requested
        selected_model = self.model_name
        if selected_model == 'auto':
            selected_model = recommended_model(policy if policy in ('adaptive','performance','eco') else 'adaptive')
            self.log.emit(f'自動模型：依本機硬體/記憶體選擇 {selected_model}')
        self.effective_model_name = selected_model

        if policy in ('adaptive','performance','eco'):
            candidates = adaptive_plan(policy)
        else:
            # Manual mode still gets a CPU safety net so another PC does not
            # become unusable just because a driver/backend is absent.
            try:
                first = resolve_acceleration(policy)
                candidates = [first] + ([] if first == 'cpu' else ['cpu'])
            except Exception as exc:
                self.log.emit(f'指定硬體不可用：{exc}；改用 CPU 保底。')
                candidates = ['cpu']

        seen=[]; errors=[]
        for accel in candidates:
            if accel in seen:
                continue
            seen.append(accel)
            try:
                if accel in ('openvino_npu','openvino_gpu'):
                    ov_device = 'NPU' if accel == 'openvino_npu' else 'GPU'
                    self._set_stage(f'載入 OpenVINO Whisper 至 {ov_device}；第一次會下載/編譯模型…', -1)
                    provider = OpenVINOWhisperProvider(
                        selected_model, device=ov_device, local_model_dir=self.local_model_dir,
                        progress_cb=lambda p, d='': (
                            self._provider_progress(p, d),
                            self.log.emit(d) if d and p >= 0 else None,
                        ),
                    )
                    actual = getattr(provider, 'device', ov_device)
                    record_accelerator_result('openvino_' + actual.lower() if actual in ('GPU','NPU') else actual.lower(), True, '初始化成功')
                    self._touch_activity(device=actual)
                    self._set_stage(f'Whisper 已就緒：OpenVINO {actual}', 100)
                    return provider, accel
                device = 'cuda' if accel == 'cuda' else 'cpu'
                compute = 'float16' if device == 'cuda' else 'int8'
                source = self.local_model_dir if self.local_model_dir else selected_model
                self._set_stage(f'載入 faster-whisper：{device}/{compute}；第一次下載模型可能需要數分鐘…', -1)
                provider = LocalWhisperProvider(source, device=device, compute_type=compute)
                record_accelerator_result(accel, True, '初始化成功')
                self._touch_activity(device=str(provider.device).upper())
                self._set_stage(f'Whisper 已就緒：{provider.device} / {provider.compute_type}', 100)
                return provider, accel
            except Exception as exc:
                record_accelerator_result(accel, False, f'{type(exc).__name__}: {exc}')
                errors.append(f'{accel}: {type(exc).__name__}: {str(exc).splitlines()[0][:220]}')
                self.log.emit(f'硬體路徑 {accel} 初始化失敗，嘗試下一個可用路徑。')

        raise RuntimeError('所有本機 Whisper 硬體路徑都無法初始化：\n' + '\n'.join(errors))

    def _transcribe_local_adaptive(self, local, chunk, lang, cb):
        """Run a chunk and fall back from CUDA to CPU if runtime inference fails.

        OpenVINO provider already performs NPU→GPU→CPU fallback internally.
        This wrapper adds the equivalent safety net for faster-whisper CUDA.
        """
        try:
            started = time.monotonic()
            result = local.transcribe(
                chunk, lang, stop_flag=self._stop.is_set,
                progress_cb=cb, wait_cb=self._wait,
            )
            elapsed = time.monotonic() - started
            audio_sec = max(0.0, audio_duration_seconds(chunk))
            dev = str(getattr(local, 'device', '')).lower()
            if dev:
                key = 'cuda' if dev == 'cuda' else ('openvino_' + dev if dev in ('gpu','npu') else dev)
                record_accelerator_result(key, True, '區段推論成功', audio_seconds=audio_sec, elapsed_seconds=elapsed)
                if audio_sec > 0:
                    self.log.emit(f'效能紀錄：{key}｜音訊 {audio_sec:.1f}s｜耗時 {elapsed:.1f}s｜RTF {elapsed/audio_sec:.2f}x')
            return result, local
        except Exception as exc:
            dev = str(getattr(local, 'device', '')).lower()
            key = 'cuda' if dev == 'cuda' else ('openvino_' + dev if dev in ('gpu','npu') else dev)
            if key:
                record_accelerator_result(key, False, f'{type(exc).__name__}: {exc}')
            if dev == 'cuda':
                self.log.emit(f'NVIDIA CUDA 推論失敗（{type(exc).__name__}）；同一區段改用 CPU 保底。')
                source = self.local_model_dir if self.local_model_dir else getattr(self, 'effective_model_name', self.model_name)
                cpu = LocalWhisperProvider(source, device='cpu', compute_type='int8')
                self._touch_activity(device='CPU')
                result = cpu.transcribe(
                    chunk, lang, stop_flag=self._stop.is_set,
                    progress_cb=cb, wait_cb=self._wait,
                )
                record_accelerator_result('cpu', True, 'CUDA 失敗後 CPU 備援成功')
                return result, cpu
            raise

    def run(self):
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._set_stage('準備音檔；需要切割的檔案會先完成切割', 0)
            plans = self._prepare_plans()
            if self._stop.is_set():
                self._set_stage('已停止；正在建立中止紀錄。', 100)
                self._export_stop_placeholder()
                self.cancelled.emit()
                return

            local = None
            gemini = None
            resolved_accel = None
            if self.mode in ('離線 Whisper', '混合模式'):
                local, resolved_accel = self._make_local_provider()
                self.log.emit(f'硬體自適應：要求={self.acceleration}｜目前實際路徑={resolved_accel}｜模型={getattr(self, "effective_model_name", self.model_name)}')
            if self.mode in ('Google Gemini', '混合模式'):
                self._set_stage('初始化 Google Gemini…', -1)
                gemini = GeminiProvider(self.api_key)
                self._set_stage('Google Gemini 已就緒', 100)

            total_files = max(1, len(plans))
            stopped = False
            for fi, plan in enumerate(plans):
                if self._stop.is_set():
                    stopped = True
                    break
                self._wait()
                source = plan['source']
                process_source = plan.get('process_source', source)
                srcp = Path(source)
                self._current_output_dir = self.output_dir / f'{fi + 1:02d}_{srcp.stem}'
                self._current_output_dir.mkdir(parents=True, exist_ok=True)
                with self._activity_lock:
                    self._current_file = srcp.name
                    self._current_chunk = 0
                    self._chunk_count = len(plan['chunks'])
                effective_split = plan['effective_split']
                chunks = plan['chunks']

                model_key = getattr(self, "effective_model_name", self.model_name)
                try:
                    sig = (
                        f"{srcp.resolve()}|{srcp.stat().st_size}|{srcp.stat().st_mtime_ns}|"
                        f"{self.mode}|{model_key}|{self.local_model_dir}|{self.acceleration}|"
                        f"{effective_split}|{self.language}|{self.diarization}|"
                        f"{self.timestamps}|{self.smart}|{self.traditional_output}"
                    )
                except Exception:
                    sig = (
                        f"{source}|{self.mode}|{model_key}|{self.local_model_dir}|"
                        f"{self.acceleration}|{effective_split}|{self.language}|"
                        f"{self.diarization}|{self.timestamps}|{self.smart}|"
                        f"{self.traditional_output}"
                    )
                profile = hashlib.sha1(sig.encode('utf-8')).hexdigest()[:12]
                work = self.output_dir / '.junba_cache' / f'{srcp.stem}_{profile}'
                work.mkdir(parents=True, exist_ok=True)

                all_segments = []
                all_text = []
                self._partial_text = all_text
                self._partial_segments = all_segments
                self._current_source_path = source
                running_offset = 0.0
                completed_chunks = 0

                for ci, chunk in enumerate(chunks):
                    if self._stop.is_set():
                        stopped = True
                        break
                    self._wait()
                    cache_file = work / f'result_{ci:03d}.json'
                    r = None
                    result_is_complete_chunk = True

                    if cache_file.exists():
                        self._set_stage(f'讀取快取：{ci+1}/{len(chunks)}', 100)
                        r = self._load_cached_result(cache_file)
                        if self.traditional_output and self.language in ('zh', 'auto'):
                            r = normalize_result_traditional(r)
                        self._chunk_progress(fi, ci, len(chunks), total_files, 100, '已載入快取')
                    else:
                        self._set_stage(f'辨識 {srcp.name}｜區段 {ci+1}/{len(chunks)}', 0)
                        cb = lambda p, d='', _fi=fi, _ci=ci, _n=len(chunks): self._chunk_progress(_fi, _ci, _n, total_files, p, d)
                        if self.mode == 'Google Gemini':
                            lang_codes = self._gemini_language_codes(self.language)
                            # Gemini returns a chunk atomically. If stop is pressed while the
                            # network call is in flight, we can only stop after this call returns.
                            r = gemini.transcribe(
                                chunk, self.diarization, self.timestamps, self.smart,
                                language_codes=lang_codes, progress_cb=cb,
                            )
                            result_is_complete_chunk = True
                        else:
                            lang = None if self.language == 'auto' else self.language
                            r, local = self._transcribe_local_adaptive(local, chunk, lang, cb)
                            if hasattr(local, 'device'):
                                self._touch_activity(device=str(local.device).upper())
                            # LocalWhisperProvider returns what it has accumulated even when
                            # stop_flag becomes true; treat that result as partial and do not
                            # overwrite the full-chunk cache.
                            result_is_complete_chunk = not self._stop.is_set()

                        if r and self.traditional_output and self.language in ('zh', 'auto'):
                            r = normalize_result_traditional(r)
                            self.log.emit('已轉為繁體中文（台灣用字）。')
                        if r and result_is_complete_chunk:
                            self._save_cached_result(cache_file, r)

                    if r:
                        for s in r.segments:
                            all_segments.append(Segment(
                                s.start + running_offset,
                                s.end + running_offset,
                                s.text,
                                s.speaker,
                            ))
                        if r.text:
                            all_text.append(r.text)

                    chunk_duration = max(0.0, audio_duration_seconds(chunk))
                    # Only advance to the next full chunk offset when this chunk completed.
                    # For a partial local Whisper chunk, its segments already carry the correct
                    # local timestamps and we are about to stop anyway.
                    if result_is_complete_chunk:
                        running_offset += chunk_duration
                        completed_chunks = ci + 1
                        self._write_checkpoint(source, completed_chunks, len(chunks), fi, total_files)

                    if self._stop.is_set():
                        stopped = True
                        if self.mode == 'Google Gemini' and r:
                            # The API call already returned a full chunk, so include it in the
                            # resumable checkpoint as completed.
                            if completed_chunks < ci + 1:
                                completed_chunks = ci + 1
                                running_offset += chunk_duration
                                self._write_checkpoint(source, completed_chunks, len(chunks), fi, total_files)
                        break

                if stopped or self._stop.is_set():
                    self._set_stage('正在整理已完成內容並輸出中止版檔案…', -1)
                    paths = self._export_current(srcp, all_text, all_segments, partial=True, source_audio=source, allow_empty_status=True)
                    if paths:
                        self.log.emit('立即停止完成：已輸出可開啟的中止版檔案。')
                    else:
                        self.log.emit('立即停止完成：已建立工作中止紀錄。')
                    break

                final_text = '\n'.join(x for x in all_text if x)
                if self.mode == '混合模式' and gemini:
                    self._set_stage('Gemini 整理逐字稿…', 0)
                    try:
                        final_text = gemini.postprocess_text(
                            final_text,
                            target='繁體中文（台灣）',
                            progress_cb=lambda p, d='': self._provider_progress(p, d),
                        )
                        all_text = [final_text]
                    except Exception as exc:
                        warning = (
                            'Gemini 文字整理暫時不可用；已保留本機 Whisper 辨識結果，'
                            '不中止整個工作並繼續匯出。稍後可再用文字整理功能重試。'
                        )
                        with self._activity_lock:
                            self._warnings.append(warning)
                        self.log.emit(warning + f'｜原因：{type(exc).__name__}: {str(exc).splitlines()[0][:260]}')
                        self._set_stage('Gemini 暫時忙碌；保留本機逐字稿並繼續匯出', 100)

                self._set_stage('匯出 Word / 字幕檔…', -1)
                self._export_current(srcp, all_text, all_segments, partial=False, source_audio=source)
                self.stage_progress.emit(100)
                self.progress.emit(95 + int(((fi + 1) / total_files) * 5))

            if stopped or self._stop.is_set():
                self._set_stage('工作已停止；目前結果已整理並輸出。', 100)
                self.cancelled.emit()
            else:
                self.progress.emit(100)
                self._set_stage('全部工作完成', 100)
                self.finished_ok.emit()
        except AudioOperationCancelled:
            self._set_stage('已安全停止；正在整理目前結果。', 100)
            self._export_stop_placeholder()
            self.cancelled.emit()
        except Exception as e:
            if self._stop.is_set():
                # Child processes/providers may surface a low-level error while
                # being interrupted. Stop should never become a failure dialog.
                self._set_stage('已安全停止；正在整理目前結果。', 100)
                self._export_stop_placeholder()
                self.cancelled.emit()
            else:
                self.failed.emit(f'{type(e).__name__}: {e}')

    @staticmethod
    def _gemini_language_codes(language: str) -> list[str]:
        return {'en': ['en-US'], 'ja': ['ja-JP']}.get(language, [])

    @staticmethod
    def _save_cached_result(path, result):
        payload = {
            'text': result.text,
            'language': result.language,
            'engine': result.engine,
            'segments': [
                {'start': s.start, 'end': s.end, 'text': s.text, 'speaker': s.speaker}
                for s in result.segments
            ],
        }
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

    @staticmethod
    def _load_cached_result(path):
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
        segs = [Segment(**x) for x in payload.get('segments', [])]
        return TranscriptResult(payload.get('text', ''), segs, payload.get('language'), payload.get('engine', ''))

    def _write_checkpoint(self, source, completed, total, file_index, total_files):
        p = self.output_dir / '.junba_checkpoint.json'
        data = {
            'source': source,
            'completed_chunks': completed,
            'total_chunks': total,
            'file_index': file_index + 1,
            'total_files': total_files,
            'mode': self.mode,
            'model': getattr(self, 'effective_model_name', self.model_name),
            'acceleration': self.acceleration,
            'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        self.checkpoint.emit(f'進度已儲存：{completed}/{total} 區段｜{p}')
