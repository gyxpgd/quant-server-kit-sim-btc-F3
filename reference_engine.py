"""Offline inverse-contract funding carry engine; no network or order access.

Economics are inherited from the frozen funding-v25 coin-collateral ledger.
F1 adds a delayed whole-group exit when residual BTC exposure exceeds its
configured share of equity. Config.residual_exit_fraction=0 replays the legacy
baseline. Neither that threshold nor historical drawdown is a loss guarantee.

F2's opt-in neutral treasury mode converts only positive residual BTC exposure,
retaining earned coins that offset an initially over-hedged principal position.
The default legacy mode preserves the F1 calculation and event record.
"""
import math
from dataclasses import dataclass, asdict, replace
from collections import Counter
from common import MINUTE, HOUR, DAY, iso
from signals import Config as SignalConfig, FundingHistory, entry_signal, adverse_funding, fill, round_step

@dataclass(frozen=True)
class Config(SignalConfig):
    allocation_mode:str='exact'
    cash_reserve:float=.05
    max_entry_delta_fraction:float=.001
    treasury_trigger_fraction:float=.02
    treasury_mode:str='legacy'
    treasury_min_collateral_ratio:float=.75
    minimum_entry_collateral_ratio:float=.5
    margin_exit_ratio:float=.25
    loss_limit_fraction:float=.05
    halt_drawdown:float=.08
    residual_exit_fraction:float=.04

    def validate(self):
        self.validate_common()
        if not 0<self.treasury_trigger_fraction<.1 or self.treasury_min_collateral_ratio<=self.margin_exit_ratio:
            raise ValueError('Invalid treasury trigger or retained collateral ratio')
        if self.allocation_mode not in ('exact','nearest','nearest_or_exact'):
            raise ValueError('allocation_mode must be exact, nearest or nearest_or_exact')
        if self.treasury_mode not in ('legacy','neutral'):
            raise ValueError('treasury_mode must be legacy or neutral')
        if not 0<=self.cash_reserve<=.2 or not 0<self.max_entry_delta_fraction<=.15:
            raise ValueError('Invalid cash reserve or entry delta limit')
        if not self.assumed_mmr+self.swap_fee<self.margin_exit_ratio<self.minimum_entry_collateral_ratio:
            raise ValueError('Margin exit must be above maintenance and below entry collateral')
        if not 0<self.loss_limit_fraction<=self.halt_drawdown<=.10:
            raise ValueError('Invalid trade loss or account drawdown limit')
        if not 0<=self.residual_exit_fraction<=.15:
            raise ValueError('residual_exit_fraction must be in 0..0.15; zero disables this guard')
        return self

