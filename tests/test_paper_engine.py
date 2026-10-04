"""Incremental/reference ledger parity and restart boundaries; no network."""
import copy
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import MINUTE, HOUR, DAY
from reference_engine import Config, simulate
from paper_engine import PaperEngine

SPEC = dict(spot_lot=1e-8, spot_min=.00001, spot_tick=.1,
            usd_step=10., usd_min=10., swap_tick=.1, mmr=.004)
START = 2*DAY


def config(**overrides):
    values = dict(capital=20., benchmark=True, entry_lookback_days=1,
                  exit_lookback_days=1, min_hold_hours=1000,
                  allocation_mode='nearest_or_exact', cash_reserve=0.,
                  max_entry_delta_fraction=.02, residual_exit_fraction=.04,
                  treasury_mode='neutral')
    values.update(overrides)
    return Config(**values)


def market(end=5*DAY, price=None, rate=.0001):
    rows = []
    for t in range(0, end, MINUTE):
        p = 10000. if price is None else float(price(t))
        rows.append([t, p, p, p, p])
    marks = {}
    for t in range(0, end, HOUR):
        block = rows[t//MINUTE:min((t+HOUR)//MINUTE, len(rows))]
        marks[t] = [t, block[0][1], max(r[2] for r in block),
                    min(r[3] for r in block), block[-1][4]]
    funding = {t: (rate(t) if callable(rate) else rate) for t in range(0, end, 8*HOUR)}
    return rows, copy.deepcopy(rows), funding, marks


def reference(data, cfg, end):
    spot, swap, funding, marks = data
    return simulate(spot[:end//MINUTE], swap[:end//MINUTE], funding, marks,
                    START, end, cfg, SPEC, force_close=False)


def advance(engine, data, end, events=None):
    spot, swap, funding, marks = data
    first = START if engine.status()['cursor'] is None else engine.status()['cursor']+MINUTE
    for t in range(first, end, MINUTE):
        engine.step(spot[t//MINUTE], swap[t//MINUTE], marks[t//HOUR*HOUR], funding,
                    settlement_mark=marks[t][1] if t in marks else None)
        emitted = engine.drain_events()
        if events is not None:
            events.extend(emitted)
    return engine.status()


class ParityTests(unittest.TestCase):
    def same(self, actual, expected, path=''):
        if isinstance(expected, dict):
            self.assertEqual(set(actual), set(expected), path)
            for key in expected:
                self.same(actual[key], expected[key], path+'.'+key)
        elif isinstance(expected, (list, tuple)):
            self.assertEqual(len(actual), len(expected), path)
            for index, (a, e) in enumerate(zip(actual, expected)):
                self.same(a, e, path+'['+str(index)+']')
        elif isinstance(expected, float):
            self.assertAlmostEqual(actual, expected, delta=1e-10, msg=path)
        else:
            self.assertEqual(actual, expected, path)

    def compare(self, engine, data, end, events):
        actual = engine.status()
        expected = reference(data, engine.cfg, end)
        for key in ('summary', 'cash', 'pending', 'next_entry'):
            self.same(actual[key], expected[key], key)
        if expected['open_position'] is None:
            self.assertIsNone(actual['open_position'])
        else:
            p = actual['open_position'].copy()
            r = expected['open_position'].copy()
            received = r.pop('funding_events')
            sales = r.pop('treasury_sales')
            self.same(p.pop('funding_count'), len(received))
            self.same(p.pop('funding_usdt_received'), sum(e['usdt_at_receipt'] for e in received))
            self.same(p.pop('treasury_count'), len(sales))
            self.same(p.pop('treasury_coins'), sum(e['coins'] for e in sales))
            self.same(p.pop('treasury_gross_usdt'), sum(e['coins']*e['price'] for e in sales))
            self.same(p.pop('treasury_fees_usdt'), sum(e['fee_usdt'] for e in sales))
            self.same(p, r, 'position')
        trades = [e['data'] for e in events if e['kind'] == 'trade_closed']
        self.assertEqual(len(trades), len(expected['trades']))
        for actual_trade, expected_trade in zip(trades, expected['trades']):
            for key, value in expected_trade.items():
                if key not in ('funding_events', 'treasury_sales'):
                    self.same(actual_trade[key], value, 'trade.'+key)
        self.same([e['data'] for e in events if e['kind'] == 'signal'], expected['signals'], 'signals')

    def test_every_minute_prefix_during_signal_and_two_leg_entry(self):
        data = market(end=START+2*HOUR)
        engine = PaperEngine(config(second_leg_delay_minutes=5), SPEC, START)
        events = []
        for end in range(START+MINUTE, START+80*MINUTE, MINUTE):
            advance(engine, data, end, events)
            self.compare(engine, data, end, events)
        self.assertTrue(engine.status()['open_position']['swap_open'])

    def test_funding_and_neutral_treasury_events_match_reference(self):
        data = market(rate=.015)
        engine = PaperEngine(config(), SPEC, START)
        events = []
        for end in (START+9*HOUR, START+17*HOUR, 4*DAY, 5*DAY):
            advance(engine, data, end, events)
            self.compare(engine, data, end, events)
        sales = [e['data'] for e in events if e['kind'] == 'treasury_sale']
        self.assertTrue(sales)
        expected = reference(data, engine.cfg, 5*DAY)['open_position']
        self.same(sales, expected['treasury_sales'])
        self.same([e['data'] for e in events if e['kind'] == 'funding'], expected['funding_events'])
        for sale in sales:
            self.assertGreaterEqual(sale['delta_after_coins'], -1e-15)
            self.assertLess(abs(sale['delta_after_coins']), abs(sale['delta_before_coins']))

    def test_adverse_funding_exit_and_cooldown_match(self):
        data = market(end=7*DAY, rate=lambda t: .0001 if t<3*DAY else -.001)
        engine = PaperEngine(config(min_hold_hours=0, exit_bad_days=1), SPEC, START)
        events = []
        advance(engine, data, 7*DAY, events)
        self.compare(engine, data, 7*DAY, events)
        self.assertTrue(any(e['kind']=='trade_closed' and e['data']['reason']=='funding_deterioration' for e in events))

    def test_residual_guard_and_delayed_exit_match(self):
        data = market(price=lambda t: 10000. if t<3*DAY else 60000., rate=0.)
        cfg = config(cash_reserve=.01, spot_fee=0., swap_fee=0., adverse_bps=0.,
                     execution_latency_minutes=5, second_leg_delay_minutes=3,
                     loss_limit_fraction=.09, halt_drawdown=.10)
        engine = PaperEngine(cfg, SPEC, START)
        events=[]
        for end in (3*DAY+MINUTE, 3*DAY+6*MINUTE, 3*DAY+9*MINUTE, 5*DAY):
            advance(engine, data, end, events)
            self.compare(engine, data, end, events)
        exits=[e['data'] for e in events if e['kind']=='trade_closed']
        self.assertEqual(exits[0]['reason'], 'residual_exposure_guard')

    def test_basis_loss_and_account_halt_match(self):
        data = market()
        crash = START+2*HOUR
        for i, row in enumerate(data[1]):
            if row[0]>=crash:
                data[1][i]=[row[0], 60000., 60000., 60000., 60000.]
        for t in data[3]:
            if t>=crash:
                data[3][t]=[t, 60000., 60000., 60000., 60000.]
        engine=PaperEngine(config(),SPEC,START)
        events=[]
        advance(engine,data,START+3*HOUR,events)
        self.compare(engine,data,START+3*HOUR,events)
        self.assertTrue(engine.status()['summary']['account_halted'])

    def test_completed_hour_mark_triggers_margin_and_liquidation_guards(self):
        for target, reason in ((.2, 'margin_guard'), (.002, 'potential_liquidation')):
            data=market(end=START+3*HOUR)
            engine=PaperEngine(config(),SPEC,START)
            events=[]
            advance(engine,data,START+HOUR+2*MINUTE,events)
            p=engine.status()['open_position']
            mark=(target-1)*p['notional_usd']/(p['coins']-p['notional_usd']/p['swap_entry'])
            data[3][START+HOUR]=[START+HOUR,10000.,mark,10000.,mark]
            advance(engine,data,START+2*HOUR,events)
            self.compare(engine,data,START+2*HOUR,events)
            self.assertEqual(engine.status()['open_position']['close_reason'],reason)

    def test_nonhourly_funding_mark_and_entry_boundary(self):
        data = market(end=START+3*HOUR)
        entry=START+HOUR+MINUTE
        settlement=entry+7*MINUTE
        data[2][entry]=.01
        data[2][settlement]=.02
        data[3][entry]=[entry,10000.,10000.,10000.,10000.]
        data[3][settlement]=[settlement,11000.,11000.,11000.,11000.]
        engine=PaperEngine(config(),SPEC,START)
        events=[]
        advance(engine,data,START+3*HOUR,events)
        self.compare(engine,data,START+3*HOUR,events)
        settlements=[e for e in events if e['kind']=='funding']
        self.assertEqual([e['ts'] for e in settlements],[settlement])
        self.assertEqual(settlements[0]['data']['mark_open'],11000.)

    def test_funding_window_trim_keeps_signal_parity(self):
        data=market(end=12*DAY,rate=lambda t: .0002 if (t//DAY)%2 else -.00005)
        engine=PaperEngine(config(min_hold_hours=0,exit_bad_days=1),SPEC,START)
        events=[]
        advance(engine,data,12*DAY,events)
        self.compare(engine,data,12*DAY,events)
        self.assertLess(len(engine.snapshot()['funding_window']),12)


class RestartAndInputTests(unittest.TestCase):
    def test_restart_with_pending_entry_and_unflushed_outbox(self):
        data=market(end=START+3*HOUR)
        cfg=config(second_leg_delay_minutes=5)
        first=PaperEngine(cfg,SPEC,START)
        advance(first,data,START+HOUR)
        self.assertIsNotNone(first.status()['pending'])
        restored=PaperEngine(cfg,SPEC,START,json.loads(json.dumps(first.snapshot())))
        e1=[];e2=[]
        advance(first,data,START+3*HOUR,e1)
        advance(restored,data,START+3*HOUR,e2)
        self.assertEqual(first.snapshot(),restored.snapshot())
        self.assertEqual(e1,e2)

    def test_restart_pending_treasury_and_funding_not_paid_twice(self):
        data=market(rate=.015)
        cfg=config()
        first=PaperEngine(cfg,SPEC,START)
        advance(first,data,START+16*HOUR+MINUTE)
        self.assertIn('treasury_due',first.status()['open_position'])
        restored=PaperEngine(cfg,SPEC,START,json.loads(json.dumps(first.snapshot())))
        e1=[];e2=[]
        advance(first,data,4*DAY,e1)
        advance(restored,data,4*DAY,e2)
        self.assertEqual(first.snapshot(),restored.snapshot())
        self.assertEqual(e1,e2)

    def test_restart_pending_exit(self):
        data=market(price=lambda t:10000. if t<3*DAY else 60000.,rate=0.)
        cfg=config(cash_reserve=.01,spot_fee=0.,swap_fee=0.,adverse_bps=0.,
                   execution_latency_minutes=5,loss_limit_fraction=.09,halt_drawdown=.10)
        first=PaperEngine(cfg,SPEC,START)
        advance(first,data,3*DAY+MINUTE)
        self.assertIn('close_due',first.status()['open_position'])
        restored=PaperEngine(cfg,SPEC,START,json.loads(json.dumps(first.snapshot())))
        e1=[];e2=[]
        advance(first,data,4*DAY,e1)
        advance(restored,data,4*DAY,e2)
        self.assertEqual(first.snapshot(),restored.snapshot())
        self.assertEqual(e1,e2)

    def test_repeated_or_skipped_bar_and_changed_rate_are_atomic(self):
        data=market()
        engine=PaperEngine(config(),SPEC,START)
        advance(engine,data,START+MINUTE)
        before=engine.snapshot()
        for t in (START,START+2*MINUTE):
            with self.assertRaises(ValueError):
                engine.step(data[0][t//MINUTE],data[1][t//MINUTE],data[3][START],data[2])
            self.assertEqual(engine.snapshot(),before)
        funding=dict(data[2]);funding[START]=-1.
        t=START+MINUTE
        with self.assertRaises(ValueError):
            engine.step(data[0][t//MINUTE],data[1][t//MINUTE],data[3][START],funding)
        self.assertEqual(engine.snapshot(),before)

    def test_future_funding_and_intrahour_mark_close_do_not_leak(self):
        data=market(end=START+2*HOUR)
        altered=copy.deepcopy(data)
        cutoff=START+HOUR+10*MINUTE
        data[2][cutoff+MINUTE]=.03
        altered[2][cutoff+MINUTE]=-.5
        for t in altered[2]:
            if t>=cutoff:altered[2][t]=-.5
        altered[3][START+HOUR]=[START+HOUR,10000.,1000000.,1.,1000000.]
        a=PaperEngine(config(),SPEC,START);b=PaperEngine(config(),SPEC,START)
        ea=[];eb=[]
        advance(a,data,cutoff,ea);advance(b,altered,cutoff,eb)
        self.assertEqual(a.snapshot(),b.snapshot())
        self.assertEqual(ea,eb)

    def test_missing_settlement_mark_and_stale_funding_fail_atomically(self):
        data=market()
        engine=PaperEngine(config(),SPEC,START)
        advance(engine,data,START+2*HOUR)
        t=START+2*HOUR
        # Nonhour timestamp to force an explicit settlement open.
        advance(engine,data,t+MINUTE)
        t+=MINUTE
        funding=dict(data[2]);funding[t]=.01
        before=engine.snapshot()
        with self.assertRaises(ValueError):
            engine.step(data[0][t//MINUTE],data[1][t//MINUTE],data[3][t//HOUR*HOUR],funding)
        self.assertEqual(engine.snapshot(),before)
        fresh=PaperEngine(config(),SPEC,START)
        with self.assertRaises(ValueError):
            fresh.step(data[0][START//MINUTE],data[1][START//MINUTE],data[3][START],{0:.0001})
        self.assertIsNone(fresh.status()['cursor'])

    def test_checkpoint_identity_and_snapshot_isolation(self):
        engine=PaperEngine(config(),SPEC,START)
        state=engine.snapshot()
        state['cash']=0.
        self.assertEqual(engine.status()['cash'],20.)
        with self.assertRaises(ValueError):
            PaperEngine(replace(config(),capital=60.),SPEC,START,engine.snapshot())

    def test_outbox_survives_restart_and_sequence_never_resets(self):
        data=market(end=START+2*HOUR)
        a=PaperEngine(config(),SPEC,START)
        for t in range(START,START+HOUR+2*MINUTE,MINUTE):
            a.step(data[0][t//MINUTE],data[1][t//MINUTE],data[3][t//HOUR*HOUR],data[2])
        b=PaperEngine(config(),SPEC,START,json.loads(json.dumps(a.snapshot())))
        out=a.drain_events()
        self.assertEqual(out,b.drain_events())
        self.assertEqual([e['seq'] for e in out],list(range(1,len(out)+1)))
        self.assertEqual(a.drain_events(),[])


if __name__=='__main__':
    unittest.main()
