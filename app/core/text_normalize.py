from __future__ import annotations
from app.core.models import TranscriptResult

# Fallback covers common Simplified Chinese characters seen in Taiwan meeting transcripts.
# The packaged Windows build includes opencc-python-reimplemented, so this table is only
# a safety net if OpenCC cannot be imported at runtime.
_FALLBACK = str.maketrans({
    '线':'線','这':'這','个':'個','为':'為','请':'請','问':'問','电':'電','点':'點','后':'後','里':'裡','吗':'嗎','们':'們','会':'會',
    '绩':'績','效':'效','设':'設','励':'勵','盗':'盜','陆':'陸','关':'關','联':'聯','么':'麼','还':'還','让':'讓','过':'過',
    '从':'從','讲':'講','听':'聽','录':'錄','转':'轉','译':'譯','问':'問','题':'題','开':'開',
    '发':'發','认':'認','识':'識','进':'進','处':'處','理':'理','实':'實','际':'際','档':'檔',
    '网':'網','总':'總','来':'來','对':'對','类':'類','请':'請','简':'簡','单':'單','时':'時',
    '间':'間','与':'與','项':'項','导':'導','数':'數','据':'據','资':'資','业':'業','务':'務',
    '区':'區','别':'別','该':'該','经':'經','济':'濟','产':'產','当':'當','台':'台','灣':'灣',
    '边':'邊','陆':'陸','剧':'劇','盗':'盜','版':'版','搜':'搜','鼓':'鼓','励':'勵','较':'較',
})


def to_traditional_taiwan(text: str) -> str:
    """Convert Chinese text to Taiwan Traditional Chinese while preserving other scripts.

    Uses OpenCC s2twp when available (Simplified -> Traditional + Taiwan phrase mapping).
    """
    if not text:
        return text
    try:
        from opencc import OpenCC
        return OpenCC('s2twp').convert(text)
    except Exception:
        return text.translate(_FALLBACK)


def normalize_result_traditional(result: TranscriptResult) -> TranscriptResult:
    result.text = to_traditional_taiwan(result.text)
    for seg in result.segments:
        seg.text = to_traditional_taiwan(seg.text)
    return result
