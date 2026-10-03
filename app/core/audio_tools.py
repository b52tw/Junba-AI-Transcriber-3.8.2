from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
from app.core.subprocess_utils import hidden_process_kwargs
import tempfile
from pathlib import Path
import imageio_ffmpeg


def ffmpeg_exe() -> str:
    bundled = Path(__file__).resolve().parents[2] / 'tools' / ('ffmpeg.exe' if os.name == 'nt' else 'ffmpeg')
    if bundled.exists():
        return str(bundled)
    return imageio_ffmpeg.get_ffmpeg_exe()


def quick_duration_seconds(path: str) -> float:
    """Fast non-blocking-ish duration probe for the GUI file list.

    It intentionally avoids spawning FFmpeg. The worker performs the robust
    compatibility probe in the background after Start is pressed.
    """
    try:
        import av
        with av.open(path) as c:
            if c.duration:
                return float(c.duration) / 1_000_000.0
            for stream in c.streams.audio:
                if stream.duration is not None and stream.time_base is not None:
                    return float(stream.duration * stream.time_base)
    except Exception:
        return 0.0
    return 0.0


def audio_duration_seconds(path: str) -> float:
    try:
        import av
        with av.open(path) as c:
            if c.duration:
                return float(c.duration) / 1_000_000.0
            for s in c.streams.audio:
                if s.duration is not None and s.time_base is not None:
                    return float(s.duration * s.time_base)
    except Exception:
        pass
    try:
        import re
        r = subprocess.run([ffmpeg_exe(), '-i', path], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding='utf-8', errors='replace', timeout=15, **hidden_process_kwargs())
        m = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', r.stderr or '')
        if m:
            h, mi, sec = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + sec
    except Exception:
        pass
    return 0.0


class AudioOperationCancelled(RuntimeError):
    """Raised when the user requests a safe cancellation during FFmpeg work."""


SPLITTER_VERSION = 'v3.7-fastcopy-v1'


def _terminate_process(p: subprocess.Popen) -> None:
    if p.poll() is not None:
        return
    try:
        p.terminate()
        p.wait(timeout=1.5)
        return
    except Exception:
        pass
    try:
        p.kill()
        p.wait(timeout=1.5)
    except Exception:
        pass


def _run_ffmpeg_progress(cmd: list[str], duration: float, progress_cb=None, cancel_cb=None):
    """Run FFmpeg without a pipe deadlock and allow immediate safe cancellation.

    Older versions kept stderr in a separate PIPE but did not drain it until the
    process ended. A verbose/slow FFmpeg job can fill that pipe and appear stuck
    at one percentage. v3.7 merges stderr into stdout and consumes it from a
    reader thread while the main loop remains free to observe the Stop button.
    """
    import queue
    import threading

    if cancel_cb and cancel_cb():
        raise AudioOperationCancelled('工作已取消')

    full = [
        cmd[0], '-hide_banner', '-loglevel', 'error', '-nostats',
        '-progress', 'pipe:1',
    ] + cmd[1:]
    p = subprocess.Popen(
        full,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
        bufsize=1,
        **hidden_process_kwargs(),
    )
    q: queue.Queue[str | None] = queue.Queue()
    captured: list[str] = []

    def _reader():
        try:
            if p.stdout:
                for line in iter(p.stdout.readline, ''):
                    q.put(line)
        finally:
            q.put(None)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    stream_done = False
    try:
        while True:
            if cancel_cb and cancel_cb():
                _terminate_process(p)
                raise AudioOperationCancelled('工作已取消')
            try:
                line = q.get(timeout=0.20)
            except queue.Empty:
                line = ''
            if line is None:
                stream_done = True
            elif line:
                stripped = line.strip()
                if len(captured) < 2000:
                    captured.append(stripped)
                if stripped.startswith('out_time_us=') and progress_cb and duration > 0:
                    try:
                        sec = float(stripped.split('=', 1)[1]) / 1_000_000.0
                        progress_cb(max(0, min(99, int(sec / duration * 100))))
                    except Exception:
                        pass
                elif stripped == 'progress=end' and progress_cb:
                    progress_cb(100)
            rc = p.poll()
            if rc is not None and (stream_done or q.empty()):
                break
        rc = p.wait()
        if rc != 0:
            msg = '\n'.join(x for x in captured if x)[-4000:]
            raise RuntimeError(msg or f'ffmpeg 失敗，代碼 {rc}')
        if progress_cb:
            progress_cb(100)
    finally:
        _terminate_process(p)


def _source_signature(input_path: str, minutes: int) -> dict:
    p = Path(input_path)
    st = p.stat()
    return {
        'source': str(p.resolve()),
        'size': int(st.st_size),
        'mtime_ns': int(st.st_mtime_ns),
        'minutes': int(minutes),
        'splitter_version': SPLITTER_VERSION,
    }


