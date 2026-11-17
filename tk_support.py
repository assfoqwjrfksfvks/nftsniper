"""Find Tcl/Tk shipped with this Python; never use another Python's libraries."""
from __future__ import annotations

import os
from pathlib import Path
import sys


def prepare_tk_environment(*, platform=None, prefixes=None, environ=None,
                           tcl_version=None, tk_version=None):
    if (platform or sys.platform) != 'win32':
        return
    env = os.environ if environ is None else environ
    if tcl_version is None or tk_version is None:
        import _tkinter
        tcl_version, tk_version = _tkinter.TCL_VERSION, _tkinter.TK_VERSION
    roots = prefixes if prefixes is not None else (sys.base_prefix, sys.base_exec_prefix)
    for variable, version, marker, folder in (
        ('TCL_LIBRARY', tcl_version, 'init.tcl', 'tcl'),
        ('TK_LIBRARY', tk_version, 'tk.tcl', 'tk'),
    ):
        candidates = []
        for root in roots:
            root = Path(root)
            candidates.extend((root / 'tcl' / f'{folder}{version}',
                               root / 'lib' / f'{folder}{version}',
                               root / f'{folder}{version}' / 'library'))
        # Prefer this interpreter's libraries over inherited values from other installs.
        found = next((p for p in candidates if (p / marker).is_file()), None)
        if found is not None:
            env[variable] = str(found)
        elif env.get(variable) and not (Path(env[variable]) / marker).is_file():
            env.pop(variable, None)


def tk_repair_message(exc):
    return (
        'Не удалось открыть графическое окно: Tcl/Tk недоступен.\n'
        f'Python: {sys.executable}\nБазовая установка: {sys.base_prefix}\n'
        f'Причина: {exc}\n\n'
        'Для восстановления окна откройте установщик этой версии Python с python.org,\n'
        'выберите Modify и включите Tcl/Tk and IDLE (либо Repair, если компонент уже выбран).\n'
        'Проверьте базовый Python командой: python -m tkinter\n'
        'Если установка Python была перемещена, установите её заново штатным установщиком.\n'
        'После исправления закройте программу, удалите только папку .venv\n'
        'в папке DM Hunter и запустите START.bat снова. Настройки и базу не удаляйте.\n'
        'Мониторинг через Telegram доступен без Tcl/Tk: START_TELEGRAM.bat.'
    )
