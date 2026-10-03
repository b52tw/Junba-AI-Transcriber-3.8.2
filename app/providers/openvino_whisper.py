from __future__ import annotations

from pathlib import Path
import os
import subprocess
import threading
import time
from app.core.subprocess_utils import hidden_process_kwargs
from app.core.models import Segment, TranscriptResult
from app.core.hardware import record_accelerator_result


HEARTBEAT_SECONDS = 2.0


REPO_MAP = {
    'large-v3': 'OpenVINO/whisper-large-v3-int8-ov',
    'large-v3-turbo': 'OpenVINO/whisper-large-v3-turbo-int8-ov',
    'medium': 'OpenVINO/whisper-medium-int8-ov',
    'small': 'OpenVINO/whisper-small-int8-ov',
    'base': 'OpenVINO/whisper-base-int8-ov',
}


def _model_root(model_name: str) -> Path:
    try:
        from platformdirs import user_data_dir
        base = Path(user_data_dir('JunbaAITranscriber', 'Junba'))
    except Exception:
        base = Path.home() / '.junba_ai_transcriber'
    return base / 'models' / 'openvino' / model_name


def ensure_openvino_model(model_name: str, local_model_dir: str = '', progress_cb=None) -> str:
    if local_model_dir:
        p = Path(local_model_dir)
        if p.is_dir() and any(p.glob('openvino_*model.xml')):
            return str(p)
    if model_name not in REPO_MAP:
        raise ValueError(f'OpenVINO 尚未設定此 Whisper 模型：{model_name}')
    target = _model_root(model_name)
    if target.is_dir() and any(target.glob('openvino_*model.xml')):
        return str(target)
    target.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault('HF_HUB_DISABLE_PROGRESS_BARS', '1')
    from app.core.runtime import prepare_windowed_runtime
    prepare_windowed_runtime()

    if progress_cb:
        progress_cb(-1, f'第一次使用 OpenVINO {model_name}：正在下載 INT8 模型，請勿關閉程式…')

    import threading
    import time
    done = threading.Event()
    started = time.monotonic()

    def heartbeat():
        while not done.wait(5):
            if progress_cb:
                elapsed = int(time.monotonic() - started)
                progress_cb(-1, f'OpenVINO {model_name} 模型下載中… 已等待 {elapsed} 秒；大型模型第一次會較久。')

    t = threading.Thread(target=heartbeat, daemon=True)
    t.start()
    try:
        from huggingface_hub import snapshot_download
        try:
            from huggingface_hub.utils import disable_progress_bars
            disable_progress_bars()
        except Exception:
            pass
        snapshot_download(repo_id=REPO_MAP[model_name], local_dir=str(target))
    except Exception as e:
        raise RuntimeError(
            'OpenVINO 模型下載失敗。請確認網路與磁碟空間後重試。'
            f'\n原始錯誤：{type(e).__name__}: {e}'
        ) from e
    finally:
        done.set()

    if not any(target.glob('openvino_*model.xml')):
        raise RuntimeError(f'OpenVINO 模型下載完成但找不到 IR XML：{target}')
    if progress_cb:
        progress_cb(10, f'OpenVINO 模型已準備：{target}')
    return str(target)


def _decode_16k_mono(path: str, progress_cb=None):
    import numpy as np
    from app.core.audio_tools import ffmpeg_exe
    if progress_cb:
        progress_cb(12, '解碼音訊為 16 kHz 單聲道')
    cmd = [
        ffmpeg_exe(), '-v', 'error', '-i', str(path),
        '-vn', '-ac', '1', '-ar', '16000',
        '-f', 's16le', '-acodec', 'pcm_s16le', 'pipe:1',
    ]
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=None, **hidden_process_kwargs())
    if r.returncode != 0:
        err = r.stderr.decode('utf-8', errors='replace')[-3000:]
        raise RuntimeError(f'FFmpeg 音訊解碼失敗：{err}')
    if not r.stdout:
        return np.empty((0,), dtype=np.float32)
    pcm = np.frombuffer(r.stdout, dtype='<i2')
    return pcm.astype(np.float32) / 32768.0


def _is_npu_runtime_error(exc: BaseException) -> bool:
    s = f'{type(exc).__name__}: {exc}'.lower()
    markers = (
        'ze_result_error_invalid_argument',
        '0x78000004',
        'pfnappendgraphexecute',
        'zegraphsetargumentvalue',
        'invalid unordered_map',
        'infer_request.cpp:246',
        'intel_npu',
    )
    return any(m in s for m in markers)


def _is_openvino_loader_error(exc: BaseException) -> bool:
    s = f'{type(exc).__name__}: {exc}'.lower()
    return any(m in s for m in (
        'openvino_tokenizers', 'cannot add extension', 'cannot load library',
        'error 126', 'error 127',
    ))


