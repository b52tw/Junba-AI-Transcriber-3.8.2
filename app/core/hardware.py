from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import platform
import subprocess
import time
from typing import Iterable

from app.core.subprocess_utils import hidden_process_kwargs


@dataclass
class AcceleratorInfo:
    key: str
    label: str
    available: bool
    detail: str = ''
    recommended_rank: int = 999


def _run_powershell(script: str) -> str:
    if os.name != 'nt':
        return ''
    try:
        p = subprocess.run(
            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding='utf-8', errors='replace', timeout=8,
            **hidden_process_kwargs(),
        )
        return p.stdout.strip() if p.returncode == 0 else ''
    except Exception:
        return ''


def cpu_identity() -> tuple[str, str]:
    name = platform.processor() or platform.machine() or 'Unknown CPU'
    vendor = ''
    if os.name == 'nt':
        raw = _run_powershell("Get-CimInstance Win32_Processor | Select-Object -First 1 Name,Manufacturer | ConvertTo-Json -Compress")
        if raw:
            try:
                d = json.loads(raw)
                name = (d.get('Name') or name).strip()
                vendor = (d.get('Manufacturer') or '').strip()
            except Exception:
                pass
    low = f'{vendor} {name}'.lower()
    if 'amd' in low:
        vendor = 'AMD'
    elif 'intel' in low:
        vendor = 'Intel'
    return vendor or 'x86-64', name


def memory_gb() -> float:
    """Best-effort physical RAM size without adding a psutil dependency."""
    if os.name == 'nt':
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]
            st = MEMORYSTATUSEX(); st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return st.ullTotalPhys / (1024 ** 3)
        except Exception:
            pass
    try:
        pages = os.sysconf('SC_PHYS_PAGES')
        size = os.sysconf('SC_PAGE_SIZE')
        return pages * size / (1024 ** 3)
    except Exception:
        return 0.0


def _cuda_count() -> int:
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        return 0


def video_controllers() -> list[str]:
    if os.name != 'nt':
        return []
    raw = _run_powershell("Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name | ConvertTo-Json -Compress")
    if not raw:
        return []
    try:
        d = json.loads(raw)
        if isinstance(d, str):
            return [d]
        if isinstance(d, list):
            return [str(x) for x in d if x]
    except Exception:
        pass
    return []


def is_legacy_intel_igpu(controllers: list[str] | None = None) -> bool:
    """Older Intel iGPUs are supported, but large Whisper models can be very slow.

    Iris Xe/UHD/HD are treated conservatively; Intel Arc is not.
    """
    ctrls = controllers if controllers is not None else video_controllers()
    text = ' '.join(ctrls).lower()
    if 'intel' not in text:
        return False
    if 'arc' in text:
        return False
    return any(x in text for x in ('iris', 'uhd', 'hd graphics'))


def openvino_devices() -> dict[str, str]:
    """Return {device_id: full device name}; empty dict if OpenVINO is unavailable."""
    try:
        import openvino as ov
        core = ov.Core()
        result: dict[str, str] = {}
        for dev in core.available_devices:
            full = dev
            try:
                full = str(core.get_property(dev, 'FULL_DEVICE_NAME'))
            except Exception:
                pass
            result[str(dev)] = full
        return result
    except Exception:
        return {}


def openvino_npu_details() -> dict[str, str]:
    try:
        import openvino as ov
        core = ov.Core()
        if not any(str(d).upper().startswith('NPU') for d in core.available_devices):
            return {}
        out: dict[str, str] = {}
        for key in (
            'FULL_DEVICE_NAME', 'DEVICE_ARCHITECTURE', 'NPU_DRIVER_VERSION',
            'NPU_COMPILER_VERSION', 'NPU_COMPILER_TYPE', 'NPU_MAX_TILES',
        ):
            try:
                out[key] = str(core.get_property('NPU', key))
            except Exception:
                pass
        return out
    except Exception:
        return {}


def _profile_path() -> Path:
    try:
        from platformdirs import user_data_dir
        root = Path(user_data_dir('JunbaAITranscriber', 'Junba'))
    except Exception:
        root = Path.home() / '.junba_ai_transcriber'
    root.mkdir(parents=True, exist_ok=True)
    return root / 'hardware_profile.json'


