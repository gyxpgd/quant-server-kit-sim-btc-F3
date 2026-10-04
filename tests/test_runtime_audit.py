"""Independent runtime failure-boundary review. Synthetic data; no network."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_runtime import FakeClient, ROOT
from common import MINUTE, HOUR, timestamp
from paper import Runner, load_settings, read_status
from store import Store


class RuntimeAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder)
        self.settings, self.cfg = load_settings(ROOT / 'config-F3-20u.json')
        self.client = FakeClient()
        self.runner = Runner(self.settings, self.cfg, self.store, self.client, {'test': 'v1'})

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def enter(self):
        self.runner.tick()
        self.client.now = timestamp('2026-01-01T09:06:00')
        self.runner.tick()
        self.assertIsNotNone(self.store.get('account')['open_position'])

    def test_crash_after_funding_event_insert_reopens_without_duplicate_receipt(self):
        self.enter()
        self.client.now = timestamp('2026-01-01T16:05:00')
        self.runner.tick()  # Six-hour chunk stops before the first settlement.
        cursor = self.store.get('cursor')
        checkpoint = self.store.get('checkpoint')
        marker = self.store.get('processed_funding')
        events_before = self.store.statistics()['events']
        original = self.store.put
        def injected_failure(key, value):
            if key == 'checkpoint':
                # commit_batch inserts the new funding event before checkpoint.
                count = self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='funding'").fetchone()[0]
                self.assertEqual(count, 1)
                raise RuntimeError('synthetic crash after event insertion')
            original(key, value)
        with patch.object(self.store, 'put', side_effect=injected_failure):
            with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
                self.runner.tick()
        self.assertEqual(self.store.get('cursor'), cursor)
        self.assertEqual(self.store.get('checkpoint'), checkpoint)
        self.assertEqual(self.store.get('processed_funding'), marker)
        self.assertEqual(self.store.statistics()['events'], events_before)
        self.store.close()
        self.store = Store(self.folder)
        restarted = Runner(self.settings, self.cfg, self.store, self.client, {'test': 'v1'})
        restarted.tick()
        self.assertEqual(self.store.get('account')['open_position']['funding_count'], 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events WHERE kind='funding'").fetchone()[0], 1)
        self.assertEqual(restarted.tick(), 0)
        self.assertEqual(self.store.get('checkpoint')['outbox'], [])

    def test_unannounced_late_settlement_never_rewrites_processed_minutes(self):
        self.enter()
        cursor, checkpoint = self.store.get('cursor'), self.store.get('checkpoint')
        original = self.client.funding_history
        late = timestamp('2026-01-01T09:03:00')
        def corrected(start, end):
            result = original(start, end)
            result[late] = .001
            return result
        self.client.funding_history = corrected
        self.client.now += MINUTE
        with self.assertRaisesRegex(ValueError, '迟到资金费'):
            self.runner.tick()
        self.assertEqual(self.store.get('cursor'), cursor)
        self.assertEqual(self.store.get('checkpoint'), checkpoint)

    def test_missing_hourly_settlement_freezes_even_when_eight_hour_gap_check_passes(self):
        due = timestamp('2026-01-01T10:00:00')
        self.client.funding_schedule = lambda: {'funding_time': due, 'next_funding_time': due + HOUR}
        missing = [True]
        def hourly(start, end):
            return {t: .0001 for t in range((start // HOUR + 1) * HOUR, end, HOUR)
                    if not (missing[0] and t == due)}
        self.client.funding_history = hourly
        self.enter()
        cursor, checkpoint = self.store.get('cursor'), self.store.get('checkpoint')
        self.client.now = timestamp('2026-01-01T10:05:00')
        with self.assertRaisesRegex(ValueError, '预告资金费'):
            self.runner.tick()
        self.assertEqual(self.store.get('cursor'), cursor)
        self.assertEqual(self.store.get('checkpoint'), checkpoint)
        missing[0] = False
        self.runner.tick()
        self.assertEqual(self.store.get('account')['open_position']['funding_count'], 1)

    def test_future_settlement_in_response_does_not_change_earlier_account(self):
        accounts = []
        future = timestamp('2026-01-01T09:05:00')
        for rate in (.5, -.5):
            with tempfile.TemporaryDirectory() as folder:
                store, client = Store(folder), FakeClient()
                original = client.funding_history
                def upcoming(start, end, rate=rate):
                    result = original(start, end)
                    if start <= future < end:
                        result[future] = rate
                    return result
                client.funding_history = upcoming
                runner = Runner(self.settings, self.cfg, store, client, {'test': 'v1'})
                try:
                    runner.tick()
                    client.now = timestamp('2026-01-01T09:06:00')
                    runner.tick()
                    self.assertLess(store.get('cursor'), future)
                    accounts.append(store.get('checkpoint'))
                finally:
                    store.close()
        self.assertEqual(accounts[0], accounts[1])

    def test_source_identity_mismatch_stops_before_any_network_request(self):
        self.enter()
        self.client.fail = True
        bad = Runner(self.settings, self.cfg, self.store, self.client, {'test': 'different'})
        with self.assertRaisesRegex(ValueError, '不一致'):
            bad.tick()

    def test_status_is_one_consistent_read_snapshot_during_concurrent_commit(self):
        self.enter()
        cursor, account = self.store.get('cursor'), self.store.get('account')
        writer = Store(self.folder)
        original = self.store.get
        updated = [False]
        def interrupted_read(key, default=None):
            result = original(key, default)
            if key == 'cursor' and not updated[0]:
                updated[0] = True
                new_account = copy.deepcopy(account)
                new_account['summary']['end_equity'] += 1
                with writer.db:
                    writer.put('cursor', cursor + MINUTE)
                    writer.put('account', new_account)
            return result
        try:
            with patch.object(self.store, 'get', side_effect=interrupted_read):
                status = read_status(self.store)
            self.assertEqual(status['cursor'], cursor)
            self.assertEqual(status['account'], account)
        finally:
            writer.close()

    def test_slow_collection_is_classified_using_completion_time(self):
        self.enter()
        self.client.now += MINUTE
        monotonic = [10.]
        original = self.client.candles
        def slow(*args, **kwargs):
            monotonic[0] = 250.
            return original(*args, **kwargs)
        self.client.candles = slow
        with patch('paper.time.monotonic', side_effect=lambda: monotonic[0]):
            self.assertEqual(self.runner.tick(), 1)
        health = self.store.get('health')
        self.assertEqual(health['last_batch_classification'], 'recovered')
        self.assertGreaterEqual(health['observed'], self.client.now + 4 * MINUTE)


if __name__ == '__main__':
    unittest.main()
