from __future__ import annotations
import tempfile
import subprocess
from app.core.subprocess_utils import hidden_process_kwargs
from pathlib import Path
from app.core.audio_tools import ffmpeg_exe
from app.core.hardware import cpu_identity, accelerator_options, recommended_acceleration, openvino_devices


def environment_report(output_dir: str = '', local_model_dir: str = '') -> tuple[bool, str]:
    lines = []
    ok = True

    def add(name, passed, detail=''):
        nonlocal ok
        ok = ok and passed
        mark = '✓' if passed else '✗'
        lines.append(f'{mark} {name}' + (f'：{detail}' if detail else ''))

    try:
        exe = ffmpeg_exe()
        r = subprocess.run([exe, '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, timeout=10, **hidden_process_kwargs())
        add('FFmpeg', r.returncode == 0, exe)
    except Exception as e:
        add('FFmpeg', False, str(e))

    modules = [
        ('faster_whisper', 'faster-whisper'), ('ctranslate2', 'CTranslate2'),
        ('av', 'PyAV'), ('docx', 'Word 匯出'), ('google.genai', 'Google GenAI'),
        ('opencc', '繁體中文轉換 OpenCC'), ('openvino', 'OpenVINO Runtime'),
        ('openvino_genai', 'OpenVINO GenAI'), ('openvino_tokenizers', 'OpenVINO Tokenizers'), ('numpy', 'NumPy'),
    ]
    for module, label in modules:
        try:
            __import__(module)
            add(label, True)
        except Exception as e:
            add(label, False, str(e))

    try:
        from app.core.openvino_runtime import versions_compatible
        compat, detail = versions_compatible()
        add('OpenVINO ABI 版本一致', compat, detail)
    except Exception as e:
        add('OpenVINO ABI 版本一致', False, str(e))

    vendor, cpu = cpu_identity()
    lines.append(f'• CPU：{cpu} ({vendor})')
    for opt in accelerator_options():
        if opt.key == 'auto':
            continue
        mark = '✓' if opt.available else '○'
        lines.append(f'{mark} {opt.label}：{opt.detail}')
    lines.append(f'• 自動硬體模式目前選擇：{recommended_acceleration()}')

    try:
        devices = openvino_devices()
        if devices:
            lines.append('• OpenVINO 裝置：' + ' | '.join(f'{k}={v}' for k,v in devices.items()))
        else:
            lines.append('• OpenVINO 裝置：未列出 GPU/NPU；CPU 仍可用 faster-whisper。')
    except Exception as e:
        lines.append(f'• OpenVINO 裝置查詢失敗：{e}')

    if local_model_dir:
        p = Path(local_model_dir)
        add('本機 Whisper 模型', p.is_dir(), str(p))
    else:
        lines.append('• 本機模型：未指定；第一次使用模型名稱時可能需要下載。')

    if output_dir:
        try:
            p = Path(output_dir); p.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=p, delete=True) as _:
                pass
            add('輸出資料夾可寫入', True, str(p))
        except Exception as e:
            add('輸出資料夾可寫入', False, str(e))

    return ok, '\n'.join(lines)
