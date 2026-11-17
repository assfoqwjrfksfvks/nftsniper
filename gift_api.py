"""Official Bot API metadata source; public HTML is only used for owner lookup."""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from dataclasses import replace
import time

from nft_client import Gift, NFTFetchError
from telegram_client import TelegramError


def parse_unique_gift(data: dict, slug: str, number: int) -> Gift:
    try:
        if data['name'].casefold() != f'{slug}-{number}'.casefold() or type(data['number']) is not int or data['number'] != number:
            raise ValueError
        def trait(key):
            item = data[key]
            name = item['name']
            rarity = item['rarity_per_mille']
            if not isinstance(name, str) or not name or type(rarity) is not int or not 0 <= rarity <= 1000:
                raise ValueError
            # Crafted models have 0 permille because percentage rarity is undefined.
            return name, rarity / 10 if rarity > 0 else 100.0
        model, mr = trait('model')
        backdrop, br = trait('backdrop')
        symbol, _ = trait('symbol')
        return Gift(slug, number, f'https://t.me/nft/{slug}-{number}', None,
                    'Username пока недоступен', model, mr, backdrop, br, symbol,
                    burned=bool(data.get('is_burned', False)))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise NFTFetchError(f'{slug} #{number}: неожиданный формат getUniqueGift; номер не пропущен') from None


class GiftAPIClient:
    def __init__(self, bot, public, candidate, log=lambda _: None, request_interval=1.0):
        self.bot, self.public, self.candidate, self.log = bot, public, candidate, log
        self.request_interval = request_interval
        self._lock = asyncio.Lock()
        self._next_request = 0.0
        self._cache = OrderedDict()
        self._owner_notice = {}

    async def probe(self, slug: str, number: int) -> Gift | None:
        key = (slug, number)
        async with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                self._cache.move_to_end(key)
                return cached[1]
            delay = self._next_request - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request = time.monotonic() + max(.5, self.request_interval)
            try:
                data = await self.bot.unique_gift(f'{slug}-{number}')
            except TelegramError as exc:
                retry = float(exc.retry_after or 0)
                self._next_request = max(self._next_request, time.monotonic() + retry)
                raise NFTFetchError(f'getUniqueGift: {exc}', exc.retry_after) from None
            gift = parse_unique_gift(data, slug, number) if data is not None else None
            self._cache[key] = (time.monotonic() + (60 if gift else 5), gift)
            self._cache.move_to_end(key)
            while len(self._cache) > 2048:
                self._cache.popitem(last=False)
            return gift

    async def fetch(self, slug: str, number: int) -> Gift | None:
        gift = await self.probe(slug, number)
        if gift is None or gift.burned or not self.candidate(gift):
            return gift
        try:
            owner_page = await asyncio.wait_for(self.public.fetch(slug, number), timeout=8)
        except (NFTFetchError, asyncio.TimeoutError) as exc:
            # Metadata still exists: owner-page failure must not block the cursor.
            owner_page = None
            self._notice(slug, f'#{number}: страница владельца недоступна ({type(exc).__name__}); номер сохранён для повтора')
        if owner_page is None or not owner_page.owner_username:
            self._notice(slug, f'#{number}: API вернул подарок, публичный username пока недоступен; повтор позже')
            return gift
        return replace(gift, owner_username=owner_page.owner_username,
                       owner_display=owner_page.owner_display)

    def _notice(self, slug, message):
        now = time.monotonic()
        if now - self._owner_notice.get(slug, float('-inf')) >= 60:
            self.log(f'{slug}: {message}.')
            self._owner_notice[slug] = now
