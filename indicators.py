import numpy as np
import pandas as pd
from spike_detector import detect_spike

def ema(s,n): return s.ewm(span=n,adjust=False).mean()

def wma(s,n=9):
 weights=np.arange(1,n+1,dtype=float)
 return s.rolling(n).apply(lambda x:float(np.dot(x,weights)/weights.sum()),raw=True)

def demarker(df,n=9):
 high=df.high.astype(float); low=df.low.astype(float)
 demax=(high-high.shift(1)).where(high>high.shift(1),0.0)
 demin=(low.shift(1)-low).where(low<low.shift(1),0.0)
 a=demax.rolling(n).mean(); b=demin.rolling(n).mean()
 return a/(a+b).replace(0,np.nan)

def rsi(s,n=14):
 d=s.diff(); up=d.clip(lower=0); dn=-d.clip(upper=0)
 rs=up.ewm(alpha=1/n,adjust=False).mean()/dn.ewm(alpha=1/n,adjust=False).mean().replace(0,np.nan)
 return 100-100/(1+rs)

def atr(df,n=14):
 pc=df.close.shift()
 tr=pd.concat([df.high-df.low,(df.high-pc).abs(),(df.low-pc).abs()],axis=1).max(axis=1)
 return tr.ewm(alpha=1/n,adjust=False).mean()

def cci(df,n=14):
 tp=(df.high+df.low+df.close)/3
 ma=tp.rolling(n).mean()
 md=tp.rolling(n).apply(lambda x:np.mean(np.abs(x-x.mean())),raw=True)
 return (tp-ma)/(0.015*md.replace(0,np.nan))

def macd(s):
 m=ema(s,12)-ema(s,26); sig=ema(m,9); return m,sig,m-sig

def osma(s,fast=10,slow=20,signal=24):
 # OSMA = oscillator (fast EMA - slow EMA) minus its signal EMA.
 osc=ema(s,fast)-ema(s,slow)
 sig=ema(osc,signal)
 return osc,sig,osc-sig

def ichimoku(df,tenkan=9,kijun=26,senkou=52):
 high=df.high.astype(float); low=df.low.astype(float)
 tenkan_s=(high.rolling(tenkan).max()+low.rolling(tenkan).min())/2
 kijun_s=(high.rolling(kijun).max()+low.rolling(kijun).min())/2
 span_a=(tenkan_s+kijun_s)/2
 span_b=(high.rolling(senkou).max()+low.rolling(senkou).min())/2
 return tenkan_s,kijun_s,span_a,span_b

def psar(df,step=.02,max_af=.2):
 h=df.high.to_numpy(); l=df.low.to_numpy(); out=np.zeros(len(df))
 if not len(df): return pd.Series(dtype=float)
 bull=True; af=step; ep=h[0]; sar=l[0]; out[0]=sar
 for i in range(1,len(df)):
  sar=sar+af*(ep-sar)
  if bull:
   sar=min(sar,l[i-1],l[i-2] if i>1 else l[i-1])
   if l[i]<sar: bull=False; sar=ep; ep=l[i]; af=step
   elif h[i]>ep: ep=h[i]; af=min(max_af,af+step)
  else:
   sar=max(sar,h[i-1],h[i-2] if i>1 else h[i-1])
   if h[i]>sar: bull=True; sar=ep; ep=h[i]; af=step
   elif l[i]<ep: ep=l[i]; af=min(max_af,af+step)
  out[i]=sar
 return pd.Series(out,index=df.index)

def smma(s,n):
 # Wilder/SMMA smoothing used by the classic Williams Alligator.
 # Seed with the first n-period SMA, then recursively smooth.
 s=s.astype(float)
 out=pd.Series(np.nan,index=s.index,dtype=float)
 if len(s)<n: return out
 out.iloc[n-1]=s.iloc[:n].mean()
 alpha=1.0/n
 for i in range(n,len(s)):
  out.iloc[i]=(out.iloc[i-1]*(n-1)+s.iloc[i])*alpha
 return out