def split_output_dir(output_root: str, input_path: str, minutes: int) -> str:
    """Visible split folder usable by this app and other transcription tools."""
    src = Path(input_path)
    digest = hashlib.sha1(str(src.resolve()).encode('utf-8')).hexdigest()[:8]
    short_stem = src.stem[:70] or 'audio'
    return str(Path(output_root) / '切割音檔' / f'{short_stem}_{minutes}min_{digest}')


def _cleanup_chunks(out_dir: str) -> None:
    out = Path(out_dir)
    for pat in ('*_chunk_*.m4a', '*_chunk_*.mp3', '*_chunk_*.aac', '*_chunk_*.wav', '*_chunk_*.flac', '*_chunk_*.ogg', '*_chunk_*.opus'):
        for old in out.glob(pat):
            try:
                old.unlink()
            except Exception:
                pass


def _fast_copy_extension(input_path: str) -> str | None:
    ext = Path(input_path).suffix.lower()
    if ext in {'.m4a', '.mp3', '.aac', '.wav', '.flac', '.ogg', '.opus'}:
        return ext
    return None


def _validate_chunks(paths: list[str]) -> bool:
    if not paths:
        return False
    for x in paths:
        p = Path(x)
        if not p.exists() or p.stat().st_size < 256:
            return False
        # Duration probing is cheap and catches malformed copy-segment outputs.
        if audio_duration_seconds(str(p)) <= 0:
            return False
    return True


def split_audio(input_path: str, out_dir: str, minutes: int, progress_cb=None, cancel_cb=None, event_cb=None) -> tuple[list[str], str]:
    """Split audio quickly, falling back only when stream-copy is incompatible.

    Preferred path: container/codec stream copy (-c:a copy). This normally runs
    dozens to hundreds of times faster than real time and does not use GPU/NPU.
    Fallback: decode once to 16 kHz mono PCM WAV and segment in the same pass.
    """
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    stem = Path(input_path).stem
    _cleanup_chunks(out_dir)
    duration = audio_duration_seconds(input_path)
    segment_seconds = str(max(1, minutes) * 60)

    ext = _fast_copy_extension(input_path)
    if ext:
        pattern = str(Path(out_dir) / f'{stem}_chunk_%03d{ext}')
        cmd = [
            ffmpeg_exe(), '-y', '-i', input_path,
            '-map', '0:a:0', '-vn', '-c:a', 'copy',
            '-f', 'segment', '-segment_time', segment_seconds,
            '-reset_timestamps', '1', '-avoid_negative_ts', 'make_zero', pattern,
        ]
        try:
            if event_cb:
                event_cb('快速切割：使用無重編碼 stream copy（CPU/磁碟；GPU/NPU 不參與）')
            _run_ffmpeg_progress(cmd, duration, progress_cb, cancel_cb)
            chunks = [str(p) for p in sorted(Path(out_dir).glob(f'*_chunk_*{ext}'))]
            if _validate_chunks(chunks):
                return chunks, 'fast-copy'
            raise RuntimeError('快速切割輸出驗證失敗')
        except AudioOperationCancelled:
            _cleanup_chunks(out_dir)
            raise
        except Exception as exc:
            _cleanup_chunks(out_dir)
            if event_cb:
                event_cb(f'快速切割不相容，改用 PCM 相容切割：{type(exc).__name__}')

    # Portable fallback: decode/re-sample only once; no AAC encode step.
    pattern = str(Path(out_dir) / f'{stem}_chunk_%03d.wav')
    cmd = [
        ffmpeg_exe(), '-y', '-i', input_path,
        '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le',
        '-f', 'segment', '-segment_time', segment_seconds,
        '-reset_timestamps', '1', pattern,
    ]
    try:
        if event_cb:
            event_cb('相容切割：轉 16kHz mono PCM WAV（CPU/磁碟；GPU/NPU 不參與）')
        _run_ffmpeg_progress(cmd, duration, progress_cb, cancel_cb)
    except AudioOperationCancelled:
        _cleanup_chunks(out_dir)
        raise
    except RuntimeError as e:
        _cleanup_chunks(out_dir)
        raise RuntimeError('音檔切割失敗：\n' + str(e))
    chunks = [str(p) for p in sorted(Path(out_dir).glob('*_chunk_*.wav'))]
    if not _validate_chunks(chunks):
        _cleanup_chunks(out_dir)
        raise RuntimeError('音檔切割完成但找不到有效區段檔。')
    return chunks, 'pcm-wav'


