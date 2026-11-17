import json
import math
import os
import tempfile
import unittest
from pathlib import Path

from settings import Settings, load_settings, save_settings


class SettingsTests(unittest.TestCase):
    def test_defaults_do_not_contain_credentials(self):
        cfg = Settings().validate()
        self.assertEqual(cfg.bot_token, "")
        self.assertEqual(cfg.chat_id, "")
        self.assertEqual(len(cfg.collections), 5)

    def test_comma_numbers_and_duplicate_collections(self):
        cfg = Settings(max_model_rarity="0,5", request_interval="0,7",
                       dm_collections="CandyCane, CandyCane, JingleBells").validate()
        self.assertEqual(cfg.max_model_rarity, 0.5)
        self.assertEqual(cfg.collections, ["CandyCane", "JingleBells"])

    def test_bad_numbers_and_ranges_rejected(self):
        for key, value in [("max_model_rarity", "nan"), ("max_backdrop_rarity", math.inf),
                           ("poll_interval", 0), ("scan_batch", 1.5), ("gap_window", True),
                           ("request_interval", -1), ("lookback", 0), ("rescan_interval", 0)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                Settings(**{key: value}).validate()

    def test_chat_owner_is_private(self):
        for value in ["-1001234", "0", "abc", True, []]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(chat_id=value).validate()
        self.assertEqual(Settings(chat_id=1234567).validate().chat_id, "1234567")

    def test_malformed_tokens_slugs_templates_rejected(self):
        for key, value in [("bot_token", "secret"), ("dm_collections", ""),
                           ("dm_collections", "https://t.me/nft/CandyCane-2"),
                           ("offer_template", "{gift.__class__}"),
                           ("offer_template", "{gift!r}"), ("offer_template", "{number:>20}"),
                           ("offer_template", "{"), ("offer_template", "{unknown}"),
                           ("initial_ids", {"CandyCane": "3"})]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                Settings(**{key: value}).validate()

    def test_roundtrip_and_original_config_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"chat_id": "123", "max_budget_ton": 6.5,
                                        "max_model_rarity": 0.7}), encoding="utf-8")
            cfg = load_settings(path)
            self.assertEqual(cfg.max_model_rarity, 0.7)
            self.assertEqual(cfg.chat_id, "123")
            save_settings(path, cfg)
            self.assertEqual(load_settings(path), cfg)
            self.assertNotIn("max_budget_ton", json.loads(path.read_text()))
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corrupted_config_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for original in ["{", "[]", '{"max_model_rarity":"bad"}']:
                path.write_text(original)
                with self.assertRaises(ValueError):
                    load_settings(path)
                self.assertEqual(path.read_text(), original)

    def test_failed_save_preserves_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_settings(path, Settings())
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                save_settings(path, Settings(max_model_rarity="not a number"))
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
