from __future__ import annotations

import os
import subprocess


def hidden_process_kwargs() -> dict:
    """Return kwargs that keep helper processes invisible on Windows GUI builds."""
    if os.name != 'nt':
        return {}
    kwargs = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)}
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = getattr(subprocess, 'SW_HIDE', 0)
        kwargs['startupinfo'] = si
    except Exception:
        pass
    return kwargs
