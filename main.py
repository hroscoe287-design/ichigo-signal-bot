import json
import os
import threading
import time
from collections import deque

import numpy as np
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from websocket import create_connection

app = FastAPI(title="ICHIGO SIGNAL BOT")

ASSET = os.getenv("ASSET", "EURUSD_otc")
TIMEFRAME = int(os.getenv("TIMEFRAME", "60"))
POCKET_WS_URL = os.getenv("POCKET_WS_URL", "")
PO_AUTH_JSON = os.getenv("PO_AUTH_JSON", "")

state = {
    "asset": ASSET,
    "timeframe": TIMEFRAME,
    "connected": False,
    "price": None,
    "candles": [],
    "signal": "WAIT",
    "score": 0,
    "reason": "Waiting for market data",
    "last_tick": 0,
    "source": "Pocket Option feed",
}
lock = threading.Lock()
candles = deque(maxlen=300)


def add_tick(ts, price):
    global candles
    ts = float(ts)
    price = float(price)
    bucket = int(ts // TIMEFRAME) * TIMEFRAME
    with lock:
        if not candles or candles[-1]["time"] != bucket:
            candles.append({"time": bucket, "open": price, "high": price, "low": price, "close": price})
        else:
            c = candles[-1]
            c["close"] = price
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
        state["price"] = price
        state["last_tick"] = time.time()


def score_signal():
    with lock:
        cs = list(candles)
    if len(cs) < 30:
        return "WAIT", 0, "Building candle history"

    close = np.array([c["close"] for c in cs], dtype=float)
    ema9 = close[-9:].mean()
    ema20 = close[-20:].mean()
    ema50 = close[-50:].mean() if len(close) >= 50 else close.mean()
    momentum = close[-1] - close[-6]
    recent = close[-15:]
    vol = np.std(np.diff(recent)) + 1e-9

    score = 50
    if ema9 > ema20 > ema50:
        score += 15
    elif ema9 < ema20 < ema50:
        score += 15

    if momentum > 0:
        score += 10
    elif momentum < 0:
        score += 10

    if abs(momentum) / vol > 1.2:
        score += 10

    body = cs[-1]["close"] - cs[-1]["open"]
    if body != 0:
        score += 5

    direction = "CALL" if momentum > 0 else "PUT" if momentum < 0 else "WAIT"
    score = int(min(99, max(0, score)))
    if score < 75:
        return "WAIT", score, "Confluence below 75%"
    return direction, score, "Multi-factor confluence confirmed"


def feed_worker():
    if not POCKET_WS_URL or not PO_AUTH_JSON:
        return
    while True:
        ws = None
        try:
            ws = create_connection(POCKET_WS_URL, timeout=25)
            with lock:
                state["connected"] = True

            auth = json.loads(PO_AUTH_JSON)
            ws.send("40")
            ws.send("42" + json.dumps(["auth", auth], separators=(",", ":")))

            while True:
                msg = ws.recv()
                if not msg:
                    continue
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", errors="ignore")
                parse_message(msg)
        except Exception:
            with lock:
                state["connected"] = False
            time.sleep(3)
        finally:
            try:
                if ws:
                    ws.close()
            except Exception:
                pass


def parse_message(msg):
    payload = msg
    if payload.startswith("42"):
        payload = payload[2:]
    elif payload.startswith("4"):
        payload = payload[1:]
    try:
        data = json.loads(payload)
    except Exception:
        return

    def consume(obj):
        if isinstance(obj, dict):
            asset = obj.get("asset") or obj.get("symbol")
            if asset and str(asset).lower() != ASSET.lower():
                return
            for key in ("price", "value", "rate", "close"):
                if key in obj:
                    try:
                        add_tick(time.time(), float(obj[key]))
                        return
                    except Exception:
                        pass
            for key in ("history", "candles", "data"):
                if key in obj and isinstance(obj[key], list):
                    for item in obj[key]:
                        consume(item)
        elif isinstance(obj, list):
            for item in obj:
                consume(item)

    consume(data)


def signal_loop():
    while True:
        sig, score, reason = score_signal()
        with lock:
            state["signal"] = sig
            state["score"] = score
            state["reason"] = reason
        time.sleep(1)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(open("index.html", encoding="utf-8").read())


@app.get("/api/state")
def api_state():
    with lock:
        out = dict(state)
        out["candles"] = list(candles)
        out["age"] = round(time.time() - state["last_tick"], 2) if state["last_tick"] else None
    return out


@app.get("/api/health")
def health():
    with lock:
        return {"status": "ok", "feed": "LIVE" if state["connected"] else "WAITING"}


threading.Thread(target=feed_worker, daemon=True).start()
threading.Thread(target=signal_loop, daemon=True).start()
