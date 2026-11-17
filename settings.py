"""Validated portable settings. No credentials or application paths are baked in."""
from __future__ import annotations

import json
import math
import os
import re
import string
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class Settings:
    bot_token: str = ""
    chat_id: str = ""
    dm_collections: str = "JingleBells, CandyCane, BerryBox, LolPop, HomemadeCake"
    max_model_rarity: float = 5.0
    max_backdrop_rarity: float = 5.0
    target_backdrops: str = "Black, Onyx Black, Midnight"
    offer_template: str = "Привет! Увидел твой подарок {gift}. Готов обсудить покупку — актуально?"
    poll_interval: float = 8.0
    request_interval: float = 1.0
    rescan_interval: float = 900.0
    lookback: int = 50
    scan_batch: int = 20
    gap_window: int = 10
    initial_ids: dict[str, int] = field(default_factory=dict)

    @property
    def collections(self) -> list[str]:
        return list(dict.fromkeys(s.strip() for s in self.dm_collections.split(",") if s.strip()))

    def validate(self) -> "Settings":
        for key in ("bot_token", "chat_id", "dm_collections", "target_backdrops", "offer_template"):
            value = getattr(self, key)
            if key == "chat_id" and isinstance(value, int) and not isinstance(value, bool):
                value = str(value)
            if not isinstance(value, str):
                raise ValueError(f"Поле {key} должно быть текстом.")
            setattr(self, key, value.strip())
        if self.bot_token and not re.fullmatch(r"[0-9]{5,}:[A-Za-z0-9_-]{20,}", self.bot_token):
            raise ValueError("Некорректный Bot Token. Скопируйте токен из BotFather целиком.")
        if self.chat_id and not re.fullmatch(r"[1-9][0-9]*", self.chat_id):
            raise ValueError("Chat ID должен быть положительным целым числом личного чата.")
        if not self.collections:
            raise ValueError("Укажите хотя бы одну коллекцию.")
        if len(self.collections) > 30:
            raise ValueError("Допускается не более 30 коллекций.")
        for slug in self.collections:
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,63}", slug):
                raise ValueError(f"Некорректная коллекция: {slug!r}. Нужен slug из ссылки t.me/nft/Slug-123.")
        self.dm_collections = ", ".join(self.collections)
        for key, lo, hi, integer in (
            ("max_model_rarity", 0, 100, False),
            ("max_backdrop_rarity", 0, 100, False),
            ("poll_interval", 1, 3600, False),
            ("request_interval", 0.5, 60, False),
            ("rescan_interval", 60, 86400, False),
            ("lookback", 1, 5000, True),
            ("scan_batch", 1, 200, True),
            ("gap_window", 1, 100, True),
        ):
            value = getattr(self, key)
            try:
                if isinstance(value, bool):
                    raise ValueError
                number = float(value.replace(",", ".") if isinstance(value, str) else value)
                if not math.isfinite(number) or not lo <= number <= hi or (integer and number != int(number)):
                    raise ValueError
            except (ValueError, TypeError, OverflowError):
                kind = "целое число" if integer else "число"
                raise ValueError(f"Поле {key}: требуется {kind} от {lo} до {hi}.") from None
            setattr(self, key, int(number) if integer else number)
        if not isinstance(self.initial_ids, dict):
            raise ValueError("initial_ids должен быть объектом с начальными номерами коллекций.")
        ids = {}
        for slug, number in self.initial_ids.items():
            if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,63}", slug):
                raise ValueError("Некорректное имя коллекции в initial_ids.")
            if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 2_000_000_000:
                raise ValueError(f"Начальный номер {slug} должен быть целым числом от 1 до 2000000000.")
            ids[slug] = number
        self.initial_ids = ids
        if len(self.target_backdrops) > 2000 or any(ord(c) < 32 for c in self.target_backdrops):
            raise ValueError("Некорректный список фонов.")
        if not self.offer_template or len(self.offer_template) > 1500:
            raise ValueError("Текст предложения должен содержать от 1 до 1500 символов.")
        allowed = {"gift", "number", "collection", "model", "backdrop"}
        try:
            for _, name, spec, conversion in string.Formatter().parse(self.offer_template):
                if name is not None and (name not in allowed or spec or conversion):
                    raise ValueError
            self.offer_template.format(**{key: "test" for key in allowed})
        except (ValueError, KeyError, IndexError, AttributeError):
            raise ValueError("Шаблон: разрешены только {gift}, {number}, {collection}, {model}, {backdrop} без форматирования.") from None
        return self


def load_settings(path: str | Path) -> Settings:
    path = Path(path)
    if not path.exists():
        return Settings().validate()
    try:
        with path.open(encoding="utf-8-sig") as source:
            data = json.load(source)
    except (OSError, ValueError) as exc:
        raise ValueError(f"Не удалось прочитать {path.name}: {exc}. Файл не изменён.") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: ожидается JSON-объект. Файл не изменён.")
    known = {item.name for item in fields(Settings)}
    # Earlier versions carried Market Sniper fields; ignore those on migration.
    return Settings(**{key: value for key, value in data.items() if key in known}).validate()


def save_settings(path: str | Path, settings: Settings) -> None:
    settings.validate()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(asdict(settings), target, ensure_ascii=False, indent=2, allow_nan=False)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
