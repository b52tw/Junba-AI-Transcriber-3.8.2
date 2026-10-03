from app.core import hardware


def test_adaptive_plan_always_has_cpu(monkeypatch):
    monkeypatch.setattr(hardware, 'openvino_devices', lambda: {})
    monkeypatch.setattr(hardware, '_cuda_count', lambda: 0)
    monkeypatch.setattr(hardware, 'video_controllers', lambda: [])
    plan = hardware.adaptive_plan('adaptive')
    assert plan == ['cpu']


def test_performance_prefers_cuda(monkeypatch):
    monkeypatch.setattr(hardware, 'openvino_devices', lambda: {'GPU':'Intel GPU', 'NPU':'Intel NPU'})
    monkeypatch.setattr(hardware, '_cuda_count', lambda: 1)
    monkeypatch.setattr(hardware, 'video_controllers', lambda: ['NVIDIA RTX'])
    assert hardware.adaptive_plan('performance')[0] == 'cuda'


def test_eco_prefers_npu(monkeypatch):
    monkeypatch.setattr(hardware, 'openvino_devices', lambda: {'GPU':'Intel GPU', 'NPU':'Intel NPU'})
    monkeypatch.setattr(hardware, '_cuda_count', lambda: 1)
    monkeypatch.setattr(hardware, 'video_controllers', lambda: ['Intel Arc','NVIDIA RTX'])
    assert hardware.adaptive_plan('eco')[0] == 'openvino_npu'


def test_auto_alias_resolves(monkeypatch):
    monkeypatch.setattr(hardware, 'adaptive_plan', lambda policy='adaptive': ['cpu'])
    assert hardware.resolve_acceleration('auto') == 'cpu'


def test_recommended_model_conservative(monkeypatch):
    monkeypatch.setattr(hardware, 'memory_gb', lambda: 8.0)
    monkeypatch.setattr(hardware, 'recommended_acceleration', lambda policy='adaptive': 'cpu')
    assert hardware.recommended_model('adaptive') == 'small'
