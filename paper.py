#!/usr/bin/env python3
"""F3 public-data paper account for Linux. Only public GET requests exist."""
import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import zipfile

from common import MINUTE, HOUR, DAY, iso
from reference_engine import Config
from store import Store, process_lock, digest, encode
from public_data import OKXClient
from paper_engine import PaperEngine

ROOT = Path(__file__).resolve().parent
SOURCE_FILES = ['paper.py', 'paper_engine.py', 'public_data.py', 'store.py',
                'reference_engine.py', 'signals.py', 'common.py']


def load_settings(path):
    settings = json.loads(Path(path).read_text(encoding='utf-8'))
    if set(settings) != {'version', 'mode', 'strategy', 'runtime'} or settings['mode'] != 'public-paper':
        raise ValueError('只支持 public-paper 模拟模式。')
    if settings['version'] != 'F3-paper-1':
        raise ValueError('未知模拟版本。')
    cfg = Config.from_dict(settings['strategy'])
    if (cfg.treasury_mode != 'neutral' or cfg.cash_reserve != 0
            or cfg.allocation_mode != 'nearest_or_exact' or cfg.residual_exit_fraction != .04):
        raise ValueError('配置与 F3 核心规则不一致。')
    rt = settings['runtime']
    if set(rt) != {'base_url','poll_seconds','settlement_delay_minutes','max_catchup_days','warmup_days'}:
        raise ValueError('未知运行配置字段。')
    for name in ('poll_seconds', 'settlement_delay_minutes', 'max_catchup_days', 'warmup_days'):
        if type(rt[name]) is not int:
            raise ValueError('运行时间参数必须是整数。')
    if not 15 <= rt['poll_seconds'] <= 60 or not 2 <= rt['settlement_delay_minutes'] <= 10:
        raise ValueError('轮询应为15至60秒，结算等待应为2至10分钟。')
    if not 1 <= rt['max_catchup_days'] <= 30 or not max(cfg.entry_lookback_days,cfg.exit_lookback_days)+1 <= rt['warmup_days'] <= 30:
        raise ValueError('历史补算或信号预热时间不合法。')
    return settings, cfg


