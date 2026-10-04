"""Strict public-GET-only OKX market data for the F3 paper observer.

Official schemas: https://www.okx.com/docs-v5/en/
Global REST origins: https://www.okx.com/docs-v5/log_en/ (2026-05-20).
Python's default TLS verification and environment HTTP(S) proxy support remain
enabled. This module has no account, order, credential or proxy logging code.
"""
import json
import math
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


MINUTE = 60000
HOUR = 60 * MINUTE
SPOT_ID = 'BTC-USDT'
SWAP_ID = 'BTC-USD-SWAP'
ALLOWED_ORIGINS = frozenset(('https://www.okx.com', 'https://openapi.okx.com'))
ALLOWED_PATHS = frozenset(('/api/v5/public/time', '/api/v5/public/instruments',
    '/api/v5/public/position-tiers', '/api/v5/public/funding-rate',
    '/api/v5/public/funding-rate-history', '/api/v5/market/history-candles',
    '/api/v5/market/history-mark-price-candles'))
BAR_MS = {'1m': MINUTE, '3m': 3 * MINUTE, '5m': 5 * MINUTE,
          '15m': 15 * MINUTE, '30m': 30 * MINUTE, '1H': HOUR,
          '2H': 2 * HOUR, '4H': 4 * HOUR}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class PublicDataError(RuntimeError):
    """No market data from this operation can be accepted."""


