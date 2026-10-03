from pathlib import Path
import json
import subprocess
import time
import pytest

from app.core.audio_tools import (
    ffmpeg_exe, ensure_split_audio, AudioOperationCancelled,
)


def make_m4a(path: Path, seconds: int = 12):
    cmd = [
        ffmpeg_exe(), '-y', '-hide_banner', '-loglevel', 'error',
        '-f', 'lavfi', '-i', f'sine=frequency=440:duration={seconds}',
        '-c:a', 'aac', '-b:a', '96k', str(path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_fast_copy_split_is_used(tmp_path):
    src = tmp_path / '中文測試.m4a'
    make_m4a(src, 12)
    out = tmp_path / 'chunks'
    events = []
    started = time.monotonic()
    chunks = ensure_split_audio(str(src), str(out), 1, event_cb=events.append)
    elapsed = time.monotonic() - started
    assert chunks
    manifest = json.loads((out / 'split_manifest.json').read_text(encoding='utf-8'))
    assert manifest['method'] == 'fast-copy'
    assert any('無重編碼' in e for e in events)
    # This is not a strict benchmark; it only catches accidental real-time encoding.
    assert elapsed < 8


def test_cancel_before_split_never_leaves_manifest(tmp_path):
    src = tmp_path / 'cancel.m4a'
    make_m4a(src, 3)
    out = tmp_path / 'chunks'
    with pytest.raises(AudioOperationCancelled):
        ensure_split_audio(str(src), str(out), 1, cancel_cb=lambda: True)
    assert not (out / 'split_manifest.json').exists()