def identity():
    return {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def normalize_funding(raw):
    result = {}
    for actual, rate in sorted(raw.items()):
        ts = ((actual + MINUTE-1)//MINUTE)*MINUTE
        if ts in result:
            raise ValueError('多个结算事件落入同一模拟分钟。')
        if not math.isfinite(rate):
            raise ValueError('资金费率不是有效数字。')
        result[ts] = {'ts': ts, 'actual_ts': actual, 'rate': rate}
    return result


def validate_funding(events, start, end, cfg):
    # More than 8h cannot be silently interpreted as a zero funding rate.
    times = sorted(events)
    if not times:
        raise ValueError('无已结算资金费历史。')
    lookback_start = start - max(cfg.entry_lookback_days,cfg.exit_lookback_days)*DAY - MINUTE
    if times[0] >= lookback_start:
        raise ValueError('资金费暖机数据不足。')
    relevant = [t for t in times if t < end]
    if not relevant or end-relevant[-1] > 8*HOUR+MINUTE:
        raise ValueError('最近资金费尚未发布或数据过期，暂停推进。')
    if any(b-a > 8*HOUR+MINUTE for a,b in zip(relevant,relevant[1:])):
        raise ValueError('资金费历史存在超过8小时的缺口，暂停推进。')


def validate_spec(spec, cfg):
    names = ('spot_lot','spot_min','spot_tick','usd_step','usd_min','swap_tick','mmr')
    if set(spec) != set(names):
        raise ValueError('交易规格字段不完整。')
    if any(not isinstance(spec[k], (float,int)) or not math.isfinite(spec[k]) or spec[k] <= 0 for k in names):
        raise ValueError('交易规格含无效值。')
    if spec['mmr'] > cfg.assumed_mmr+1e-12:
        raise ValueError('当前维持保证金率高于 F3 假设；请停止此版本并核对规则。')


def checked_grid(rows, start, end, label):
    if [r[0] for r in rows] != list(range(start,end,MINUTE)):
        raise ValueError(label+' 缺少完整已确认分钟行情。')
    for row in rows:
        if len(row) != 5 or any(not math.isfinite(p) or p <= 0 for p in row[1:]):
            raise ValueError(label+' 价格无效。')
        if row[2] < max(row[1],row[3],row[4]) or row[3] > min(row[1],row[2],row[4]):
            raise ValueError(label+' OHLC不一致。')
    return {r[0]: r for r in rows}


class Runner:
    def __init__(self, settings, cfg, store, client, source_identity):
        self.settings, self.cfg, self.store, self.client = settings,cfg,store,client
        self.source_identity = source_identity
        self.last_spec_check = 0

    def tick(self):
        s,c,rt = self.store,self.client,self.settings['runtime']
        s.verify_identity(self.settings,self.source_identity)
        observed = c.server_time()
        measured = time.monotonic()
        seen_now = lambda: observed + int(max(0.,time.monotonic()-measured)*1000)
        if type(observed) is not int or observed <= 0:
            raise ValueError('交易所时间无效。')
        if s.get('settings') is None or observed-self.last_spec_check >= DAY:
            info = c.instruments(); spec = info['spec']; validate_spec(spec,self.cfg)
            tier_max = info['first_tier_max_notional_usd']
            if not math.isfinite(tier_max) or tier_max < self.cfg.capital:
                raise ValueError('虚拟本金超出已核实的首档保证金范围。')
            start = s.get('start', ((observed+MINUTE-1)//MINUTE)*MINUTE)
            s.initialize(self.settings,self.source_identity,spec,info['raw'],start)
            with s.db:s.put('first_tier_max_notional_usd',tier_max)
            self.last_spec_check = observed
        start,cursor = s.get('start'),s.get('cursor')
        frontier = observed//MINUTE*MINUTE - rt['settlement_delay_minutes']*MINUTE
        if observed-cursor > rt['max_catchup_days']*DAY:
            raise ValueError('停机超过允许补算天数。请保留旧账本，用新数据目录开始测试。')
        schedule = c.funding_schedule()
        s.record_schedules([schedule['funding_time'],schedule['next_funding_time']],seen_now(),start)
        # Fetch a bounded block. Restart catches up in consecutive blocks and
        # explicitly labels old executions rather than presenting live fills.
        end = min(frontier,cursor+6*HOUR)
        raw = c.funding_history(cursor-rt['warmup_days']*DAY, observed)
        events = normalize_funding(raw)
        validate_funding(events,cursor,max(cursor+MINUTE,end),self.cfg)
        with s.db:
            s.ingest('funding', [(t,e) for t,e in events.items()],seen_now())
        # A just-arriving historical settlement must never be silently applied
        # after that minute's state was already published.
        known_events = s.get('processed_funding', {})
        for ts,e in events.items():
            if start <= ts < cursor and str(ts) not in known_events:
                raise ValueError('发现已推进时段的迟到资金费，停止推进以免漏账；请导出检查。')
        if end <= cursor:
            s.health({'state':'WAITING','message':'等待已确认行情/结算等待期','observed':seen_now()})
            return 0
        for due in s.due_schedules(cursor,end):
            if not any(due//MINUTE == e['actual_ts']//MINUTE for e in events.values()):
                raise ValueError('预告资金费结算时间已到，已结算费率尚未完整返回；暂停推进。')
        hour_start = cursor//HOUR*HOUR
        spot = c.candles('BTC-USDT','1m',cursor,end)
        swap = c.candles('BTC-USD-SWAP','1m',cursor,end)
        marks = c.candles('BTC-USD-SWAP','1m',hour_start,end,mark=True)
        a = checked_grid(spot,cursor,end,'现货')
        b = checked_grid(swap,cursor,end,'永续')
        m = checked_grid(marks,hour_start,end,'标记价格')
        with s.db:
            s.ingest('spot',[(r[0],r) for r in spot],seen_now())
            s.ingest('swap',[(r[0],r) for r in swap],seen_now())
            s.ingest('mark',[(r[0],r) for r in marks],seen_now())
        engine = PaperEngine(self.cfg,s.get('spec'),start,state=s.get('checkpoint'))
        tier_max=s.get('first_tier_max_notional_usd')
        funding = {t:e['rate'] for t,e in events.items()}
        hourly = None
        for ts in range(hour_start,end,MINUTE):
            row = m[ts]
            if ts%HOUR == 0:
                hourly = list(row)
            else:
                hourly[2] = max(hourly[2],row[2]); hourly[3] = min(hourly[3],row[3]); hourly[4] = row[4]
            if ts >= cursor:
                result=engine.step(a[ts],b[ts],tuple(hourly),funding,settlement_mark=row[1])
                position=result.get('open_position')
                if position and position['notional_usd'] > tier_max:
                    raise ValueError('模拟仓位超出已核实保证金档位，停止推进。')
        observed=seen_now()
        classification = 'recovered' if observed-cursor > (rt['settlement_delay_minutes']+3)*MINUTE else 'observed'
        # "observed" means timely collection of completed candles, not an
        # exchange fill or executable order-book quote at that historical open.
        health = {'state':'OK' if end==frontier else 'CATCHING_UP',
                  'message':'公开行情模拟；成交按已确认K线假设计算', 'observed':observed,
                  'last_success':observed, 'model_end':end,'processing_lag_seconds':(observed-end)/1000,
                  'last_batch_classification':classification}
        # Bound this auxiliary set; engine and immutable inputs preserve the
        # full ledger. Publish it atomically with events, checkpoint and cursor.
        s.commit_batch(engine,observed,cursor,end,classification,health,
                       {str(t):True for t in events if start<=t<end})
        return (end-cursor)//MINUTE


def read_status(store):
    owned=not store.db.in_transaction
    if owned:store.db.execute('BEGIN')
    try:
        return {'mode':'public-paper','strategy':'F3','start':store.get('start'),
                'cursor':store.get('cursor'),'health':store.get('health',{}),
                'account':store.get('account'), 'statistics':store.statistics()}
    finally:
        if owned:store.db.rollback()


def print_status(value):
    h=value['health']; a=value.get('account') or {}; summary=a.get('summary',{})
    observed=h.get('observed')
    stale = observed is not None and int(time.time()*1000)-observed > 180000
    print('F3 公开行情模拟账户（无真实资金、无真实订单）')
    print('运行状态：',h.get('state','未初始化'), '；超过3分钟未更新' if stale else '')
    print('说明：',h.get('message',''))
    if value.get('start'): print('开始时间：',iso(value['start']))
    if value.get('cursor'): print('行情推进至：',iso(value['cursor']))
    if observed: print('最近检查：',iso(observed))
    if summary:
        print('初始虚拟余额：%.4f U；当前估算净值：%.6f U；收益：%.4f%%；最大回撤：%.4f%%' %
              (summary['capital'],summary['end_equity'],summary['return_pct'],summary['max_drawdown_pct']))
        print('持仓：', 'BTC现货 + BTC-USD永续空头' if a.get('open_position') else '空仓')
        print('已完成整组交易：',summary.get('trades',0),'；账户风控停止：',summary.get('account_halted',False))
        p=a.get('open_position') or {}
        receipt=summary.get('funding_usdt_on_receipt',0.)+p.get('funding_usdt_received',0.)
        print('累计资金费（到账时折U，未扣全部成本）：%.8f U'%receipt)
    print('分钟数据分类：',value['statistics']['minutes_by_classification'])
    print('observed=及时采集的K线模拟；recovered=延迟补算，均不是交易所成交。')


def configure_log(directory):
    logger=logging.getLogger('f3'); logger.setLevel(logging.INFO); logger.handlers.clear()
    fmt=logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    logdir=Path(directory);logdir.mkdir(parents=True,exist_ok=True)
    for handler in [logging.StreamHandler(),RotatingFileHandler(logdir/'run.log',maxBytes=2_000_000,backupCount=4,encoding='utf-8')]:
        handler.setFormatter(fmt);logger.addHandler(handler)
    return logger


def safe_error(error):
    # Never log proxy URLs, environment variables or exceptions carrying them.
    if isinstance(error,(ValueError,RuntimeError)) and '://' not in str(error):
        return str(error)[:350]
    return type(error).__name__+'：公开数据请求失败，请检查网络、代理或欧易接口。'


def export_snapshot(store, output):
    output=Path(output).expanduser().resolve()
    if output.exists():raise ValueError('导出文件已存在，请更换文件名。')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='f3-export-') as directory:
        folder=Path(directory)
        store.backup(folder/'paper.sqlite3')
        snapshot=Store(folder,readonly=True)
        try:
            files={'status.json':read_status(snapshot),'settings.json':snapshot.get('settings'),
                   'source-identity.json':snapshot.get('identity')}
            for name,value in files.items():(folder/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        finally:snapshot.close()
        with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(folder.iterdir()):archive.write(path,path.name)
    print('导出完成：',output)


def main(argv=None):
    if sys.version_info < (3,10): raise SystemExit('需要 Python 3.10 或更高版本。')
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('check','run','status','export'):
        p=sub.add_parser(name)
        p.add_argument('--config',type=Path,default=ROOT/'config-F3-20u.json')
        p.add_argument('--data-dir',type=Path,default=ROOT/'var/20u')
        if name=='run':p.add_argument('--once',action='store_true')
        if name=='export':p.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.command in ('status','export'):
        if not (args.data_dir/'paper.sqlite3').exists():raise ValueError('尚无账本，请先运行 run --once。')
        store=Store(args.data_dir,readonly=True)
        try:
            if args.command=='status':print_status(read_status(store))
            else:export_snapshot(store,args.output)
        finally:store.close()
        return 0
    settings,cfg=load_settings(args.config); client=OKXClient(base_url=settings['runtime']['base_url'],timeout=15)
    if args.command=='check':
        now=client.server_time(); info=client.instruments();validate_spec(info['spec'],cfg)
        if cfg.capital > info['first_tier_max_notional_usd']:
            raise ValueError('虚拟本金超出已核实的首档保证金范围。')
        raw=client.funding_history(now-settings['runtime']['warmup_days']*DAY,now)
        validate_funding(normalize_funding(raw),now,now,cfg)
        schedule=client.funding_schedule()
        end=now//MINUTE*MINUTE-3*MINUTE
        for inst,mark in [('BTC-USDT',False),('BTC-USD-SWAP',False),('BTC-USD-SWAP',True)]:
            checked_grid(client.candles(inst,'1m',end-2*MINUTE,end,mark=mark),end-2*MINUTE,end,inst)
        print('检查通过：交易所时间、两腿行情、标记价格、已结算资金费、交易规格均可读取。')
        print('北京时间：',iso(now),'；虚拟本金：',cfg.capital,'U；资金费历史条数：',len(raw))
        print('下一结算预告：',iso(schedule['funding_time']))
        print('仅检查公开数据，没有创建交易所账户或下单。')
        return 0
    with process_lock(args.data_dir):
        store=Store(args.data_dir);logger=configure_log(args.data_dir);stop=threading.Event()
        def shutdown(signum,frame):stop.set()
        for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,shutdown)
        runner=Runner(settings,cfg,store,client,identity())
        try:
            store.verify_identity(settings,runner.source_identity)
            while not stop.is_set():
                try:
                    count=runner.tick()
                    if count:logger.info('推进 %s 分钟；%s',count,store.get('health').get('last_batch_classification'))
                    if args.once:print_status(read_status(store));return 0
                    wait=1 if store.get('health',{}).get('state')=='CATCHING_UP' else settings['runtime']['poll_seconds']
                except Exception as exc:
                    message=safe_error(exc);logger.error('%s',message)
                    store.health({'state':'ERROR','message':message,'observed':int(time.time()*1000)})
                    if args.once:return 1
                    wait=settings['runtime']['poll_seconds']
                stop.wait(wait)
            logger.info('已停止；模拟仓位与待执行动作已保存在本地，重启后续接。')
        finally:store.close()
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception as exc:
        print(safe_error(exc),file=sys.stderr);sys.exit(1)
