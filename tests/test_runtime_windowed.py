import os
import sys
from pathlib import Path


def test_prepare_windowed_runtime_recovers_none_streams(monkeypatch, tmp_path):
    import app.core.runtime as runtime
    monkeypatch.setattr(sys, 'stdout', None)
    monkeypatch.setattr(sys, 'stderr', None)
    monkeypatch.setattr(sys, 'stdin', None)
    runtime.prepare_windowed_runtime()
    assert sys.stdout is not None
    assert sys.stderr is not None
    assert sys.stdin is not None
    sys.stdout.write('')
    sys.stderr.write('')


def test_hf_progress_disabled_by_default():
    import app.core.runtime  # noqa: F401
    assert os.environ.get('HF_HUB_DISABLE_PROGRESS_BARS') == '1'
