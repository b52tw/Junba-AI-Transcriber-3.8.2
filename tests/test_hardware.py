from app.core import hardware
from app.providers.openvino_whisper import REPO_MAP


def test_accelerator_priority_cuda_then_intel_gpu_then_npu(monkeypatch):
    monkeypatch.setattr(hardware, '_cuda_count', lambda: 1)
    monkeypatch.setattr(hardware, 'openvino_devices', lambda: {
        'CPU': 'CPU', 'GPU': 'Intel Arc Test', 'NPU': 'Intel AI Boost Test'
    })
    monkeypatch.setattr(hardware, 'cpu_identity', lambda: ('Intel', 'Intel Test CPU'))
    assert hardware.recommended_acceleration() == 'cuda'
    opts = {o.key: o for o in hardware.accelerator_options()}
    assert opts['cuda'].available
    assert opts['openvino_gpu'].available
    assert opts['openvino_npu'].available
    assert opts['cpu'].available


def test_accelerator_priority_intel_gpu_without_cuda(monkeypatch):
    monkeypatch.setattr(hardware, '_cuda_count', lambda: 0)
    monkeypatch.setattr(hardware, 'openvino_devices', lambda: {
        'CPU': 'CPU', 'GPU.0': 'Intel Arc 130V', 'NPU': 'Intel AI Boost'
    })
    monkeypatch.setattr(hardware, 'cpu_identity', lambda: ('Intel', 'Core Ultra'))
    assert hardware.recommended_acceleration() == 'openvino_gpu'
    assert hardware.resolve_acceleration('openvino_npu') == 'openvino_npu'


def test_cpu_fallback_supports_amd(monkeypatch):
    monkeypatch.setattr(hardware, '_cuda_count', lambda: 0)
    monkeypatch.setattr(hardware, 'openvino_devices', lambda: {})
    monkeypatch.setattr(hardware, 'cpu_identity', lambda: ('AMD', 'AMD Ryzen Test'))
    assert hardware.recommended_acceleration() == 'cpu'
    cpu = [o for o in hardware.accelerator_options() if o.key == 'cpu'][0]
    assert 'AMD' in cpu.label
    assert cpu.available


def test_official_openvino_model_map_has_gui_models():
    assert REPO_MAP['large-v3'] == 'OpenVINO/whisper-large-v3-int8-ov'
    assert set(REPO_MAP) >= {'large-v3', 'medium', 'small', 'base'}