def validate_inputs(spot,swap,funding,marks,start,end,cfg,spec):
    """Reject incomplete/invalid research inputs before recording any fills.

    Hourly mark OHLC rows are indexed by their opening timestamp. Settlement
    aliases at minute boundaries may reuse that hour's OPEN. Intrahour mark
    high/low/close fields never drive earlier decisions in this engine.
    """
    if any(not isinstance(t,int) or isinstance(t,bool) or t%MINUTE for t in (start,end)) or end<=start:
        raise ValueError('start/end must be increasing integer minute boundaries')
    if not spot or not swap or len(spot)!=len(swap):
        raise ValueError('Spot and swap must contain matching nonempty minute histories')
    if not isinstance(spec,dict):
        raise ValueError('spec must be a dictionary')
    required=('spot_lot','spot_min','spot_tick','usd_step','usd_min','swap_tick','mmr')
    for name in required:
        value=spec.get(name)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):
            raise ValueError('Missing/nonfinite contract specification: '+name)
        if value<0 or (name!='mmr' and value==0):
            raise ValueError('Invalid contract specification: '+name)
    if cfg.assumed_mmr+1e-12<spec['mmr']:
        raise ValueError('Configured maintenance margin understates supplied contract specification')

    def row_valid(row,label):
        if not isinstance(row,(list,tuple)) or len(row)<5:
            raise ValueError(label+' row must contain timestamp and OHLC')
        if not isinstance(row[0],int) or isinstance(row[0],bool) or row[0]%MINUTE:
            raise ValueError(label+' timestamp must be an integer minute boundary')
        if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<=0 for x in row[1:5]):
            raise ValueError(label+' prices must be finite and positive')
        if row[2]<max(row[1],row[3],row[4]) or row[3]>min(row[1],row[2],row[4]):
            raise ValueError(label+' OHLC range is inconsistent')

    previous=None
    for a,b in zip(spot,swap):
        row_valid(a,'spot');row_valid(b,'swap')
        if a[0]!=b[0] or (previous is not None and a[0]!=previous+MINUTE):
            raise ValueError('Spot/swap timestamps must match on a continuous minute grid')
        previous=a[0]
    if spot[0][0]>start or spot[-1][0]<end-MINUTE:
        raise ValueError('Minute prices do not cover the requested simulation interval')
    if not isinstance(marks,dict):
        raise ValueError('marks must map timestamps to mark OHLC rows')
    for timestamp,row in marks.items():
        row_valid(row,'mark')
        if timestamp!=row[0]:
            raise ValueError('Mark key and row timestamp differ')
    for timestamp in range(start//HOUR*HOUR,end,HOUR):
        if timestamp not in marks:
            raise ValueError('Missing hourly mark row: '+str(timestamp))
    history=FundingHistory(funding)
    for timestamp in funding:
        if start<=timestamp<end and timestamp not in marks:
            raise ValueError('Missing funding settlement mark: '+str(timestamp))
    # Allow one minute of exchange timestamp rounding. Actual rate schedules
    # may be 1h, 4h or 8h; a longer gap cannot be silently treated as zero carry.
    maximum_gap=8*HOUR+MINUTE
    preceding=[t for t in history.times if t<=start]
    relevant=[t for t in history.times if start<t<end]
    if not preceding or start-preceding[-1]>maximum_gap:
        raise ValueError('Funding history does not cover the simulation start')
    coverage=[preceding[-1]]+relevant
    if any(b-a>maximum_gap for a,b in zip(coverage,coverage[1:])) or end-coverage[-1]>maximum_gap:
        raise ValueError('Missing/stale funding history inside the simulation interval')

def short_pnl(notional,entry,exit):return notional*(1/exit-1/entry)

def collateral_ratio(coins,notional,entry,mark):
    return (coins+short_pnl(notional,entry,mark))*mark/notional

def neutral_treasury_amount(coins,notional,entry,mark,unconverted,cfg,spec):
    """A sale may remove excess BTC, never the coins needed by the hedge.

    With an inverse short, the signed BTC exposure is coins - notional/entry.
    Funding already offsets any initially negative exposure before it becomes
    eligible for conversion. Retained margin and earned-funding limits apply
    independently; the spot amount is always rounded DOWN to an exchange lot.
    """
    collateral=coins+short_pnl(notional,entry,mark)
    available=max(0.,collateral-cfg.treasury_min_collateral_ratio*notional/mark)
    safe_limit=max(0.,coins-notional/entry)
    amount=round_step(max(0.,min(unconverted,available,safe_limit)),spec['spot_lot'])
    return amount,available,safe_limit

def size(cash,sraw,praw,cfg,spec):
    sp=fill(sraw,True,cfg,spec['spot_tick']);pp=fill(praw,False,cfg,spec['swap_tick'])
    budget=cash*(1-cfg.cash_reserve)
    def purchase(gross):
        fee=round_step(gross*cfg.spot_fee,spec['spot_lot'],up=True)
        return gross-fee,fee
    if cfg.allocation_mode=='exact':
        n=round_step(budget/sp*(1-cfg.spot_fee)*pp/(1+cfg.swap_fee),spec['usd_step'])
        while n>=spec['usd_min']-1e-12:
            need=n/pp*(1+cfg.swap_fee)
            gross=round_step(need/(1-cfg.spot_fee),spec['spot_lot'],up=True)
            net,fee=purchase(gross)
            # Rounded base-coin fee can require one extra spot lot.
            if net<need-1e-15:gross=round_step(gross+spec['spot_lot'],spec['spot_lot']);net,fee=purchase(gross)
            if gross*sp<=budget+1e-10:break
            n=round_step(n-spec['usd_step'],spec['usd_step'])
    else:
        gross=round_step(budget/sp,spec['spot_lot']);net,fee=purchase(gross)
        raw=net*pp/(1+cfg.swap_fee)
        n=round_step(raw+spec['usd_step']/2,spec['usd_step'])
    if n<spec['usd_min']-1e-12:return None,'minimum_lot'
    coins=net-n/pp*cfg.swap_fee
    delta=(coins-n/pp)*sp
    if gross<spec['spot_min'] or gross*sp>budget+1e-10:return None,'spot_budget'
    if abs(delta)>cash*cfg.max_entry_delta_fraction+1e-10:
        if cfg.allocation_mode=='nearest_or_exact':
            # Profit/loss can move equity away from the inverse contract's USD
            # step. Preserve the entry exposure bound by buying fewer coins;
            # leave unused cash idle rather than forcing a larger hedge.
            plan,reason=size(cash,sraw,praw,replace(cfg,allocation_mode='exact'),spec)
            if plan is not None:plan['allocation_method']='exact_fallback'
            return plan,reason
        return None,'entry_delta_limit'
    if coins*pp/n<cfg.minimum_entry_collateral_ratio:return None,'entry_margin'
    plan=dict(notional_usd=n,gross_coins=gross,net_coins=net,spot_entry=sp,spot_cost=gross*sp,
         spot_entry_fee_coins=fee,estimated_swap_entry=pp,estimated_delta_usdt=delta)
    if cfg.allocation_mode=='nearest_or_exact':plan['allocation_method']='nearest'
    return plan,None

def simulate(spot,swap,funding,marks,start,end,cfg,spec,force_close=True):
    """Simulate supplied public data without any live orders.

    ``force_close=True`` preserves the old research sample-end liquidation.
    ``False`` returns the open coin ledger and delayed instructions, allowing
    causal prefix checks without inventing a final fill at the cutoff.
    """
    cfg.validate()
    if not isinstance(force_close,bool):raise ValueError('force_close must be boolean')
    validate_inputs(spot,swap,funding,marks,start,end,cfg,spec)
    hist=FundingHistory(funding);cash=cfg.capital;p=None;pending=None;next_entry=start
    trades=[];signals=[];curve=[];rejections=Counter();peak=cash;maxdd=0.;halt=False
    flags=[];minimum=None;max_delta=0.;last=None;naked=0

    def nav(sp,pp):
        if p is None:return cash
        value=p['coins']
        if p['swap_open']:
            cover=fill(pp,True,cfg,spec['swap_tick'])
            value+=short_pnl(p['notional_usd'],p['swap_entry'],cover)-p['notional_usd']/cover*cfg.swap_fee
        if value>=0:return cash+value*fill(sp,False,cfg,spec['spot_tick'])*(1-cfg.spot_fee)
        return cash+value*fill(sp,True,cfg,spec['spot_tick'])*(1+cfg.spot_fee)

    def request(reason,known):
        if p.get('close_due') is None:p.update(close_due=known+cfg.execution_latency_minutes*MINUTE,exit_known_at=known,close_reason=reason)

    def cover(raw,t):
        price=fill(raw,True,cfg,spec['swap_tick']);n=p['notional_usd']
        pnl=short_pnl(n,p['swap_entry'],price);fee=n/price*cfg.swap_fee
        p['coins']+=pnl-fee
        p.update(swap_open=False,swap_exit=price,swap_exited_at=t,swap_pnl_coins=pnl,swap_exit_fee_coins=fee)

    def sell(raw,t,reason):
        nonlocal cash,p,next_entry
        price=fill(raw,False,cfg,spec['spot_tick'])
        if p['coins']<0:raise ValueError('Negative coin balance: execution model invalid')
        sold=round_step(p['coins'],spec['spot_lot']);dust=p['coins']-sold
        if sold<spec['spot_min']:raise ValueError('Cannot execute whole-group exit below the spot minimum')
        fee=sold*price*cfg.spot_fee;cash+=sold*price-fee
        p.update(spot_exit=price,spot_exited_at=t,exited_at=t,reason=reason,spot_exit_fee_usdt=fee,
            sold_coins=sold,dust_coins_written_off=dust,end_equity=cash,net=cash-p['capital_at_entry'])
        # Exact USDT ledger identity. Crypto funding/fees are converted at the
        # actual final spot conversion; receipt-time value is reported separately.
        funding_end=p['funding_coins']*price+sum(e['coins']*(e['price']-price) for e in p['treasury_sales'])
        entry_fee_end=p.get('swap_entry_fee_coins',0.)*price
        exit_fee_end=p.get('swap_exit_fee_coins',0.)*price
        price_pnl=p['gross_coins']*(price-p['spot_entry'])+p.get('swap_pnl_coins',0.)*price
        fees=p['spot_entry_fee_coins']*price+entry_fee_end+exit_fee_end+fee+sum(e['fee_usdt'] for e in p['treasury_sales'])
        dust_cost=dust*price
        if abs(p['net']-(price_pnl+funding_end-fees-dust_cost))>=1e-8:
            raise ValueError('Coin/USDT exit ledger identity failed')
        p.update(funding_usdt_on_receipt=sum(e['usdt_at_receipt'] for e in p['funding_events']),
            funding_usdt_at_exit=funding_end,funding_fx_pnl=funding_end-sum(e['usdt_at_receipt'] for e in p['funding_events']),
            price_pnl=price_pnl,fees_usdt_at_exit=fees,dust_cost=dust_cost)
        if 'swap_entry' in p:
            # Residual principal BTC exposure; basis is everything left of the
            # two principal legs after valuing this explicit spot-price move.
            residual_coins=p['gross_coins']-p['notional_usd']/p['swap_entry']
            residual_pnl=residual_coins*(price-p['spot_entry'])
            p.update(principal_residual_price_pnl=residual_pnl,basis_pnl=price_pnl-residual_pnl)
        trades.append(p);p=None;next_entry=t+cfg.cooldown_hours*HOUR

    for s,b in zip(spot,swap):
        t=s[0]
        if t<start:continue
        if t>=end:break
        last=s,b;known=t+MINUTE
        if p and p['swap_open'] and p['swap_entered_at']<t and t in funding:
            rate=funding[t];eligible=rate<0 or p.get('close_due',end+1)>t
            coins=p['notional_usd']/marks[t][1]*rate if eligible else 0.
            p['coins']+=coins;p['funding_coins']+=coins
            p['funding_events'].append(dict(ts=t,rate=rate,mark_open=marks[t][1],amount_coins=coins,
                spot_open=s[1],usdt_at_receipt=coins*s[1],eligible=eligible))
        if p and p.get('close_due')==t:
            if p['swap_open']:cover(b[1],t)
            p['sell_due']=t+cfg.second_leg_delay_minutes*MINUTE
        if p and p.get('sell_due')==t:sell(s[1],t,p['close_reason'])
        if p and p.get('treasury_due')==t:
            requested_at=p.pop('treasury_known_at');p.pop('treasury_due')
            if p['swap_open'] and p.get('close_due') is None:
                mark=marks[t][1] if t%HOUR==0 else b[1]
                collateral=p['coins']+short_pnl(p['notional_usd'],p['swap_entry'],mark)
                unconverted=p['funding_coins']-sum(e['coins'] for e in p['treasury_sales'])
                available=max(0.,collateral-cfg.treasury_min_collateral_ratio*p['notional_usd']/mark)
                amount=round_step(max(0.,min(unconverted,available)),spec['spot_lot'])
                before_nav=nav(s[1],b[1]);delta=(p['coins']-p['notional_usd']/p['swap_entry'])*s[1]
                risk_reducing=True
                if cfg.treasury_mode=='neutral':
                    amount,available,safe_limit=neutral_treasury_amount(p['coins'],p['notional_usd'],
                        p['swap_entry'],mark,unconverted,cfg,spec)
                    delta_before_coins=p['coins']-p['notional_usd']/p['swap_entry']
                    delta_after_coins=(p['coins']-amount)-p['notional_usd']/p['swap_entry']
                    # Allow only floating-point subtraction dust, not one full
                    # lot of a newly created short exposure.
                    tolerance=max(1e-15,spec['spot_lot']*1e-8)
                    risk_reducing=(delta_before_coins>0 and delta_after_coins>=-tolerance
                                   and abs(delta_after_coins)<abs(delta_before_coins))
                if amount>=spec['spot_min'] and delta>cfg.treasury_trigger_fraction*before_nav and risk_reducing:
                    price=fill(s[1],False,cfg,spec['spot_tick']);fee=amount*price*cfg.spot_fee
                    previous_cash=cash;previous_coins=p['coins'];p['coins']-=amount;cash+=amount*price-fee
                    p['treasury_sales'].append(dict(known_at=requested_at,executed_at=t,coins=amount,price=price,fee_usdt=fee,
                        cash_before=previous_cash,cash_after=cash,coins_before=previous_coins,coins_after=p['coins'],
                        proxy_mark=mark,collateral_ratio_after=(collateral-amount)*mark/p['notional_usd'],
                        unconverted_before=unconverted,equity_before=before_nav,equity_after=nav(s[1],b[1])))
                    if cfg.treasury_mode=='neutral':
                        p['treasury_sales'][-1].update(treasury_mode='neutral',
                            delta_before_coins=delta_before_coins,delta_after_coins=delta_after_coins,
                            delta_before_usdt=delta_before_coins*s[1],delta_after_usdt=delta_after_coins*s[1],
                            neutral_sell_limit_coins=safe_limit,available_collateral_coins=available)
        if pending and pending['due']==t:
            if p is not None:raise ValueError('Cannot enter while another position is open')
            plan,reason=size(cash,s[1],b[1],cfg,spec)
            if reason:rejections[reason]+=1
            elif not halt and t>=next_entry:
                p=dict(plan,capital_at_entry=cash,entered_at=t,entry_known_at=pending['known_at'],entry_frame=pending['frame'],
                    coins=plan['net_coins'],swap_open=False,hedge_due=t+cfg.second_leg_delay_minutes*MINUTE,
                    funding_coins=0.,funding_events=[],bad_days=0,treasury_sales=[])
                cash-=p['spot_cost']
            pending=None
        if p and not p['swap_open'] and p.get('hedge_due')==t and p.get('sell_due') is None:
            price=fill(b[1],False,cfg,spec['swap_tick']);n=p['notional_usd'];fee=n/price*cfg.swap_fee
            delta=(p['coins']-fee-n/price)*s[1]
            ratio=(p['coins']-fee)*price/n
            if abs(delta)>p['capital_at_entry']*cfg.max_entry_delta_fraction+1e-10 or ratio<cfg.minimum_entry_collateral_ratio:
                sell(s[1],t,'entry_hedge_rejected')
            else:
                p['coins']-=fee;p.update(swap_open=True,swap_entry=price,swap_entered_at=t,swap_entry_fee_coins=fee,
                    initial_collateral_coins=p['coins'],initial_delta_usdt=delta,initial_collateral_ratio=ratio)
        equity=nav(s[4],b[4]);peak=max(peak,equity);dd=(peak-equity)/peak;maxdd=max(maxdd,dd)
        if p:
            if not p['swap_open']:naked+=1
            if p['swap_open']:
                mark=marks[t//HOUR*HOUR][4] if known%HOUR==0 else b[4]
                ratio=collateral_ratio(p['coins'],p['notional_usd'],p['swap_entry'],mark)
                minimum=ratio if minimum is None else min(minimum,ratio)
                delta=(p['coins']-p['notional_usd']/p['swap_entry'])*s[4]
                max_delta=max(max_delta,abs(delta)/max(equity,1e-12))
                if p.get('close_due') is None and p.get('treasury_due') is None and delta>cfg.treasury_trigger_fraction*equity:
                    unconverted=p['funding_coins']-sum(e['coins'] for e in p['treasury_sales'])
                    schedulable=round_step(max(0.,unconverted),spec['spot_lot'])
                    if cfg.treasury_mode=='neutral':
                        schedulable,_,_=neutral_treasury_amount(p['coins'],p['notional_usd'],
                            p['swap_entry'],mark,unconverted,cfg,spec)
                    if schedulable>=spec['spot_min']:
                        p.update(treasury_known_at=known,treasury_due=known+cfg.execution_latency_minutes*MINUTE)
                if ratio<=cfg.assumed_mmr+cfg.swap_fee:
                    flags.append(dict(ts=known,ratio=ratio));request('potential_liquidation',known)
                elif ratio<=cfg.margin_exit_ratio:request('margin_guard',known)
                # This uses only the just-completed minute and balances AFTER
                # any treasury conversion executed at that minute's open. A
                # pending conversion cannot substitute for a completed sale.
                fraction=abs(delta)/max(equity,1e-12)
                if (cfg.residual_exit_fraction and fraction>cfg.residual_exit_fraction
                        and p.get('close_due') is None):
                    p['residual_guard_frame']=dict(known_at=known,signed_delta_usdt=delta,
                                                  equity=equity,fraction=fraction)
                    request('residual_exposure_guard',known)
            if equity-p['capital_at_entry']<=-cfg.loss_limit_fraction*p['capital_at_entry']:request('combined_loss_guard',known)
        if dd>=cfg.halt_drawdown and not halt:
            halt=True
            if p:request('account_halt',known)
        if (known+8*HOUR)%DAY==cfg.decision_hour_bjt*HOUR:
            if p and p['swap_open'] and p.get('close_due') is None and known-p['entered_at']>=cfg.min_hold_hours*HOUR:
                frame=hist.stats(known,cfg.exit_lookback_days,cfg.funding_available_lag_minutes)
                if frame:
                    p['bad_days']=p['bad_days']+1 if adverse_funding(frame,cfg) else 0
                    if p['bad_days']>=cfg.exit_bad_days:p['exit_funding_frame']=frame;request('funding_deterioration',known)
            if p is None and pending is None and not halt and known>=next_entry:
                allowed,frame=entry_signal(hist,known,cfg)
                signals.append(dict(known_at=known,allowed=allowed,frame=frame))
                if allowed:pending=dict(known_at=known,due=known+cfg.execution_latency_minutes*MINUTE,frame=frame)
        if known%HOUR==0:curve.append((known,equity))
    if force_close and p and last:
        s,b=last
        if p['swap_open']:cover(b[4],end-1)
        sell(s[4],end-1,'data_end')
    final_equity=nav(last[0][4],last[1][4]) if p and last else cash
    maxdd=max(maxdd,(peak-final_equity)/peak);curve.append((end,final_equity))
    if p is None and abs(sum(t['net'] for t in trades)-(cash-cfg.capital))>=1e-8:
        raise ValueError('Closed-trade/cash ledger identity failed')
    if p is not None:
        expected_cash=cfg.capital+sum(t['net'] for t in trades)-p['spot_cost']
        expected_cash+=sum(e['coins']*e['price']-e['fee_usdt'] for e in p['treasury_sales'])
        if abs(cash-expected_cash)>=1e-8:
            raise ValueError('Open-position/cash ledger identity failed')
    summary=dict(capital=cfg.capital,start_bjt=iso(start),end_bjt_exclusive=iso(end),days=(end-start)/DAY,
        end_equity=final_equity,net=final_equity-cfg.capital,return_pct=100*(final_equity/cfg.capital-1),max_drawdown_pct=100*maxdd,
        trades=len(trades),account_halted=halt,minimum_collateral_ratio=minimum,potential_liquidation_flags=len(flags),
        max_residual_delta_pct=100*max_delta,naked_minutes=naked,rejections=dict(rejections),
        exit_reasons=dict(Counter(t['reason'] for t in trades)),
        treasury_sales=sum(len(t['treasury_sales']) for t in trades),
        treasury_converted_usdt=sum(e['coins']*e['price']-e['fee_usdt'] for t in trades for e in t['treasury_sales']))
    for key in ['funding_usdt_on_receipt','funding_usdt_at_exit','funding_fx_pnl','price_pnl','fees_usdt_at_exit','dust_cost',
                'principal_residual_price_pnl','basis_pnl']:summary[key]=sum(t.get(key,0.) for t in trades)
    return dict(config=asdict(cfg),summary=summary,trades=trades,signals=signals,hourly_equity=curve,liquidation_flags=flags,
                open_position=p,cash=cash,pending=pending,next_entry=next_entry,force_close=force_close)
