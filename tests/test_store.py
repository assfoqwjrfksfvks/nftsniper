import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.gift = {"slug": "CandyCane", "number": 42, "owner_username": "alice"}

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def enqueue(self, gift=None):
        return self.store.enqueue(gift or self.gift, "Редкая модель", "hello", {"inline_keyboard": []})

    def test_cursor_persists(self):
        self.assertIsNone(self.store.get_cursor("CandyCane"))
        self.store.set_cursor("CandyCane", 42)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.get_cursor("CandyCane"), 42)
        self.store.set_cursor("CandyCane", 43)
        self.assertEqual(self.store.get_cursor("CandyCane"), 43)

    def test_zero_cursor_means_no_gifts_processed(self):
        self.store.set_cursor("CandyCane", 0)
        self.assertEqual(self.store.get_cursor("CandyCane"), 0)
        with self.assertRaises(ValueError):
            self.store.set_cursor("CandyCane", -1)

    def test_dedup_is_persistent_before_and_after_delivery(self):
        self.assertTrue(self.enqueue())
        self.assertFalse(self.enqueue())
        pending = self.store.pending()
        self.assertEqual(pending[0]["gift"], self.gift)
        self.store.mark_sent(pending[0]["id"])
        self.assertEqual(self.store.pending(), [])
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.enqueue())
        self.assertTrue(self.store.has_seen("CandyCane", 42))
        self.assertEqual(self.store.stats()["sent"], 1)
        self.assertEqual(self.store.recent()[0]["delivery_status"], "sent")

    def test_failed_delivery_retries_after_restart(self):
        with patch("store.time.time", return_value=1000):
            self.enqueue()
            item = self.store.pending()[0]
            self.store.mark_failed(item["id"], "network error", retry_after=30)
            self.assertEqual(self.store.pending(), [])
        self.store.close()
        self.store = Store(self.path)
        with patch("store.time.time", return_value=1030):
            retry = self.store.pending()[0]
            self.assertEqual(retry["attempts"], 1)
            self.store.mark_failed(retry["id"], "again", retry_after=30)
        with patch("store.time.time", return_value=1089):
            self.assertEqual(self.store.pending(), [])
        with patch("store.time.time", return_value=1090):
            self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(self.store.stats()["failed"], 1)

    def test_queue_limit_and_explicit_retry_after_honored(self):
        with patch("store.time.time", return_value=1000):
            for n in range(1, 6):
                self.enqueue({"slug": "CandyCane", "number": n})
            self.assertEqual(len(self.store.pending(limit=2)), 2)
            item = self.store.pending()[0]
            self.store.mark_failed(item["id"], "throttled", retry_after=10000)
        with patch("store.time.time", return_value=1300):
            self.assertEqual(len(self.store.pending()), 4)
        with patch("store.time.time", return_value=11000):
            self.assertEqual(len(self.store.pending()), 5)

    def test_persisted_gaps_are_retried_and_expire(self):
        with patch("store.time.time", return_value=1000):
            self.store.add_gap("CandyCane", 7)
            self.store.add_gap("CandyCane", 7)
            self.store.add_gap("CandyCane", 8)
            self.assertEqual(self.store.due_gaps("CandyCane"), [])
        self.store.close()
        self.store = Store(self.path)
        with patch("store.time.time", return_value=1030):
            self.assertEqual(self.store.due_gaps("CandyCane"), [7, 8])
            self.store.defer_gap("CandyCane", 7)
            self.store.remove_gap("CandyCane", 8)
            self.assertEqual(self.store.due_gaps("CandyCane"), [])
        with patch("store.time.time", return_value=1150):
            self.assertEqual(self.store.due_gaps("CandyCane"), [7])
        with patch("store.time.time", return_value=90000):
            self.assertEqual(self.store.due_gaps("CandyCane"), [])

    def test_owner_status_is_durable_and_updates_without_double_counting(self):
        with patch('store.time.time',return_value=1000):
            self.store.record_owner_status('CandyCane',1,'no_public_username')
            self.store.record_owner_status('CandyCane',1,'no_public_username')
            self.store.record_owner_status('CandyCane',2,'unavailable')
            self.assertEqual(self.store.stats()['matched_24h'],2)
            self.assertEqual(self.store.stats()['owners_without_username'],1)
        self.store.close();self.store=Store(self.path)
        with patch('store.time.time',return_value=1010):
            self.assertEqual(self.store.stats()['owners_without_username'],1)
            self.store.record_owner_status('CandyCane',1,'public')
            self.assertEqual(self.store.stats()['owners_without_username'],0)
            self.assertEqual(self.store.stats()['owners_with_username'],1)
        with patch('store.time.time',return_value=90000):
            self.assertEqual(self.store.stats()['matched_24h'],0)

    def test_old_database_adds_owner_counters_without_losing_queue_or_cursor(self):
        self.enqueue();self.store.set_cursor('CandyCane',42)
        self.store.db.execute('DROP TABLE owner_checks');self.store.db.commit()
        self.store.close();self.store=Store(self.path)
        self.assertEqual(self.store.stats()['pending'],1)
        self.assertEqual(self.store.get_cursor('CandyCane'),42)
        self.store.record_owner_status('CandyCane',42,'public')
        self.assertEqual(self.store.stats()['owners_with_username'],1)

    def test_private_owner_retry_interval_survives_restart(self):
        with patch('store.time.time',return_value=1000):
            self.store.add_gap('CandyCane',42)
            self.store.defer_gap('CandyCane',42,retry_after=1800)
        self.store.close();self.store=Store(self.path)
        with patch('store.time.time',return_value=2799):
            self.assertEqual(self.store.due_gaps('CandyCane'),[])
        with patch('store.time.time',return_value=2800):
            self.assertEqual(self.store.due_gaps('CandyCane'),[42])


if __name__ == "__main__":
    unittest.main()