def alligator(df):
 # Classic Williams Alligator: SMMA of median price (HL/2).
 # Raw values are used for live logic; the traditional 8/5/3 forward
 # offsets are display offsets, not future-looking inputs.
 median=(df.high.astype(float)+df.low.astype(float))/2.0
 jaw=smma(median,13)
 teeth=smma(median,8)
 lips=smma(median,5)
 return jaw,teeth,lips

def fractal(df,span=2):
 return df.high.rolling(2*span+1,center=True).max().eq(df.high),df.low.rolling(2*span+1,center=True).min().eq(df.low)



def fractal_chaos_bands(df, span=2):
 # Fractal Chaos Bands use the latest confirmed fractal high/low as
 # adaptive upper/lower structure. No future candle is used in live logic.
 highs, lows = fractal(df, span)
 upper=pd.Series(np.nan,index=df.index,dtype=float)
 lower=pd.Series(np.nan,index=df.index,dtype=float)
 last_high=np.nan; last_low=np.nan
 for i in range(len(df)):
  confirmed_i=i-span
  if confirmed_i>=0:
   if bool(highs.iloc[confirmed_i]): last_high=float(df.high.iloc[confirmed_i])
   if bool(lows.iloc[confirmed_i]): last_low=float(df.low.iloc[confirmed_i])
  upper.iloc[i]=last_high; lower.iloc[i]=last_low
 mid=(upper+lower)/2.0
 return upper,lower,mid

def bollinger(s,n=20,stds=2.0):
 mid=s.rolling(n).mean()
 dev=s.rolling(n).std(ddof=0)
 upper=mid+stds*dev; lower=mid-stds*dev
 width=(upper-lower)/mid.replace(0,np.nan)
 pct=(s-lower)/(upper-lower).replace(0,np.nan)
 return mid,upper,lower,width,pct

def stochastic(df,k_period=14,d_period=3,smooth=3):
 close=df.close.astype(float)
 low=df.low.rolling(k_period).min()
 high=df.high.rolling(k_period).max()
 k=100*(close-low)/(high-low).replace(0,np.nan)
 k=k.rolling(smooth).mean()
 d=k.rolling(d_period).mean()
 return k,d

def adx_dmi(df,n=14):
 high=df.high.astype(float); low=df.low.astype(float); close=df.close.astype(float)
 up=high.diff()
 down=-low.diff()
 plus_dm=up.where((up>down)&(up>0),0.0)
 minus_dm=down.where((down>up)&(down>0),0.0)
 tr=pd.concat([(high-low),(high-close.shift()).abs(),(low-close.shift()).abs()],axis=1).max(axis=1)
 atr_w=tr.ewm(alpha=1/n,adjust=False).mean()
 plus_di=100*plus_dm.ewm(alpha=1/n,adjust=False).mean()/atr_w.replace(0,np.nan)
 minus_di=100*minus_dm.ewm(alpha=1/n,adjust=False).mean()/atr_w.replace(0,np.nan)
 dx=100*(plus_di-minus_di).abs()/(plus_di+minus_di).replace(0,np.nan)
 adx=dx.ewm(alpha=1/n,adjust=False).mean()
 return adx,plus_di,minus_di

def supertrend(df,period=10,multiplier=3.0):
 hl2=(df.high+df.low)/2.0
 a=atr(df,period)
 upper=hl2+multiplier*a
 lower=hl2-multiplier*a
 final_upper=upper.copy()
 final_lower=lower.copy()
 direction=pd.Series(1,index=df.index,dtype=int)
 st=pd.Series(np.nan,index=df.index,dtype=float)
 for i in range(1,len(df)):
  prev=i-1
  final_upper.iloc[i]=upper.iloc[i] if upper.iloc[i]<final_upper.iloc[prev] or df.close.iloc[prev]>final_upper.iloc[prev] else final_upper.iloc[prev]
  final_lower.iloc[i]=lower.iloc[i] if lower.iloc[i]>final_lower.iloc[prev] or df.close.iloc[prev]<final_lower.iloc[prev] else final_lower.iloc[prev]
  if pd.isna(a.iloc[i]):
   direction.iloc[i]=direction.iloc[prev]
  elif st.iloc[prev] == final_upper.iloc[prev]:
   direction.iloc[i]=1 if df.close.iloc[i]>final_upper.iloc[i] else -1
  else:
   direction.iloc[i]=-1 if df.close.iloc[i]<final_lower.iloc[i] else 1
  st.iloc[i]=final_lower.iloc[i] if direction.iloc[i]==1 else final_upper.iloc[i]
 st.iloc[0]=final_lower.iloc[0]
 return st,direction

