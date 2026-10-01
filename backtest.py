from indicators import calculate
from engine import SignalEngine


def run_backtest(candles, min_confidence=60, max_bars=150):
    """Simple diagnostic replay on supplied candles.

    The result is directional only: CALL wins when the next candle closes
    above the signal candle close; PUT wins when it closes below. It does not
    model Pocket Option payout, latency, slippage, or execution.
    """
    data=list(candles or [])[-max_bars-36:]
    if len(data)<36:
        return {"ready":False,"reason":"Need at least 36 candles","trades":[]}
    wins=losses=ties=0
    trades=[]
    for i in range(35,len(data)-1):
        engine=SignalEngine(min_confidence)
        result=calculate(data[:i+1])
        sig=engine.evaluate(result)
        side=sig.get("signal","WAIT")
        if side not in ("CALL","PUT"):
            continue
        entry=float(data[i]["close"]); nxt=float(data[i+1]["close"])
        outcome="TIE" if nxt==entry else ("WIN" if (side=="CALL" and nxt>entry) or (side=="PUT" and nxt<entry) else "LOSS")
        if outcome=="WIN": wins+=1
        elif outcome=="LOSS": losses+=1
        else: ties+=1
        trades.append({"ts":data[i].get("ts"),"side":side,"confidence":sig.get("confidence",0),"entry":entry,"next_close":nxt,"outcome":outcome})
    decided=wins+losses
    return {"ready":True,"bars_tested":len(data),"signals":len(trades),"wins":wins,"losses":losses,"ties":ties,"win_rate":round((wins/decided*100),2) if decided else 0.0,"trades":trades[-100:]}