class OpenVINOWhisperProvider:
    """Whisper on Intel GPU/NPU using OpenVINO GenAI.

    v3.0 adds resilient runtime fallback. On some Intel NPU/driver combinations
    (including Arrow Lake NPU 3720) a model can compile successfully but fail
    on the first generate() with a Level Zero invalid-argument error. We retry
    NPU once in compatibility mode and then continue the *same audio chunk* on
    Intel GPU, finally OpenVINO CPU, instead of aborting the whole job.
    """

    def __init__(self, model_name='large-v3', device='NPU', local_model_dir='', progress_cb=None):
        from app.core.openvino_runtime import prepare_openvino_runtime
        prepare_openvino_runtime(strict=True)
        import openvino as ov
        import openvino_genai as ov_genai

        self.ov = ov
        self.ov_genai = ov_genai
        self.requested_device = device.upper()
        self.device = self.requested_device
        self.model_name = model_name
        self.progress_cb = progress_cb
        self.core = ov.Core()
        self.available = [str(d).upper() for d in self.core.available_devices]
        if not any(d.startswith(self.device) for d in self.available):
            raise RuntimeError(f'OpenVINO 找不到 {self.device}。目前裝置：{self.core.available_devices}')

        self.model_dir = ensure_openvino_model(model_name, local_model_dir, progress_cb)
        self.compat_retry_used = False
        self.fallback_history: list[str] = []
        try:
            self.pipe = self._build_pipe(self.device, compat=False)
        except Exception as first:
            if self.device == 'NPU' and not _is_openvino_loader_error(first):
                if progress_cb:
                    progress_cb(15, 'NPU 初次編譯失敗；正在套用相容模式重試…')
                try:
                    os.environ['DISABLE_OPENVINO_GENAI_NPU_L0'] = '1'
                    self.pipe = self._build_pipe('NPU', compat=True)
                    self.compat_retry_used = True
                except Exception as second:
                    reason = f'NPU 編譯失敗（{type(second).__name__}: {str(second).splitlines()[0][:160]}）'
                    if self._device_exists('GPU'):
                        self._switch_device('GPU', reason, progress_cb)
                    else:
                        self._switch_device('CPU', reason, progress_cb)
            elif self.device == 'GPU' and not _is_openvino_loader_error(first):
                reason = f'Intel GPU 編譯失敗（{type(first).__name__}: {str(first).splitlines()[0][:160]}）'
                self._switch_device('CPU', reason, progress_cb)
            else:
                raise
        if progress_cb:
            progress_cb(20, f'OpenVINO {self.device} 已就緒')

    def _supported_props(self, device: str) -> set[str]:
        try:
            vals = self.core.get_property(device, 'SUPPORTED_PROPERTIES')
            return {str(v) for v in vals}
        except Exception:
            return set()

    def _build_pipe(self, device: str, compat: bool = False):
        device = device.upper()
        cache_dir = str(_model_root(self.model_name) / f'compiled_cache_{device.lower()}')
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        kwargs = {'CACHE_DIR': cache_dir}

        if device == 'NPU':
            props = self._supported_props('NPU')
            # On OpenVINO 2026.1+, Compiler-in-Plugin is preferred. For the
            # compatibility retry we explicitly request it where supported.
            if compat and ('NPU_COMPILER_TYPE' in props or not props):
                kwargs['NPU_COMPILER_TYPE'] = 'PLUGIN'
            if 'NPU_RUN_INFERENCES_SEQUENTIALLY' in props:
                kwargs['NPU_RUN_INFERENCES_SEQUENTIALLY'] = True

        if self.progress_cb:
            mode = '（相容模式）' if compat else ''
            self.progress_cb(15, f'正在編譯 Whisper 至 {device}{mode}；第一次可能較久…')
        try:
            return self.ov_genai.WhisperPipeline(self.model_dir, device, **kwargs)
        except Exception as exc:
            if _is_openvino_loader_error(exc):
                raise RuntimeError(
                    'OpenVINO Tokenizers DLL 無法載入；這是封裝/ABI 問題，不是 NPU/GPU 故障。'
                    f'\n原始錯誤：{type(exc).__name__}: {exc}'
                ) from exc
            raise

    def _device_exists(self, prefix: str) -> bool:
        p = prefix.upper()
        return any(d.startswith(p) for d in self.available)

    def _switch_device(self, new_device: str, reason: str, progress_cb=None, compat: bool = False):
        old_device = str(getattr(self, 'device', '')).upper()
        if old_device in ('NPU','GPU'):
            record_accelerator_result('openvino_' + old_device.lower(), False, reason)
        new_device = new_device.upper()
        cb = progress_cb or self.progress_cb
        if cb:
            cb(25, f'{reason}；改用 OpenVINO {new_device} 繼續同一區段…')
        self.pipe = self._build_pipe(new_device, compat=compat)
        self.device = new_device
        self.fallback_history.append(f'{reason} → {new_device}')

    def _generate_once_with_heartbeat(self, audio, kwargs, progress_cb=None):
        """Run a blocking OpenVINO generate call with a GUI heartbeat.

        OpenVINO GenAI Whisper does not expose reliable token-level progress on
        all devices. v3.0 therefore emits an indeterminate progress heartbeat
        every 2 seconds so the GUI can show that the call is still alive and
        how long it has been waiting for the engine.
        """
        done = threading.Event()
        started = time.monotonic()
        device = self.device

        def beat():
            while not done.wait(HEARTBEAT_SECONDS):
                if progress_cb:
                    elapsed = int(time.monotonic() - started)
                    mm, ss = divmod(elapsed, 60)
                    hh, mm = divmod(mm, 60)
                    stamp = f'{hh:02d}:{mm:02d}:{ss:02d}' if hh else f'{mm:02d}:{ss:02d}'
                    progress_cb(-1, f'OpenVINO {device} 推論中｜已執行 {stamp}｜程式仍在等待 OpenVINO 引擎回傳結果')

        t = threading.Thread(target=beat, daemon=True, name='junba-openvino-heartbeat')
        t.start()
        try:
            return self.pipe.generate(audio, **kwargs)
        finally:
            done.set()

    def _generate_with_fallback(self, audio, kwargs, progress_cb=None):
        try:
            return self._generate_once_with_heartbeat(audio, kwargs, progress_cb)
        except Exception as first:
            # NPU runtime failures on some Arrow Lake/Lunar Lake driver/runtime
            # combinations happen only at generate(), after successful compile.
            if self.device == 'NPU' and _is_npu_runtime_error(first):
                if progress_cb:
                    progress_cb(25, 'Intel NPU 執行期回傳無效參數；正在套用 NPU 相容模式重試…')
                try:
                    os.environ['DISABLE_OPENVINO_GENAI_NPU_L0'] = '1'
                    self.pipe = self._build_pipe('NPU', compat=True)
                    self.compat_retry_used = True
                    return self._generate_once_with_heartbeat(audio, kwargs, progress_cb)
                except Exception as second:
                    reason = (
                        'Intel NPU 相容模式仍失敗'
                        f'（{type(second).__name__}: {str(second).splitlines()[0][:160]}）'
                    )
                    if self._device_exists('GPU'):
                        self._switch_device('GPU', reason, progress_cb)
                        try:
                            return self._generate_once_with_heartbeat(audio, kwargs, progress_cb)
                        except Exception as gpu_error:
                            gpu_reason = (
                                'Intel GPU 也無法完成此區段'
                                f'（{type(gpu_error).__name__}: {str(gpu_error).splitlines()[0][:160]}）'
                            )
                            self._switch_device('CPU', gpu_reason, progress_cb)
                            return self._generate_once_with_heartbeat(audio, kwargs, progress_cb)
                    self._switch_device('CPU', reason, progress_cb)
                    return self._generate_once_with_heartbeat(audio, kwargs, progress_cb)

            # Explicit Intel GPU can also fall back to OpenVINO CPU so a long
            # transcription is not lost just because the accelerator rejects a graph.
            if self.device == 'GPU':
                reason = f'Intel GPU 推論失敗（{type(first).__name__}: {str(first).splitlines()[0][:160]}）'
                self._switch_device('CPU', reason, progress_cb)
                return self._generate_once_with_heartbeat(audio, kwargs, progress_cb)
            raise

    def transcribe(self, path: str, language: str | None = None, stop_flag=None,
                   progress_cb=None, wait_cb=None) -> TranscriptResult:
        if wait_cb:
            wait_cb()
        if stop_flag and stop_flag():
            return TranscriptResult('', [], engine=f'OpenVINO Whisper ({self.device})')
        audio = _decode_16k_mono(path, progress_cb)
        if len(audio) == 0:
            raise RuntimeError('音訊解碼後沒有資料')
        if progress_cb:
            progress_cb(25, f'OpenVINO {self.device} 推論準備完成')
            progress_cb(-1, f'OpenVINO {self.device} 推論中｜程式等待 OpenVINO 引擎回傳')
        kwargs = {'task': 'transcribe', 'return_timestamps': True}
        if language and language != 'auto':
            kwargs['language'] = language

        result = self._generate_with_fallback(audio, kwargs, progress_cb)
        text = ''
        try:
            texts = list(result.texts)
            text = texts[0] if texts else ''
        except Exception:
            text = str(result)
        segs: list[Segment] = []
        try:
            chunks = result.chunks or []
        except Exception:
            chunks = []
        for c in chunks:
            txt = str(getattr(c, 'text', '') or '').strip()
            if txt:
                segs.append(Segment(float(getattr(c, 'start_ts', 0.0)),
                                    float(getattr(c, 'end_ts', 0.0)), txt))
        if not segs and text.strip():
            segs = [Segment(0.0, 0.0, text.strip())]
        if progress_cb:
            progress_cb(100, f'OpenVINO {self.device} 轉錄完成')
        lang = getattr(result, 'language', None)
        fallback = '；'.join(self.fallback_history)
        engine = f'OpenVINO GenAI Whisper ({self.device})'
        if fallback:
            engine += f' [自動備援：{fallback}]'
        if self.device in ('NPU','GPU'):
            record_accelerator_result('openvino_' + self.device.lower(), True, 'OpenVINO 區段推論成功')
        elif self.device == 'CPU':
            record_accelerator_result('cpu', True, 'OpenVINO CPU 區段推論成功')
        return TranscriptResult(text.strip(), segs, lang, engine)
