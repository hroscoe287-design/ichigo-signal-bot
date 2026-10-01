import asyncio,logging,time
from fastapi import FastAPI,Request
from fastapi.responses import HTMLResponse
from config import APP_NAME,ASSETS,TIMEFRAMES,FOREX,settings
from candles import CandleBuilder
from indicators import calculate
from engine import SignalEngine
from pocket_feed import PocketOptionFeed
from pro_guards import apply_pro_guards
from backtest import run_backtest
from scanner_ml import scan as ml_scan
from pipgems_context import confirm as pipgems_confirm
logging.basicConfig(level=logging.INFO)
app=FastAPI(title=APP_NAME)
builder=CandleBuilder(TIMEFRAMES.get(settings.timeframe,60),settings.history_size)
engine=SignalEngine(settings.min_confidence)

# High-margin background scanner: separate feed so it never interrupts the user's selected pair.
SCAN_ASSETS=[]
# Keep the live scanner lightweight on Render free tier. The selected asset
# feed remains fully live; the AI scanner samples a smaller live subset instead
# of opening a websocket subscription for every instrument at once.
SCANNER_FEED_ASSETS=["EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","USDCAD","NZDUSD","EURGBP"]
scanner_builders={}
scanner_engines={}
scanner_ticks={}
scanner_entry={}
scanner_candidates=[]
scanner_feed=None
scanner_task=None
scanner_loop_task=None

def scanner_history(asset, candles):
    asset=str(asset).lstrip("#")
    b=scanner_builders.setdefault(asset,CandleBuilder(TIMEFRAMES.get(state["timeframe"],60),settings.history_size))
    b.load_candles(candles)
    scanner_ticks.setdefault(asset,0.0)

def scanner_tick(asset, price, ts):
    asset=str(asset).lstrip("#")
    b=scanner_builders.setdefault(asset,CandleBuilder(TIMEFRAMES.get(state["timeframe"],60),settings.history_size))
    b.update(price,ts)
    scanner_ticks[asset]=time.time()

