"""Incremental, restartable public-data F3 simulation. No network or orders.

One completed minute advances one paper ledger. Historical funding and trade
events are emitted through the outbox; only bounded funding lookbacks and
current balances remain in checkpoints. Prices are valued exactly as in the
frozen research engine: estimated two-leg liquidation including costs, not a
claim that either order traded on OKX. No sample-end forced close is applied.
"""
import copy
import math
from bisect import bisect_right
from dataclasses import asdict

from common import MINUTE, HOUR, DAY, iso
from reference_engine import (Config, collateral_ratio, short_pnl, size,
                              neutral_treasury_amount)
from signals import adverse_funding, entry_signal, fill, round_step


ECONOMIC_FIELDS = ('funding_usdt_on_receipt', 'funding_usdt_at_exit',
                   'funding_fx_pnl', 'price_pnl', 'fees_usdt_at_exit',
                   'dust_cost', 'principal_residual_price_pnl', 'basis_pnl')


def _number(value, label, positive=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or (positive and value <= 0)):
        raise ValueError('Invalid finite number: ' + label)


def _row(row, label):
    if not isinstance(row, (list, tuple)) or len(row) < 5:
        raise ValueError(label + ' requires timestamp and OHLC')
    if not isinstance(row[0], int) or isinstance(row[0], bool) or row[0] % MINUTE:
        raise ValueError(label + ' timestamp must be a minute boundary')
    for value in row[1:5]:
        _number(value, label + ' price', positive=True)
    if row[2] < max(row[1], row[3], row[4]) or row[3] > min(row[1], row[2], row[4]):
        raise ValueError(label + ' OHLC range is inconsistent')


class _FundingWindow:
    """Bounded prefix sums retaining the event before the earliest lookback."""
    def __init__(self, rows, horizon):
        self.rows = rows
        self.horizon = horizon

    def merge(self, funding, t):
        if not isinstance(funding, dict):
            raise ValueError('funding must be a timestamp/rate dictionary')
        current = {r[0]: r[1] for r in self.rows}
        incoming = []
        for timestamp, rate in funding.items():
            if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp % MINUTE:
                raise ValueError('Funding timestamp must be a minute boundary')
            if timestamp > t:
                continue  # Future settlements cannot influence this minute.
            _number(rate, 'funding rate')
            if timestamp in current:
                if rate != current[timestamp]:
                    raise ValueError('Previously observed funding rate changed')
            elif self.rows and timestamp <= self.rows[-1][0]:
                if timestamp >= self.rows[0][0]:
                    raise ValueError('Late funding would rewrite an already processed ledger')
            else:
                incoming.append((timestamp, rate))
        for timestamp, rate in sorted(incoming):
            cumulative = (self.rows[-1][2] if self.rows else 0.) + rate
            self.rows.append([timestamp, rate, cumulative])
        cutoff = t - self.horizon
        # Retain the last event at/before cutoff for FundingHistory's i != 0
        # coverage check and its original prefix-sum subtraction convention.
        while len(self.rows) > 1 and self.rows[1][0] <= cutoff:
            self.rows.pop(0)
        if not self.rows or t + MINUTE - self.rows[-1][0] > 8 * HOUR + MINUTE:
            raise ValueError('Missing/stale funding history for this minute')
        if any(b[0] - a[0] > 8 * HOUR + MINUTE for a, b in zip(self.rows, self.rows[1:])):
            raise ValueError('Gap in funding history')

    def rate_at(self, timestamp):
        return self.rows[-1][1] if self.rows and self.rows[-1][0] == timestamp else None

    def stats(self, known_at, days, lag_minutes=1):
        cutoff = known_at - lag_minutes * MINUTE
        times = [r[0] for r in self.rows]
        j = bisect_right(times, cutoff)
        i = bisect_right(times, cutoff - days * DAY)
        if i == 0 or not j or cutoff - times[j - 1] > 8 * HOUR:
            return None
        total = self.rows[j - 1][2] - self.rows[i - 1][2]
        return dict(daily_rate=total / days, observations=j-i,
                    last_rate=self.rows[j-1][1], last_settlement=times[j-1], known_at=known_at)


