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
                    burned=bool(data.get('is_burned', False)), owner_status="not_checked")
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
        self._fallback_owner_time = {}

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
                if exc.status_code != 400:
                    raise NFTFetchError(f'{slug} #{number}: getUniqueGift: {exc}', exc.retry_after) from None
                # An unclassified 400 is not evidence that this gift is absent.
                # Try the same public card before pausing this collection.
                try:
                    gift = await asyncio.wait_for(self.public.fetch(slug, number), timeout=8)
                except (NFTFetchError, asyncio.TimeoutError):
                    raise NFTFetchError(f'{slug} #{number}: {exc}; публичная карточка тоже недоступна') from None
                if gift is None:
                    confirmed = getattr(self.public, 'confirmed_missing', frozenset())
                    if not isinstance(confirmed, (set, frozenset)) or key not in confirmed:
                        raise NFTFetchError(f'{slug} #{number}: {exc}; пустая HTML-карточка не подтверждает отсутствие') from None
                if gift is not None:
                    self._fallback_owner_time[key] = time.monotonic()
                self._notice(slug, f'#{number}: {exc}; ответ проверен по публичной карточке', kind='api_fallback')
            else:
                self._fallback_owner_time.pop(key, None)
                gift = parse_unique_gift(data, slug, number) if data is not None else None
            self._cache[key] = (time.monotonic() + (60 if gift else 5), gift)
            self._cache.move_to_end(key)
            while len(self._cache) > 2048:
                expired, _ = self._cache.popitem(last=False)
                self._fallback_owner_time.pop(expired, None)
            return gift

    async def fetch(self, slug: str, number: int) -> Gift | None:
        gift = await self.probe(slug, number)
        if gift is None or gift.burned or not self.candidate(gift):
            return gift
        # A public fallback in probe() can already contain the owner result.
        if gift.owner_status in ('public', 'no_public_username') and time.monotonic() - self._fallback_owner_time.get((slug, number), float('-inf')) < 5:
            return self._with_owner_result(gift, gift)
        try:
            owner_page = await asyncio.wait_for(self.public.fetch(slug, number), timeout=8)
        except (NFTFetchError, asyncio.TimeoutError) as exc:
            self._notice(slug, f'#{number}: страница владельца не загружена ({type(exc).__name__}); повтор позже', kind='unavailable')
            return replace(gift, owner_username=None, owner_status='unavailable', owner_display='Страница владельца не загружена')
        if owner_page is None:
            self._notice(slug, f'#{number}: страница владельца не загружена; повтор позже', kind='unavailable')
            return replace(gift, owner_username=None, owner_status='unavailable', owner_display='Страница владельца не загружена')
        return self._with_owner_result(gift, owner_page)

    def _with_owner_result(self, gift, owner_page):
        if owner_page.owner_username:
            return replace(gift, owner_username=owner_page.owner_username,
                           owner_display=owner_page.owner_display, owner_status='public')
        if owner_page.owner_status == 'no_public_username':
            self._notice(gift.slug, f'#{gift.number}: карточка загружена, у владельца нет публичной ссылки на username; следующая проверка через 30 минут', kind='private')
            return replace(gift, owner_username=None, owner_display=owner_page.owner_display,
                           owner_status='no_public_username')
        self._notice(gift.slug, f'#{gift.number}: карточка загружена без данных владельца; повтор позже', kind='unavailable')
        return replace(gift, owner_username=None, owner_status='unavailable', owner_display='Нет данных владельца на странице')

    def _notice(self, slug, message, kind='owner'):
        now = time.monotonic()
        key = (slug, kind)
        if now - self._owner_notice.get(key, float('-inf')) >= 60:
            self.log(f'{slug}: {message}.')
            self._owner_notice[key] = now
