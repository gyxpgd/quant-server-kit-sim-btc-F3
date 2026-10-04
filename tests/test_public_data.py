"""No-network boundary tests for the F3 public-data collector."""
import io
import json
import sys
import unittest
import urllib.error
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from public_data import (OKXClient, PublicDataError, DataGapError, NoRedirect,
                         MINUTE, HOUR, SPOT_ID, SWAP_ID)


def candle(ts, confirm='1', mark=False, close='101'):
    prices = [str(ts), '100', '102', '99', close]
    return prices + ([confirm] if mark else ['1', '1', '100', confirm])


def funding(ts, rate='.0001', realized=True):
    row = {'instId': SWAP_ID, 'fundingTime': str(ts), 'fundingRate': '.99'}
    if realized:
        row['realizedRate'] = rate
    return row


def client():
    c = OKXClient()
    c._monotonic = lambda: 0.
    c._server_clock = (1000 * HOUR, 0.)
    c._sleep = lambda duration: None
    return c


def install_pages(c, pages):
    queue, seen = deque(pages), []
    def request(path, params=None):
        seen.append((path, dict(params or {})))
        if not queue:
            raise AssertionError('Unexpected extra page request')
        return queue.popleft()
    c._get = request
    return seen


class Response:
    def __init__(self, payload):
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self, *unused):
        return False
    def read(self, limit):
        return self.content[:limit]


class PublicTransportTests(unittest.TestCase):
    def test_origin_credentials_paths_and_redirects_are_rejected(self):
        for origin in ('http://www.okx.com', 'https://example.com',
                       'https://user:password@www.okx.com', 'https://www.okx.com.evil.test',
                       'https://www.okx.com/api', 'https://www.okx.com:444'):
            with self.assertRaises(ValueError):
                OKXClient(origin)
        c = client()
        with self.assertRaises(ValueError):
            c._get('/api/v5/trade/order')
        with self.assertRaises(ValueError):
            c._get('/api/v5/public/time', {'api_key': 'secret'})
        with self.assertRaises(PublicDataError):
            NoRedirect().redirect_request(None, None, 302, '', {}, 'https://example.com')

    def test_public_get_success_has_no_auth_or_order_body(self):
        c, requests = client(), []
        def transport(request, timeout):
            requests.append((request, timeout))
            return Response({'code': '0', 'data': [{'ts': '12345'}]})
        c._open = transport
        result = c._get('/api/v5/public/time')
        self.assertEqual(result, [{'ts': '12345'}])
        request, timeout = requests[0]
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertFalse(any('access' in k.lower() or 'authorization' in k.lower()
                             for k in request.headers))
        self.assertEqual(timeout, 15)

    def test_api_rate_limit_retries_but_no_partial_data_escapes(self):
        c = client()
        responses = deque([{'code': '50011', 'data': []},
                           {'code': '0', 'data': [{'ts': '12345'}]}])
        c._open = lambda request, timeout: Response(responses.popleft())
        self.assertEqual(c._get('/api/v5/public/time'), [{'ts': '12345'}])
        self.assertFalse(responses)

    def test_http_429_retry_and_terminal_error_redacts_transport_details(self):
        c, attempts = client(), []
        def limited(request, timeout):
            attempts.append(request)
            if len(attempts) == 1:
                raise urllib.error.HTTPError(request.full_url, 429, 'limited', {'Retry-After': '0'}, io.BytesIO())
            return Response({'code': '0', 'data': []})
        c._open = limited
        self.assertEqual(c._get('/api/v5/public/time'), [])
        self.assertEqual(len(attempts), 2)
        def failing(request, timeout):
            raise urllib.error.URLError('proxy://private-user:private-password@server')
        c._open = failing
        with self.assertRaises(PublicDataError) as raised:
            c._get('/api/v5/public/time')
        self.assertNotIn('private-password', str(raised.exception))

    def test_failed_code_invalid_json_and_invalid_data_fail_closed(self):
        for payload in ({'code': '51000', 'data': []}, {'code': '0', 'data': {}},
                        {'code': None, 'data': []}, b'<html>not market data</html>'):
            c = client()
            c._open = lambda request, timeout: Response(payload)
            with self.assertRaises(PublicDataError):
                c._get('/api/v5/public/time')

    def test_server_time_parsed_without_predictive_clock_advance(self):
        c = client()
        c._server_clock = None
        install_pages(c, [[{'ts': '1800000000000'}]])
        self.assertEqual(c.server_time(), 1800000000000)
        self.assertEqual(c._now(), 1800000000000)