class PaperEngine:
    SCHEMA = 1

    def __init__(self, cfg, spec, start, state=None):
        if not isinstance(cfg, Config):
            raise ValueError('cfg must be reference_engine.Config')
        self.cfg = cfg.validate()
        if not isinstance(start, int) or isinstance(start, bool) or start % MINUTE:
            raise ValueError('start must be a minute boundary')
        self.spec = copy.deepcopy(spec)
        for name in ('spot_lot', 'spot_min', 'spot_tick', 'usd_step', 'usd_min', 'swap_tick', 'mmr'):
            _number(spec.get(name), 'spec.' + name, positive=name != 'mmr')
        if spec['mmr'] < 0 or cfg.assumed_mmr + 1e-12 < spec['mmr']:
            raise ValueError('Maintenance margin specification is invalid/understated')
        self.start = start
        if state is not None:
            if (state.get('schema') != self.SCHEMA or state.get('config') != asdict(cfg)
                    or state.get('spec') != self.spec or state.get('start') != start):
                raise ValueError('Checkpoint schema, config, specification or start differs')
            self._s = copy.deepcopy(state)
            cursor = self._s.get('cursor')
            if cursor is not None and (not isinstance(cursor, int) or cursor < start or cursor % MINUTE):
                raise ValueError('Invalid checkpoint cursor')
            return
        self._s = dict(schema=self.SCHEMA, config=asdict(cfg), spec=self.spec,
                       start=start, cursor=None, cash=cfg.capital, position=None,
                       pending=None, next_entry=start, peak=cfg.capital, maxdd=0.,
                       halt=False, minimum=None, max_delta=0., naked=0,
                       funding_window=[], rejections={}, exit_reasons={},
                       closed_count=0, closed_net=0., liquidation_count=0,
                       closed_treasury_count=0, closed_treasury_usdt=0.,
                       economics={k: 0. for k in ECONOMIC_FIELDS},
                       last_spot=None, last_swap=None, equity=cfg.capital,
                       seq=0, outbox=[])

    def snapshot(self):
        return copy.deepcopy(self._s)

    def drain_events(self):
        result = self._s['outbox']
        self._s['outbox'] = []
        return result

    def _emit(self, kind, ts, data):
        self._s['seq'] += 1
        self._s['outbox'].append(dict(seq=self._s['seq'], kind=kind, ts=ts, data=copy.deepcopy(data)))

    def _nav(self, sp, pp):
        q = self._s
        p = q['position']
        if p is None:
            return q['cash']
        value = p['coins']
        if p['swap_open']:
            cover = fill(pp, True, self.cfg, self.spec['swap_tick'])
            value += short_pnl(p['notional_usd'], p['swap_entry'], cover) - p['notional_usd']/cover*self.cfg.swap_fee
        if value >= 0:
            return q['cash'] + value*fill(sp, False, self.cfg, self.spec['spot_tick'])*(1-self.cfg.spot_fee)
        return q['cash'] + value*fill(sp, True, self.cfg, self.spec['spot_tick'])*(1+self.cfg.spot_fee)

    def _request(self, reason, known):
        p = self._s['position']
        if p.get('close_due') is None:
            p.update(close_due=known+self.cfg.execution_latency_minutes*MINUTE,
                     exit_known_at=known, close_reason=reason)
            self._emit('exit_requested', known, dict(reason=reason, due=p['close_due']))

    def _cover(self, raw, t):
        p = self._s['position']
        price = fill(raw, True, self.cfg, self.spec['swap_tick'])
        n = p['notional_usd']
        pnl = short_pnl(n, p['swap_entry'], price)
        fee = n/price*self.cfg.swap_fee
        p['coins'] += pnl-fee
        p.update(swap_open=False, swap_exit=price, swap_exited_at=t,
                 swap_pnl_coins=pnl, swap_exit_fee_coins=fee)
        self._emit('swap_closed', t, dict(price=price, notional_usd=n, pnl_coins=pnl, fee_coins=fee))

    def _sell(self, raw, t, reason):
        q, cfg, spec = self._s, self.cfg, self.spec
        p = q['position']
        price = fill(raw, False, cfg, spec['spot_tick'])
        if p['coins'] < 0:
            raise ValueError('Negative coin balance: execution model invalid')
        sold = round_step(p['coins'], spec['spot_lot'])
        dust = p['coins']-sold
        if sold < spec['spot_min']:
            raise ValueError('Cannot execute whole-group exit below the spot minimum')
        fee = sold*price*cfg.spot_fee
        q['cash'] += sold*price-fee
        p.update(spot_exit=price, spot_exited_at=t, exited_at=t, reason=reason,
                 spot_exit_fee_usdt=fee, sold_coins=sold, dust_coins_written_off=dust,
                 end_equity=q['cash'], net=q['cash']-p['capital_at_entry'])
        funding_end = p['funding_coins']*price + p['treasury_gross_usdt']-p['treasury_coins']*price
        price_pnl = p['gross_coins']*(price-p['spot_entry'])+p.get('swap_pnl_coins', 0.)*price
        fees = (p['spot_entry_fee_coins']+p.get('swap_entry_fee_coins', 0.)
                +p.get('swap_exit_fee_coins', 0.))*price+fee+p['treasury_fees_usdt']
        dust_cost = dust*price
        if abs(p['net']-(price_pnl+funding_end-fees-dust_cost)) >= 1e-8:
            raise ValueError('Coin/USDT exit ledger identity failed')
        p.update(funding_usdt_on_receipt=p['funding_usdt_received'], funding_usdt_at_exit=funding_end,
                 funding_fx_pnl=funding_end-p['funding_usdt_received'], price_pnl=price_pnl,
                 fees_usdt_at_exit=fees, dust_cost=dust_cost)
        if 'swap_entry' in p:
            residual_pnl = (p['gross_coins']-p['notional_usd']/p['swap_entry'])*(price-p['spot_entry'])
            p.update(principal_residual_price_pnl=residual_pnl, basis_pnl=price_pnl-residual_pnl)
        q['closed_count'] += 1
        q['closed_net'] += p['net']
        q['exit_reasons'][reason] = q['exit_reasons'].get(reason, 0)+1
        q['closed_treasury_count'] += p['treasury_count']
        q['closed_treasury_usdt'] += p['treasury_gross_usdt']-p['treasury_fees_usdt']
        for key in ECONOMIC_FIELDS:
            q['economics'][key] += p.get(key, 0.)
        self._emit('trade_closed', t, p)
        q['position'] = None
        q['next_entry'] = t+cfg.cooldown_hours*HOUR

    def step(self, spot_row, swap_row, mark_row, funding, settlement_mark=None):
        """Apply exactly one completed minute, or raise without changing state.

        ``mark_row`` belongs to that hour; its close is used only at hour end.
        Non-hourly funding requires the settlement minute's mark OPEN supplied
        separately. The caller controls public-data completeness/publication.
        Historical catch-up remains model reconstruction, never actual fills.
        """
        for row, label in ((spot_row, 'spot'), (swap_row, 'swap'), (mark_row, 'mark')):
            _row(row, label)
        t = spot_row[0]
        expected = self.start if self._s['cursor'] is None else self._s['cursor']+MINUTE
        if t != expected or swap_row[0] != t or mark_row[0] != t//HOUR*HOUR:
            raise ValueError('Expected the next continuous spot/swap minute and its hourly mark')
        if settlement_mark is not None:
            _number(settlement_mark, 'settlement mark', positive=True)
        # State is bounded independently of runtime duration when the caller
        # drains the outbox. A failed step leaves the committed ledger intact.
        previous = self._s
        self._s = copy.deepcopy(previous)
        try:
            self._advance(spot_row, swap_row, mark_row, funding, settlement_mark)
        except Exception:
            self._s = previous
            raise
        return self.status()

    def _advance(self, s, b, mark_row, funding, settlement_mark):
        q, cfg, spec = self._s, self.cfg, self.spec
        t, known = s[0], s[0]+MINUTE
        horizon = (max(cfg.entry_lookback_days, cfg.exit_lookback_days)+1)*DAY + cfg.funding_available_lag_minutes*MINUTE
        hist = _FundingWindow(q['funding_window'], horizon)
        hist.merge(funding, t)
        rate = hist.rate_at(t)
        p = q['position']
        if p and p['swap_open'] and p['swap_entered_at'] < t and rate is not None:
            if settlement_mark is None and t % HOUR:
                raise ValueError('Non-hourly settlement requires its minute mark OPEN')
            mark = settlement_mark if settlement_mark is not None else mark_row[1]
            eligible = rate < 0 or p.get('close_due', math.inf) > t
            coins = p['notional_usd']/mark*rate if eligible else 0.
            p['coins'] += coins
            p['funding_coins'] += coins
            p['funding_usdt_received'] += coins*s[1]
            p['funding_count'] += 1
            self._emit('funding', t, dict(ts=t, rate=rate, mark_open=mark, amount_coins=coins,
                                        spot_open=s[1], usdt_at_receipt=coins*s[1], eligible=eligible))
        if p and p.get('close_due') == t:
            if p['swap_open']:
                self._cover(b[1], t)
            p['sell_due'] = t+cfg.second_leg_delay_minutes*MINUTE
        if p and p.get('sell_due') == t:
            self._sell(s[1], t, p['close_reason'])
            p = q['position']
        if p and p.get('treasury_due') == t:
            requested_at = p.pop('treasury_known_at')
            p.pop('treasury_due')
            if p['swap_open'] and p.get('close_due') is None:
                mark = mark_row[1] if t % HOUR == 0 else b[1]
                collateral = p['coins']+short_pnl(p['notional_usd'], p['swap_entry'], mark)
                unconverted = p['funding_coins']-p['treasury_coins']
                available = max(0., collateral-cfg.treasury_min_collateral_ratio*p['notional_usd']/mark)
                amount = round_step(max(0., min(unconverted, available)), spec['spot_lot'])
                before_nav = self._nav(s[1], b[1])
                delta = (p['coins']-p['notional_usd']/p['swap_entry'])*s[1]
                risk_reducing = True
                if cfg.treasury_mode == 'neutral':
                    amount, available, safe_limit = neutral_treasury_amount(p['coins'], p['notional_usd'],
                        p['swap_entry'], mark, unconverted, cfg, spec)
                    delta_before_coins = p['coins']-p['notional_usd']/p['swap_entry']
                    delta_after_coins = (p['coins']-amount)-p['notional_usd']/p['swap_entry']
                    tolerance = max(1e-15, spec['spot_lot']*1e-8)
                    risk_reducing = (delta_before_coins > 0 and delta_after_coins >= -tolerance
                                     and abs(delta_after_coins) < abs(delta_before_coins))
                if amount >= spec['spot_min'] and delta > cfg.treasury_trigger_fraction*before_nav and risk_reducing:
                    price = fill(s[1], False, cfg, spec['spot_tick'])
                    fee = amount*price*cfg.spot_fee
                    previous_cash, previous_coins = q['cash'], p['coins']
                    p['coins'] -= amount
                    q['cash'] += amount*price-fee
                    p['treasury_coins'] += amount
                    p['treasury_gross_usdt'] += amount*price
                    p['treasury_fees_usdt'] += fee
                    p['treasury_count'] += 1
                    sale = dict(known_at=requested_at, executed_at=t, coins=amount, price=price, fee_usdt=fee,
                        cash_before=previous_cash, cash_after=q['cash'], coins_before=previous_coins, coins_after=p['coins'],
                        proxy_mark=mark, collateral_ratio_after=(collateral-amount)*mark/p['notional_usd'],
                        unconverted_before=unconverted, equity_before=before_nav, equity_after=self._nav(s[1], b[1]))
                    if cfg.treasury_mode == 'neutral':
                        sale.update(treasury_mode='neutral', delta_before_coins=delta_before_coins,
                            delta_after_coins=delta_after_coins, delta_before_usdt=delta_before_coins*s[1],
                            delta_after_usdt=delta_after_coins*s[1], neutral_sell_limit_coins=safe_limit,
                            available_collateral_coins=available)
                    self._emit('treasury_sale', t, sale)
        pending = q['pending']
        if pending and pending['due'] == t:
            if p is not None:
                raise ValueError('Cannot enter while another position is open')
            plan, reason = size(q['cash'], s[1], b[1], cfg, spec)
            if reason:
                q['rejections'][reason] = q['rejections'].get(reason, 0)+1
                self._emit('entry_rejected', t, dict(reason=reason))
            elif not q['halt'] and t >= q['next_entry']:
                p = dict(plan, capital_at_entry=q['cash'], entered_at=t,
                    entry_known_at=pending['known_at'], entry_frame=pending['frame'],
                    coins=plan['net_coins'], swap_open=False, hedge_due=t+cfg.second_leg_delay_minutes*MINUTE,
                    funding_coins=0., funding_usdt_received=0., funding_count=0, bad_days=0,
                    treasury_coins=0., treasury_gross_usdt=0., treasury_fees_usdt=0., treasury_count=0)
                q['position'] = p
                q['cash'] -= p['spot_cost']
                self._emit('spot_opened', t, p)
            q['pending'] = None
        if p and not p['swap_open'] and p.get('hedge_due') == t and p.get('sell_due') is None:
            price = fill(b[1], False, cfg, spec['swap_tick'])
            n = p['notional_usd']
            fee = n/price*cfg.swap_fee
            delta = (p['coins']-fee-n/price)*s[1]
            ratio = (p['coins']-fee)*price/n
            if abs(delta) > p['capital_at_entry']*cfg.max_entry_delta_fraction+1e-10 or ratio < cfg.minimum_entry_collateral_ratio:
                self._sell(s[1], t, 'entry_hedge_rejected')
                p = q['position']
            else:
                p['coins'] -= fee
                p.update(swap_open=True, swap_entry=price, swap_entered_at=t, swap_entry_fee_coins=fee,
                         initial_collateral_coins=p['coins'], initial_delta_usdt=delta, initial_collateral_ratio=ratio)
                self._emit('swap_opened', t, dict(price=price, notional_usd=n, fee_coins=fee,
                                                delta_usdt=delta, collateral_ratio=ratio))
        equity = self._nav(s[4], b[4])
        q['peak'] = max(q['peak'], equity)
        dd = (q['peak']-equity)/q['peak']
        q['maxdd'] = max(q['maxdd'], dd)
        if p:
            if not p['swap_open']:
                q['naked'] += 1
            if p['swap_open']:
                mark = mark_row[4] if known % HOUR == 0 else b[4]
                ratio = collateral_ratio(p['coins'], p['notional_usd'], p['swap_entry'], mark)
                q['minimum'] = ratio if q['minimum'] is None else min(q['minimum'], ratio)
                delta = (p['coins']-p['notional_usd']/p['swap_entry'])*s[4]
                q['max_delta'] = max(q['max_delta'], abs(delta)/max(equity, 1e-12))
                if p.get('close_due') is None and p.get('treasury_due') is None and delta > cfg.treasury_trigger_fraction*equity:
                    unconverted = p['funding_coins']-p['treasury_coins']
                    schedulable = round_step(max(0., unconverted), spec['spot_lot'])
                    if cfg.treasury_mode == 'neutral':
                        schedulable, _, _ = neutral_treasury_amount(p['coins'], p['notional_usd'],
                            p['swap_entry'], mark, unconverted, cfg, spec)
                    if schedulable >= spec['spot_min']:
                        p.update(treasury_known_at=known, treasury_due=known+cfg.execution_latency_minutes*MINUTE)
                        self._emit('treasury_requested', known, dict(due=p['treasury_due']))
                if ratio <= cfg.assumed_mmr+cfg.swap_fee:
                    q['liquidation_count'] += 1
                    self._emit('liquidation_risk', known, dict(ts=known, ratio=ratio))
                    self._request('potential_liquidation', known)
                elif ratio <= cfg.margin_exit_ratio:
                    self._request('margin_guard', known)
                fraction = abs(delta)/max(equity, 1e-12)
                if cfg.residual_exit_fraction and fraction > cfg.residual_exit_fraction and p.get('close_due') is None:
                    p['residual_guard_frame'] = dict(known_at=known, signed_delta_usdt=delta, equity=equity, fraction=fraction)
                    self._request('residual_exposure_guard', known)
            if equity-p['capital_at_entry'] <= -cfg.loss_limit_fraction*p['capital_at_entry']:
                self._request('combined_loss_guard', known)
        if dd >= cfg.halt_drawdown and not q['halt']:
            q['halt'] = True
            self._emit('account_halted', known, dict(drawdown=dd))
            if p:
                self._request('account_halt', known)
        if (known+8*HOUR) % DAY == cfg.decision_hour_bjt*HOUR:
            if p and p['swap_open'] and p.get('close_due') is None and known-p['entered_at'] >= cfg.min_hold_hours*HOUR:
                frame = hist.stats(known, cfg.exit_lookback_days, cfg.funding_available_lag_minutes)
                if frame:
                    p['bad_days'] = p['bad_days']+1 if adverse_funding(frame, cfg) else 0
                    if p['bad_days'] >= cfg.exit_bad_days:
                        p['exit_funding_frame'] = frame
                        self._request('funding_deterioration', known)
            if p is None and q['pending'] is None and not q['halt'] and known >= q['next_entry']:
                allowed, frame = entry_signal(hist, known, cfg)
                self._emit('signal', known, dict(known_at=known, allowed=allowed, frame=frame))
                if allowed:
                    q['pending'] = dict(known_at=known, due=known+cfg.execution_latency_minutes*MINUTE, frame=frame)
        if p is None:
            expected_cash = cfg.capital+q['closed_net']
        else:
            expected_cash = cfg.capital+q['closed_net']-p['spot_cost']+p['treasury_gross_usdt']-p['treasury_fees_usdt']
        if abs(q['cash']-expected_cash) >= 1e-8:
            raise ValueError('Incremental cash ledger identity failed')
        q.update(cursor=t, last_spot=list(s[:5]), last_swap=list(b[:5]), equity=equity)
        if known % HOUR == 0:
            self._emit('hourly_equity', known, dict(equity=equity))

    def status(self):
        q, cfg = self._s, self.cfg
        end = self.start if q['cursor'] is None else q['cursor']+MINUTE
        summary = dict(capital=cfg.capital, start_bjt=iso(self.start), end_bjt_exclusive=iso(end),
            days=(end-self.start)/DAY, end_equity=q['equity'], net=q['equity']-cfg.capital,
            return_pct=100*(q['equity']/cfg.capital-1), max_drawdown_pct=100*q['maxdd'],
            trades=q['closed_count'], account_halted=q['halt'], minimum_collateral_ratio=q['minimum'],
            potential_liquidation_flags=q['liquidation_count'], max_residual_delta_pct=100*q['max_delta'],
            naked_minutes=q['naked'], rejections=copy.deepcopy(q['rejections']), exit_reasons=copy.deepcopy(q['exit_reasons']),
            treasury_sales=q['closed_treasury_count'], treasury_converted_usdt=q['closed_treasury_usdt'], **q['economics'])
        return dict(summary=summary, open_position=copy.deepcopy(q['position']), cash=q['cash'],
                    pending=copy.deepcopy(q['pending']), next_entry=q['next_entry'], cursor=q['cursor'],
                    force_close=False, sequence=q['seq'])
