"""Scanner regressions using public-page fakes and a real durable SQLite queue."""
import asyncio
from dataclasses import replace
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from hunter import HunterEngine, discover_latest, match_gift
from nft_client import Gift, NFTFetchError
from settings import Settings
from store import Store
from telegram_client import TelegramError


def gift(number, **changes):
    values = dict(slug='CandyCane', number=number,
                  url=f'https://t.me/nft/CandyCane-{number}',
                  owner_username='alice_test', owner_display='Alice',
                  model='Common', model_rarity=50.0,
                  backdrop='Black', backdrop_rarity=50.0, symbol='Stars')
    values.update(changes)
    return Gift(**values)


class EndCollection(Exception):
    """Stop an intentionally infinite scanner at a deterministic fake request."""


class FakeNFT:
    def __init__(self, last, holes=(), issued=None):
        self.last = last
        self.holes = set(holes)
        self.issued = issued
        self.requests = []

    async def fetch(self, slug, number):
        self.requests.append(number)
        if number > self.last or number in self.holes:
            return None
        return gift(number, slug=slug, issued=self.issued)


class MatchTests(unittest.TestCase):
    def test_exact_background_and_or_rarity_triggers(self):
        cfg = Settings(target_backdrops='Black', max_model_rarity=1, max_backdrop_rarity=1).validate()
        self.assertIsNotNone(match_gift(gift(1), cfg))
        self.assertIsNone(match_gift(gift(1, backdrop='Blackberry'), cfg))
        self.assertIsNotNone(match_gift(gift(1, backdrop='Blackberry', model_rarity=0.5), cfg))
        self.assertIsNotNone(match_gift(gift(1, backdrop='White', backdrop_rarity=0.2), cfg))
        self.assertIsNone(match_gift(gift(1, owner_username=None), cfg))


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_contiguous_frontier_without_fixed_search_ceiling(self):
        client = FakeNFT(last=350_000)
        self.assertEqual(await discover_latest(client, 'CandyCane', 1), 350_000)
        self.assertLess(len(client.requests), 600)

    async def test_short_holes_in_binary_search_do_not_truncate_tail(self):
        client = FakeNFT(last=1200, holes={1100, 1150, 1199})
        self.assertEqual(await discover_latest(client, 'CandyCane', 1000, window=10), 1200)

    async def test_short_hole_at_exponential_probe_does_not_truncate_tail(self):
        client = FakeNFT(last=2050, holes={33})
        self.assertEqual(await discover_latest(client, 'CandyCane', 1, window=10), 2050)

    async def test_quantity_is_hint_not_upper_serial_limit(self):
        client = FakeNFT(last=2050, issued=1700)
        self.assertEqual(await discover_latest(client, 'CandyCane', 1000), 2050)

    async def test_transport_failure_propagates_instead_of_becoming_missing(self):
        client = FakeNFT(last=100)
        client.fetch = AsyncMock(side_effect=NFTFetchError('temporary network failure'))
        with self.assertRaises(NFTFetchError):
            await discover_latest(client, 'CandyCane', 1)


class HunterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'queue.sqlite3'
        self.store = Store(self.path)
        self.engine = object.__new__(HunterEngine)
        self.engine.cfg = Settings(dm_collections='CandyCane').validate()
        self.engine.store = self.store
        self.engine.events = queue.Queue()
        self.engine.log = lambda message: None
        self.engine._scanner = None
        self.engine._bot = None
        self.engine._outbox = None
        self.engine._frontiers = {}
        self.engine._positions = {}
        self.engine._requests = 0
        self.engine._checked = 0
        self.engine._last_error = ''
        self.engine._verified = False
        self.engine._tg_cooldown = 0.0
        self.engine._lock = asyncio.Lock()
        self.tasks = []

    async def asyncTearDown(self):
        if self.engine._scanner:
            self.engine._scanner.cancel()
            self.tasks.append(self.engine._scanner)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.store.close()
        self.temporary.cleanup()

    async def test_start_stop_start_awaits_old_task_and_start_is_idempotent(self):
        active = 0
        maximum = 0
        starts = 0

        async def scan():
            nonlocal active, maximum, starts
            active += 1
            starts += 1
            maximum = max(maximum, active)
            try:
                await asyncio.Future()
            finally:
                # Cancellation cleanup itself yields: the next start must wait for it.
                await asyncio.sleep(0)
                active -= 1

        self.engine._scan_all = scan
        await self.engine._handle('start')
        await asyncio.sleep(0)
        first = self.engine._scanner
        await self.engine._handle('start')
        self.assertIs(self.engine._scanner, first)
        await self.engine._handle('stop')
        self.assertTrue(first.done())
        self.assertEqual(active, 0)
        await self.engine._handle('start')
        await asyncio.sleep(0)
        self.assertIsNot(self.engine._scanner, first)
        self.assertEqual(starts, 2)
        self.assertEqual(maximum, 1)
        await self.engine._handle('stop')

    async def test_process_is_durable_and_deduplicated_across_restart(self):
        await self.engine._process(gift(12))
        await self.engine._process(gift(12))
        self.assertEqual(self.store.stats()['pending'], 1)
        original_text = self.store.pending()[0]['text']
        self.store.close()
        self.store = Store(self.path)
        self.engine.store = self.store
        await self.engine._process(gift(12))
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(self.store.pending()[0]['text'], original_text)
        self.assertIn('CandyCane #12', original_text)

    async def test_delivery_failure_is_retried_before_marking_sent(self):
        await self.engine._process(gift(22))
        self.engine.cfg = replace(self.engine.cfg, bot_token='12345:' + 'x' * 24, chat_id='12345')
        self.engine._verified = True
        clock = [1000.0]
        delivered = asyncio.Event()
        calls = []
        original_sleep = asyncio.sleep
        original_emit = self.engine._emit

        async def sleep(seconds):
            clock[0] += seconds
            await original_sleep(0)

        class FakeBot:
            async def send(inner, text, markup):
                calls.append((clock[0], text))
                if len(calls) == 1:
                    raise TelegramError('rate limit', retry_after=11)
                self.assertEqual(self.store.stats()['sent'], 0)
                return True

        def emit(kind, **data):
            original_emit(kind, **data)
            if kind == 'delivered':
                delivered.set()

        self.engine._telegram = lambda: FakeBot()
        self.engine._emit = emit
        with patch('store.time.time', side_effect=lambda: clock[0]), patch('hunter.asyncio.sleep', new=sleep):
            task = asyncio.create_task(self.engine._deliver_loop())
            self.tasks.append(task)
            await asyncio.wait_for(delivered.wait(), 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(calls[1][0] - calls[0][0], 11)
        self.assertEqual(self.store.stats()['sent'], 1)
        self.assertEqual(self.store.stats()['pending'], 0)
        self.assertEqual(self.store.recent()[0]['attempts'], 1)
        await self.engine._process(gift(22))
        self.assertEqual(self.store.stats()['total'], 1)

    async def test_bot_accepts_only_recent_commands_from_configured_private_owner(self):
        self.engine.cfg = replace(self.engine.cfg, bot_token='12345:' + 'x' * 24, chat_id='12345')
        self.engine.session = object()
        self.engine._started = 1000.0
        actions = []
        messages = []
        offsets = []

        def update(update_id, chat='12345', sender='12345', kind='private', date=1001, text='/start_scan'):
            return {'update_id': update_id, 'message': {
                'chat': {'id': int(chat), 'type': kind},
                'from': {'id': int(sender)}, 'date': date, 'text': text}}

        batch = [update(1, chat='99999'), update(2, sender='99999'),
                 update(3, kind='group'), update(4, date=990),
                 update(5), update(6, text='/stop'), update(7, text='/status')]

        async def handle(action):
            actions.append(action)

        class FakeBot:
            async def verify(inner):
                return {'is_bot': True, 'username': 'test_bot'}

            async def updates(inner, offset, timeout):
                offsets.append(offset)
                if len(offsets) > 1:
                    raise asyncio.CancelledError
                return batch

            async def send(inner, text, markup):
                messages.append(text)
                return True

        self.engine._handle = handle
        with patch('hunter.TelegramClient', return_value=FakeBot()), self.assertRaises(asyncio.CancelledError):
            await self.engine._bot_loop()
        self.assertEqual(actions, ['start', 'stop'])
        self.assertEqual(len(messages), 3)
        self.assertEqual(offsets, [0, 8])
        self.assertTrue(self.engine._verified)

    async def test_collection_crosses_missing_number_and_retains_retry_gap(self):
        self.store.set_cursor('CandyCane', 100)
        self.engine.cfg = replace(self.engine.cfg, scan_batch=5, gap_window=3)
        requests = []

        async def fetch(slug, number):
            requests.append(number)
            if len(requests) > 5:
                raise EndCollection
            return gift(number) if number == 102 else None

        self.engine.client = type('Client', (), {'fetch': staticmethod(fetch)})()
        with patch('hunter.discover_latest', new=AsyncMock(return_value=102)), self.assertRaises(EndCollection):
            await self.engine._collection('CandyCane')
        self.assertEqual(self.store.get_cursor('CandyCane'), 102)
        self.assertTrue(self.store.has_seen('CandyCane', 102))
        gaps = {r['number'] for r in self.store.db.execute('SELECT number FROM gaps')}
        self.assertIn(101, gaps)
        self.assertEqual(requests[:5], [101, 102, 103, 104, 105])

    async def test_confirmed_far_frontier_crosses_entire_missing_batch(self):
        self.store.set_cursor('CandyCane', 100)
        self.engine.cfg = replace(self.engine.cfg, scan_batch=20, gap_window=3)
        self.engine._frontiers['CandyCane'] = 140
        calls = []

        async def fetch(slug, number):
            calls.append(number)
            if number > 140:
                raise EndCollection
            if len(calls) > 200:
                raise AssertionError('Scanner repeats a missing first batch despite a confirmed farther frontier')
            return gift(number) if number == 140 else None

        self.engine.client = type('Client', (), {'fetch': staticmethod(fetch)})()
        original_sleep = asyncio.sleep

        async def fast_sleep(seconds):
            await original_sleep(0)

        with patch('hunter.asyncio.sleep', new=fast_sleep), patch('hunter.discover_latest', new=AsyncMock(return_value=140)), self.assertRaises(EndCollection):
            await self.engine._collection('CandyCane')
        self.assertEqual(self.store.get_cursor('CandyCane'), 140)
        self.assertTrue(self.store.has_seen('CandyCane', 140))
        gaps = {r['number'] for r in self.store.db.execute('SELECT number FROM gaps')}
        self.assertTrue(set(range(101, 140)).issubset(gaps))

    async def test_small_new_collection_scans_number_one(self):
        self.engine.cfg = replace(self.engine.cfg, lookback=50)

        async def fetch(slug, number):
            if number > 1:
                raise EndCollection
            return gift(number)

        self.engine.client = type('Client', (), {'fetch': staticmethod(fetch)})()
        with patch('hunter.discover_latest', new=AsyncMock(return_value=10)), self.assertRaises(EndCollection):
            await self.engine._collection('CandyCane')
        self.assertTrue(self.store.has_seen('CandyCane', 1))


if __name__ == '__main__':
    unittest.main()
