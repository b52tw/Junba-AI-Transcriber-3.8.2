from __future__ import annotations

import os
import sys
from pathlib import Path

# Hugging Face/tqdm progress bars write to stderr. In a PyInstaller windowed
# executable on Windows, stdout/stderr may be None. Disable those console bars
# and guarantee safe dummy streams before importing third-party libraries.
os.environ.setdefault('HF_HUB_DISABLE_PROGRESS_BARS', '1')
os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')
os.environ.setdefault('PYTHONIOENCODING', 'utf-8')

_devnull_out = None
_devnull_in = None


def prepare_windowed_runtime() -> None:
    global _devnull_out, _devnull_in
    if sys.stdout is None or sys.stderr is None:
        if _devnull_out is None:
            _devnull_out = open(os.devnull, 'w', encoding='utf-8', errors='replace', buffering=1)
        if sys.stdout is None:
            sys.stdout = _devnull_out
        if sys.stderr is None:
            sys.stderr = _devnull_out
    if sys.stdin is None:
        if _devnull_in is None:
            _devnull_in = open(os.devnull, 'r', encoding='utf-8', errors='replace')
        sys.stdin = _devnull_in


def stdio_self_test(path: str) -> int:
    prepare_windowed_runtime()
    ok = True
    lines = []
    for name, stream in [('stdin', sys.stdin), ('stdout', sys.stdout), ('stderr', sys.stderr)]:
        state = stream is not None
        lines.append(f"{'PASS' if state else 'FAIL'} {name}_available")
        ok = ok and state
    try:
        sys.stdout.write('')
        sys.stdout.flush()
        sys.stderr.write('')
        sys.stderr.flush()
        lines.append('PASS stdout_stderr_write_flush')
    except Exception as e:
        ok = False
        lines.append(f'FAIL stdout_stderr_write_flush: {type(e).__name__}: {e}')
    Path(path).write_text(('WINDOWED_IO_SELFTEST_OK\n' if ok else 'WINDOWED_IO_SELFTEST_FAILED\n') + '\n'.join(lines), encoding='utf-8')
    return 0 if ok else 4
