from dataclasses import dataclass,asdict
import time

@dataclass
class Candle:
 ts:int; open:float; high:float; low:float; close:float; volume:float=0.0
 def as_dict(self): return asdict(self)

class CandleBuilder:
 def __init__(self,timeframe_seconds,max_candles=250):
  self.timeframe=timeframe_seconds; self.max_candles=max_candles; self.candles=[]

 def load_candles(self,items):
  loaded=[]
  for item in items or []:
   if not isinstance(item,dict): continue
   try:
    ts=float(item.get("timestamp",item.get("time",item.get("ts",0))))
    if ts>10_000_000_000: ts/=1000
    o=float(item["open"]); h=float(item["high"]); l=float(item["low"]); c=float(item["close"])
   except (KeyError,TypeError,ValueError):
    continue
   if ts<=0 or min(o,h,l,c)<=0: continue
   loaded.append(Candle(int((ts//self.timeframe)*self.timeframe),o,h,l,c,float(item.get("volume",0) or 0)))
  if loaded:
   merged={x.ts:x for x in self.candles}
   merged.update({x.ts:x for x in loaded})
   self.candles=[merged[k] for k in sorted(merged)][-self.max_candles:]
  return len(loaded)

 def update(self,price,ts=None,volume=0.0):
  ts=time.time() if ts is None else ts; bucket=int(ts//self.timeframe)*self.timeframe
  if not self.candles or self.candles[-1].ts!=bucket:
   self.candles.append(Candle(bucket,price,price,price,price,volume)); self.candles=self.candles[-self.max_candles:]
  else:
   c=self.candles[-1]; c.high=max(c.high,price); c.low=min(c.low,price); c.close=price; c.volume+=volume
  return self.candles[-1]

 def snapshot(self): return [c.as_dict() for c in self.candles]
