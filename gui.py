"""Tk stays on its main thread; background work communicates only via queues."""
from __future__ import annotations

import csv
from dataclasses import asdict
from pathlib import Path
import queue
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import webbrowser
from urllib.parse import urlencode, quote

from hunter import HunterEngine
from settings import Settings, save_settings


class HunterGUI(tk.Tk):
    def __init__(self, cfg: Settings, directory: Path):
        super().__init__()
        self.title('DM Hunter 5 — Telegram Gifts')
        self.geometry('1180x820')
        self.minsize(900, 650)
        self.cfg, self.directory = cfg, directory
        self.events = queue.Queue()
        self.rows: dict[str, dict] = {}
        self.fields = {}
        self.running = False
        self.closing = False
        self.ready = False
        self._build()
        self.engine = HunterEngine(cfg, directory, self.events)
        self.protocol('WM_DELETE_WINDOW', self._close)
        self.after(100, self._drain)

    def _build(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('Treeview', rowheight=28)
        style.configure('Header.TLabel', font=('Segoe UI', 18, 'bold'))
        top = ttk.Frame(self, padding=14)
        top.pack(fill='x')
        ttk.Label(top, text='DM HUNTER', style='Header.TLabel').pack(side='left')
        self.btn = ttk.Button(top, text='▶ Запустить мониторинг', command=self._toggle, state='disabled')
        self.btn.pack(side='right', padx=5)
        self.status = tk.StringVar(value='Запуск движка…')
        ttk.Label(self, textvariable=self.status, padding=(14, 2)).pack(fill='x')
        self.positions = tk.StringVar(value='Позиции коллекций появятся после поиска свежих номеров.')
        ttk.Label(self, textvariable=self.positions, padding=(14, 2), wraplength=1100).pack(fill='x')
        tabs = ttk.Notebook(self)
        tabs.pack(fill='both', expand=True, padx=14, pady=10)
        finds = ttk.Frame(tabs, padding=8)
        settings = ttk.Frame(tabs, padding=12)
        logs = ttk.Frame(tabs, padding=8)
        tabs.add(finds, text='Находки')
        tabs.add(settings, text='Настройки')
        tabs.add(logs, text='Журнал')
        info = ttk.Frame(finds)
        info.pack(fill='x', pady=(0, 8))
        ttk.Label(info, text='Новые совпадения сверху. Двойной клик — чат с готовым предложением.').pack(side='left')
        ttk.Button(info, text='Экспорт CSV', command=self._export).pack(side='right')
        table_frame = ttk.Frame(finds)
        table_frame.pack(fill='both', expand=True)
        columns = ('gift', 'model', 'backdrop', 'owner', 'reason', 'delivery')
        self.tree = ttk.Treeview(table_frame, columns=columns, show='headings', selectmode='browse')
        for key, label, width in zip(columns,
                ('Подарок', 'Модель (%)', 'Фон (%)', 'Владелец', 'Причина', 'Telegram'),
                (170, 150, 150, 150, 250, 100)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, minwidth=75, stretch=True)
        sy = ttk.Scrollbar(table_frame, orient='vertical', command=self.tree.yview)
        sx = ttk.Scrollbar(table_frame, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        sy.grid(row=0, column=1, sticky='ns')
        sx.grid(row=1, column=0, sticky='ew')
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind('<Double-1>', lambda event: self._open('chat'))
        actions = ttk.Frame(finds)
        actions.pack(fill='x', pady=8)
        ttk.Button(actions, text='💬 Написать владельцу', command=lambda: self._open('chat')).pack(side='left')
        ttk.Button(actions, text='🎁 Открыть подарок', command=lambda: self._open('gift')).pack(side='left', padx=8)
        ttk.Label(finds, text='Мониторинг публичных номеров: время улучшения Telegram на странице не сообщает.',
                  wraplength=1000).pack(anchor='w')

        # A scrollable settings page works on smaller laptop screens too.
        canvas = tk.Canvas(settings, highlightthickness=0)
        scroll = ttk.Scrollbar(settings, orient='vertical', command=canvas.yview)
        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor='nw')
        canvas.configure(yscrollcommand=scroll.set)
        inner.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        specs = [
            ('bot_token', 'Bot Token (BotFather)'), ('chat_id', 'Ваш личный Chat ID'),
            ('dm_collections', 'Коллекции — slug через запятую'),
            ('max_model_rarity', 'Макс. редкость модели, %'),
            ('max_backdrop_rarity', 'Макс. редкость фона, %'),
            ('target_backdrops', 'Точные названия нужных фонов'),
            ('poll_interval', 'Пауза у границы, сек.'),
            ('request_interval', 'Интервал между запросами, сек.'),
            ('rescan_interval', 'Повторный поиск границы, сек.'),
            ('lookback', 'При первом запуске: последних номеров'),
            ('scan_batch', 'Размер прохода, номеров'),
            ('gap_window', 'Окно пропусков, номеров'),
        ]
        for row, (key, label) in enumerate(specs):
            ttk.Label(inner, text=label).grid(row=row, column=0, sticky='w', padx=(0, 16), pady=4)
            var = tk.StringVar(value=str(getattr(self.cfg, key)))
            ent = ttk.Entry(inner, textvariable=var, width=65, show='•' if key == 'bot_token' else '')
            ent.grid(row=row, column=1, sticky='ew', pady=4)
            self.fields[key] = var
            if key == 'bot_token':
                show = tk.BooleanVar(value=False)
                ttk.Checkbutton(inner, text='Показать', variable=show,
                    command=lambda: ent_token.configure(show='' if show.get() else '•')).grid(row=row, column=2, padx=6)
                ent_token = ent
        row = len(specs)
        ttk.Label(inner, text='Шаблон предложения').grid(row=row, column=0, sticky='nw', pady=6)
        self.offer = tk.Text(inner, height=4, width=65, wrap='word', font=('Segoe UI', 10))
        self.offer.insert('1.0', self.cfg.offer_template)
        self.offer.grid(row=row, column=1, sticky='ew', pady=6)
        ttk.Label(inner, text='Подстановки: {gift}, {number}, {collection}, {model}, {backdrop}.',
                  wraplength=700).grid(row=row + 1, column=1, sticky='w')
        ttk.Label(inner, text='Достаточно одного совпадения: модель ИЛИ редкий фон ИЛИ выбранное название фона.\n'
                  'Уведомления только о владельцах с публичным username. Для отключения фильтра редкости укажите 0.\n'
                  'Без токена работает локальный поиск; уведомления сохраняются в очередь.',
                  wraplength=750).grid(row=row + 2, column=0, columnspan=3, sticky='w', pady=12)
        buttons = ttk.Frame(inner)
        buttons.grid(row=row + 3, column=0, columnspan=3, sticky='w', pady=6)
        ttk.Button(buttons, text='Сохранить и применить', command=self._save).pack(side='left')
        ttk.Button(buttons, text='Тест Telegram', command=self._test).pack(side='left', padx=8)
        inner.columnconfigure(1, weight=1)

        self.log = tk.Text(logs, wrap='word', state='disabled', font=('Consolas', 10))
        logscroll = ttk.Scrollbar(logs, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=logscroll.set)
        self.log.pack(side='left', fill='both', expand=True)
        logscroll.pack(side='right', fill='y')
        ttk.Label(self, text='Данные: config.json · hunter.sqlite3 · dm_hunter.log — в папке программы',
                  padding=(14, 4)).pack(fill='x')

    def _read_settings(self):
        values = asdict(self.cfg)
        values.update({key: variable.get() for key, variable in self.fields.items()})
        values['offer_template'] = self.offer.get('1.0', 'end-1c')
        return Settings(**values).validate()

    def _save(self):
        try:
            cfg = self._read_settings()
            save_settings(self.directory / 'config.json', cfg)
        except (ValueError, OSError) as exc:
            messagebox.showerror('Настройки не сохранены', str(exc), parent=self)
            return False
        self.cfg = cfg
        self.engine.request('apply', cfg)
        self._write_log('Настройки сохранены.')
        return True

    def _toggle(self):
        if self.running:
            self.engine.request('stop')
        elif self._save():
            self.engine.request('start')

    def _test(self):
        if self._save():
            self.engine.request('test')

    def _write_log(self, message):
        from datetime import datetime
        self.log.configure(state='normal')
        self.log.insert('end', f'{datetime.now():%H:%M:%S}  {message}\n')
        if int(self.log.index('end-1c').split('.')[0]) > 2000:
            self.log.delete('1.0', '201.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def _insert(self, gift, newest=True):
        key = f"{gift['slug']}-{gift['number']}"
        self.rows[key] = gift
        delivery = 'Доставлено' if gift.get('delivery_status') == 'sent' else 'В очереди'
        values = (f"{gift['slug']} #{gift['number']}",
                  f"{gift['model']} ({gift['model_rarity']:g}%)",
                  f"{gift['backdrop']} ({gift['backdrop_rarity']:g}%)",
                  '@' + (gift.get('owner_username') or ''), gift.get('reason', ''), delivery)
        if self.tree.exists(key):
            self.tree.item(key, values=values)
        else:
            self.tree.insert('', 0 if newest else 'end', iid=key, values=values)
        children = self.tree.get_children()
        if len(children) > 1000:
            for iid in children[1000:]:
                self.tree.delete(iid)
                self.rows.pop(iid, None)

    def _drain(self):
        for _ in range(250):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event['type']
            if kind == 'log':
                self._write_log(event['message'])
            elif kind == 'ready':
                self.ready = True
                self.btn.configure(state='normal')
                for gift in event['recent']:
                    self._insert(gift, newest=False)
            elif kind == 'gift':
                self._insert(event['gift'])
            elif kind == 'delivered':
                gift = event['gift']
                key = f"{gift['slug']}-{gift['number']}"
                if key in self.rows:
                    self.rows[key]['delivery_status'] = 'sent'
                    self._insert(self.rows[key])
            elif kind == 'status':
                self.running = event['running']
                self.btn.configure(text='⏹ Остановить мониторинг' if self.running else '▶ Запустить мониторинг')
                stats = event['stats']
                tg = 'бот проверен' if event['telegram'] else 'нет связи / не настроен'
                self.status.set(f"{'● Работает' if self.running else '○ Остановлен'}   |   Telegram: {tg}   |   "
                                f"Проверено: {event['checked']}   |   В очереди: {stats['pending']}   |   Доставлено: {stats['sent']}")
                if event.get('collection_errors'):
                    self.status.set(self.status.get() + f"   |   Нет данных: {len(event['collection_errors'])} коллекций")
                self.positions.set('  ·  '.join(f'{slug}: #{number}' for slug, number in event['positions'].items())
                                   or 'Позиции коллекций появятся после поиска свежих номеров.')
            elif kind in ('error', 'fatal') and not self.closing:
                messagebox.showerror('DM Hunter', event['message'], parent=self)
            elif kind == 'closed':
                self.ready = False
                self.btn.configure(state='disabled')
                if self.closing:
                    self.destroy()
                    return
        if self.closing and not self.engine.thread.is_alive():
            self.destroy()
            return
        self.after(100, self._drain)

    def _open(self, mode):
        sel = self.tree.selection()
        if not sel:
            return
        gift = self.rows[sel[0]]
        if mode == 'gift':
            url = gift['url']
        else:
            username = gift.get('owner_username')
            if not username:
                return
            draft = self.cfg.offer_template.format(gift=f"{gift['slug']} #{gift['number']}",
                number=gift['number'], collection=gift['slug'], model=gift['model'], backdrop=gift['backdrop'])
            url = f"https://t.me/{username}?{urlencode({'text': draft}, quote_via=quote)}"
        webbrowser.open(url)

    def _export(self):
        target = filedialog.asksaveasfilename(parent=self, title='Экспорт находок', defaultextension='.csv',
                                             filetypes=[('CSV', '*.csv')], initialfile='dm_hunter_finds.csv')
        if not target:
            return
        try:
            keys = ['slug', 'number', 'model', 'model_rarity', 'backdrop', 'backdrop_rarity',
                    'owner_username', 'reason', 'url', 'delivery_status']
            with open(target, 'w', newline='', encoding='utf-8-sig') as stream:
                writer = csv.DictWriter(stream, keys, extrasaction='ignore')
                writer.writeheader()
                for iid in self.tree.get_children():
                    # Prevent public names from becoming spreadsheet formulas.
                    row = {k: ("'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v)
                           for k, v in self.rows[iid].items()}
                    writer.writerow(row)
        except OSError as exc:
            messagebox.showerror('Экспорт', str(exc), parent=self)

    def _close(self):
        if self.closing:
            return
        self.closing = True
        self.btn.configure(state='disabled')
        self.status.set('Остановка задач и сохранение состояния…')
        self.engine.request('shutdown')
