"""Backward-looking funding signals and conservative execution arithmetic.

Only Python's standard library is used.  Some inherited configuration fields
are retained solely to permit exact replay of the frozen funding-v25 settings;
the inverse-coin engine does not multiply exposure by ``leverage``.
"""
from bisect import bisect_right
from dataclasses import dataclass, fields, replace
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import math

from common import MINUTE, HOUR, DAY


@dataclass(frozen=True)
class Config:
    capital: float = 20.
    spot_budget_fraction: float = .40
    reserve_fraction: float = .20
    leverage: float = 1.
    spot_fee: float = .001
    swap_fee: float = .0005
    adverse_bps: float = 3.
    cost_cover: float = 1.5
    expected_hold_days: int = 30
    entry_lookback_days: int = 7
    exit_lookback_days: int = 3
    exit_bad_days: int = 2
    min_hold_hours: int = 24
    cooldown_hours: int = 24
    decision_hour_bjt: int = 9
    loss_limit_fraction: float = .02
    halt_drawdown: float = .05
    margin_exit_ratio: float = .50
    assumed_mmr: float = .004
    second_leg_delay_minutes: int = 0
    execution_latency_minutes: int = 1
    funding_available_lag_minutes: int = 1
    benchmark: bool = False
    exit_projection_days: int = 0
    exit_switch_cost_multiple: float = 1.
    margin_topup_trigger: float = 0.
    margin_topup_target: float = 0.

    @classmethod
    def from_dict(cls, values):
        if not isinstance(values, dict):
            raise ValueError('Configuration must be a dictionary')
        unknown = set(values) - {field.name for field in fields(cls)}
        if unknown:
            raise ValueError('Unknown configuration fields: ' + ', '.join(sorted(unknown)))
        return cls(**values).validate()

    def validate_common(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name in ('allocation_mode', 'treasury_mode', 'benchmark'):
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('Configuration requires a finite number: ' + field.name)
        if not isinstance(self.benchmark, bool):
            raise ValueError('benchmark must be boolean')
        positive_ints = ('expected_hold_days', 'entry_lookback_days', 'exit_lookback_days',
                         'exit_bad_days', 'execution_latency_minutes', 'funding_available_lag_minutes')
        nonnegative_ints = ('min_hold_hours', 'cooldown_hours', 'second_leg_delay_minutes',
                            'exit_projection_days', 'decision_hour_bjt')
        for name in positive_ints + nonnegative_ints:
            value = getattr(self, name)
            lower = 1 if name in positive_ints else 0
            if not isinstance(value, int) or isinstance(value, bool) or value < lower:
                raise ValueError(name + ' must be an integer >= ' + str(lower))
        if self.decision_hour_bjt > 23:
            raise ValueError('decision_hour_bjt must be in 0..23')
        if self.capital <= 0 or not 1 <= self.leverage <= 15:
            raise ValueError('capital must be positive and legacy leverage must be in 1..15')
        if not 0 < self.spot_budget_fraction <= .85 or not 0 < self.reserve_fraction < 1:
            raise ValueError('Invalid legacy budget/reserve fraction')
        if not 0 <= self.spot_fee < 1 or not 0 <= self.swap_fee < 1 or not 0 <= self.adverse_bps < 10000:
            raise ValueError('Fees and slippage must be nonnegative and below 100%')
        if self.assumed_mmr < 0 or self.cost_cover <= 0 or self.exit_switch_cost_multiple <= 0:
            raise ValueError('Invalid maintenance margin or funding cost multiple')
        if self.margin_topup_trigger < 0 or self.margin_topup_target < 0:
            raise ValueError('Legacy margin top-up ratios cannot be negative')
        return self

    def validate(self):
        self.validate_common()
        if not 0 < self.loss_limit_fraction <= self.halt_drawdown < 1:
            raise ValueError('Invalid loss/drawdown limits')
        if not self.assumed_mmr + self.swap_fee < self.margin_exit_ratio < 1 / self.leverage:
            raise ValueError('Invalid margin exit ratio')
        if self.margin_topup_trigger and not (
            self.margin_exit_ratio < self.margin_topup_trigger < self.margin_topup_target <= 1 / self.leverage
        ):
            raise ValueError('Invalid margin top-up ratios')
        return self

    def stressed(self):
        return replace(self, spot_fee=2 * self.spot_fee, swap_fee=2 * self.swap_fee,
                       adverse_bps=2 * self.adverse_bps)


def round_step(x, step, up=False):
    if not math.isfinite(x) or not math.isfinite(step) or step <= 0:
        raise ValueError('Rounding requires a finite amount and positive step')
    return float((Decimal(str(x)) / Decimal(str(step))).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR) * Decimal(str(step)))


def fill(price, buy, cfg, tick):
    if not math.isfinite(price) or price <= 0:
        raise ValueError('Fill price must be finite and positive')
    result = round_step(price * (1 + (1 if buy else -1) * cfg.adverse_bps / 10000), tick, up=buy)
    if result <= 0:
        raise ValueError('Executable fill price must be positive')
    return result


class FundingHistory:
    def __init__(self, events):
        if not hasattr(events, 'items'):
            raise ValueError('Funding events must map settlement timestamps to realized rates')
        for timestamp, rate in events.items():
            if (not isinstance(timestamp, int) or isinstance(timestamp, bool)
                    or timestamp % MINUTE != 0):
                raise ValueError('Funding timestamps must be integer minute boundaries')
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate):
                raise ValueError('Funding rates must be finite numbers')
        self.events = sorted(events.items())
        self.times = [x[0] for x in self.events]
        self.prefix = [0.]
        for _, rate in self.events:
            self.prefix.append(self.prefix[-1] + rate)

    def stats(self, known_at, days, lag_minutes=1):
        if days <= 0 or lag_minutes < 1:
            raise ValueError('Funding lookback and information lag must be positive')
        cutoff = known_at - lag_minutes * MINUTE
        j = bisect_right(self.times, cutoff)
        i = bisect_right(self.times, cutoff - days * DAY)
        if i == 0 or not j or cutoff - self.times[j - 1] > 8 * HOUR:
            return None
        return dict(daily_rate=(self.prefix[j] - self.prefix[i]) / days,
                    observations=j-i, last_rate=self.events[j-1][1],
                    last_settlement=self.times[j-1], known_at=known_at)


def total_cost_rate(cfg):
    return 2 * cfg.spot_fee + 2 * cfg.swap_fee + 4 * cfg.adverse_bps / 10000


def adverse_funding(frame, cfg):
    # Already-paid opening costs are sunk. This estimates future negative carry
    # from realized trailing rates; it does not know future settlement rates.
    if cfg.exit_projection_days:
        return -frame['daily_rate'] * cfg.exit_projection_days >= cfg.exit_switch_cost_multiple * total_cost_rate(cfg)
    return frame['daily_rate'] <= 0


def entry_signal(history, known_at, cfg):
    frame = history.stats(known_at, cfg.entry_lookback_days, cfg.funding_available_lag_minutes)
    if frame is None:
        return False, None
    allowed = cfg.benchmark or (frame['last_rate'] > 0 and
        frame['daily_rate'] * cfg.expected_hold_days >= cfg.cost_cover * total_cost_rate(cfg))
    return allowed, frame
