from app.providers.openvino_whisper import OpenVINOWhisperProvider, _is_npu_runtime_error


class FailPipe:
    def __init__(self, msg):
        self.msg = msg
    def generate(self, audio, **kwargs):
        raise RuntimeError(self.msg)


class OkPipe:
    def __init__(self, value='OK'):
        self.value = value
    def generate(self, audio, **kwargs):
        return self.value


def bare_provider():
    p = OpenVINOWhisperProvider.__new__(OpenVINOWhisperProvider)
    p.device = 'NPU'
    p.requested_device = 'NPU'
    p.available = ['CPU', 'GPU.0', 'NPU']
    p.model_name = 'small'
    p.progress_cb = None
    p.compat_retry_used = False
    p.fallback_history = []
    return p


def test_detect_level_zero_invalid_argument():
    e = RuntimeError('L0 pfnAppendGraphExecute result: ZE_RESULT_ERROR_INVALID_ARGUMENT, code 0x78000004')
    assert _is_npu_runtime_error(e)


def test_npu_invalid_argument_falls_back_to_gpu(monkeypatch):
    p = bare_provider()
    p.pipe = FailPipe('L0 pfnAppendGraphExecute result: ZE_RESULT_ERROR_INVALID_ARGUMENT, code 0x78000004')

    def build(device, compat=False):
        if device == 'NPU':
            return FailPipe('L0 pfnAppendGraphExecute result: ZE_RESULT_ERROR_INVALID_ARGUMENT, code 0x78000004')
        if device == 'GPU':
            return OkPipe('GPU_OK')
        return OkPipe('CPU_OK')

    monkeypatch.setattr(p, '_build_pipe', build)
    out = p._generate_with_fallback([0.0], {'task': 'transcribe'})
    assert out == 'GPU_OK'
    assert p.device == 'GPU'
    assert p.compat_retry_used is True
    assert p.fallback_history


def test_npu_and_gpu_failure_falls_back_to_cpu(monkeypatch):
    p = bare_provider()
    p.pipe = FailPipe('ZE_RESULT_ERROR_INVALID_ARGUMENT 0x78000004')

    def build(device, compat=False):
        if device == 'NPU':
            return FailPipe('ZE_RESULT_ERROR_INVALID_ARGUMENT 0x78000004')
        if device == 'GPU':
            return FailPipe('GPU graph rejected')
        return OkPipe('CPU_OK')

    monkeypatch.setattr(p, '_build_pipe', build)
    out = p._generate_with_fallback([0.0], {'task': 'transcribe'})
    assert out == 'CPU_OK'
    assert p.device == 'CPU'
    assert len(p.fallback_history) == 2
