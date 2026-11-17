"""Read public Telegram gift pages; no account or bot credentials are used here."""
from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import aiohttp
from bs4 import BeautifulSoup


@dataclass(frozen=True)
class Gift:
    slug: str
    number: int
    url: str
    owner_username: str | None
    owner_display: str
    model: str
    model_rarity: float
    backdrop: str
    backdrop_rarity: float
    symbol: str
    issued: int | None = None
    burned: bool = False


class NFTFetchError(Exception):
    """A temporary/unrecognised response, never evidence that a serial is absent."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class _GiftPlaceholder(NFTFetchError):
    """HTTP 200 generic NFT preview; no reliable claim about the cause."""


_RARITY = re.compile(r"(\d+(?:[.,]\d+)?)\s*([%‰])")
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}\Z")
_RESERVED = {
    'nft', 'share', 'proxy', 'socks', 'joinchat', 'c', 's', 'addstickers',
    'addemoji', 'setlanguage', 'confirmphone', 'iv', 'login', 'boost',
    'boosts', 'contact', 'giftcode', 'm', 'passport', 'resolve', 'invoice',
    'addlist', 'bg', 'privacy', 'tour', 'faq', 'apps', 'blog',
}


def parse_rarity(text: str) -> float:
    """Return rarity in percent (12 permille = 1.2 percent)."""
    match = _RARITY.search(text)
    if not match:
        return 100.0
    value = float(match.group(1).replace(',', '.'))
    return value / 10 if match.group(2) == '‰' else value


def _username(href: str) -> str | None:
    parsed = urlparse(href)
    if parsed.scheme == 'tg' and parsed.netloc == 'resolve':
        candidate = parse_qs(parsed.query).get('domain', [''])[0]
    elif parsed.scheme in ('https', 'http', '') and parsed.netloc.lower() in {
        't.me', 'www.t.me', 'telegram.me', 'www.telegram.me',
    }:
        # No nested paths, credentials, alternate ports, or invite links.
        candidate = parsed.path.strip('/')
    else:
        return None
    if _USERNAME.fullmatch(candidate) and candidate.lower() not in _RESERVED:
        return candidate
    return None


def _trait(cell) -> tuple[str, float]:
    rarity = parse_rarity(cell.get_text(' ', strip=True))
    # Telegram uses <mark> for rarity. Keep only the trait name, not its badge.
    for mark in cell.find_all('mark'):
        mark.decompose()
    value = cell.get_text(' ', strip=True)
    value = re.sub(r'\s*\(?\d+(?:[.,]\d+)?\s*[%‰]\)?\s*$', '', value)
    return value.strip() or 'Unknown', rarity


def parse_gift_html(html: str, slug: str, number: int) -> Gift | None:
    """Parse known Telegram markup; return None only for a known missing page.

    Unknown HTML raises NFTFetchError so a changed layout or gateway error cannot
    silently advance the scanner past gifts.
    """
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table', class_='tgme_gift_table')
    if table is None:
        # Missing NFT URLs currently redirect to the Telegram home page.
        title = soup.title.get_text(strip=True) if soup.title else ''
        if title == 'Telegram Messenger' and soup.select_one('.tl_main_head'):
            return None
        # Telegram also returns this shell for KNOWN EXISTING gifts when
        # throttling public-page reads. It must never mean 'missing'.
        page_title = soup.select_one('.tgme_page_title')
        description = soup.select_one('.tgme_page_description')
        if (title == 'Telegram: Collectible Gift' and page_title is not None
                and page_title.get_text(' ', strip=True) == 'Collectible Gift'
                and description is not None
                and description.get_text(' ', strip=True) == 'This is a unique collectible gift on Telegram.'):
            raise _GiftPlaceholder(f'{slug} #{number}: HTTP 200, пустая карточка NFT; характеристики недоступны')
        raise NFTFetchError('Telegram вернул неизвестную страницу вместо карточки подарка')
    cells = {}
    for row in table.find_all('tr'):
        th, td = row.find('th'), row.find('td')
        if th is not None and td is not None:
            cells[th.get_text(' ', strip=True).casefold()] = td
    if not {'model', 'backdrop'}.issubset(cells):
        raise NFTFetchError('Изменилась таблица Telegram: отсутствуют Model или Backdrop')

    owner = cells.get('owner')
    owner_display = owner.get_text(' ', strip=True) if owner is not None else 'Скрыт'
    owner_username = None
    if owner is not None:
        for link in owner.find_all('a', href=True):
            owner_username = _username(link['href'])
            if owner_username:
                break
    model, model_rarity = _trait(cells['model'])
    backdrop, backdrop_rarity = _trait(cells['backdrop'])
    symbol = _trait(cells['symbol'])[0] if 'symbol' in cells else 'Unknown'
    quantity = cells.get('quantity')
    issued = None
    if quantity is not None:
        match = re.search(r'([\d\s\u00a0\u202f,]+)\s*/', quantity.get_text(' ', strip=True))
        if match:
            digits = re.sub(r'\D', '', match.group(1))
            if digits:
                issued = int(digits)
    return Gift(
        slug=slug, number=number, url=f'https://t.me/nft/{slug}-{number}',
        owner_username=owner_username, owner_display=owner_display or 'Скрыт',
        model=model, model_rarity=model_rarity,
        backdrop=backdrop, backdrop_rarity=backdrop_rarity, symbol=symbol,
        issued=issued,
    )


def _retry_after(value: str | None, fallback: float = 30) -> float:
    if not value:
        return fallback
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else fallback
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return fallback


class NFTClient:
    """Paced reader. Only explicit HTTP failures impose a shared cooldown.

    A generic HTTP 200 shell is rechecked once, then recorded as unavailable.
    It is not proof of throttling or permanent absence; the scanner retains gaps.
    """
    def __init__(self, session: aiohttp.ClientSession, log=lambda _: None, request_interval: float = 1.0):
        self.session = session
        self.log = log
        self.request_interval = max(0.0, float(request_interval))
        self._pace_lock = asyncio.Lock()
        self._next_request = 0.0
        self._unavailable: set[tuple[str, int]] = set()
        self._last_notice: dict[str, float] = {}

    async def _pace(self):
        async with self._pace_lock:
            loop = asyncio.get_running_loop()
            # A concurrent in-flight response can extend the global cooldown
            # while this coroutine sleeps, so always re-check the deadline.
            while (delay := self._next_request - loop.time()) > 0:
                await asyncio.sleep(delay)
            self._next_request = max(self._next_request, loop.time()) + self.request_interval

    def _cooldown(self, delay: float = 60, adaptive: bool = False):
        self._next_request = max(self._next_request, asyncio.get_running_loop().time() + delay)
        if adaptive:
            old_interval = self.request_interval
            self.request_interval = max(old_interval, min(5.0, max(1.0, old_interval * 1.5)))
            if self.request_interval > old_interval:
                self.log(f'Telegram: снижаю частоту запросов; интервал {self.request_interval:g} с.')

    async def fetch(self, slug: str, number: int) -> Gift | None:
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{1,63}', slug) or not isinstance(number, int) or isinstance(number, bool) or number < 1:
            raise ValueError('Некорректные коллекция или номер подарка')
        try:
            gift = await self._fetch_page(slug, number)
        except _GiftPlaceholder:
            # Telegram sometimes serves this template for real serials too.
            # Retry this exact request once, without probing unrelated gifts or
            # treating an unavailable anchor as a global server rate limit.
            try:
                gift = await self._fetch_page(slug, number)
            except _GiftPlaceholder:
                if len(self._unavailable) >= 5000:
                    self._unavailable.clear()
                self._unavailable.add((slug, number))
                now = asyncio.get_running_loop().time()
                if now - self._last_notice.get(slug, float('-inf')) >= 60:
                    self.log(f'{slug} #{number}: HTTP 200, пустая карточка; номер будет перепроверен.')
                    self._last_notice[slug] = now
                return None
        self._unavailable.discard((slug, number))
        return gift

    async def _fetch_page(self, slug: str, number: int) -> Gift | None:
        await self._pace()
        url = f'https://t.me/nft/{slug}-{number}'
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status in (404, 410):
                    return None
                if response.status == 429:
                    retry_after = _retry_after(response.headers.get('Retry-After'))
                    self._cooldown(retry_after, adaptive=True)
                    raise NFTFetchError('Telegram ограничил частоту запросов (HTTP 429)', retry_after)
                if response.status != 200:
                    retry_after = 60 if response.status == 403 else None
                    if retry_after:
                        self._cooldown(retry_after, adaptive=True)
                    raise NFTFetchError(f'Ошибка страницы Telegram: HTTP {response.status}', retry_after)
                # Redirects must remain on Telegram's own public sites.
                final = urlparse(str(response.url))
                if final.hostname not in {'t.me', 'www.t.me', 'telegram.org', 'www.telegram.org'}:
                    raise NFTFetchError('Telegram перенаправил запрос на неизвестный сайт')
                body = await response.text()
                return parse_gift_html(body, slug, number)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise NFTFetchError(f'Не удалось загрузить {slug} #{number}: {type(exc).__name__}') from exc