def ensure_split_audio(input_path: str, out_dir: str, minutes: int, progress_cb=None, cancel_cb=None, event_cb=None) -> list[str]:
    """Reuse previous v3.7 split output when source metadata is unchanged."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / 'split_manifest.json'
    sig = _source_signature(input_path, minutes)
    if manifest.exists():
        try:
            old = json.loads(manifest.read_text(encoding='utf-8'))
            chunks = [str(out / x) for x in old.get('chunks', [])]
            if old.get('signature') == sig and chunks and _validate_chunks(chunks):
                if event_cb:
                    event_cb(f'沿用既有切割快取：{old.get("method", "unknown")}｜{len(chunks)} 段')
                if progress_cb:
                    progress_cb(100)
                return chunks
        except Exception:
            pass
    try:
        chunks, method = split_audio(input_path, out_dir, minutes, progress_cb, cancel_cb, event_cb)
    except AudioOperationCancelled:
        try:
            manifest.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    manifest.write_text(json.dumps({
        'signature': sig,
        'method': method,
        'chunks': [Path(x).name for x in chunks],
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    return chunks

def merge_audio(inputs: list[str], output_path: str, progress_cb=None, cancel_cb=None) -> str:
    if not inputs:
        raise ValueError('沒有可合併的音檔')
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    duration = sum(audio_duration_seconds(x) for x in inputs)
    with tempfile.TemporaryDirectory() as td:
        lst = Path(td) / 'concat.txt'
        with lst.open('w', encoding='utf-8') as f:
            for item in inputs:
                escaped = str(Path(item).resolve()).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")
        cmd = [
            ffmpeg_exe(), '-y', '-f', 'concat', '-safe', '0', '-i', str(lst),
            '-vn', '-c:a', 'aac', '-b:a', '128k', output_path,
        ]
        try:
            _run_ffmpeg_progress(cmd, duration, progress_cb, cancel_cb)
        except RuntimeError as e:
            if str(e) == '工作已取消':
                raise
            raise RuntimeError('音檔合併失敗：\n' + str(e))
    return output_path


def copy_or_prepare(input_path: str, work_dir: str) -> str:
    Path(work_dir).mkdir(parents=True, exist_ok=True)
    dst = Path(work_dir) / Path(input_path).name
    if Path(input_path).resolve() != dst.resolve():
        shutil.copy2(input_path, dst)
    return str(dst)


def probe_audio(path: str, decode_seconds: int = 4) -> dict:
    """Quick compatibility probe used before long transcription jobs.

    Returns a small dict instead of raising so the UI/worker can decide whether
    to normalize the source. The decode test uses the bundled FFmpeg and catches
    many M4A/container/codec edge cases that only show up on another PC.
    """
    p = Path(path)
    info = {
        'path': str(p), 'exists': p.exists(), 'size': 0, 'duration': 0.0,
        'decode_ok': False, 'error': '', 'needs_prepare': False,
    }
    if not p.exists():
        info['error'] = '找不到檔案'
        info['needs_prepare'] = True
        return info
    try:
        info['size'] = int(p.stat().st_size)
    except Exception:
        pass
    info['duration'] = audio_duration_seconds(str(p))
    try:
        cmd = [
            ffmpeg_exe(), '-v', 'error', '-t', str(max(1, int(decode_seconds))),
            '-i', str(p), '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000',
            '-f', 'null', '-',
        ]
        r = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=max(15, decode_seconds + 12), **hidden_process_kwargs(),
        )
        if r.returncode == 0:
            info['decode_ok'] = True
        else:
            info['error'] = (r.stderr or b'').decode('utf-8', errors='replace')[-1600:]
    except Exception as exc:
        info['error'] = f'{type(exc).__name__}: {exc}'
    info['needs_prepare'] = (not info['decode_ok']) or info['duration'] <= 0 or info['size'] <= 0
    return info


def _prepared_signature(input_path: str) -> dict:
    p = Path(input_path)
    st = p.stat()
    return {
        'source': str(p.resolve()),
        'size': int(st.st_size),
        'mtime_ns': int(st.st_mtime_ns),
        'format': 'wav-pcm16-16k-mono-v1',
    }


def prepare_compatible_audio(input_path: str, work_root: str, progress_cb=None, cancel_cb=None) -> str:
    """Create a cacheable 16 kHz mono PCM WAV for hard-to-read audio files.

    This is intentionally a fallback, not the default path. It makes cloud-sync
    files and unusual M4A/AAC/container variants much more portable across PCs.
    """
    src = Path(input_path)
    root = Path(work_root)
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(src.resolve()).encode('utf-8')).hexdigest()[:10]
    target = root / f'{src.stem[:70]}_{digest}_prepared.wav'
    manifest = root / f'{src.stem[:70]}_{digest}_prepared.json'
    sig = _prepared_signature(input_path)
    if target.exists() and manifest.exists():
        try:
            old = json.loads(manifest.read_text(encoding='utf-8'))
            if old.get('signature') == sig and target.stat().st_size > 1024:
                if progress_cb:
                    progress_cb(100)
                return str(target)
        except Exception:
            pass
    duration = audio_duration_seconds(input_path)
    cmd = [
        ffmpeg_exe(), '-y', '-i', input_path,
        '-map', '0:a:0', '-vn', '-ac', '1', '-ar', '16000',
        '-c:a', 'pcm_s16le', str(target),
    ]
    try:
        _run_ffmpeg_progress(cmd, duration, progress_cb, cancel_cb)
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    if not target.exists() or target.stat().st_size <= 1024:
        raise RuntimeError('相容音檔轉換完成，但輸出檔案無效。')
    manifest.write_text(json.dumps({'signature': sig}, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(target)
