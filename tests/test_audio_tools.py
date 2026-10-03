from pathlib import Path
import subprocess
from app.core.audio_tools import (
    ffmpeg_exe, audio_duration_seconds, merge_audio,
    ensure_split_audio, split_output_dir,
)
from app.providers.gemini import ascii_upload_alias


def make_tone(path: Path, seconds=1):
    subprocess.run([
        ffmpeg_exe(), '-y', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000',
        '-t', str(seconds), '-c:a', 'pcm_s16le', str(path)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)


def test_ffmpeg_duration_and_merge(tmp_path):
    a = tmp_path / 'a.wav'; b = tmp_path / 'b.wav'; out = tmp_path / 'merged.m4a'
    make_tone(a, 1); make_tone(b, 1)
    assert 0.7 <= audio_duration_seconds(str(a)) <= 1.3
    merge_audio([str(a), str(b)], str(out))
    assert out.exists() and out.stat().st_size > 100
    assert audio_duration_seconds(str(out)) >= 1.5


def test_unicode_filename_split_and_reuse(tmp_path):
    src = tmp_path / '20260917_(四)_效率評估與生產管理 林泰宇.wav'
    make_tone(src, 3)
    out_dir = split_output_dir(str(tmp_path / 'out'), str(src), 1)
    chunks = ensure_split_audio(str(src), out_dir, 1)
    assert chunks and all(Path(x).exists() for x in chunks)
    mtimes = [Path(x).stat().st_mtime_ns for x in chunks]
    chunks2 = ensure_split_audio(str(src), out_dir, 1)
    assert chunks2 == chunks
    assert [Path(x).stat().st_mtime_ns for x in chunks2] == mtimes


def test_gemini_ascii_upload_alias_for_unicode_filename(tmp_path):
    src = tmp_path / '20260917_(四)_效率評估.m4a'
    src.write_bytes(b'abc123')
    with ascii_upload_alias(str(src)) as alias:
        p = Path(alias)
        # This is the critical condition for multipart filename headers.
        p.name.encode('ascii')
        assert p.read_bytes() == b'abc123'
        assert p != src
    assert not p.exists()


def test_gemini_provider_uses_ascii_alias_without_network(tmp_path):
    from types import SimpleNamespace
    from app.providers.gemini import GeminiProvider

    src = tmp_path / '20260917_(四)_效率評估與生產管理.m4a'
    src.write_bytes(b'fake-audio')

    class FakeFiles:
        def upload(self, file):
            Path(file).name.encode('ascii')
            assert Path(file).read_bytes() == b'fake-audio'
            return SimpleNamespace(uri='files/fake', mime_type='audio/mp4', name='files/fake')
        def delete(self, name):
            assert name == 'files/fake'

    class FakeInteractions:
        def create(self, **kwargs):
            return SimpleNamespace(output_text='測試成功', steps=[])

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.client = SimpleNamespace(files=FakeFiles(), interactions=FakeInteractions())
    provider.model = GeminiProvider.TRANSCRIBE_MODEL
    result = provider.transcribe(str(src), diarization=False, timestamps=False)
    assert result.text == '測試成功'


def test_gemini_diarization_and_timestamps_request_shape(tmp_path):
    from types import SimpleNamespace
    from app.providers.gemini import GeminiProvider

    src = tmp_path / 'meeting.m4a'
    src.write_bytes(b'fake-audio')
    captured = {}

    class FakeFiles:
        def upload(self, file):
            return SimpleNamespace(uri='files/fake', mime_type='audio/m4a', name='files/fake')
        def delete(self, name):
            pass

    class FakeInteractions:
        def create(self, **kwargs):
            captured.update(kwargs)
            annotations = [
                SimpleNamespace(type='word_info', text='您好', speaker='spk_1', start_offset='0.1s', end_offset='0.4s'),
                SimpleNamespace(type='word_info', text='老師', speaker='spk_2', start_offset='0.5s', end_offset='0.8s'),
            ]
            content = SimpleNamespace(annotations=annotations)
            step = SimpleNamespace(content=[content])
            return SimpleNamespace(output_text='您好老師', steps=[step])

    provider = GeminiProvider.__new__(GeminiProvider)
    provider.client = SimpleNamespace(files=FakeFiles(), interactions=FakeInteractions())
    provider.model = GeminiProvider.TRANSCRIBE_MODEL
    result = provider.transcribe(str(src), diarization=True, timestamps=True, smart=False)
    mode = captured['generation_config']['transcription_config']['mode']
    assert mode['type'] == 'verbatim'
    assert mode['diarization_mode'] == 'speaker'
    assert mode['timestamp_granularities'] == ['word']
    assert [s.speaker for s in result.segments] == ['spk_1', 'spk_2']


def test_traditional_taiwan_output_conversion():
    from app.core.text_normalize import to_traditional_taiwan
    src = '请问这个线上版本，大陆那边一搜就有，是不是鼓励盗版。绩效设定方式。'
    out = to_traditional_taiwan(src)
    assert '線上' in out
    assert '大陸' in out
    assert '鼓勵' in out
    assert '盜版' in out
    assert '績效' in out
    assert '設定' in out


def test_partial_export_is_openable(tmp_path):
    from docx import Document
    from app.core.models import TranscriptResult, Segment
    from app.exporters.exporters import export_all

    result = TranscriptResult(
        text='第一段\n第二段',
        segments=[
            Segment(0.0, 1.0, '第一段', '講者1'),
            Segment(1.0, 2.0, '第二段', '講者2'),
        ],
        engine='Google Gemini（中止版／部分結果）',
    )
    base = tmp_path / '會議_中止版_逐字稿'
    paths = export_all(
        result,
        str(base),
        ['docx', 'txt', 'srt', 'vtt'],
        note='【中止版】只包含停止前已完成的內容。',
    )
    assert len(paths) == 4
    docx_path = base.with_suffix('.docx')
    txt_path = base.with_suffix('.txt')
    srt_path = base.with_suffix('.srt')
    vtt_path = base.with_suffix('.vtt')
    for p in (docx_path, txt_path, srt_path, vtt_path):
        assert p.exists() and p.stat().st_size > 0
    # python-docx can reopen the generated Word document: this verifies that the
    # partial output is a valid, user-openable DOCX rather than a half-written file.
    doc = Document(docx_path)
    all_text = '\n'.join(p.text for p in doc.paragraphs)
    assert '中止版' in all_text
    assert '第一段' in all_text
    assert '中止版' in txt_path.read_text(encoding='utf-8-sig')
    assert '-->' in srt_path.read_text(encoding='utf-8-sig')
    assert 'NOTE' in vtt_path.read_text(encoding='utf-8-sig')


def test_openvino_audio_decode_16k_mono(tmp_path):
    from app.providers.openvino_whisper import _decode_16k_mono
    src = tmp_path / 'openvino_decode_test.m4a'
    subprocess.run([
        ffmpeg_exe(), '-y', '-f', 'lavfi', '-i', 'sine=frequency=660:sample_rate=44100',
        '-t', '1', '-c:a', 'aac', str(src)
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    samples = _decode_16k_mono(str(src))
    assert 14000 <= len(samples) <= 18000
    assert max(abs(float(x)) for x in samples[:5000]) <= 1.0