def ut_bot_direction(df, key_value=4.0, atr_period=10):
 # UT Bot-style ATR trailing stop direction.
 close=df.close.astype(float).to_numpy()
 a=atr(df,atr_period).to_numpy()
 n=len(close)
 if n==0: return pd.Series(dtype=int,index=df.index)
 stop=np.full(n,np.nan); direction=np.zeros(n,dtype=int)
 for j in range(n):
  if not np.isfinite(a[j]):
   if j==0: stop[j]=close[j]; direction[j]=0
   else: stop[j]=stop[j-1]; direction[j]=direction[j-1]
   continue
  loss=key_value*a[j]
  if j==0:
   stop[j]=close[j]-loss; direction[j]=1; continue
  prev_stop=stop[j-1]; prev_close=close[j-1]
  if close[j]>prev_stop and prev_close>prev_stop:
   stop[j]=max(prev_stop,close[j]-loss)
  elif close[j]<prev_stop and prev_close<prev_stop:
   stop[j]=min(prev_stop,close[j]+loss)
  elif close[j]>prev_stop:
   stop[j]=close[j]-loss
  else:
   stop[j]=close[j]+loss
  direction[j]=1 if close[j]>stop[j] else -1 if close[j]<stop[j] else direction[j-1]
 return pd.Series(direction,index=df.index)

