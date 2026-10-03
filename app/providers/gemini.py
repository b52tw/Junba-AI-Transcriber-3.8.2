from __future__ import annotations
import mimetypes
import os
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from app.core.models import Segment, TranscriptResult


def _seconds(value) -> float:
    if value is None:
        return 0.0
    s = str(value).strip()
    if s.endswith('s'):
        s = s[:-1]
    try:
        return float(s)
    except Exception:
        return 0.0


@contextmanager
def ascii_upload_alias(path: str):
    """Create an ASCII-basename alias for SDK multipart upload."""
    src = Path(path)
    suffix = src.suffix.lower()
    if not suffix or any(ord(ch) > 127 for ch in suffix):
        suffix = '.m4a'
    alias_name = f'junba_audio_{uuid.uuid4().hex}{suffix}'
    alias = src.with_name(alias_name)
    created = False
    try:
        try:
            os.link(src, alias)
            created = True
        except Exception:
            try:
                shutil.copy2(src, alias)
                created = True
            except Exception:
                td = tempfile.mkdtemp(prefix='junba_gemini_')
                alias = Path(td) / alias_name
                shutil.copy2(src, alias)
                created = True
        yield str(alias)
    finally:
        if created:
            try:
                alias.unlink(missing_ok=True)
            except Exception:
                pass
        try:
            parent = alias.parent
            if parent.name.startswith('junba_gemini_'):
                shutil.rmtree(parent, ignore_errors=True)
        except Exception:
            pass


