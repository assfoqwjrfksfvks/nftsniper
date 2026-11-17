"""DM Hunter entry point; usable as a desktop app or console service."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import queue
import signal
import sys
import time

from settings import load_settings
from instance_lock import InstanceLock

BASE_DIR = Path(__file__).resolve().parent


def console(cfg, autostart=False):
    from hunter import HunterEngine
    engine = HunterEngine(cfg, BASE_DIR)
    stopping = False

    def stop(*_):
        nonlocal stopping
        if not stopping:
            stopping = True
            engine.request('shutdown')

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, stop)
    if autostart:
        engine.request('start')
    exit_code = 0
    while engine.thread.is_alive() or not engine.events.empty():
        try:
            event = engine.events.get(timeout=0.2)
        except queue.Empty:
            continue
        if event['type'] == 'log':
            print(event['message'], flush=True)
        elif event['type'] == 'fatal':
            print(event['message'], file=sys.stderr)
            exit_code = 1
        elif event['type'] == 'closed':
            break
    engine.thread.join(timeout=5)
    return exit_code


def main():
    parser = argparse.ArgumentParser(description='Мониторинг публичных NFT-подарков Telegram')
    parser.add_argument('--headless', action='store_true', help='без графического интерфейса')
    parser.add_argument('--autostart', action='store_true', help='запустить мониторинг сразу (с --headless)')
    args = parser.parse_args()
    try:
        cfg = load_settings(BASE_DIR / 'config.json')
        with InstanceLock(BASE_DIR / '.dm_hunter.lock'):
            if args.headless:
                return console(cfg, args.autostart)
            from gui import HunterGUI
            app = HunterGUI(cfg, BASE_DIR)
            app.mainloop()
            return 0
    except Exception as exc:
        # Configuration errors are safe; arbitrary network tracebacks are never shown with token URLs.
        message = str(exc)
        if 'cfg' in locals() and cfg.bot_token:
            message = message.replace(cfg.bot_token, '[TOKEN]')
        print(f'DM Hunter: {message}', file=sys.stderr)
        if not args.headless:
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror('DM Hunter — ошибка запуска', message, parent=root)
                root.destroy()
            except Exception:
                pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
