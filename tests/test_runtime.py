import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import MINUTE,HOUR,DAY,timestamp
from paper import Runner,load_settings,normalize_funding,export_snapshot
from store import Store,process_lock

ROOT=Path(__file__).resolve().parents[1]
SPEC={'spot_lot':1e-8,'spot_min':1e-5,'spot_tick':.1,'usd_step':10.,'usd_min':10.,'swap_tick':.1,'mmr':.004}
NOW=timestamp('2026-01-01T08:56:30')


class FakeClient:
    def __init__(self):
        self.now=NOW;self.fail=False;self.gap=False;self.revise=False;self.missing_due=False

    def server_time(self):
        if self.fail:raise TimeoutError('https://secret:password@proxy')
        return self.now

    def instruments(self):return {'spec':dict(SPEC),'raw':{'test':True},'first_tier_max_notional_usd':1_000_000.}

    def funding_schedule(self):
        due=((self.now//(8*HOUR))+1)*(8*HOUR)
        return {'funding_time':due,'next_funding_time':due+8*HOUR}

    def funding_history(self,start,end):
        times=range(((start+8*HOUR-1)//(8*HOUR))*8*HOUR,end,8*HOUR)
        return {t:(.0002 if self.revise else .0001) for t in times if not self.missing_due or t<timestamp('2026-01-01T16:00:00')}

    def candles(self,inst,bar,start,end,mark=False):
        rows=[(t,10000.,10000.,10000.,10000.) for t in range(start,end,MINUTE)]
        return rows[:-1] if self.gap else rows


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.folder=Path(self.temp.name)
        self.store=Store(self.folder)
        self.settings,self.cfg=load_settings(ROOT/'config-F3-20u.json')
        self.client=FakeClient()
        self.runner=Runner(self.settings,self.cfg,self.store,self.client,{'test':'v1'})

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def bootstrap_and_enter(self):
        self.assertEqual(self.runner.tick(),0)
        self.assertEqual(self.store.get('cursor'),timestamp('2026-01-01T08:57:00'))
        self.assertEqual(self.store.statistics()['events'],0)
        self.client.now=timestamp('2026-01-01T09:06:00')
        self.assertEqual(self.runner.tick(),7)
        self.assertIsNotNone(self.store.get('account')['open_position'])

    def test_warmup_does_not_open_historical_positions_and_restart_preserves_account(self):
        self.bootstrap_and_enter()
        prior=self.store.get('account')
        count=self.store.statistics()['events']
        # Same server boundary: no repeated events or fresh capital.
        restarted=Runner(self.settings,self.cfg,self.store,self.client,{'test':'v1'})
        self.assertEqual(restarted.tick(),0)
        self.assertEqual(prior,self.store.get('account'))
        self.assertEqual(count,self.store.statistics()['events'])
        self.client.now+=MINUTE
        self.assertEqual(restarted.tick(),1)
        self.assertEqual(self.store.get('account')['open_position']['entered_at'],prior['open_position']['entered_at'])

    def test_network_and_candle_gap_do_not_advance(self):
        self.bootstrap_and_enter();checkpoint=self.store.get('checkpoint');cursor=self.store.get('cursor')
        self.client.fail=True
        with self.assertRaises(TimeoutError):self.runner.tick()
        self.client.fail=False;self.client.gap=True;self.client.now+=MINUTE
        with self.assertRaisesRegex(ValueError,'分钟行情'):self.runner.tick()
        self.assertEqual(self.store.get('checkpoint'),checkpoint)
        self.assertEqual(self.store.get('cursor'),cursor)

    def test_revision_is_rejected_without_rewriting_saved_state(self):
        self.bootstrap_and_enter();checkpoint=self.store.get('checkpoint')
        self.client.revise=True;self.client.now+=MINUTE
        with self.assertRaisesRegex(ValueError,'修订'):self.runner.tick()
        self.assertEqual(self.store.get('checkpoint'),checkpoint)

    def test_expected_settlement_missing_stops_and_later_recovery_is_labelled(self):
        self.bootstrap_and_enter();cursor=self.store.get('cursor')
        self.client.now=timestamp('2026-01-01T16:05:00');self.client.missing_due=True
        # First catch-up block ends before due; subsequent block must not skip it.
        self.runner.tick();checkpoint=self.store.get('checkpoint')
        with self.assertRaises(ValueError):self.runner.tick()
        self.assertEqual(self.store.get('checkpoint'),checkpoint)
        self.client.missing_due=False;self.runner.tick()
        account=self.store.get('account')
        self.assertEqual(account['open_position']['funding_count'],1)
        self.assertGreater(self.store.statistics()['minutes_by_classification'].get('recovered',0),0)
        self.assertGreater(self.store.get('cursor'),cursor)

    def test_settings_change_cannot_reset_balance(self):
        self.bootstrap_and_enter()
        changed=json.loads(json.dumps(self.settings));changed['strategy']['capital']=60
        with self.assertRaisesRegex(ValueError,'不一致'):
            self.store.verify_identity(changed,{'test':'v1'})

    def test_process_lock_prevents_double_running(self):
        with process_lock(self.folder):
            with self.assertRaises(RuntimeError):
                with process_lock(self.folder):pass

    def test_funding_rounding_never_moves_information_earlier(self):
        raw={60_001:.001,120_001:.002};events=normalize_funding(raw)
        self.assertEqual(list(events),[120_000,180_000])
        self.assertTrue(all(e['ts']>=e['actual_ts'] for e in events.values()))

    def test_export_is_consistent_snapshot_and_contains_no_env(self):
        self.bootstrap_and_enter();output=self.folder/'export.zip'
        (self.folder/'.env').write_text('HTTPS_PROXY=secret')
        export_snapshot(self.store,output)
        import zipfile
        with zipfile.ZipFile(output) as z:
            self.assertNotIn('.env',z.namelist())
            snap=json.loads(z.read('status.json'))
            self.assertEqual(snap['cursor'],self.store.get('cursor'))
            z.extractall(self.folder/'extracted')
        copied=Store(self.folder/'extracted',readonly=True)
        try:self.assertEqual(copied.get('checkpoint'),self.store.get('checkpoint'))
        finally:copied.close()

    def test_batch_failure_rolls_back_cursor_events_and_funding_marker(self):
        self.bootstrap_and_enter();cursor=self.store.get('cursor');checkpoint=self.store.get('checkpoint')
        events=self.store.statistics()['events'];funding=self.store.get('processed_funding')
        original=self.store.put
        def fail(key,value):
            if key=='processed_funding':raise RuntimeError('injected disk error')
            original(key,value)
        self.client.now+=MINUTE
        with patch.object(self.store,'put',side_effect=fail):
            with self.assertRaisesRegex(RuntimeError,'injected'):self.runner.tick()
        self.assertEqual(self.store.get('cursor'),cursor)
        self.assertEqual(self.store.get('checkpoint'),checkpoint)
        self.assertEqual(self.store.get('processed_funding'),funding)
        self.assertEqual(self.store.statistics()['events'],events)
        self.assertEqual(self.runner.tick(),1)


if __name__=='__main__':unittest.main()
