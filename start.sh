#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/python || ! -f .venv/pyvenv.cfg ]]; then
    dm_python=""
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            dm_python="$candidate"
            break
        fi
    done
    if [[ -z "$dm_python" ]]; then
        echo 'Установите Python 3.10 или новее и запустите start.sh снова.' >&2
        exit 1
    fi
    if ! "$dm_python" -m venv .venv; then
        echo 'Не удалось создать .venv. В Debian/Ubuntu установите пакет python3-venv.' >&2
        exit 1
    fi
fi

dm_python='.venv/bin/python'
if ! "$dm_python" -c 'import sys; sys.exit(sys.prefix == sys.base_prefix or sys.version_info < (3, 10))'; then
    echo 'В .venv используется старый Python. Переименуйте .venv и повторите запуск с Python 3.10+.' >&2
    exit 1
fi
if ! "$dm_python" -c 'import aiohttp, bs4; from importlib.metadata import version; a=tuple(map(int,version("aiohttp").split(".")[:2])); b=tuple(map(int,version("beautifulsoup4").split(".")[:2])); assert (3,10) <= a < (4,0) and (4,12) <= b < (5,0)' >/dev/null 2>&1; then
    echo 'Устанавливаются зависимости в .venv; требуется интернет.'
    "$dm_python" -m ensurepip --upgrade >/dev/null
    "$dm_python" -m pip --isolated install --disable-pip-version-check -r requirements.txt
fi
# --headless does not require Tkinter or a graphical display.
if [[ " ${*} " != *" --headless "* ]] && ! "$dm_python" -c 'import tkinter' >/dev/null 2>&1; then
    echo 'Нет Tkinter. В Debian/Ubuntu установите python3-tk или используйте --headless --autostart.' >&2
    exit 1
fi
exec "$dm_python" gift_flipper_app.py "$@"