class CandleTests(unittest.TestCase):
    def test_pages_use_exclusive_older_cursor_and_deduplicate_overlap(self):
        c = client()
        seen = install_pages(c, [[candle(3 * MINUTE), candle(2 * MINUTE)],
                                 [candle(2 * MINUTE), candle(MINUTE)]])
        rows = c.candles(SPOT_ID, '1m', MINUTE, 4 * MINUTE)
        self.assertEqual([r[0] for r in rows], [MINUTE, 2 * MINUTE, 3 * MINUTE])
        self.assertEqual(seen[0][1]['after'], 4 * MINUTE)
        self.assertEqual(seen[1][1]['after'], 2 * MINUTE)
        self.assertEqual(rows[0][1:], (100., 102., 99., 101.))

    def test_unconfirmed_or_absent_candle_cannot_fill_gap(self):
        for rows in ([candle(2 * MINUTE, confirm='0'), candle(MINUTE)],
                     [candle(MINUTE)]):
            c = client()
            install_pages(c, [rows])
            with self.assertRaises(DataGapError):
                c.candles(SPOT_ID, '1m', MINUTE, 3 * MINUTE)

    def test_conflicting_duplicate_is_rejected(self):
        c = client()
        install_pages(c, [[candle(2 * MINUTE), candle(2 * MINUTE, close='100'), candle(MINUTE)]])
        with self.assertRaises(PublicDataError):
            c.candles(SPOT_ID, '1m', MINUTE, 3 * MINUTE)

    def test_mark_has_distinct_schema_and_confirmed_history_path(self):
        c = client()
        seen = install_pages(c, [[candle(MINUTE, mark=True)]])
        rows = c.candles(SWAP_ID, '1m', MINUTE, 2 * MINUTE, mark=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(seen[0][0], '/api/v5/market/history-mark-price-candles')
        c = client()
        install_pages(c, [[candle(MINUTE)]])
        with self.assertRaises(PublicDataError):
            c.candles(SWAP_ID, '1m', MINUTE, 2 * MINUTE, mark=True)

    def test_bad_ohlc_confirmation_and_future_range_are_rejected(self):
        for row in (candle(MINUTE, close='500'), candle(MINUTE, close='NaN'),
                    candle(MINUTE, confirm='yes')):
            c = client()
            install_pages(c, [[row]])
            with self.assertRaises(PublicDataError):
                c.candles(SPOT_ID, '1m', MINUTE, 2 * MINUTE)
        c = client()
        c._server_clock = (2 * MINUTE - 1, 0.)
        with self.assertRaises(DataGapError):
            c.candles(SPOT_ID, '1m', MINUTE, 2 * MINUTE)

    def test_nonprogress_cannot_loop_and_misalignment_is_rejected(self):
        c = client()
        install_pages(c, [[candle(2 * MINUTE)]])
        with self.assertRaises(PublicDataError):
            c.candles(SPOT_ID, '1m', MINUTE, 2 * MINUTE)
        with self.assertRaises(ValueError):
            client().candles(SPOT_ID, '1H', MINUTE, HOUR)


class FundingTests(unittest.TestCase):
    def test_realized_not_predicted_rate_and_exact_ms_are_retained(self):
        c = client()
        ts = 8 * HOUR + 321
        install_pages(c, [[funding(ts), funding(1)]])
        rates = c.funding_history(2 * HOUR, 10 * HOUR)
        self.assertEqual(rates, {ts: .0001})
        self.assertNotEqual(rates[ts], .99)
        self.assertEqual(next(iter(rates)) % MINUTE, 321)

    def test_missing_realized_rate_is_not_replaced_with_prediction(self):
        c = client()
        install_pages(c, [[funding(8 * HOUR, realized=False), funding(1)]])
        with self.assertRaises(DataGapError):
            c.funding_history(2 * HOUR, 10 * HOUR)

    def test_settlement_is_not_duplicated_at_page_boundary(self):
        c = client()
        seen = install_pages(c, [[funding(16 * HOUR), funding(8 * HOUR)],
                                 [funding(8 * HOUR), funding(1)]])
        rates = c.funding_history(2 * HOUR, 18 * HOUR)
        self.assertEqual(list(rates), [8 * HOUR, 16 * HOUR])
        self.assertEqual(seen[1][1]['after'], 8 * HOUR)

    def test_conflict_negative_nonfinite_and_long_gap_handling(self):
        c = client()
        install_pages(c, [[funding(8 * HOUR, '-.001'), funding(1)]])
        self.assertEqual(c.funding_history(2 * HOUR, 10 * HOUR), {8 * HOUR: -.001})
        for rows in ([funding(8 * HOUR), funding(8 * HOUR, '.02'), funding(1)],
                     [funding(8 * HOUR, 'inf'), funding(1)]):
            c = client()
            install_pages(c, [rows])
            with self.assertRaises(PublicDataError):
                c.funding_history(2 * HOUR, 10 * HOUR)
        c = client()
        install_pages(c, [[]])
        with self.assertRaises(DataGapError):
            c.funding_history(1, 30 * HOUR)

    def test_schedule_retains_raw_prediction_but_exposes_only_times(self):
        c = client()
        c._server_clock = (10 * HOUR, 0.)
        row = {'instId': SWAP_ID, 'fundingTime': str(16 * HOUR),
               'nextFundingTime': str(24 * HOUR), 'fundingRate': '.99'}
        install_pages(c, [[row]])
        schedule = c.funding_schedule()
        self.assertEqual(schedule['funding_time'], 16 * HOUR)
        self.assertEqual(schedule['next_funding_time'], 24 * HOUR)
        self.assertNotIn('rate', schedule)
        self.assertNotIn('realizedRate', schedule)
        self.assertEqual(schedule['raw']['fundingRate'], '.99')

    def test_stale_schedule_and_wrong_instrument_fail(self):
        for row in ({'instId': SWAP_ID, 'fundingTime': str(HOUR), 'nextFundingTime': str(9 * HOUR)},
                    {'instId': 'BTC-USDT-SWAP', 'fundingTime': str(1001 * HOUR),
                     'nextFundingTime': str(1009 * HOUR)}):
            c = client()
            install_pages(c, [[row]])
            with self.assertRaises(PublicDataError):
                c.funding_schedule()


class SpecificationTests(unittest.TestCase):
    def records(self, mmr='.004'):
        spot = {'instId': SPOT_ID, 'instType': 'SPOT', 'state': 'live', 'baseCcy': 'BTC',
                'quoteCcy': 'USDT', 'lotSz': '.00000001', 'minSz': '.00001', 'tickSz': '.1'}
        swap = {'instId': SWAP_ID, 'instType': 'SWAP', 'state': 'live', 'ctType': 'inverse',
                'settleCcy': 'BTC', 'ctValCcy': 'USD', 'instFamily': 'BTC-USD', 'ctVal': '100',
                'ctMult': '1', 'lotSz': '.1', 'minSz': '.1', 'tickSz': '.1'}
        tier = {'tier': '1', 'instFamily': 'BTC-USD', 'mmr': mmr, 'maxSz': '10000'}
        return [[spot], [swap], [tier]]

    def test_normalization_uses_real_contract_value_and_current_tier(self):
        c = client()
        install_pages(c, self.records(mmr='.005'))
        result = c.instruments()
        self.assertEqual(result['spec'], {'spot_lot': 1e-8, 'spot_min': 1e-5, 'spot_tick': .1,
                                         'usd_step': 10., 'usd_min': 10., 'swap_tick': .1, 'mmr': .005})
        self.assertEqual(result['raw']['selected_tier']['mmr'], '.005')
        self.assertEqual(result['first_tier_max_notional_usd'], 1000000.)

    def test_missing_or_ambiguous_tier_never_falls_back_to_assumed_mmr(self):
        for tiers in ([], [{'tier': '1', 'instFamily': 'BTC-USD', 'maxSz': '10'}],
                      self.records()[2] * 2):
            c, pages = client(), self.records()
            pages[2] = tiers
            install_pages(c, pages)
            with self.assertRaises(PublicDataError):
                c.instruments()

    def test_wrong_contract_or_suspended_spot_is_rejected(self):
        for index, key, value in ((0, 'state', 'suspend'), (1, 'ctType', 'linear'),
                                   (1, 'settleCcy', 'USDT')):
            c, pages = client(), self.records()
            pages[index][0][key] = value
            install_pages(c, pages)
            with self.assertRaises(PublicDataError):
                c.instruments()


if __name__ == '__main__':
    unittest.main()
