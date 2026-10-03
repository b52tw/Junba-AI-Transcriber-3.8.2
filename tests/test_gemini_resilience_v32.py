from types import SimpleNamespace
import app.providers.gemini as gm
from app.providers.gemini import GeminiProvider


class FakeModels:
    def __init__(self, fail_counts):
        self.fail_counts = dict(fail_counts)
        self.calls = []

    def generate_content(self, model, contents):
        self.calls.append(model)
        left = self.fail_counts.get(model, 0)
        if left > 0:
            self.fail_counts[model] = left - 1
            raise RuntimeError("503 UNAVAILABLE: This model is currently experiencing high demand")
        return SimpleNamespace(text=f"OK:{model}")


class FakeClient:
    def __init__(self, fail_counts):
        self.models = FakeModels(fail_counts)


def make_provider(fail_counts):
    p = GeminiProvider.__new__(GeminiProvider)
    p.client = FakeClient(fail_counts)
    p.model = GeminiProvider.TRANSCRIBE_MODEL
    return p


def test_postprocess_retries_transient_503(monkeypatch):
    monkeypatch.setattr(gm.time, 'sleep', lambda _s: None)
    p = make_provider({'gemini-3.8-flash': 2})
    seen=[]
    out = p.postprocess_text('測試逐字稿', progress_cb=lambda pct, detail='': seen.append((pct, detail)))
    assert out == 'OK:gemini-3.8-flash'
    assert p.client.models.calls == ['gemini-3.8-flash'] * 3
    assert any('自動重試' in detail for _, detail in seen)


def test_postprocess_falls_back_to_stable_flash(monkeypatch):
    monkeypatch.setattr(gm.time, 'sleep', lambda _s: None)
    p = make_provider({'gemini-3.8-flash': 99})
    out = p.postprocess_text('測試逐字稿')
    assert out == 'OK:gemini-3.7-flash'
    assert p.client.models.calls[:5] == ['gemini-3.8-flash'] * 5
    assert p.client.models.calls[-1] == 'gemini-3.7-flash'


def test_non_transient_error_does_not_retry(monkeypatch):
    monkeypatch.setattr(gm.time, 'sleep', lambda _s: None)
    p = make_provider({})
    def bad(*_args, **_kwargs):
        raise RuntimeError('400 INVALID_ARGUMENT')
    p.client.models.generate_content = bad
    try:
        p.postprocess_text('測試')
    except RuntimeError as exc:
        assert '400 INVALID_ARGUMENT' in str(exc)
    else:
        raise AssertionError('expected RuntimeError')