class DataGapError(PublicDataError):
    """Requested confirmed public history is incomplete."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PublicDataError('Public API redirect refused; use a supported official origin')


def _integer(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise PublicDataError('Invalid integer field: ' + label)
    if isinstance(value, str) and (not value or not value.isascii() or not value.isdecimal()):
        raise PublicDataError('Invalid integer field: ' + label)
    parsed = int(value)
    if parsed < minimum:
        raise PublicDataError('Out-of-range integer field: ' + label)
    return parsed


def _number(value, label, positive=False, nonnegative=False):
    if isinstance(value, bool):
        raise PublicDataError('Invalid numeric field: ' + label)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise PublicDataError('Invalid numeric field: ' + label) from None
    if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        raise PublicDataError('Out-of-range numeric field: ' + label)
    return result


class OKXClient:
    def __init__(self, base_url='https://www.okx.com', timeout=15):
        if base_url not in ALLOWED_ORIGINS:
            raise ValueError('Only supported HTTPS OKX global public API origins are allowed')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError('timeout must be a finite number in (0, 60] seconds')
        self.base_url, self.timeout = base_url, timeout
        # build_opener supplies the standard ProxyHandler and verified HTTPS
        # handler; NoRedirect prevents a whitelisted request moving hosts.
        self._open = urllib.request.build_opener(NoRedirect()).open
        self._sleep, self._monotonic = time.sleep, time.monotonic
        self._last_request = None
        self._server_clock = None
        self._minimum_interval = .25
        self._retries = 3
        self.requests_completed = 0

    def _get(self, path, params=None):
        if self.base_url not in ALLOWED_ORIGINS or path not in ALLOWED_PATHS:
            raise ValueError('Only explicit public GET endpoints are supported')
        params = params or {}
        if not isinstance(params, dict) or any(str(k).lower() in
                {'apikey', 'api_key', 'secret', 'signature', 'passphrase', 'authorization'} for k in params):
            raise ValueError('Credentials are unsupported')
        query = urllib.parse.urlencode({k: str(v) for k, v in params.items() if v is not None})
        url = self.base_url + path + ('?' + query if query else '')
        for attempt in range(self._retries + 1):
            now = self._monotonic()
            if self._last_request is not None:
                delay = self._minimum_interval - (now - self._last_request)
                if delay > 0:
                    self._sleep(delay)
            self._last_request = self._monotonic()
            request = urllib.request.Request(url, method='GET', headers={
                'User-Agent': 'OKX-Funding-F3-Paper/1', 'Accept': 'application/json',
                'Cache-Control': 'no-cache'})
            retry_delay = min(4., .5 * 2 ** attempt)
            try:
                with self._open(request, timeout=self.timeout) as response:
                    content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise PublicDataError('Public response exceeds allowed size')
                try:
                    payload = json.loads(content)
                except (ValueError, UnicodeDecodeError):
                    raise PublicDataError('Public response is not valid JSON') from None
                if not isinstance(payload, dict):
                    raise PublicDataError('Public response has invalid envelope')
                code = str(payload.get('code'))
                if code in ('50011', '50040', '50013') and attempt < self._retries:
                    self._sleep(retry_delay)
                    continue
                if code != '0':
                    # Do not echo response text or a transport exception: an
                    # upstream proxy could include credentials in either.
                    safe_code = code if code.isascii() and code.isdecimal() else 'unknown'
                    raise PublicDataError('OKX public API rejected request; code=' + safe_code)
                if not isinstance(payload.get('data'), list):
                    raise PublicDataError('Public response data must be a list')
                self.requests_completed += 1
                return payload['data']
            except urllib.error.HTTPError as error:
                retryable = error.code in (429, 500, 502, 503, 504)
                retry_after = error.headers.get('Retry-After') if error.headers else None
                if retry_after:
                    try:
                        retry_delay = min(8., max(retry_delay, float(retry_after)))
                    except (TypeError, ValueError):
                        pass
                status = error.code
                error.close()
                if not retryable or attempt == self._retries:
                    raise PublicDataError('Public HTTP request failed; status=' + str(status)) from None
            except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
                if attempt == self._retries:
                    raise PublicDataError('Public network request failed; check DNS, TLS and proxy connectivity') from None
            self._sleep(retry_delay)
        raise PublicDataError('Public request retry limit reached')

    def server_time(self):
        started = self._monotonic()
        rows = self._get('/api/v5/public/time')
        completed = self._monotonic()
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise PublicDataError('Public server time is unavailable')
        stamp = _integer(rows[0].get('ts'), 'server time', minimum=1)
        if completed - started > 5:
            raise PublicDataError('Server-time response too slow for a reliable timestamp')
        # Never move the published server stamp forward to compensate latency;
        # candle eligibility uses this conservative clock plus elapsed time.
        if self._server_clock is not None and stamp < self._now() - 2000:
            raise PublicDataError('OKX server clock moved backwards')
        self._server_clock = (stamp, completed)
        return stamp

    def _now(self):
        if self._server_clock is None:
            return self.server_time()
        stamp, measured = self._server_clock
        return stamp + int(max(0., self._monotonic() - measured) * 1000)

    def instruments(self):
        spot = self._one_instrument('SPOT', SPOT_ID)
        swap = self._one_instrument('SWAP', SWAP_ID)
        if spot.get('baseCcy') != 'BTC' or spot.get('quoteCcy') != 'USDT':
            raise PublicDataError('Unexpected BTC spot currencies')
        if (swap.get('ctType') != 'inverse' or swap.get('settleCcy') != 'BTC'
                or swap.get('ctValCcy') != 'USD' or swap.get('instFamily') != 'BTC-USD'):
            raise PublicDataError('Unexpected inverse BTC-USD contract specification')
        tiers = self._get('/api/v5/public/position-tiers', {
            'instType': 'SWAP', 'tdMode': 'isolated', 'instFamily': 'BTC-USD', 'tier': '1'})
        selected = [row for row in tiers if isinstance(row, dict)
                    and str(row.get('tier')) == '1'
                    and row.get('instFamily', row.get('uly')) == 'BTC-USD']
        if len(selected) != 1:
            raise PublicDataError('Unambiguous first BTC-USD maintenance tier unavailable')
        tier = selected[0]
        multiplier = _number(swap.get('ctVal'), 'ctVal', positive=True) * _number(
            swap.get('ctMult') or 1, 'ctMult', positive=True)
        mmr = _number(tier.get('mmr'), 'mmr', positive=True)
        if mmr >= 1:
            raise PublicDataError('Invalid maintenance margin ratio')
        spec = dict(spot_lot=_number(spot.get('lotSz'), 'spot lot', positive=True),
                    spot_min=_number(spot.get('minSz'), 'spot minimum', positive=True),
                    spot_tick=_number(spot.get('tickSz'), 'spot tick', positive=True),
                    usd_step=multiplier * _number(swap.get('lotSz'), 'swap lot', positive=True),
                    usd_min=multiplier * _number(swap.get('minSz'), 'swap minimum', positive=True),
                    swap_tick=_number(swap.get('tickSz'), 'swap tick', positive=True), mmr=mmr)
        maximum = multiplier * _number(tier.get('maxSz'), 'first tier maxSz', positive=True)
        return {'spec': spec, 'raw': {'spot': spot, 'swap': swap, 'tiers': tiers,
                 'selected_tier': tier, 'observed_at': self._now()},
                'first_tier_max_notional_usd': maximum}

    def _one_instrument(self, kind, instrument):
        rows = self._get('/api/v5/public/instruments', {'instType': kind, 'instId': instrument})
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise PublicDataError('Requested public instrument unavailable')
        row = rows[0]
        if row.get('instId') != instrument or row.get('instType') != kind or row.get('state') != 'live':
            raise PublicDataError('Public instrument mismatched or suspended')
        return row

    def funding_schedule(self):
        rows = self._get('/api/v5/public/funding-rate', {'instId': SWAP_ID})
        if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get('instId') != SWAP_ID:
            raise PublicDataError('Current BTC-USD funding schedule unavailable')
        row = rows[0]
        current = _integer(row.get('fundingTime'), 'fundingTime', minimum=1)
        following = _integer(row.get('nextFundingTime'), 'nextFundingTime', minimum=1)
        observed = self._now()
        if not 0 < following - current <= 8 * HOUR or following <= observed:
            raise PublicDataError('Funding schedule is stale or has an unsupported interval')
        result = {'funding_time': current, 'next_funding_time': following,
                  'observed_at': observed, 'raw': row}
        # Predictions in raw are retained as evidence only, never normalized
        # into a realized-rate field or used to credit the paper account.
        if row.get('prevFundingTime') not in (None, '', '0'):
            result['previous_funding_time'] = _integer(row['prevFundingTime'], 'prevFundingTime', minimum=1)
        return result

    def funding_history(self, since_ms, until_ms):
        start, end = self._interval(since_ms, until_ms)
        if start == end:
            return {}
        if end > self._now():
            raise PublicDataError('Cannot request future realized funding')
        cursor, found = end, {}
        for _ in range(1000):
            rows = self._get('/api/v5/public/funding-rate-history', {
                'instId': SWAP_ID, 'after': cursor, 'limit': 100})
            if not rows:
                break
            oldest = cursor
            for row in rows:
                if not isinstance(row, dict) or row.get('instId') != SWAP_ID:
                    raise PublicDataError('Funding history instrument mismatch')
                stamp = _integer(row.get('fundingTime'), 'fundingTime', minimum=1)
                oldest = min(oldest, stamp)
                if not start <= stamp < end:
                    continue
                if row.get('realizedRate') in (None, ''):
                    raise DataGapError('Settled funding lacks realizedRate; wait for publication')
                rate = _number(row['realizedRate'], 'realizedRate')
                if stamp in found and found[stamp] != rate:
                    raise PublicDataError('Conflicting settled funding records')
                found[stamp] = rate
            if oldest >= cursor:
                raise PublicDataError('Funding-history pagination made no progress')
            if oldest <= start:
                break
            cursor = oldest
        else:
            raise PublicDataError('Funding-history page bound exceeded')
        times = sorted(found)
        # A fixed 8h tolerance detects long gaps/retention exhaustion without
        # inventing settlement payments. It cannot detect every missing 1h/4h
        # event; the runtime must additionally match observed schedule times.
        coverage = [start] + times + [end]
        if any(b - a > 8 * HOUR + MINUTE for a, b in zip(coverage, coverage[1:])):
            raise DataGapError('Realized funding history is missing or outside retained coverage')
        return {stamp: found[stamp] for stamp in times}

    @staticmethod
    def _interval(start, end):
        if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
            raise ValueError('History bounds must be integer milliseconds')
        if start < 0 or end < start:
            raise ValueError('History bounds must be nonnegative and increasing')
        return start, end

    def candles(self, inst_id, bar, start_ms, end_ms, mark=False):
        if inst_id not in (SPOT_ID, SWAP_ID) or (mark and inst_id != SWAP_ID):
            raise ValueError('Only F3 spot and inverse swap instruments are supported')
        if not isinstance(mark, bool) or bar not in BAR_MS:
            raise ValueError('Unsupported candle type or interval')
        start, end = self._interval(start_ms, end_ms)
        step = BAR_MS[bar]
        if start % step or end % step:
            raise ValueError('Candle bounds must align to the requested bar interval')
        if start == end:
            return []
        now = self._now()
        if end > now:
            raise DataGapError('Requested candle interval has not finished')
        path = '/api/v5/market/history-' + ('mark-price-candles' if mark else 'candles')
        cursor, found = end, {}
        maximum_pages = (end - start) // step + 2
        for _ in range(maximum_pages):
            rows = self._get(path, {'instId': inst_id, 'bar': bar, 'after': cursor, 'limit': 100})
            if not rows:
                break
            oldest = cursor
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) != (6 if mark else 9):
                    raise PublicDataError('Unexpected candle schema')
                stamp = _integer(row[0], 'candle timestamp')
                oldest = min(oldest, stamp)
                confirm = str(row[-1])
                if confirm not in ('0', '1'):
                    raise PublicDataError('Unknown candle confirmation flag')
                if not start <= stamp < end or confirm == '0':
                    continue
                if stamp % step or stamp + step > now:
                    raise PublicDataError('Misaligned or future confirmed candle')
                values = tuple(_number(value, 'OHLC', positive=True) for value in row[1:5])
                opening, high, low, closing = values
                if not low <= min(opening, closing) <= max(opening, closing) <= high:
                    raise PublicDataError('Inconsistent candle OHLC range')
                if not mark:
                    for value in row[5:8]:
                        _number(value, 'candle volume', nonnegative=True)
                parsed = (stamp,) + values
                if stamp in found and found[stamp] != parsed:
                    raise PublicDataError('Conflicting confirmed candles')
                found[stamp] = parsed
            if oldest >= cursor:
                raise PublicDataError('Candle pagination made no progress')
            if oldest <= start:
                break
            cursor = oldest
        else:
            raise PublicDataError('Candle page bound exceeded')
        wanted = range(start, end, step)
        if len(found) != len(wanted) or any(stamp not in found for stamp in wanted):
            missing = next(stamp for stamp in wanted if stamp not in found)
            raise DataGapError('Confirmed candle history has a gap at ' + str(missing))
        return [found[stamp] for stamp in wanted]
