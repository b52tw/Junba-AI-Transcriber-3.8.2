from app.core.openvino_runtime import _first3, versions_compatible


def test_version_parser():
    assert _first3('2026.4.0.0') == (2026, 4, 0)
    assert _first3('2026.4.0') == (2026, 4, 0)


def test_openvino_abi_match():
    ok, _ = versions_compatible({
        'openvino': '2026.4.0',
        'openvino-tokenizers': '2026.4.0.0',
        'openvino-genai': '2026.4.0.0',
    })
    assert ok


def test_openvino_abi_mismatch():
    ok, _ = versions_compatible({
        'openvino': '2026.4.0',
        'openvino-tokenizers': '2026.3.1.0',
        'openvino-genai': '2026.4.0.0',
    })
    assert not ok
