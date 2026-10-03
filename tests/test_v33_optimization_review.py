from pathlib import Path

from app.core import hardware
from app.core.models import TranscriptResult, Segment
from app.exporters.exporters import export_all


def test_legacy_intel_igpu_detected():
    assert hardware.is_legacy_intel_igpu(['Intel(R) Iris(R) Xe Graphics']) is True
    assert hardware.is_legacy_intel_igpu(['Intel(R) Arc(TM) 130T GPU']) is False


def test_recommended_split_long_audio(monkeypatch):
    monkeypatch.setattr(hardware, 'recommended_acceleration', lambda policy='adaptive': 'cpu')
    assert hardware.recommended_split_minutes('混合模式', 82 * 60, 'adaptive', 'auto') == 3
    assert hardware.recommended_split_minutes('Google Gemini', 82 * 60, 'adaptive', 'auto') == 10
    assert hardware.recommended_split_minutes('混合模式', 5 * 60, 'adaptive', 'auto') == 0


def test_review_export_contains_clickable_transcript(tmp_path):
    audio = tmp_path / 'sample.wav'
    audio.write_bytes(b'RIFF0000WAVE')
    result = TranscriptResult(
        '你好，這是一段測試。',
        [Segment(1.0, 3.5, '你好'), Segment(4.0, 6.0, '這是一段測試', '講者1')],
        'zh', 'test-engine'
    )
    base = tmp_path / 'sample_逐字稿'
    paths = export_all(result, str(base), ['docx', 'review'], source_audio=str(audio))
    htmls = [Path(x) for x in paths if x.endswith('.html')]
    vtts = [Path(x) for x in paths if x.endswith('.vtt')]
    assert htmls and htmls[0].exists()
    assert vtts and vtts[0].exists()
    text = htmls[0].read_text(encoding='utf-8')
    assert '錄音與逐字稿核對' in text
    assert 'audio.currentTime=s.start' in text
    assert '這是一段測試' in text
    assert 'file:///' in text or 'file://' in text


def test_record_rtf_updates_profile(monkeypatch, tmp_path):
    p = tmp_path / 'hardware_profile.json'
    monkeypatch.setattr(hardware, '_profile_path', lambda: p)
    hardware.record_accelerator_result('cpu', True, 'ok', audio_seconds=60, elapsed_seconds=30)
    d = hardware.load_hardware_profile()
    item = d['stats']['cpu']
    assert item['success'] == 1
    assert 0.49 <= item['avg_rtf'] <= 0.51
