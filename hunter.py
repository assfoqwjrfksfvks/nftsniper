"""Resumable public-page monitor. All mutable engine state lives on one async loop."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import queue
import threading
import time

import aiohttp

from nft_client import Gift, NFTClient, NFTFetchError
from settings import Settings
from store import Store
from telegram_client import TelegramClient, TelegramError, build_notification, control_keyboard

# Hints from the original app, never treated as upper bounds.
SEEDS = {"JingleBells": 91000, "CandyCane": 222000, "BerryBox": 52000,
         "LolPop": 310000, "HomemadeCake": 140000}


def match_gift(gift: Gift, cfg: Settings) -> str | None:
    """Each configured criterion is an OR trigger. Named backgrounds match exactly."""
    if not gift.owner_username:
        return None
    reasons = []
    targets = {s.strip().casefold() for s in cfg.target_backdrops.split(',') if s.strip()}
    if gift.backdrop.casefold() in targets:
        reasons.append(f"Фон: {gift.backdrop}")
    elif cfg.max_backdrop_rarity > 0 and gift.backdrop_rarity <= cfg.max_backdrop_rarity:
        reasons.append(f"Редкий фон: {gift.backdrop} ({gift.backdrop_rarity:g}%)")
    if cfg.max_model_rarity > 0 and gift.model_rarity <= cfg.max_model_rarity:
        reasons.append(f"Редкая модель: {gift.model} ({gift.model_rarity:g}%)")
    return '; '.join(reasons) or None


async def discover_latest(client: NFTClient, slug: str, seed: int, window: int = 10) -> int:
    """Find a tail using exponential bounds and binary search with a hole window.

    Public serials are only a proxy for upgrade time; no timestamp is claimed.
    A run of more than `window` unpublished IDs cannot be ruled out by this API.
    """
    cache: dict[int, Gift | None] = {}

    async def fetch(number):
        if number not in cache:
            if len(cache) >= 600:
                raise NFTFetchError(f'{slug}: превышен лимит поиска номера; задайте initial_ids')
            cache[number] = await client.fetch(slug, number)
        return cache[number]

    anchor = None
    for number in dict.fromkeys([max(1, seed), 1, 100, 1000]):
        anchor = await fetch(number)
        if anchor is not None:
            break
    if anchor is None:
        raise NFTFetchError(f'{slug}: не найдены опорные подарки. Проверьте название коллекции и initial_ids')
    low = anchor.number
    # Quantity is a useful probe, not a maximum serial or a guaranteed existing ID.
    if anchor.issued and anchor.issued > low:
        candidate = await fetch(anchor.issued)
        if candidate:
            low = candidate.number
    step = max(32, window + 1)
    high = low + step
    while True:
        upper_gift = await fetch(high)
        if upper_gift is None:
            for number in range(high + 1, high + window + 1):
                upper_gift = await fetch(number)
                if upper_gift:
                    break
        if upper_gift is None:
            break
        low = upper_gift.number
        step *= 2
        high = low + step
        if high > 2_000_000_000:
            raise NFTFetchError(f'{slug}: не удалось ограничить диапазон номеров')
    while high - low > 1:
        mid = (low + high) // 2
        gift = await fetch(mid)
        if gift:
            low = mid
        else:
            # A missing serial is not conclusive: check for a short publication gap.
            found = None
            for number in range(mid + 1, min(mid + window + 1, high)):
                if await fetch(number):
                    found = number
                    break
            if found is None:
                high = mid
            else:
                low = found
    return low


class HunterEngine:
    """Thread-safe command/event boundary for Tk and optional console mode."""

    def __init__(self, cfg: Settings, directory: Path, events: queue.Queue | None = None):
        self.cfg = replace(cfg, initial_ids=dict(cfg.initial_ids)).validate()
        self.directory = Path(directory)
        self.events = events if events is not None else queue.Queue()
        self.commands: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._thread_main, name='DM-Hunter', daemon=True)
        self._scanner = None
        self._bot = None
        self._closing = False
        self._verified = False
        self._last_error = ''
        self._positions: dict[str, int] = {}
        self._frontiers: dict[str, int] = {}
        self._tg_cooldown = 0.0
        self._requests = 0
        self._checked = 0
        self._started = time.time()
        self._logger = logging.getLogger(f'dm-hunter-{id(self)}')
        self._logger.setLevel(logging.INFO)
        handler = RotatingFileHandler(self.directory / 'dm_hunter.log', maxBytes=2_000_000,
                                      backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        self._logger.addHandler(handler)
        self._logger.propagate = False
        self.thread.start()

    def request(self, action: str, cfg: Settings | None = None):
        self.commands.put((action, cfg))

    def _emit(self, kind, **data):
        self.events.put({'type': kind, **data})

    def log(self, message):
        message = str(message)
        if self.cfg.bot_token:
            message = message.replace(self.cfg.bot_token, '[TOKEN]')
        self._logger.info(message)
        self._emit('log', message=message)

    def _thread_main(self):
        try:
            asyncio.run(self._main())
        except Exception as exc:
            self.log(f'Критическая ошибка: {type(exc).__name__}: {exc}')
            self._emit('fatal', message='Не удалось запустить движок. Подробности в журнале.')
        finally:
            self._emit('closed')
            for handler in self._logger.handlers[:]:
                handler.close()
                self._logger.removeHandler(handler)

    async def _main(self):
        self.store = Store(self.directory / 'hunter.sqlite3')
        try:
            async with aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=6, limit_per_host=3),
                timeout=aiohttp.ClientTimeout(total=25),
                headers={'User-Agent': 'DMHunter/5.0 (public gift monitor)'},
                trust_env=True,
            ) as self.session:
                self.client = NFTClient(self.session, log=self.log, request_interval=self.cfg.request_interval)
                self._lock = asyncio.Lock()
                self._outbox = asyncio.create_task(self._supervise('Очередь Telegram', self._deliver_loop))
                self._restart_bot()
                self._emit('ready', recent=self.store.recent(200))
                self.log('DM Hunter готов. Мониторинг запускается кнопкой или командой бота.')
                last_status = 0.0
                try:
                    while not self._closing:
                        try:
                            action, cfg = self.commands.get_nowait()
                        except queue.Empty:
                            pass
                        else:
                            try:
                                await self._handle(action, cfg)
                            except Exception as exc:
                                self.log(f'Команда не выполнена: {type(exc).__name__}: {exc}')
                                self._emit('error', message=str(exc))
                        if time.monotonic() - last_status >= 2:
                            self._status()
                            last_status = time.monotonic()
                        await asyncio.sleep(0.1)
                finally:
                    for task in (self._scanner, self._bot, self._outbox):
                        if task is not None:
                            task.cancel()
                    await asyncio.gather(*(t for t in (self._scanner, self._bot, self._outbox) if t),
                                         return_exceptions=True)
        finally:
            self.store.close()

    async def _handle(self, action, cfg=None):
        async with self._lock:
            if cfg is not None:
                new = replace(cfg, initial_ids=dict(cfg.initial_ids)).validate()
                old = self.cfg
                if asdict(old) != asdict(new):
                    running = self._scanner is not None and not self._scanner.done()
                    await self._stop_scan(quiet=True)
                    self.cfg = new
                    self.client.request_interval = new.request_interval
                    if old.bot_token != new.bot_token or old.chat_id != new.chat_id:
                        self._verified = False
                        if self._bot:
                            self._bot.cancel()
                            await asyncio.gather(self._bot, return_exceptions=True)
                        self._restart_bot()
                    if running and action not in ('stop', 'shutdown'):
                        self._start_scan()
                    self.log('Настройки применены. Позиции сканирования сохранены.')
            if action == 'start':
                self._start_scan()
            elif action == 'stop':
                await self._stop_scan()
            elif action == 'shutdown':
                self._closing = True
                await self._stop_scan(quiet=True)
            elif action == 'test':
                if not self.cfg.bot_token or not self.cfg.chat_id:
                    raise ValueError('Укажите Bot Token и личный Chat ID, затем нажмите «Сохранить».')
                bot = self._telegram()
                me = await bot.verify()
                self._verified = True
                await bot.send('✅ <b>DM Hunter подключён.</b>\n/start — управление мониторингом.', control_keyboard())
                self.log(f"Тест доставлен. Бот: @{me.get('username', 'bot')}")
            elif action == 'status':
                self._status()
            elif action != 'apply':
                raise ValueError(f'Неизвестная команда: {action}')
            self._status()

    def _start_scan(self):
        if self._scanner is not None and not self._scanner.done():
            return
        self._last_error = ''
        self._scanner = asyncio.create_task(self._scan_all())
        self._scanner.add_done_callback(self._scan_done)
        self.log('Мониторинг запущен.')

    async def _stop_scan(self, quiet=False):
        if self._scanner is not None:
            self._scanner.cancel()
            await asyncio.gather(self._scanner, return_exceptions=True)
            self._scanner = None
        if not quiet:
            self.log('Мониторинг остановлен. Очередь уведомлений продолжает доставляться.')

    def _scan_done(self, task):
        if not task.cancelled():
            error = task.exception()
            if error:
                self._last_error = f'{type(error).__name__}: {error}'
                self.log(f'Мониторинг остановлен из-за ошибки: {self._last_error}')
        self._status()

    async def _scan_all(self):
        tasks = [asyncio.create_task(self._collection(slug)) for slug in self.cfg.collections]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _collection(self, slug):
        cfg = self.cfg
        cursor = self.store.get_cursor(slug)
        if cursor is not None:
            self._positions[slug] = cursor
        last_discovery = float('-inf')
        failures = 0
        while True:
            try:
                if cursor is None:
                    self.log(f'{slug}: ищу свежие номера; первый поиск может занять несколько минут…')
                    latest = await discover_latest(self.client, slug,
                        cfg.initial_ids.get(slug, SEEDS.get(slug, 1)), cfg.gap_window)
                    cursor = max(0, latest - cfg.lookback)
                    self.store.set_cursor(slug, cursor)
                    self._frontiers[slug] = latest
                    self._positions[slug] = cursor
                    self.log(f'{slug}: найден номер #{latest}; проверю последние {cfg.lookback}.')
                    last_discovery = time.monotonic()
                elif time.monotonic() - last_discovery >= cfg.rescan_interval:
                    # Re-probe the frontier; do not jump a saved cursor or discard intervening gifts.
                    latest = await discover_latest(self.client, slug, max(1, cursor), cfg.gap_window)
                    self.log(f'{slug}: проверка границы — #{latest}; текущая позиция #{cursor}.')
                    if latest > cursor + cfg.gap_window:
                        # Missing serials ahead must not block later published ones. Schedule all
                        # intervening IDs for processing in normal batches, never move past unseen IDs.
                        self._frontiers[slug] = latest
                    last_discovery = time.monotonic()

                # Previously missing/hidden pages are retried durably for 24 hours.
                for number in self.store.due_gaps(slug, limit=3):
                    gift = await self._fetch(slug, number)
                    if gift is None:
                        self.store.defer_gap(slug, number)
                    else:
                        await self._process(gift)
                        if gift.owner_username:
                            self.store.remove_gap(slug, number)
                        else:
                            self.store.defer_gap(slug, number)

                misses = 0
                advanced = False
                for number in range(cursor + 1, cursor + max(cfg.scan_batch, cfg.gap_window) + 1):
                    gift = await self._fetch(slug, number)
                    if gift is None:
                        self.store.add_gap(slug, number)
                        if number < self._frontiers.get(slug, 0):
                            self.store.set_cursor(slug, number)
                            cursor = number
                            self._positions[slug] = cursor
                            advanced = True
                        misses += 1
                        # Continue across holes; wait at the real frontier instead of racing ahead.
                        if misses >= cfg.gap_window and number >= self._frontiers.get(slug, 0):
                            break
                    else:
                        await self._process(gift)
                        if not gift.owner_username:
                            self.store.add_gap(slug, number)
                        else:
                            self.store.remove_gap(slug, number)
                        misses = 0
                        self.store.set_cursor(slug, number)
                        cursor = number
                        self._positions[slug] = cursor
                        advanced = True
                failures = 0
                await asyncio.sleep(0.1 if advanced else cfg.poll_interval)
            except NFTFetchError as exc:
                failures += 1
                delay = max(float(getattr(exc, 'retry_after', 0) or 0), min(120, 3 * 2 ** min(failures, 5)))
                self.log(f'{slug}: {exc}. Повтор через {delay:g} с.')
                await asyncio.sleep(delay)

    async def _fetch(self, slug, number):
        self._requests += 1
        gift = await self.client.fetch(slug, number)
        if gift:
            self._checked += 1
        return gift

    async def _process(self, gift):
        if self.store.has_seen(gift.slug, gift.number):
            return
        reason = match_gift(gift, self.cfg)
        if reason is None:
            return
        data = asdict(gift)
        text, markup = build_notification(data, reason, self.cfg.offer_template)
        if self.store.enqueue(data, reason, text, markup):
            data.update(reason=reason, delivery_status='pending')
            self._emit('gift', gift=data)
            self.log(f'Найден {gift.slug} #{gift.number} — @{gift.owner_username}; в очереди Telegram.')

    async def _supervise(self, name, worker):
        while True:
            try:
                await worker()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log(f'{name}: {type(exc).__name__}: {exc}. Перезапуск через 10 с.')
                await asyncio.sleep(10)

    def _telegram(self):
        return TelegramClient(self.session, self.cfg.bot_token, self.cfg.chat_id, log=self.log)

    def _restart_bot(self):
        self._bot = asyncio.create_task(self._supervise('Управление Telegram', self._bot_loop)) if self.cfg.bot_token and self.cfg.chat_id else None

    async def _deliver_loop(self):
        while True:
            if not self.cfg.bot_token or not self.cfg.chat_id or not self._verified:
                await asyncio.sleep(1)
                continue
            await self._wait_telegram()
            for row in self.store.pending(limit=5):
                # Saving token/chat during an awaited request must not mix client credentials.
                bot = self._telegram()
                try:
                    await bot.send(row['text'], row['reply_markup'])
                except TelegramError as exc:
                    base_delay = max(5, float(exc.retry_after or 0))
                    delay = max(base_delay, min(300, base_delay * 2 ** min(row['attempts'], 20)))
                    self._tg_cooldown = max(self._tg_cooldown, time.monotonic() + float(exc.retry_after or 0))
                    self.store.mark_failed(row['id'], str(exc), retry_after=base_delay)
                    self.log(f'Уведомление #{row["id"]}: {exc}; повтор через {delay:g} с.')
                    await asyncio.sleep(max(1, float(exc.retry_after or 0)))
                    break
                else:
                    self.store.mark_sent(row['id'])
                    self._emit('delivered', id=row['id'], gift=row['gift'])
                    self.log(f"Доставлено: {row['gift']['slug']} #{row['gift']['number']}")
                    await asyncio.sleep(1.1)
            await asyncio.sleep(1)

    async def _wait_telegram(self):
        delay = self._tg_cooldown - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    async def _bot_loop(self):
        cfg = self.cfg
        bot = TelegramClient(self.session, cfg.bot_token, cfg.chat_id, log=self.log)
        offset = 0
        connected = False
        while True:
            try:
                if not connected:
                    me = await bot.verify()
                    connected = True
                    self._verified = True
                    self.log(f"Telegram подключён: @{me.get('username', 'bot')}")
                await self._wait_telegram()
                updates = await bot.updates(offset, timeout=20)
                for update in updates:
                    # Advance in memory before interpreting. Only live private commands are accepted.
                    offset = update['update_id'] + 1
                    msg = update.get('message', {})
                    chat = msg.get('chat', {})
                    if (str(chat.get('id', '')) != str(cfg.chat_id) or
                        str(msg.get('from', {}).get('id', '')) != str(cfg.chat_id) or
                        chat.get('type') != 'private' or msg.get('date', 0) < self._started - 5):
                        continue
                    text = msg.get('text', '').strip()
                    if text in ('/start', '/help'):
                        await bot.send('🎯 <b>DM Hunter</b>\n/start_scan — запуск\n/stop — остановка\n/status — состояние\n\nКнопка в находке открывает чат с готовым предложением. Отправку подтверждаете вы.', control_keyboard())
                    elif text in ('/start_scan', '🟢 Старт DM Hunter'):
                        await self._handle('start')
                        await bot.send('🟢 Мониторинг запущен.', control_keyboard())
                    elif text in ('/stop', '🔴 Стоп DM Hunter'):
                        await self._handle('stop')
                        await bot.send('⏹ Мониторинг остановлен. Доставка очереди продолжается.', control_keyboard())
                    elif text in ('/status', '📊 Статус', '📊 Статус модулей'):
                        stats = self.store.stats()
                        state = 'работает' if self._scanner and not self._scanner.done() else 'остановлен'
                        positions = ', '.join(f'{s}: #{n}' for s, n in self._positions.items()) or 'поиск границы'
                        await bot.send(f'📊 DM Hunter: {state}\nПроверено страниц: {self._checked}\nОчередь: {stats.get("pending", 0)}\nДоставлено: {stats.get("sent", 0)}\n{positions}', control_keyboard())
            except TelegramError as exc:
                self._tg_cooldown = max(self._tg_cooldown, time.monotonic() + float(exc.retry_after or 0))
                self.log(f'Telegram: {exc}; переподключение…')
                await asyncio.sleep(max(5, float(exc.retry_after or 0)))
            except Exception as exc:
                self.log(f'Ошибка обработки команды Telegram: {type(exc).__name__}: {exc}')
                await asyncio.sleep(5)

    def _status(self):
        if not hasattr(self, 'store'):
            return
        self._emit('status', running=bool(self._scanner and not self._scanner.done()),
                   telegram=self._verified, positions=dict(self._positions), checked=self._checked,
                   stats=self.store.stats(), error=self._last_error)