def _scanner_entry_window(asset, direction, confidence, candle_ts, tf):
    now=time.time()
    key=scanner_entry.get(asset)
    bucket=int((candle_ts or now)//tf)*tf
    candle_close=bucket+tf
    if key and key["direction"]==direction and key["bucket"]==bucket and key["until"]>now:
        return key["until"]
    strength=max(0.0,min(1.0,(float(confidence or 0)-settings.min_confidence)/max(1.0,100.0-settings.min_confidence)))
    window=max(5.0,min(tf*0.40,tf*(0.15+0.25*strength)))
    until=min(candle_close,now+window)
    scanner_entry[asset]={"direction":direction,"bucket":bucket,"until":until}
    return until

def refresh_scanner():
    global scanner_candidates
    ranked=[]
    tf=TIMEFRAMES.get(state["timeframe"],60)
    now=time.time()
    # This scanner is deliberately independent of SignalEngine. It reproduces
    # the public VitalySvyatyuk ML/PSAR approach and is signal-only.
    for asset,b in list(scanner_builders.items()):
        if len(b.candles)<90:
            continue
        ml=ml_scan(b.snapshot(),horizon=1,min_probability=0.90,cache_key=asset)
        age=now-scanner_ticks.get(asset,0) if scanner_ticks.get(asset) else 9999
        if age>settings.stale_seconds or not ml.get("ready"):
            continue
        direction=ml.get("signal","WAIT")
        if direction not in ("CALL","PUT"):
            continue
        # Only surface the strongest scanner setups. Require both 90%+
        # RF confidence and a decisive 80-point CALL/PUT margin, plus PSAR
        # agreement. The main SignalEngine is intentionally untouched.
        if float(ml.get("probability",0)) < 90.0:
            continue
        if float(ml.get("margin",0)) < 80.0:
            continue
        if ml.get("psar_signal") != direction:
            continue
        pg=pipgems_confirm(b.snapshot(),direction)
        if not pg.get("ready") or not pg.get("confirmed"):
            continue
        candle_ts=b.candles[-1].ts
        until=_scanner_entry_window(asset,direction,float(ml.get("probability",0)),candle_ts,tf)
        remaining=max(0.0,until-now)
        if remaining<=0:
            continue
        ranked.append({
            "asset":asset,
            "timeframe":state["timeframe"],
            "signal":direction,
            "confidence":round(float(ml.get("probability",0)),1),
            "setup_probability":round(float(ml.get("probability",0)),1),
            "ml_probability":round(float(ml.get("probability",0)),1),
            "call_probability":round(float(ml.get("call_probability",0)),1),
            "put_probability":round(float(ml.get("put_probability",0)),1),
            "ml_accuracy":ml.get("accuracy"),
            "training_rows":ml.get("training_rows",0),
            "psar_signal":ml.get("psar_signal","WAIT"),
            "psar_reason":ml.get("psar_reason",""),
            "core_trend":ml.get("psar_signal","WAIT"),
            "margin":round(float(ml.get("margin",0)),1),
            "pipgems_score":float(pg.get("score",0)),
            "pipgems_confirmed":bool(pg.get("confirmed",False)),
            "pipgems_reason":pg.get("reason",""),
            "ema20":pg.get("ema20"),
            "ema40":pg.get("ema40"),
            "ema_separation":pg.get("ema_separation"),
            "support":pg.get("support"),
            "resistance":pg.get("resistance"),
            "qualified":True,
            "age":round(age,2),
            "entry_remaining":round(remaining,1),
            "entry_open":True,
            "reason":ml.get("reason","Vitaly Random Forest scanner")
        })
    ranked.sort(key=lambda x:(x["ml_probability"],x["margin"],x.get("pipgems_score",0),x["ml_accuracy"] or 0),reverse=True)
    scanner_candidates=ranked[:10]
    return scanner_candidates

async def scanner_loop():
    while True:
        try:
            await asyncio.to_thread(refresh_scanner)
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("High-margin scanner cycle failed")
            await asyncio.sleep(1.0)

state={"asset":settings.asset,"timeframe":settings.timeframe,"price":None,"last_tick":0.0,"signal":{"signal":"WAIT","confidence":0,"reason":"Waiting for market data"},"indicators":{},"entry_until":0.0,"entry_signal":"WAIT"}
feed=None
feed_task=None
signal_task=None
signal_generation=0
signal_calc_pending=False
signal_calc_next_at=0.0
def refresh_entry_window(signal, candle_ts=None):
 now=time.time()
 direction=signal.get("signal","WAIT")
 confidence=float(signal.get("confidence",0) or 0)
 tf=TIMEFRAMES.get(state["timeframe"],60)
 if direction in ("CALL","PUT") and confidence >= settings.min_confidence:
  bucket=int((candle_ts or now)//tf)*tf
  candle_close=bucket+tf
  # Timeframe-aware entry window: no fixed 12-second lock.
  # Stronger signals receive more of the available candle, but never
  # beyond the current candle close.
  strength=max(0.0,min(1.0,(confidence-settings.min_confidence)/max(1.0,100.0-settings.min_confidence)))
  window=max(5.0,min(tf*0.40,tf*(0.15+0.25*strength)))
  proposed=min(candle_close,now+window)
  if state["entry_signal"] != direction or state["entry_until"] <= now:
   state["entry_until"]=proposed
   state["entry_signal"]=direction
 else:
  state["entry_until"]=0.0
  state["entry_signal"]="WAIT"


def on_history(candles):
 loaded=builder.load_candles(candles)
 if loaded:
  result=calculate(builder.snapshot())
  state["indicators"]=result.get("values",{})
  state["signal"]=engine.evaluate(result)
  state["signal"]=apply_pro_guards(state["signal"],state["indicators"],last_tick=state["last_tick"],timeframe_seconds=builder.timeframe,candle_ts=builder.candles[-1].ts if builder.candles else None)
  refresh_entry_window(state["signal"], builder.candles[-1].ts if builder.candles else None)

async def process_latest_ticks():
 global signal_task,signal_calc_pending,signal_calc_next_at
 try:
  generation=signal_generation
  snapshot=builder.snapshot()
  result=await asyncio.to_thread(calculate,snapshot)
  if generation==signal_generation:
   state["indicators"]=result.get("values",{})
   state["signal"]=engine.evaluate(result)
   state["signal"]=apply_pro_guards(state["signal"],state["indicators"],last_tick=state["last_tick"],timeframe_seconds=builder.timeframe,candle_ts=builder.candles[-1].ts if builder.candles else None)
   refresh_entry_window(state["signal"],snapshot[-1]["ts"] if snapshot else None)
 finally:
  signal_task=None
  signal_calc_pending=False
  signal_calc_next_at=time.monotonic()+5.0
  if signal_generation>generation and not signal_calc_pending:
   signal_calc_pending=True
   asyncio.get_running_loop().call_later(0.75,_schedule_signal_calc)

def _schedule_signal_calc():
 global signal_task,signal_calc_pending
 signal_calc_pending=False
 if signal_task is None or signal_task.done():
  signal_task=asyncio.create_task(process_latest_ticks())

def on_tick(asset,price,ts):
 global signal_task,signal_generation,signal_calc_pending
 if asset and asset.lower()!=state["asset"].lower():return
 state["price"]=price
 state["last_tick"]=time.time()
 builder.update(price,ts)
 signal_generation+=1
 if (signal_task is None or signal_task.done()) and not signal_calc_pending and time.monotonic() >= signal_calc_next_at:
  signal_task=asyncio.create_task(process_latest_ticks())
 elif signal_task is None and not signal_calc_pending:
  signal_calc_pending=True
  delay=max(0.05,signal_calc_next_at-time.monotonic())
  asyncio.get_running_loop().call_later(delay,_schedule_signal_calc)

@app.on_event("startup")
async def startup():
 global feed,feed_task,scanner_feed,scanner_task,scanner_loop_task
 feed=PocketOptionFeed(settings.ws_url,settings.auth_json,on_tick,on_history,asset=state["asset"],period=TIMEFRAMES.get(state["timeframe"],60))
 feed_task=asyncio.create_task(feed.run())
 scanner_assets=set(SCANNER_FEED_ASSETS)
 scanner_assets.add(state["asset"])
 scanner_feed=PocketOptionFeed(settings.ws_url,settings.auth_json,scanner_tick,scanner_history,asset=state["asset"],period=TIMEFRAMES.get(state["timeframe"],60),assets=scanner_assets,on_history_asset=scanner_history)
 scanner_task=asyncio.create_task(scanner_feed.run())
 scanner_loop_task=asyncio.create_task(scanner_loop())
 logging.info("%s started; auth configured=%s; AI scanner enabled=%s",APP_NAME,bool(settings.auth_json),bool(scanner_feed))
@app.on_event("shutdown")
async def shutdown():
 if feed:await feed.stop()
 if scanner_feed:await scanner_feed.stop()
 if feed_task:feed_task.cancel()
 if scanner_task:scanner_task.cancel()
 if scanner_loop_task:scanner_loop_task.cancel()
@app.get("/api/health")
async def health():
 age=time.time()-state["last_tick"] if state["last_tick"] else None
 live=bool(feed and feed.connected and age is not None and age<=settings.stale_seconds)
 return {"service":APP_NAME,"feed":"LIVE" if live else "WAITING","engine":"READY" if builder.candles else "WAITING_FOR_FEED","last_tick_age":age,"feed_tick_latency_ms":feed.last_tick_latency_ms if feed else None,"feed_tick_source":feed.last_tick_source if feed else "","auth_configured":bool(settings.auth_json),"error":feed.last_error if feed else ""}
@app.get("/api/scanner")
async def api_scanner():
 return {"enabled":bool(scanner_feed and scanner_feed.connected),"candidates":refresh_scanner()}

@app.get("/api/state")
async def api_state():
 age=time.time()-state["last_tick"] if state["last_tick"] else None
 entry_remaining=max(0.0,state["entry_until"]-time.time()) if state["entry_until"] else 0.0
 if state["entry_until"] and (state["entry_signal"] != state["signal"].get("signal") or state["signal"].get("signal") not in ("CALL","PUT")):
  state["entry_until"]=0.0; state["entry_signal"]="WAIT"; entry_remaining=0.0
 return {"app":APP_NAME,"asset":state["asset"],"timeframe":state["timeframe"],"payout":settings.payout,"expiry":settings.expiry_minutes,"price":state["price"],"last_tick_age":age,"feed_tick_latency_ms":feed.last_tick_latency_ms if feed else None,"feed_tick_source":feed.last_tick_source if feed else "","feed_connected":bool(feed and feed.connected),"candles":builder.snapshot()[-120:],"indicators":state["indicators"],"signal":state["signal"],"scanner":{"enabled":bool(scanner_feed and scanner_feed.connected),"candidates":scanner_candidates},"entry_remaining":round(entry_remaining,1),"entry_open":entry_remaining>0}
@app.get("/api/backtest")
async def api_backtest():
 return run_backtest(builder.snapshot(),settings.min_confidence,150)
@app.get("/api/assets")
async def assets():return {"assets":ASSETS,"timeframes":list(TIMEFRAMES)}
@app.post("/api/config")
async def config(request:Request):
 global builder
 body=await request.json()
 new_asset=body.get("asset") if body.get("asset") in sum(ASSETS.values(),[]) else state["asset"]
 new_tf=body.get("timeframe") if body.get("timeframe") in TIMEFRAMES else state["timeframe"]
 changed_asset=new_asset!=state["asset"]
 changed_tf=new_tf!=state["timeframe"]
 state["asset"]=new_asset
 state["timeframe"]=new_tf
 builder=CandleBuilder(TIMEFRAMES[new_tf],settings.history_size)
 state["price"]=None
 state["last_tick"]=0.0
 state["indicators"]={}
 state["entry_until"]=0.0
 state["entry_signal"]="WAIT"
 state["signal"]={"signal":"WAIT","confidence":0,"reason":"Loading selected market data","votes":[]}
 if feed:
  # Do not make the mobile Apply button wait on the Pocket Option
  # websocket subscription. The UI switches immediately; the feed
  # finishes the subscription in the background.
  async def _switch_feed():
   try:
    await asyncio.wait_for(feed.change_subscription(new_asset,TIMEFRAMES[new_tf]),timeout=5.0)
   except asyncio.TimeoutError:
    logging.warning("configuration subscription timed out for %s/%s; feed will reconnect with the new selection",new_asset,new_tf)
   except Exception:
    logging.exception("configuration change failed for %s/%s",new_asset,new_tf)
  asyncio.create_task(_switch_feed())
 return {"ok":True,"asset":new_asset,"timeframe":new_tf,"changed_asset":changed_asset,"changed_timeframe":changed_tf}
HTML='''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>ICHIGO</title><style>
*{box-sizing:border-box}body{margin:0;background:#08090d;color:#e9e9ee;font-family:system-ui,sans-serif;background:linear-gradient(90deg,rgba(5,6,10,.97),rgba(5,6,10,.76),rgba(5,6,10,.42)),url('/ichigo-bg.jpg') center/cover fixed no-repeat}header{padding:18px 22px;border-bottom:1px solid #262833;background:rgba(13,14,20,.78);backdrop-filter:blur(10px)}h1{margin:0;font-size:22px;letter-spacing:2px}.wrap{max-width:1200px;margin:auto;padding:18px}small,.label,.foot{color:#858b9b}.tabs,.controls{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.tab,select,button,.status{background:#151823;color:#eee;border:1px solid #343846;border-radius:8px;padding:9px 12px}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{background:#11131b;border:1px solid #252834;border-radius:12px;padding:15px;margin-top:12px}.label{font-size:11px;text-transform:uppercase}.value{font-size:23px;margin-top:7px;font-weight:700}.signal{font-size:32px;letter-spacing:2px}.call{color:#56e39f}.put{color:#ff6577}.wait{color:#f1c75b}.chart{height:330px;position:relative;overflow:hidden;border-radius:10px;background:rgba(4,5,9,.72);border:1px solid #252834}.chart canvas{width:100%;height:100%;display:block}.threshold{margin-top:12px;padding:12px;background:#171923;border:1px solid #2d3040;border-radius:10px}.thresholdLine{display:flex;align-items:center;justify-content:space-between;gap:12px}.threshold input{width:100%;accent-color:#ff6577}.thresholdValue{font-weight:900;min-width:54px;text-align:right;color:#ff9aa6}.matrix{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.matrix div{padding:10px;background:#171923;border-radius:7px;font-size:12px}.foot{font-size:12px;margin-top:18px}@media(max-width:800px){.grid{grid-template-columns:1fr 1fr}.matrix{grid-template-columns:1fr 1fr}}@media(max-width:500px){.value{font-size:18px}.signal{font-size:27px}}
</style></head><body><header><div class="wrap"><h1>☠ ICHIGO SIGNAL BOT</h1><small>GOTHIC MARKET INTELLIGENCE • LIVE SIGNAL ENGINE</small></div></header><main class="wrap"><div class="tabs"><div class="tab">Signals</div><div class="tab">Trades</div><div class="tab">Performance</div><div class="tab">Settings</div></div><div class="controls"><select id="asset"></select><select id="tf"></select><button onclick="applyCfg()">APPLY</button><span id="feed" class="status">FEED: WAITING</span><span id="feedAge" class="status">AGE: —</span><span id="eng" class="status">ENGINE: WAITING</span></div><div class="threshold"><div class="thresholdLine"><div><div class="label">Probability threshold</div><small>Only show CALL/PUT when confidence meets this level</small></div><div id="thresholdValue" class="thresholdValue">78%</div></div><input id="threshold" type="range" min="50" max="99" step="1" value="78" aria-label="Probability threshold"></div><div class="grid"><div class="card"><div class="label">Signal</div><div id="sig" class="value signal wait">WAIT</div></div><div class="card"><div class="label">Entry Window</div><div id="count" class="value">—</div><small id="entryStatus">WAITING FOR SIGNAL</small></div><div class="card"><div class="label">LIVE CLOCK</div><div id="clock" class="value">--:--:--</div><small>LOCAL TIME • RUNNING</small></div><div class="card"><div class="label">Confidence</div><div id="conf" class="value">0%</div></div><div class="card"><div class="label">Asset</div><div id="as" class="value">EURUSD_otc</div></div><div class="card"><div class="label">Price</div><div id="price" class="value">—</div></div></div><div class="card"><div class="label">Moving market chart</div><div id="chart" class="chart"><canvas id="priceChart"></canvas></div></div><div class="card"><div class="label">Indicator matrix</div><div id="matrix" class="matrix"></div></div><div class="card"><div class="label">AI Scanner • Vitaly RF + PipGems Context</div><div id="scanner">SCANNING FEED…</div></div><div class="card"><div class="label">Engine reason</div><div id="reason" style="margin-top:8px">Waiting for live market data.</div></div><div class="foot">Signal-only architecture. No order execution is enabled. Entry window is dynamic and closes early if confirmation is lost.</div></main><script>
const $=id=>document.getElementById(id);let probabilityThreshold=Number(localStorage.getItem("ichigoProbabilityThreshold")||78);let lastCandles=[];let chartFrame=0;
function initThreshold(){const el=$("threshold"),out=$("thresholdValue");if(!el)return;el.value=probabilityThreshold;out.textContent=probabilityThreshold+"%";el.oninput=()=>{probabilityThreshold=Number(el.value);out.textContent=probabilityThreshold+"%";localStorage.setItem("ichigoProbabilityThreshold",String(probabilityThreshold));if(window.lastState)render(window.lastState);};}
function drawMovingChart(c){const canvas=$("priceChart"),box=$("chart");if(!canvas||!box)return;const d=devicePixelRatio||1,w=box.clientWidth,h=box.clientHeight;canvas.width=w*d;canvas.height=h*d;const x=canvas.getContext("2d");x.setTransform(d,0,0,d,0,0);x.clearRect(0,0,w,h);const cs=(c||[]).slice(-70);if(!cs.length){x.fillStyle="#858b9b";x.font="13px system-ui";x.fillText("Waiting for live candles…",16,28);return;}let lo=Math.min(...cs.map(q=>Number(q.low))),hi=Math.max(...cs.map(q=>Number(q.high)));const span=(hi-lo)||1,pad=14,vw=w-pad*2,vh=h-pad*2;const px=i=>pad+i*(vw/Math.max(1,cs.length-1)),py=v=>pad+(hi-v)/span*vh;x.strokeStyle="rgba(255,255,255,.07)";x.lineWidth=1;for(let i=1;i<5;i++){const yy=pad+i*vh/5;x.beginPath();x.moveTo(pad,yy);x.lineTo(w-pad,yy);x.stroke();}const bw=Math.max(3,(vw/cs.length)*.62);cs.forEach((q,i)=>{const xx=px(i),up=Number(q.close)>=Number(q.open);x.strokeStyle=up?"#56e39f":"#ff6577";x.fillStyle=x.strokeStyle;x.beginPath();x.moveTo(xx,py(Number(q.high)));x.lineTo(xx,py(Number(q.low)));x.stroke();const top=py(Math.max(Number(q.open),Number(q.close))),bot=py(Math.min(Number(q.open),Number(q.close)));x.fillRect(xx-bw/2,top,bw,Math.max(2,bot-top));});const latest=cs[cs.length-1];x.fillStyle="#fff";x.font="11px system-ui";x.fillText(Number(latest.close).toFixed(5),Math.max(pad,w-82),20);chartFrame++;if(window._chartAnimating){requestAnimationFrame(()=>drawMovingChart(lastCandles));}}
function startChartAnimation(){window._chartAnimating=true;drawMovingChart(lastCandles);}
window.addEventListener("resize",()=>drawMovingChart(lastCandles));
function updateClock(){const e=$("clock");if(e)e.textContent=new Date().toLocaleTimeString([], {hour12:false});}
async function getJson(u){const r=await fetch(u+"?t="+Date.now(),{cache:"no-store"});if(!r.ok)throw Error("HTTP "+r.status);return r.json();}
async function applyCfg(){const b=document.querySelector("button[onclick='applyCfg()']");if(b)b.disabled=true;try{const r=await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},cache:"no-store",body:JSON.stringify({asset:$("asset").value,timeframe:$("tf").value})});const d=await r.json();if(!r.ok||!d.ok)throw Error(d.error||"Configuration failed");$("as").textContent=d.asset;$("sig").textContent="WAIT";$("sig").className="value signal wait";$("conf").textContent="0%";$("count").textContent="00:00";$("reason").textContent="Loading selected market data";}catch(e){console.error(e);}finally{if(b)b.disabled=false;}}
function draw(c){lastCandles=c||[];drawMovingChart(lastCandles);}
function scanner(cands){const e=$("scanner");if(!e)return;if(!cands||!cands.length){e.innerHTML='<small>NO QUALIFIED HIGH-MARGIN SETUP — scanner requires 90%+ RF probability, PSAR agreement, EMA trend separation, support/resistance room and candle confirmation.</small>';return;}e.innerHTML=cands.slice(0,5).map((x,i)=>'<div style="margin:8px 0;padding:9px;background:#171923;border-radius:7px"><b>#'+(i+1)+' '+x.asset+'</b> • <b>'+x.timeframe+'</b> • <span class="'+String(x.signal).toLowerCase()+'">'+x.signal+'</span><br><small>RF '+x.ml_probability+'% • Margin '+x.margin+'% • PipGems '+(x.pipgems_score||0)+'% • PSAR '+x.psar_signal+'</small><br><small>EMA20/40 separation '+(x.ema_separation||0)+' • Entry '+(x.entry_open?'OPEN':'CLOSED')+' • '+(x.entry_remaining||0)+'s</small></div>').join('');}
function matrix(v){const e=$("matrix");if(!e)return;const n=x=>typeof x==="number"?x.toFixed(5):"—",n2=x=>typeof x==="number"?x.toFixed(2):"—";const a=[["EMA 9 / 20 / 50",typeof v.ema9==="number"?[v.ema9,v.ema20,v.ema50].map(n).join(" / "):"—"],["Alligator",typeof v.alligator_lips==="number"?[v.alligator_lips,v.alligator_teeth,v.alligator_jaw].map(n).join(" / "):"—"],["Parabolic SAR",n(v.psar)],["MACD histogram",n(v.macd_hist)],["RSI",n2(v.rsi)],["CCI",n2(v.cci)],["Bollinger 20/2",typeof v.bb_pct==="number"?"%B "+n2(v.bb_pct)+" • W "+(typeof v.bb_width==="number"?v.bb_width.toFixed(4):"—"):"—"],["ADX / DMI",typeof v.adx==="number"?"ADX "+n2(v.adx)+" • +DI "+n2(v.plus_di)+" • -DI "+n2(v.minus_di):"—"],["Fractal Chaos Bands",typeof v.fcb_mid==="number"?(v.fcb_direction||"WAIT")+" • mid "+n(v.fcb_mid):"—"],["Stochastic",typeof v.stoch_k==="number"?"%K "+n2(v.stoch_k)+" • %D "+n2(v.stoch_d):"—"]];e.innerHTML=a.map(x=>"<div><b>"+x[0]+"</b><br>"+x[1]+"</div>").join("");}
function render(s){window.lastState=s;$("as").textContent=s.asset||"—";$("price").textContent=s.price==null?"—":s.price;const z=s.signal||{},rawQ=z.signal||"WAIT",confidence=Number(z.confidence||0),qualified=confidence>=probabilityThreshold,q=qualified?rawQ:"WAIT";$("sig").textContent=q;$("sig").className="value signal "+q.toLowerCase();$("conf").textContent=confidence+"%";const r=Math.max(0,Number(s.entry_remaining||0));$("count").textContent=(q==="CALL"||q==="PUT")&&r>0?"00:"+String(Math.ceil(r)).padStart(2,"0"):"00:00";$("entryStatus").textContent=(q==="CALL"||q==="PUT")&&r>0?"ENTRY OPEN — VALIDATION ACTIVE":"ENTRY CLOSED — WAIT FOR NEXT SIGNAL";$("reason").textContent=!qualified&&(rawQ==="CALL"||rawQ==="PUT")?("Below probability threshold ("+probabilityThreshold+"%)"):z.reason||"Waiting for live market data.";draw(s.candles||[]);matrix(s.indicators||{});scanner((s.scanner||{}).candidates||[]);}
async function poll(){try{render(await getJson("/api/state"));}catch(e){console.warn("state",e);}try{const h=await getJson("/api/health");$("feed").textContent="FEED: "+(h.feed||"WAITING");$("feedAge").textContent="AGE: "+(h.last_tick_age==null?"—":Number(h.last_tick_age).toFixed(2)+"s")+" • NET "+(h.feed_tick_latency_ms==null?"—":Number(h.feed_tick_latency_ms).toFixed(0)+"ms");$("eng").textContent="ENGINE: "+(h.engine||"WAITING");}catch(e){console.warn("health",e);}}
initThreshold();startChartAnimation();setInterval(updateClock,1000);setInterval(poll,1000);updateClock();poll();
</script></body></html>'''
@app.get("/",response_class=HTMLResponse)
async def home():
    # Server-render the current state as a fallback so the dashboard still shows
    # live data even if a mobile browser delays or fails to execute the inline JS.
    now=time.time()
    age=now-state["last_tick"] if state["last_tick"] else None
    live=bool(feed and feed.connected and age is not None and age<=settings.stale_seconds)
    sig=state.get("signal",{}) or {}
    rem=max(0.0,state.get("entry_until",0.0)-now) if state.get("entry_until") else 0.0
    initial_clock=time.strftime("%H:%M:%S")
    initial_feed="LIVE" if live else "WAITING"
    initial_engine="READY" if builder.candles else "WAITING_FOR_FEED"
    initial_age="—" if age is None else f"{age:.2f}s"
    initial_price="—" if state.get("price") is None else str(state.get("price"))
    initial_signal=str(sig.get("signal","WAIT"))
    initial_conf=f"{float(sig.get('confidence',0) or 0):.0f}%"
    initial_reason=str(sig.get("reason","Waiting for live market data"))
    initial_count=f"00:{max(0,int(rem+0.999)):02d}" if rem>0 else "00:00"
    # Render scanner candidates server-side too. This prevents the mobile
    # fallback page from showing a permanent "SCANNING FEED" placeholder when
    # JavaScript is delayed or unavailable.
    scanner_markup = "NO HIGH-MARGIN SETUP"
    if scanner_candidates:
        blocks = []
        for i, item in enumerate(scanner_candidates[:5], 1):
            asset = str(item.get("asset", "—"))
            signal = str(item.get("signal", "WAIT"))
            conf = float(item.get("ml_probability", item.get("confidence", 0)) or 0)
            margin = float(item.get("margin", 0) or 0)
            psar = str(item.get("psar_signal", "WAIT"))
            rem = max(0, int(float(item.get("entry_remaining", 0) or 0) + 0.999))
            blocks.append(
                f'<div style="margin:8px 0;padding:8px;background:#171923;border-radius:7px">'
                f'<b>#{i} {asset}</b> • <b>TF: {state["timeframe"]}</b> • '
                f'<span class="{signal.lower()}">{signal}</span><br>'
                f'<small>RF: <b>{conf:.0f}%</b> • MARGIN: <b>{margin:.0f}%</b> • '
                f'PSAR: <b>{psar}</b></small><br>'
                f'<b>ENTRY: <span>00:{rem:02d}</span></b> • ENTRY OPEN</div>'
            )
        scanner_markup = "".join(blocks)


    html=HTML

    # Server-render the full selectors so the asset/timeframe controls remain
    # visible even if mobile JavaScript is delayed or fails to initialize.
    asset_groups = []
    for group, items in ASSETS.items():
        opts = []
        for item in items:
            selected = ' selected' if item == state["asset"] else ''
            opts.append(f'<option value="{item}"{selected}>{item}</option>')
        asset_groups.append(
            f'<optgroup label="{group}">{"".join(opts)}</optgroup>'
        )
    tf_opts = []
    for tf_name in TIMEFRAMES:
        selected = ' selected' if tf_name == state["timeframe"] else ''
        tf_opts.append(f'<option value="{tf_name}"{selected}>{tf_name}</option>')
    controls_markup = (
        f'<select id="asset" aria-label="Asset" style="min-width:190px">'
        f'{"".join(asset_groups)}</select>'
        f'<select id="tf" aria-label="Timeframe" style="min-width:90px">'
        f'{"".join(tf_opts)}</select>'
        f'<button onclick="applyCfg()">APPLY</button>'
    )
    html=html.replace(
        '<select id="asset"></select><select id="tf"></select><button onclick="applyCfg()">APPLY</button>',
        controls_markup
    )
    html=html.replace('<meta name="viewport" content="width=device-width,initial-scale=1">','<meta name="viewport" content="width=device-width,initial-scale=1">')
    html=html.replace('<div id="scanner">SCANNING FEED…</div>',f'<div id="scanner">{scanner_markup}</div>')
    html=html.replace('FEED: WAITING',f'FEED: {initial_feed}',1)
    html=html.replace('AGE: —',f'AGE: {initial_age} • NET —',1)
    html=html.replace('ENGINE: WAITING',f'ENGINE: {initial_engine}',1)
    html=html.replace('<div id="sig" class="value signal wait">WAIT</div>',f'<div id="sig" class="value signal {initial_signal.lower()}">{initial_signal}</div>')
    html=html.replace('<div id="count" class="value">—</div>',f'<div id="count" class="value">{initial_count}</div>')
    html=html.replace('<div id="clock" class="value">--:--:--</div>',f'<div id="clock" class="value">{initial_clock}</div>')
    html=html.replace('<div id="conf" class="value">0%</div>',f'<div id="conf" class="value">{initial_conf}</div>')
    html=html.replace('<div id="price" class="value">—</div>',f'<div id="price" class="value">{initial_price}</div>')
    html=html.replace('<div id="reason" style="margin-top:8px">Waiting for live market data.</div>',f'<div id="reason" style="margin-top:8px">{initial_reason}</div>')
    return HTMLResponse(content=html, headers={"Cache-Control":"no-store, no-cache, must-revalidate, max-age=0","Pragma":"no-cache","Expires":"0"})
