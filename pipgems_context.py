import pandas as pd
import numpy as np

def _df(candles):
    df=pd.DataFrame(candles)
    if df.empty:
        return df
    for c in ("open","high","low","close"):
        df[c]=pd.to_numeric(df[c],errors="coerce")
    return df.dropna(subset=["open","high","low","close"]).reset_index(drop=True)

def _ema(s,n):
    return s.ewm(span=n,adjust=False).mean()

def _context(candles, direction):
    df=_df(candles)
    if len(df)<45 or direction not in ("CALL","PUT"):
        return {"ready":False,"confirmed":False,"score":0.0,"reason":"Need more candles for PipGems-style context"}
    close=df.close
    e20=_ema(close,20)
    e40=_ema(close,40)
    price=float(close.iloc[-1])
    prev=float(close.iloc[-2])
    slope20=float(e20.iloc[-1]-e20.iloc[-4])
    slope40=float(e40.iloc[-1]-e40.iloc[-4])
    separation=abs(float(e20.iloc[-1]-e40.iloc[-1]))/max(abs(price),1e-9)*10000

    if direction=="CALL":
        trend=(e20.iloc[-1]>e40.iloc[-1] and slope20>0 and slope40>=0)
        trend_score=1.0 if trend else (0.5 if e20.iloc[-1]>e40.iloc[-1] else 0.0)
    else:
        trend=(e20.iloc[-1]<e40.iloc[-1] and slope20<0 and slope40<=0)
        trend_score=1.0 if trend else (0.5 if e20.iloc[-1]<e40.iloc[-1] else 0.0)

    recent=df.iloc[-20:]
    support=float(recent.low.min())
    resistance=float(recent.high.max())
    span=max(resistance-support,1e-9)
    room_up=(resistance-price)/span
    room_down=(price-support)/span

    # Avoid calls directly into resistance / puts directly into support.
    room_score=room_up if direction=="CALL" else room_down
    room_ok=room_score>=0.22

    o=float(df.open.iloc[-1]); h=float(df.high.iloc[-1]); l=float(df.low.iloc[-1]); c=float(df.close.iloc[-1])
    body=abs(c-o); rng=max(h-l,1e-9)
    upper=h-max(o,c); lower=min(o,c)-l
    bullish=c>o; bearish=c<o
    prev_o=float(df.open.iloc[-2]); prev_c=float(df.close.iloc[-2])
    engulf_call=bullish and prev_c<prev_o and c>=prev_o and o<=prev_c
    engulf_put=bearish and prev_c>prev_o and c<=prev_o and o>=prev_c
    pin_call=bullish and lower>=body*1.5 and lower>=upper*1.25
    pin_put=bearish and upper>=body*1.5 and upper>=lower*1.25
    candle_score=1.0 if ((direction=="CALL" and (engulf_call or pin_call)) or (direction=="PUT" and (engulf_put or pin_put))) else (0.65 if ((direction=="CALL" and bullish) or (direction=="PUT" and bearish)) else 0.0)

    # A very tight EMA relationship is treated as a range/noise condition.
    separation_score=min(1.0,separation/3.0)
    if separation<0.35:
        separation_score=0.0

    score=100*(0.45*trend_score+0.30*max(0.0,min(1.0,room_score))+0.15*candle_score+0.10*separation_score)
    confirmed=bool(trend and room_ok and candle_score>=0.65 and separation>=0.35)

    reasons=[]
    reasons.append("EMA 20/40 trend aligned" if trend else "EMA trend not fully aligned")
    reasons.append("room before resistance" if direction=="CALL" else "room before support") if room_ok else reasons.append("price too close to key level")
    reasons.append("candle confirmation" if candle_score>=0.65 else "weak candle confirmation")
    reasons.append("EMA separation active" if separation>=0.35 else "EMAs too compressed")

    return {
        "ready":True,
        "confirmed":confirmed,
        "score":round(float(score),1),
        "ema20":float(e20.iloc[-1]),
        "ema40":float(e40.iloc[-1]),
        "ema_separation":round(float(separation),2),
        "support":support,
        "resistance":resistance,
        "room_ratio":round(float(room_score),3),
        "candle_confirmation":round(float(candle_score),2),
        "reason":"PipGems-style context: "+"; ".join(reasons)
    }

def confirm(candles, direction):
    return _context(candles,direction)
