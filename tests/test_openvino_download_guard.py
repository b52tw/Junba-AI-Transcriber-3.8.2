from pathlib import Path


def test_openvino_download_guard_uses_safe_stdio(monkeypatch, tmp_path):
    import sys
    import types
    import app.providers.openvino_whisper as mod

    target = tmp_path / 'model'
    monkeypatch.setattr(mod, '_model_root', lambda name: target)
    monkeypatch.setitem(mod.REPO_MAP, 'small', 'fake/repo')

    fake_hf = types.ModuleType('huggingface_hub')
    def snapshot_download(repo_id, local_dir):
        # Simulate a third-party downloader that writes to stderr.
        sys.stderr.write('')
        p = Path(local_dir)
        p.mkdir(parents=True, exist_ok=True)
        (p / 'openvino_encoder_model.xml').write_text('<xml/>', encoding='utf-8')
        return str(p)
    fake_hf.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, 'huggingface_hub', fake_hf)

    fake_utils = types.ModuleType('huggingface_hub.utils')
    fake_utils.disable_progress_bars = lambda: None
    monkeypatch.setitem(sys.modules, 'huggingface_hub.utils', fake_utils)

    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = None
        sys.stderr = None
        got = mod.ensure_openvino_model('small')
        assert Path(got).is_dir()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