def load_hardware_profile() -> dict:
    p = _profile_path()
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_hardware_profile(data: dict) -> None:
    data = dict(data or {})
    data['updated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    _profile_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def clear_hardware_profile() -> None:
    try:
        _profile_path().unlink(missing_ok=True)
    except Exception:
        pass


def _normalize_accel_key(key: str) -> str:
    key = str(key or '').lower()
    if key in ('gpu', 'openvino_gpu'):
        return 'openvino_gpu'
    if key in ('npu', 'openvino_npu'):
        return 'openvino_npu'
    if key == 'cuda':
        return 'cuda'
    if key == 'cpu':
        return 'cpu'
    return key


def record_accelerator_result(key: str, success: bool, detail: str = '', *, audio_seconds: float = 0.0, elapsed_seconds: float = 0.0) -> None:
    """Remember stability and, when available, observed real-time factor (RTF).

    RTF = processing seconds / audio seconds. Lower is faster. This lets the
    adaptive route learn separately on every Windows PC instead of assuming
    the same Intel/NVIDIA path is best everywhere.
    """
    if not key:
        return
    key = _normalize_accel_key(key)
    p = load_hardware_profile()
    stats = p.setdefault('stats', {})
    item = stats.setdefault(key, {'success': 0, 'failure': 0})
    item['success' if success else 'failure'] = int(item.get('success' if success else 'failure', 0)) + 1
    item['last_detail'] = (detail or '')[:500]
    item['last_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    if success and audio_seconds > 0 and elapsed_seconds > 0:
        rtf = float(elapsed_seconds) / float(audio_seconds)
        old = item.get('avg_rtf')
        n = max(1, int(item.get('rtf_samples', 0)))
        item['avg_rtf'] = rtf if old is None else (float(old) * n + rtf) / (n + 1)
        item['rtf_samples'] = n + (0 if old is None else 1)
        item['last_rtf'] = rtf
    if success:
        p['last_success'] = key
    save_hardware_profile(p)


def accelerator_average_rtf(key: str) -> float | None:
    item = (load_hardware_profile().get('stats') or {}).get(_normalize_accel_key(key), {})
    try:
        value = float(item.get('avg_rtf'))
        return value if value > 0 else None
    except Exception:
        return None


def _failed_too_often(key: str, profile: dict | None = None) -> bool:
    p = profile or load_hardware_profile()
    item = (p.get('stats') or {}).get(key, {})
    fail = int(item.get('failure', 0)); ok = int(item.get('success', 0))
    # Do not permanently blacklist a device. Only deprioritize after repeated
    # failures without any successful run on this Windows installation.
    return fail >= 2 and ok == 0


def accelerator_options() -> list[AcceleratorInfo]:
    vendor, cpu = cpu_identity()
    ov_devices = openvino_devices()
    cuda = _cuda_count()
    controllers = video_controllers()

    npu_keys = [k for k in ov_devices if k.upper().startswith('NPU')]
    gpu_keys = [k for k in ov_devices if k.upper().startswith('GPU')]
    npu_detail = '; '.join(f'{k}: {ov_devices[k]}' for k in npu_keys)
    gpu_detail = '; '.join(f'{k}: {ov_devices[k]}' for k in gpu_keys)
    amd_gpu = [x for x in controllers if 'amd' in x.lower() or 'radeon' in x.lower()]

    cpu_detail = f'{cpu}｜x86-64 CPU 保底路徑，Intel/AMD 皆可使用'
    if amd_gpu and not gpu_keys:
        cpu_detail += '｜偵測到 AMD GPU；目前此版本不以 OpenVINO 驅動 AMD GPU，會安全使用 CPU'

    return [
        AcceleratorInfo('adaptive', '自動自適應（建議）', True,
                        '依本機成功紀錄、硬體與驅動自動選擇；失敗會降級並記住結果', 0),
        AcceleratorInfo('performance', '效能優先（自動）', True,
                        '優先：NVIDIA CUDA → Intel GPU/OpenVINO → Intel NPU/OpenVINO → CPU', 0),
        AcceleratorInfo('eco', '省電優先（自動）', True,
                        '優先：Intel NPU/OpenVINO → Intel GPU/OpenVINO → CPU → NVIDIA CUDA', 0),
        AcceleratorInfo('cuda', 'NVIDIA GPU / CUDA', cuda > 0,
                        f'偵測到 {cuda} 個 CUDA 裝置' if cuda else '未偵測到 NVIDIA CUDA', 1),
        AcceleratorInfo('openvino_gpu', 'Intel GPU / OpenVINO', bool(gpu_keys),
                        gpu_detail or '未偵測到 OpenVINO Intel GPU', 2),
        AcceleratorInfo('openvino_npu', 'Intel NPU / OpenVINO（實驗／省電）', bool(npu_keys),
                        npu_detail or '未偵測到 OpenVINO Intel NPU', 3),
        AcceleratorInfo('cpu', f'CPU / faster-whisper（{vendor}）', True, cpu_detail, 4),
    ]


def _available_map() -> dict[str, AcceleratorInfo]:
    return {o.key: o for o in accelerator_options()}


def adaptive_plan(policy: str = 'adaptive') -> list[str]:
    policy = {'auto': 'adaptive'}.get(policy, policy)
    opts = _available_map(); profile = load_hardware_profile()
    legacy_igpu = is_legacy_intel_igpu()
    if policy == 'eco':
        order = ['openvino_npu', 'openvino_gpu', 'cpu', 'cuda']
    elif policy == 'adaptive' and legacy_igpu:
        # 8th-11th gen Iris/UHD systems frequently spend much longer compiling
        # and decoding large Whisper graphs on iGPU than on CPU INT8. Start
        # conservatively, then let measured RTF history override this.
        order = ['cuda', 'cpu', 'openvino_gpu', 'openvino_npu']
    else:
        order = ['cuda', 'openvino_gpu', 'openvino_npu', 'cpu']

    available = [k for k in order if opts.get(k) and opts[k].available]
    if policy == 'adaptive':
        stable = [k for k in available if not _failed_too_often(k, profile)]
        unstable = [k for k in available if _failed_too_often(k, profile)]
        # Prefer measured fast paths once this PC has enough successful history.
        def score(k):
            item = (profile.get('stats') or {}).get(k, {})
            try:
                samples = int(item.get('rtf_samples', 0))
                rtf = float(item.get('avg_rtf'))
                if samples >= 1 and rtf > 0:
                    return (0, rtf)
            except Exception:
                pass
            return (1, order.index(k) if k in order else 99)
        stable = sorted(stable, key=score)
        available = stable + unstable
    return available or ['cpu']


def recommended_acceleration(policy: str = 'performance') -> str:
    return adaptive_plan(policy)[0]


def resolve_acceleration(requested: str) -> str:
    requested = {'auto': 'adaptive'}.get(requested or 'adaptive', requested or 'adaptive')
    if requested in ('adaptive', 'performance', 'eco'):
        return recommended_acceleration(requested)
    opts = _available_map()
    info = opts.get(requested)
    if info is None:
        raise ValueError(f'未知硬體加速模式：{requested}')
    if not info.available:
        raise RuntimeError(f'目前電腦無法使用「{info.label}」：{info.detail}')
    return requested


def recommended_model(policy: str = 'adaptive') -> str:
    """Conservative automatic model choice for portable use across PCs."""
    ram = memory_gb()
    route = recommended_acceleration(policy)
    legacy_igpu = is_legacy_intel_igpu()
    if route == 'cuda':
        return 'large-v3-turbo' if ram and ram < 16 else 'large-v3'
    if route == 'openvino_gpu':
        if legacy_igpu:
            return 'small' if ram < 20 else 'medium'
        if ram >= 24:
            return 'large-v3'
        if ram >= 14:
            return 'large-v3-turbo'
        return 'medium' if ram >= 10 else 'small'
    if route == 'openvino_npu':
        return 'medium' if ram >= 12 else 'small'
    # CPU path: optimize for completion speed on ordinary office PCs.
    if ram >= 24:
        return 'large-v3-turbo'
    if ram >= 14:
        return 'medium'
    return 'small'


def recommended_split_minutes(mode: str, duration_seconds: float, policy: str = 'adaptive', model: str = 'auto') -> int:
    """Automatic chunk size for progress visibility, recovery and cross-PC stability."""
    duration_minutes = max(0.0, float(duration_seconds or 0.0) / 60.0)
    if duration_minutes <= 8:
        return 0
    if mode == 'Google Gemini':
        return 10
    route = recommended_acceleration(policy if policy in ('adaptive','performance','eco') else 'adaptive')
    chosen = recommended_model(policy) if model == 'auto' else model
    if route == 'cuda':
        return 10 if duration_minutes <= 90 else 15
    if route in ('openvino_npu', 'openvino_gpu'):
        if is_legacy_intel_igpu() or chosen in ('large-v3', 'large-v3-turbo'):
            return 3
        return 5
    return 3 if duration_minutes > 30 else 5


def hardware_summary() -> str:
    vendor, cpu = cpu_identity(); ram = memory_gb(); profile = load_hardware_profile()
    lines = [f'CPU：{cpu} ({vendor})', f'記憶體：約 {ram:.1f} GB' if ram else '記憶體：無法判定']
    ctrls = video_controllers()
    if ctrls:
        lines.append('顯示裝置：' + '；'.join(ctrls))
    for o in accelerator_options():
        if o.key in ('adaptive', 'performance', 'eco'):
            continue
        lines.append(f"{'✓' if o.available else '○'} {o.label}：{o.detail}")
    details = openvino_npu_details()
    if details:
        lines.append('NPU 詳細：' + ' | '.join(f'{k}={v}' for k,v in details.items()))
    plan = adaptive_plan('adaptive')
    lines.append('自適應備援順序：' + ' → '.join(plan))
    lines.append(f'自動推薦模型：{recommended_model("adaptive")}')
    if profile.get('last_success'):
        lines.append(f'本機上次成功裝置：{profile.get("last_success")}')
    stats = profile.get('stats') or {}
    if stats:
        summary = []
        for key, item in stats.items():
            summary.append(f'{key} 成功{item.get("success",0)}/失敗{item.get("failure",0)}' + (f'/RTF {float(item.get("avg_rtf")):.2f}x' if item.get('avg_rtf') else ''))
        lines.append('本機學習紀錄：' + '；'.join(summary))
    return '\n'.join(lines)
