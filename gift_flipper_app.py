"""DM Hunter entry point; usable as a desktop app or console service."""
from __future__ import annotations

import argparse
from pathlib import Path
import queue
import signal
import sys

from dataclasses import replace
from getpass import getpass
from settings import load_settings, save_settings
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


def setup_console(cfg, input_fn=None, token_fn=None):
    """Fill missing bot credentials without printing the token or requiring Tk."""
    input_fn = input if input_fn is None else input_fn
    token_fn = getpass if token_fn is None else token_fn
    if cfg.bot_token and cfg.chat_id:
        return cfg
    print('Настройка Telegram без графического окна. Токен при вводе скрыт.')
    print('Создайте бота в @BotFather. Нужен ваш положительный числовой Telegram ID.')
    while True:
        token = cfg.bot_token or token_fn('Bot Token: ').strip()
        chat_id = cfg.chat_id or input_fn('Ваш личный Chat ID: ').strip()
        if not token or not chat_id:
            print('Оба поля обязательны. Повторите ввод; Ctrl+C — выход.')
            continue
        try:
            configured = replace(cfg, bot_token=token, chat_id=chat_id).validate()
        except ValueError as exc:
            print(str(exc))
            continue
        save_settings(BASE_DIR / 'config.json', configured)
        print('Настройки сохранены. Откройте своего бота и отправьте /start.')
        return configured


def run_desktop(cfg, autostart=False):
    from tk_support import prepare_tk_environment, tk_repair_message
    app = None
    try:
        prepare_tk_environment()
        from gui import HunterGUI
        app = HunterGUI(cfg, BASE_DIR)
    except Exception as exc:
        missing_tk = isinstance(exc, ImportError) and getattr(exc, 'name', '') in ('tkinter', '_tkinter')
        if not missing_tk and type(exc).__name__ != 'TclError':
            raise
        # Tk creation failed before the engine starts. Do not retry the same Tk
        # in a messagebox: that would hide diagnostics behind a second failure.
        print(tk_repair_message(exc), file=sys.stderr)
        if not sys.stdin.isatty():
            return 1
        print('Перехожу в режим без окна. Управление — через вашего Telegram-бота.')
        cfg = setup_console(cfg)
        return console(cfg, autostart=True)
    if autostart:
        app.engine.request('start')
    app.mainloop()
    return 0


def main():
    parser = argparse.ArgumentParser(description='Мониторинг публичных NFT-подарков Telegram')
    parser.add_argument('--headless', action='store_true', help='без графического интерфейса')
    parser.add_argument('--autostart', action='store_true', help='запустить мониторинг сразу')
    parser.add_argument('--setup', action='store_true', help='запросить недостающие токен и Chat ID в консоли')
    args = parser.parse_args()
    try:
        cfg = load_settings(BASE_DIR / 'config.json')
        with InstanceLock(BASE_DIR / '.dm_hunter.lock'):
            if args.setup:
                cfg = setup_console(cfg)
            if args.headless:
                return console(cfg, args.autostart)
            return run_desktop(cfg, args.autostart)
    except (KeyboardInterrupt, EOFError):
        print('Настройка отменена. Настройки и база сохранены без удаления данных.', file=sys.stderr)
        return 1
    except Exception as exc:
        message = str(exc)
        if 'cfg' in locals() and cfg.bot_token:
            message = message.replace(cfg.bot_token, '[TOKEN]')
        print(f'DM Hunter: {message}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
