# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = []
binaries = []
hiddenimports = []
for pkg in [
    'faster_whisper', 'ctranslate2', 'av', 'imageio_ffmpeg',
    'google.genai', 'keyring', 'docx', 'platformdirs', 'huggingface_hub', 'openvino', 'openvino_genai',
    'openvino_tokenizers', 'numpy'
]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass
for pkg in ['keyring.backends', 'google.genai']:
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass
hiddenimports = list(dict.fromkeys(hiddenimports))

a = Analysis(
    ['main.py'], pathex=['.'], binaries=binaries, datas=datas,
    hiddenimports=hiddenimports, hookspath=[], hooksconfig={}, runtime_hooks=['hooks/runtime_safe_windows.py'],
    excludes=['torch', 'pyannote', 'tensorflow'], noarchive=False
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name='Junba', debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False,
    version='packaging/version_info.txt', manifest='packaging/app.manifest',
    contents_directory='_i'
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[],
    name='JunbaP'
)
