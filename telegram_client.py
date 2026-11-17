"""Telegram Bot API client and safe notification rendering.

This bot sends messages only to the configured owner. Contact buttons open a
public owner's chat with a draft; they never send an unsolicited private message.
"""
from __future__ import annotations

import asyncio
import html
import math
import re
from typing import Any, Callable
from urllib.parse import quote, urlencode

import aiohttp


_GIFT_NOT_FOUND = object()


class TelegramError(Exception):
    """Safe to log: no URLs, credentials or raw transport exceptions."""

    def __init__(self, message: str, retry_after: float | None = None,
                 status_code: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code


def _retry_seconds(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return max(1.0, seconds) if math.isfinite(seconds) and seconds >= 0 else None


def control_keyboard() -> dict:
    return {
        "keyboard": [
            [{"text": "🟢 Старт DM Hunter"}, {"text": "🔴 Стоп DM Hunter"}],
            [{"text": "📊 Статус"}],
        ],
        "resize_keyboard": True,
    }


class TelegramClient:
    def __init__(self, session: aiohttp.ClientSession, token: str, chat_id: str,
                 log: Callable[[str], None] | None = None):
        self.session = session
        self.token = str(token).strip()
        self.chat_id = str(chat_id).strip()
        self.log = log or (lambda message: None)

    @staticmethod
    def _api_error(code: int, retry_after: float | None = None) -> TelegramError:
        messages = {
            400: "Telegram: некорректный запрос. Проверьте Chat ID и содержимое сообщения.",
            401: "Telegram: неверный или отозванный Bot Token.",
            403: "Telegram: отправка запрещена. Откройте бота и нажмите /start; проверьте блокировку.",
            404: "Telegram: бот или метод API не найден. Проверьте Bot Token.",
            409: "Telegram: конфликт получения обновлений. Остановите другой экземпляр бота и удалите webhook.",
            429: "Telegram: превышен лимит запросов. Доставка будет повторена после паузы.",
        }
        return TelegramError(messages.get(code, f"Telegram: ошибка API ({code})."),
                             retry_after=retry_after, status_code=code)

    async def _request(self, method: str, payload: dict | None = None,
                       timeout: float = 15) -> Any:
        if not self.token:
            raise TelegramError("Telegram: Bot Token не заполнен.")
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            async with self.session.post(url, json=payload or {}, timeout=timeout) as response:
                retry_after = _retry_seconds(response.headers.get("Retry-After"))
                try:
                    body = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    if response.status != 200:
                        raise self._api_error(response.status, retry_after)
                    raise TelegramError("Telegram: сервер вернул некорректный JSON.") from None
                if not isinstance(body, dict):
                    raise TelegramError("Telegram: неверный формат ответа API.")
                parameters = body.get("parameters")
                if isinstance(parameters, dict):
                    retry_after = _retry_seconds(parameters.get("retry_after")) or retry_after
                if response.status != 200 or body.get("ok") is not True:
                    code = body.get("error_code", response.status)
                    if not isinstance(code, int) or code == 200:
                        code = response.status if response.status != 200 else 400
                    if method == 'getUniqueGift' and code == 400:
                        description = str(body.get('description', '')).casefold()
                        missing = ('gift not found', 'gift is not found', 'gift_not_found',
                                   'gift_slug_invalid', 'gift name is invalid', 'gift_name_invalid',
                                   'unique gift not found', 'gift does not exist')
                        if any(marker in description for marker in missing):
                            return _GIFT_NOT_FOUND
                        raise TelegramError('Telegram getUniqueGift: сервер отклонил запрос подарка.', status_code=400)
                    raise self._api_error(code, retry_after)
                return body.get("result")
        except TelegramError:
            raise
        except (asyncio.TimeoutError, TimeoutError):
            raise TelegramError("Telegram: время ожидания ответа истекло.", retry_after=5) from None
        except (aiohttp.ClientError, OSError):
            # Raw aiohttp exceptions can contain the full URL and bot token.
            raise TelegramError("Telegram: ошибка сети. Доставка будет повторена.", retry_after=5) from None

    async def send(self, text: str, reply_markup: dict | None = None) -> bool:
        if not self.chat_id:
            raise TelegramError("Telegram: Chat ID не заполнен.")
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self._request("sendMessage", payload)
        if not isinstance(result, dict) or "message_id" not in result:
            raise TelegramError("Telegram: сервер не подтвердил доставку сообщения.")
        return True

    async def updates(self, offset: int, timeout: int = 20) -> list[dict]:
        result = await self._request("getUpdates", {
            "offset": offset,
            "timeout": timeout,
            "limit": 50,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=timeout + 10)
        if not isinstance(result, list) or not all(isinstance(item, dict) for item in result):
            raise TelegramError("Telegram: неверный формат обновлений.")
        return result

    async def verify(self) -> dict:
        result = await self._request("getMe")
        if not isinstance(result, dict) or not result.get("is_bot"):
            raise TelegramError("Telegram: Bot Token не соответствует боту.")
        return result

    async def unique_gift(self, name: str) -> dict | None:
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{1,63}-[1-9][0-9]*', name):
            raise ValueError('Некорректное имя уникального подарка')
        result = await self._request('getUniqueGift', {'name': name})
        if result is _GIFT_NOT_FOUND:
            return None
        if not isinstance(result, dict):
            raise TelegramError('Telegram getUniqueGift: неверный формат ответа.')
        return result

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._request("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def build_notification(gift: dict, reason: str, offer_template: str) -> tuple[str, dict]:
    """Return escaped HTML plus safe gift and prefilled-contact buttons."""
    slug = str(gift["slug"])
    number = int(gift.get("number", gift.get("num", 0)))
    gift_name = f"{slug} #{number}"
    model = str(gift.get("model", "Неизвестно"))
    backdrop = str(gift.get("backdrop", "Неизвестно"))
    symbol = str(gift.get("symbol", "Неизвестно"))
    username = str(gift.get("owner_username") or "").removeprefix("@")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
        username = ""
    owner = f"@{username}" if username else str(gift.get("owner_display") or "Скрыт / TON")
    esc = lambda value: html.escape(str(value), quote=True)
    text = "\n".join([
        "🎯 <b>DM HUNTER: НАЙДЕН ПОДАРОК</b>",
        "",
        f"🎁 <b>{esc(gift_name)}</b>",
        f"⚡ <b>Совпадение:</b> {esc(reason)}",
        f"🧩 <b>Модель:</b> {esc(model)}",
        f"🖼 <b>Фон:</b> {esc(backdrop)}",
        f"🔣 <b>Символ:</b> {esc(symbol)}",
        f"👤 <b>Владелец:</b> {esc(owner)}",
        "",
        "Кнопка чата откроет готовый текст; отправку подтверждаете вы." if username
        else "У владельца нет доступного публичного username.",
    ])
    gift_url = f"https://t.me/nft/{quote(slug, safe='')}-{number}"
    buttons = [[{"text": "🎁 Открыть подарок", "url": gift_url}]]
    if username:
        draft = offer_template.format(
            gift=gift_name, number=number, collection=slug,
            model=model, backdrop=backdrop,
        )
        dm_url = f"https://t.me/{username}?{urlencode({'text': draft}, quote_via=quote)}"
        buttons.append([{"text": "💬 Написать владельцу", "url": dm_url}])
    return text, {"inline_keyboard": buttons}
