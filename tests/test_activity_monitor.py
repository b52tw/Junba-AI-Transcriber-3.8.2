import pytest
pytest.importorskip('PySide6')
import time
from app.core.worker import TranscribeWorker
from app.providers.openvino_whisper import OpenVINOWhisperProvider
import app.providers.openvino_whisper as ovw


def make_worker(tmp_path):
    return TranscribeWorker(
        files=[], output_dir=str(tmp_path), mode='離線 Whisper', split_minutes=0,
        model_name='small', local_model_dir='', language='auto', api_key='',
        diarization=False, timestamps=False, smart=False, traditional_output=True,
        formats=['txt'], acceleration='cpu',
    )


def test_worker_activity_snapshot_tracks_stage_and_device(tmp_path):
    w = make_worker(tmp_path)
    w._set_stage('測試階段', 10)
    w._touch_activity(detail='OpenVINO GPU 推論中', device='GPU')
    snap = w.activity_snapshot()
    assert snap['stage'] == '測試階段'
    assert snap['detail'] == 'OpenVINO GPU 推論中'
    assert snap['device'] == 'GPU'
    assert snap['pct'] == 10
    assert snap['seconds_since_event'] < 2


def test_openvino_blocking_generate_emits_heartbeat(monkeypatch):
    p = OpenVINOWhisperProvider.__new__(OpenVINOWhisperProvider)
    p.device = 'GPU'

    class SlowPipe:
        def generate(self, audio, **kwargs):
            time.sleep(0.08)
            return 'OK'

    p.pipe = SlowPipe()
    monkeypatch.setattr(ovw, 'HEARTBEAT_SECONDS', 0.01)
    seen = []
    out = p._generate_once_with_heartbeat([0.0], {'task': 'transcribe'}, lambda pct, detail='': seen.append((pct, detail)))
    assert out == 'OK'
    assert any(pct < 0 and 'OpenVINO GPU 推論中' in detail for pct, detail in seen)
