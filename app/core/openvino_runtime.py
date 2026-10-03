from __future__ import annotations

import importlib.metadata as md
import importlib.util
import os
import sys
from pathlib import Path

_DLL_HANDLES = []
_PREPARED = False


def _first3(version: str) -> tuple[int, int, int]:
    parts = []
    for token in version.split('.'):
        digits = ''.join(ch for ch in token if ch.isdigit())
        if digits == '':
            break
        parts.append(int(digits))
        if len(parts) == 3:
            break
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def openvino_versions() -> dict[str, str]:
    out = {}
    for dist in ('openvino', 'openvino-tokenizers', 'openvino-genai'):
        try:
            out[dist] = md.version(dist)
        except Exception:
            out[dist] = 'not-installed'
    return out


def versions_compatible(versions: dict[str, str] | None = None) -> tuple[bool, str]:
    versions = versions or openvino_versions()
    missing = [k for k, v in versions.items() if v == 'not-installed']
    if missing:
        return False, '缺少套件：' + ', '.join(missing)
    triples = {k: _first3(v) for k, v in versions.items()}
    ok = len(set(triples.values())) == 1
    detail = ' | '.join(f'{k}={versions[k]}' for k in ('openvino','openvino-tokenizers','openvino-genai'))
    if not ok:
        detail += '；三者 MAJOR.MINOR.PATCH 必須一致，否則可能發生 DLL/ABI 不相容。'
    return ok, detail


def _candidate_roots() -> list[Path]:
    roots: list[Path] = []
    # PyInstaller one-file extraction directory.
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        roots.append(Path(meipass))
    # Executable directory / portable directory.
    try:
        roots.append(Path(sys.executable).resolve().parent)
    except Exception:
        pass
    # Installed package locations.
    for module in ('openvino_tokenizers', 'openvino', 'openvino_genai'):
        try:
            spec = importlib.util.find_spec(module)
            if spec and spec.origin:
                p = Path(spec.origin).resolve()
                roots.extend([p.parent, p.parent.parent])
            if spec and spec.submodule_search_locations:
                roots.extend(Path(x).resolve() for x in spec.submodule_search_locations)
        except Exception:
            pass
    # Preserve order while de-duplicating.
    seen = set(); result = []
    for r in roots:
        key = str(r).lower()
        if key not in seen and r.exists():
            seen.add(key); result.append(r)
    return result


def _dll_dirs() -> list[Path]:
    if os.name != 'nt':
        return []
    names = {
        'openvino_tokenizers.dll', 'core_tokenizers.dll',
        'openvino.dll', 'openvino_genai.dll',
        'tbb12.dll', 'tbbmalloc.dll', 'tbbmalloc_proxy.dll',
    }
    dirs: list[Path] = []
    for root in _candidate_roots():
        # Exact/common locations first.
        for d in (root, root/'lib', root/'libs', root/'openvino_tokenizers'/'lib', root/'openvino'/'libs'):
            if d.is_dir() and any((d/n).exists() for n in names):
                dirs.append(d)
        # PyInstaller keeps package data in nested dirs. Search only a few levels.
        try:
            for p in root.rglob('*.dll'):
                if p.name.lower() in names:
                    dirs.append(p.parent)
        except Exception:
            pass
    seen = set(); out = []
    for d in dirs:
        k = str(d).lower()
        if k not in seen:
            seen.add(k); out.append(d)
    return out


def prepare_openvino_runtime(strict: bool = True) -> str:
    """Prepare OpenVINO/GenAI DLL resolution in PyInstaller windowed builds.

    v2.7 packaged OpenVINO GenAI without explicitly collecting the
    openvino-tokenizers wheel. On Windows this can surface as error 126/127
    while loading openvino_tokenizers.dll. v3.7 pins the ABI-compatible trio,
    bundles tokenizers explicitly, and adds their DLL directories before GenAI
    is imported.
    """
    global _PREPARED
    ok, detail = versions_compatible()
    if strict and not ok:
        raise RuntimeError('OpenVINO 套件版本不相容。' + detail)

    if os.name == 'nt':
        dll_dirs = _dll_dirs()
        if dll_dirs:
            current = os.environ.get('PATH', '')
            prefix = os.pathsep.join(str(d) for d in dll_dirs)
            if prefix and prefix.lower() not in current.lower():
                os.environ['PATH'] = prefix + os.pathsep + current
            if hasattr(os, 'add_dll_directory'):
                for d in dll_dirs:
                    try:
                        _DLL_HANDLES.append(os.add_dll_directory(str(d)))
                    except Exception:
                        pass
        if strict:
            found = []
            for d in dll_dirs:
                for n in ('openvino_tokenizers.dll', 'core_tokenizers.dll'):
                    if (d/n).exists():
                        found.append(n)
            if 'openvino_tokenizers.dll' not in found:
                raise RuntimeError(
                    '找不到 openvino_tokenizers.dll。此 EXE 的 OpenVINO Tokenizers DLL 未完整封裝；'
                    '請使用 v3.7 Portable 或重新以 v3.7 GitHub Actions 建置。'
                )

    # Importing openvino_tokenizers registers tokenizer operations/extensions.
    try:
        import openvino_tokenizers  # noqa: F401
    except Exception as e:
        if strict:
            raise RuntimeError(
                'OpenVINO Tokenizers 無法載入。這通常是 OpenVINO / Tokenizers / GenAI '
                '版本或 DLL 相依性不一致。\n' + detail +
                f'\n原始錯誤：{type(e).__name__}: {e}'
            ) from e
    _PREPARED = True
    return detail


def _find_tokenizers_dll() -> Path | None:
    if os.name != 'nt':
        return None
    for d in _dll_dirs():
        p = d / 'openvino_tokenizers.dll'
        if p.exists():
            return p
    return None


def binary_self_test() -> tuple[bool, str]:
    """Exercise the exact extension-loading path that failed in v2.7.

    Import-only checks are not enough on Windows: the DLL may exist but still
    fail with WinError 126/127 if a dependency is missing or the ABI mismatches.
    Calling Core.add_extension() forces Windows to resolve the binary now.
    """
    lines = []
    try:
        detail = prepare_openvino_runtime(strict=True)
        lines.append('PASS version_abi ' + detail)
        import openvino as ov
        import openvino_genai  # noqa: F401
        import openvino_tokenizers  # noqa: F401
        core = ov.Core()
        dll = _find_tokenizers_dll()
        if os.name == 'nt':
            if dll is None:
                raise RuntimeError('openvino_tokenizers.dll was not found after runtime preparation')
            lines.append('INFO tokenizers_dll=' + str(dll))
            core.add_extension(str(dll))
            lines.append('PASS core.add_extension(openvino_tokenizers.dll)')
        lines.append('PASS import_openvino_tokenizers')
        lines.append('PASS openvino_core devices=' + ','.join(map(str, core.available_devices)))
        return True, '\n'.join(lines)
    except Exception as e:
        lines.append(f'FAIL {type(e).__name__}: {e}')
        return False, '\n'.join(lines)
