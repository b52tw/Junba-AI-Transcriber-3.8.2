import pytest
pytest.importorskip('PySide6')
from pathlib import Path
from docx import Document
from app.core.worker import TranscribeWorker


def make_worker(tmp_path):
    src = tmp_path / '測試音檔.m4a'
    src.write_bytes(b'placeholder')
    w = TranscribeWorker(
        files=[str(src)], output_dir=str(tmp_path), mode='離線 Whisper', split_minutes=3,
        model_name='small', local_model_dir='', language='auto', api_key='',
        diarization=False, timestamps=False, smart=False, traditional_output=True,
        formats=['docx'], acceleration='cpu', optimize=True, review_player=True,
    )
    w._current_source_path = str(src)
    return w


def test_stop_is_idempotent(tmp_path):
    w = make_worker(tmp_path)
    assert w.stop() is True
    assert w.stop() is False


def test_stop_before_transcription_still_exports_readable_docx(tmp_path):
    w = make_worker(tmp_path)
    paths = w._export_stop_placeholder()
    docx = [Path(x) for x in paths if x.lower().endswith('.docx')]
    assert docx and docx[0].exists()
    doc = Document(docx[0])
    text = '\n'.join(p.text for p in doc.paragraphs)
    assert '中止' in text
    assert '尚未進入辨識階段' in text