def calculate(candles):
 if len(candles)<35:return {"ready":False,"reason":"Need at least 35 candles","values":{}}
 df=pd.DataFrame(candles); close=df.close.astype(float)
 e9,e20,e50=ema(close,9),ema(close,20),ema(close,50)
 m,ms,mh=macd(close); om,oms,omh=osma(close,10,20,24); ps=psar(df); jaw,teeth,lips=alligator(df); fu,fd=fractal(df,2)
 tenkan_s,kijun_s,span_a,span_b=ichimoku(df,9,26,52)
 bbmid,bbup,bblow,bbwidth,bbpct=bollinger(close,20,2.0)
 atr_series=atr(df)
 atr_base=atr_series.rolling(50,min_periods=14).mean()
 st,st_dir=supertrend(df,10,3.0)
 fcb_upper,fcb_lower,fcb_mid=fractal_chaos_bands(df,2)
 ut_fast=ut_bot_direction(df,1.2,10)
 ut_slow=ut_bot_direction(df,1.5,20)
 stoch_k,stoch_d=stochastic(df,14,3,3)
 adx_series,plus_di,minus_di=adx_dmi(df,14)
 cci_series=cci(df,14)
 demarker_series=demarker(df,9)
 wma9=wma(close,9)
 spike=detect_spike(df,20)
 # Evelyn-style market structure from confirmed swing highs/lows.
 # This is a price-action confirmation layer, not a new weighted vote.
 swing_highs=[]; swing_lows=[]
 for i in range(2,len(df)-2):
  if bool(fu.iloc[i]): swing_highs.append((i,float(df.high.iloc[i])))
  if bool(fd.iloc[i]): swing_lows.append((i,float(df.low.iloc[i])))
 last_highs=swing_highs[-2:]; last_lows=swing_lows[-2:]
 structure_bias="WAIT"; structure_pattern="INSUFFICIENT"
 if len(last_highs)==2 and len(last_lows)==2:
  h1,h2=last_highs[0][1],last_highs[1][1]
  l1,l2=last_lows[0][1],last_lows[1][1]
  if h2>h1 and l2>l1:
   structure_bias="CALL"; structure_pattern="HH_HL"
  elif h2<h1 and l2<l1:
   structure_bias="PUT"; structure_pattern="LH_LL"
  elif h2>h1 and l2<l1:
   structure_bias="WAIT"; structure_pattern="MIXED_EXPANSION"
  elif h2<h1 and l2>l1:
   structure_bias="WAIT"; structure_pattern="MIXED_COMPRESSION"
 latest_close=float(close.iloc[-1])
 structure_break="WAIT"
 if last_highs and latest_close>last_highs[-1][1]: structure_break="CALL"
 elif last_lows and latest_close<last_lows[-1][1]: structure_break="PUT"

 # Automatic support/resistance from recent completed-candle swing range.
 sr_window=min(30,len(df)-1)
 recent=df.iloc[-(sr_window+1):-1]
 support=float(recent.low.min())
 resistance=float(recent.high.max())
 current_price=float(close.iloc[-1])
 sr_atr=float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else 0.0
 sr_buffer=max(sr_atr*0.35,current_price*0.00005)
 near_support=abs(current_price-support)<=sr_buffer
 near_resistance=abs(resistance-current_price)<=sr_buffer
 support_break=current_price<support
 resistance_break=current_price>resistance
 prev=df.iloc[-2]
 candle_range=float(prev.high-prev.low)
 candle_body=abs(float(prev.close-prev.open))
 candle_body_ratio=(candle_body/candle_range) if candle_range>0 else 0.0
 candle_direction="CALL" if prev.close>prev.open else "PUT" if prev.close<prev.open else "WAIT"
 candle_confirmed=bool(candle_direction!="WAIT" and candle_body_ratio>=0.55)
 last=lambda s:float(s.iloc[-1]) if pd.notna(s.iloc[-1]) else None
 return {"ready":True,"values":{
  "price":float(close.iloc[-1]),"ema9":last(e9),"ema20":last(e20),"ema50":last(e50),
  "macd":last(m),"macd_signal":last(ms),"macd_hist":last(mh),
  "macd_hist_prev":float(mh.iloc[-2]) if len(mh)>1 and pd.notna(mh.iloc[-2]) else None,
  "macd_slope_direction":"CALL" if len(mh)>1 and pd.notna(mh.iloc[-1]) and pd.notna(mh.iloc[-2]) and mh.iloc[-1]>mh.iloc[-2] else "PUT" if len(mh)>1 and pd.notna(mh.iloc[-1]) and pd.notna(mh.iloc[-2]) and mh.iloc[-1]<mh.iloc[-2] else "WAIT",
  "confirmed_macd_direction":"CALL" if len(mh)>2 and pd.notna(mh.iloc[-2]) and mh.iloc[-2]>0 else "PUT" if len(mh)>2 and pd.notna(mh.iloc[-2]) and mh.iloc[-2]<0 else "WAIT",
  "confirmed_cci_direction":"CALL" if len(cci_series)>2 and pd.notna(cci_series.iloc[-2]) and cci_series.iloc[-2]>0 else "PUT" if len(cci_series)>2 and pd.notna(cci_series.iloc[-2]) and cci_series.iloc[-2]<0 else "WAIT",
  "confirmed_alligator_direction":"CALL" if len(jaw)>2 and pd.notna(jaw.iloc[-2]) and pd.notna(teeth.iloc[-2]) and pd.notna(lips.iloc[-2]) and lips.iloc[-2]>teeth.iloc[-2]>jaw.iloc[-2] else "PUT" if len(jaw)>2 and pd.notna(jaw.iloc[-2]) and pd.notna(teeth.iloc[-2]) and pd.notna(lips.iloc[-2]) and lips.iloc[-2]<teeth.iloc[-2]<jaw.iloc[-2] else "WAIT",
  "osma":last(om),"osma_signal":last(oms),"osma_hist":last(omh),
  "ichimoku_tenkan":last(tenkan_s),"ichimoku_kijun":last(kijun_s),
  "ichimoku_span_a":last(span_a),"ichimoku_span_b":last(span_b),
  "rsi":last(rsi(close)),
  "cci":last(cci_series),"cci_prev":float(cci_series.iloc[-2]) if len(cci_series)>1 and pd.notna(cci_series.iloc[-2]) else None,"cci_prev2":float(cci_series.iloc[-3]) if len(cci_series)>2 and pd.notna(cci_series.iloc[-3]) else None,"atr":last(atr_series),"atr_baseline":last(atr_base),"psar":last(ps),
  "candle_direction":candle_direction,"candle_body_ratio":round(candle_body_ratio,3),"candle_confirmed":candle_confirmed,
  "support":support,"resistance":resistance,"near_support":near_support,"near_resistance":near_resistance,"support_break":support_break,"resistance_break":resistance_break,
  "alligator_jaw":last(jaw),"alligator_teeth":last(teeth),"alligator_lips":last(lips),
  "alligator_jaw_prev":float(jaw.iloc[-2]) if len(jaw)>1 and pd.notna(jaw.iloc[-2]) else None,
  "alligator_teeth_prev":float(teeth.iloc[-2]) if len(teeth)>1 and pd.notna(teeth.iloc[-2]) else None,
  "alligator_lips_prev":float(lips.iloc[-2]) if len(lips)>1 and pd.notna(lips.iloc[-2]) else None,
  "fractal_up":bool(fu.iloc[-3]),"fractal_down":bool(fd.iloc[-3]),
  "market_structure":structure_bias,"market_structure_pattern":structure_pattern,
  "market_structure_break":structure_break,
  "market_structure_highs":[x[1] for x in last_highs],"market_structure_lows":[x[1] for x in last_lows],
  "bb_mid":last(bbmid),"bb_upper":last(bbup),"bb_lower":last(bblow),
  "bb_width":last(bbwidth),"bb_pct":last(bbpct),
  "supertrend":last(st),"supertrend_direction":int(st_dir.iloc[-1]),
  "fcb_upper":last(fcb_upper),"fcb_lower":last(fcb_lower),"fcb_mid":last(fcb_mid),
  "fcb_upper_prev":float(fcb_upper.iloc[-2]) if len(fcb_upper)>1 and pd.notna(fcb_upper.iloc[-2]) else None,
  "fcb_lower_prev":float(fcb_lower.iloc[-2]) if len(fcb_lower)>1 and pd.notna(fcb_lower.iloc[-2]) else None,
  "fcb_mid_prev":float(fcb_mid.iloc[-2]) if len(fcb_mid)>1 and pd.notna(fcb_mid.iloc[-2]) else None,
  "ut_fast_direction":int(ut_fast.iloc[-1]),"ut_slow_direction":int(ut_slow.iloc[-1]),
  "ut_fast_prev":int(ut_fast.iloc[-2]) if len(ut_fast)>1 else 0,"ut_slow_prev":int(ut_slow.iloc[-2]) if len(ut_slow)>1 else 0,
  "stoch_k":last(stoch_k),"stoch_d":last(stoch_d),
  "adx":last(adx_series),"plus_di":last(plus_di),"minus_di":last(minus_di),
  "demarker":last(demarker_series),"demarker_prev":float(demarker_series.iloc[-2]) if len(demarker_series)>1 and pd.notna(demarker_series.iloc[-2]) else None,
  "wma9":last(wma9),"wma9_prev":float(wma9.iloc[-2]) if len(wma9)>1 and pd.notna(wma9.iloc[-2]) else None,
  "momentum_1":float(close.iloc[-1]-close.iloc[-2]),
  "momentum_2":float(close.iloc[-2]-close.iloc[-3]),
  "momentum_3":float(close.iloc[-3]-close.iloc[-4]),
  "spike_ready":spike.get("ready",False),"spike_detected":spike.get("spike",False),"spike_direction":spike.get("direction","WAIT"),"spike_strength":spike.get("strength",0.0),"spike_price":spike.get("price_spike",False),"spike_volume":spike.get("volume_spike",False),"spike_volume_available":spike.get("volume_available",False),"spike_range_ratio":spike.get("range_ratio",1.0),"spike_move_ratio":spike.get("move_ratio",1.0),"spike_volume_ratio":spike.get("volume_ratio",0.0),"spike_exhaustion":spike.get("exhaustion",False),"spike_prior":spike.get("prior_spike",False),"spike_acceleration_direction":spike.get("acceleration_direction","WAIT")
 }}
