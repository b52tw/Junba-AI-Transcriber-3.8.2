from pathlib import Path

from app.core.models import TranscriptResult, Segment
from app.exporters.exporters import export_all


def test_v38_review_player_contains_ktv_and_import_controls(tmp_path: Path):
    result = TranscriptResult(
        text='這是 AI 整理後的全文。第二句內容。',
        segments=[
            Segment(0.0, 2.5, '第一段原始逐字稿', '講者1'),
            Segment(2.5, 5.0, '第二段原始逐字稿', '講者2'),
        ],
        language='zh',
        engine='混合模式',
    )
    out = export_all(result, str(tmp_path/'demo_逐字稿'), ['review'], source_audio=str(tmp_path/'demo.m4a'))
    html_path = next(Path(x) for x in out if x.endswith('.html'))
    text = html_path.read_text(encoding='utf-8')
    assert '完整全文 KTV 同步' in text
    assert '加入其他翻譯／逐字稿' in text
    assert 'accept=".txt,.srt,.vtt,.json' in text
    assert '匯出目前文字軌 VTT' in text
    assert 'AI 整理全文（估算同步）' in text
    assert 'function parseTimedText' in text
    assert 'function alignPlainText' in text
    assert '下方雙稿：開' in text
    assert '第一段原始逐字稿' in text


def test_v38_word_timeline_still_kept(tmp_path: Path):
    result = TranscriptResult(
        text='整理全文',
        segments=[Segment(1.0, 3.0, '逐字內容', 'A')],
        engine='混合模式',
    )
    out = export_all(result, str(tmp_path/'demo'), ['docx'])
    p = Path(out[0])
    assert p.exists() and p.stat().st_size > 0