class GeminiProvider:
    TRANSCRIBE_MODEL = 'gemini-3.5-transcribe'
    POSTPROCESS_MODEL = 'gemini-3.8-flash'
    POSTPROCESS_FALLBACK_MODELS = ('gemini-3.7-flash', 'gemini-3.6-flash')
    RETRY_DELAYS = (3, 6, 12, 20)

    def __init__(self, api_key: str, model: str | None = None):
        if not api_key:
            raise ValueError('尚未設定 Gemini API Key')
        from google import genai
        self.client = genai.Client(api_key=api_key)
        self.model = model or self.TRANSCRIBE_MODEL

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        s = f'{type(exc).__name__}: {exc}'.upper()
        tokens = (
            '503', 'UNAVAILABLE', 'HIGH DEMAND', 'TEMPORARILY',
            '429', 'RESOURCE_EXHAUSTED', '500', '502', '504',
            'DEADLINE_EXCEEDED', 'TIMEOUT', 'TIMED OUT',
            'CONNECTION RESET', 'CONNECTION ABORTED', 'SERVICE UNAVAILABLE',
        )
        return any(t in s for t in tokens)

    @staticmethod
    def _sleep_with_heartbeat(seconds: int, progress_cb, message: str):
        seconds = max(1, int(seconds))
        for remain in range(seconds, 0, -1):
            if progress_cb:
                progress_cb(-1, f'{message}｜{remain} 秒後自動重試')
            time.sleep(1)

    def _call_with_retry(self, func, *, action: str, progress_cb=None, attempts: int = 5):
        """App-level retry above the SDK retry layer.

        The Google SDK already retries many 429/5xx responses. This outer layer is
        intentional: when a high-demand 503 still escapes the SDK, the GUI shows
        an explicit countdown instead of failing the whole transcription job.
        """
        last = None
        attempts = max(1, int(attempts))
        for attempt in range(1, attempts + 1):
            try:
                if attempt > 1 and progress_cb:
                    progress_cb(-1, f'{action}：第 {attempt}/{attempts} 次嘗試')
                return func()
            except Exception as exc:
                last = exc
                if not self._is_transient_error(exc) or attempt >= attempts:
                    raise
                delay = self.RETRY_DELAYS[min(attempt - 1, len(self.RETRY_DELAYS) - 1)]
                short = str(exc).replace('\n', ' ')
                if len(short) > 160:
                    short = short[:157] + '...'
                self._sleep_with_heartbeat(
                    delay, progress_cb,
                    f'{action}遇到暫時性雲端忙碌／限流（{short}）'
                )
        raise last

    def transcribe(self, path: str, diarization=True, timestamps=True, smart=False,
                   language_codes=None, progress_cb=None) -> TranscriptResult:
        original_mime = mimetypes.guess_type(path)[0] or 'audio/mp4'
        uploaded = None
        try:
            if progress_cb:
                progress_cb(5, '建立安全上傳檔名')
            with ascii_upload_alias(path) as upload_path:
                if progress_cb:
                    progress_cb(10, '上傳音檔至 Google')
                uploaded = self._call_with_retry(
                    lambda: self.client.files.upload(file=upload_path),
                    action='Gemini 音檔上傳', progress_cb=progress_cb,
                )
            if progress_cb:
                progress_cb(35, 'Google 已收到音檔，開始轉錄')
            if smart:
                mode = 'smart'
            else:
                mode = {'type': 'verbatim'}
                if diarization:
                    mode['diarization_mode'] = 'speaker'
                if timestamps:
                    mode['timestamp_granularities'] = ['word']
            cfg = {'transcription_config': {'language_codes': language_codes or [], 'mode': mode}}
            interaction = self._call_with_retry(
                lambda: self.client.interactions.create(
                    model=self.model,
                    input=[{
                        'type': 'audio',
                        'uri': uploaded.uri,
                        'mime_type': uploaded.mime_type or original_mime,
                    }],
                    generation_config=cfg,
                ),
                action='Gemini 音訊轉錄', progress_cb=progress_cb,
            )
            if progress_cb:
                progress_cb(90, '解析 Gemini 回傳內容')
            text = getattr(interaction, 'output_text', '') or ''
            words = []
            for step in getattr(interaction, 'steps', []) or []:
                for content in getattr(step, 'content', []) or []:
                    for ann in getattr(content, 'annotations', []) or []:
                        if getattr(ann, 'type', None) == 'word_info':
                            words.append(ann)
            segs = self._group_words(words)
            if not segs and text:
                segs = [Segment(0.0, 0.0, text)]
            if progress_cb:
                progress_cb(100, 'Gemini 轉錄完成')
            return TranscriptResult(text=text, segments=segs, language=None, engine=self.model)
        except UnicodeEncodeError as e:
            raise RuntimeError(
                'Google 音檔上傳遇到檔名字元編碼錯誤。v3.7 已使用 ASCII 暫存別名；'
                f'若仍出現此訊息，請回報完整錯誤：{e}'
            ) from e
        except Exception as e:
            if self._is_transient_error(e):
                raise RuntimeError(
                    'Google Gemini 目前暫時高負載或服務忙碌；程式已自動重試仍未成功。'
                    '已完成的本機／快取內容不會被刪除，稍後可重新開始續跑。'
                ) from e
            raise
        finally:
            if uploaded is not None:
                try:
                    name = getattr(uploaded, 'name', None)
                    if name:
                        self.client.files.delete(name=name)
                except Exception:
                    pass

    @staticmethod
    def _group_words(words) -> list[Segment]:
        result: list[Segment] = []
        current = None
        for w in words:
            speaker = getattr(w, 'speaker', None) or 'spk_1'
            start = _seconds(getattr(w, 'start_offset', None))
            end = _seconds(getattr(w, 'end_offset', None))
            txt = str(getattr(w, 'text', '') or '').strip()
            if not txt:
                continue
            if current and current.speaker == speaker and start - current.end <= 1.2:
                current.end = end
                if txt[:1] in '，。！？,.!?;；:：':
                    current.text += txt
                else:
                    current.text += (' ' if current.text and current.text[-1:].isascii() and txt[:1].isascii() else '') + txt
            else:
                current = Segment(start, end, txt, speaker)
                result.append(current)
        return result

    def postprocess_text(self, text: str, target='繁體中文', progress_cb=None) -> str:
        if not text.strip():
            return text
        prompt = (
            '請整理以下語音逐字稿。保留原意，不自行補造事實；修正明顯標點與斷句，'
            f'輸出使用{target}。若有講者標記請保留。只回傳整理後逐字稿，不要加入額外說明。\n\n{text}'
        )
        models = (self.POSTPROCESS_MODEL,) + tuple(self.POSTPROCESS_FALLBACK_MODELS)
        last = None
        for mi, model in enumerate(models):
            try:
                if progress_cb:
                    label = '送出文字整理要求' if mi == 0 else f'改用備援模型 {model} 整理文字'
                    progress_cb(10 if mi == 0 else -1, label)
                # Primary model gets the full retry budget; fallback models get a
                # shorter budget so the user is not left waiting several minutes.
                attempts = 5 if mi == 0 else 2
                r = self._call_with_retry(
                    lambda _m=model: self.client.models.generate_content(model=_m, contents=prompt),
                    action=f'Gemini 文字整理（{model}）',
                    progress_cb=progress_cb,
                    attempts=attempts,
                )
                if progress_cb:
                    progress_cb(100, f'Gemini 文字整理完成（{model}）')
                return getattr(r, 'text', '') or text
            except Exception as exc:
                last = exc
                if not self._is_transient_error(exc):
                    raise
                if progress_cb and mi < len(models) - 1:
                    progress_cb(-1, f'{model} 暫時不可用，準備切換備援模型…')
        raise RuntimeError(
            'Google Gemini 文字整理服務目前高負載（503/UNAVAILABLE）。'
            '已自動重試並切換備援模型仍未成功。'
        ) from last
