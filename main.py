import json
import os
import threading
import time
from collections import deque

import numpy as np
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from websocket import create_connection

POCKET_WS_DEFAULTS = [
    "wss://api-c.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://api-l.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://demo-api-eu.po.market/socket.io/?EIO=4&transport=websocket",
    "wss://try-demo-eu.po.market/socket.io/?EIO=4&transport=websocket",
]
POCKET_WS_HEADERS = [
    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language: en-US,en;q=0.9",
]

app = FastAPI(title="ICHIGO SIGNAL BOT")

ASSET = os.getenv("ASSET", "EURUSD_otc")
TIMEFRAME = int(os.getenv("TIMEFRAME", "60"))
POCKET_WS_URL = os.getenv("POCKET_WS_URL") or os.getenv("POCKET_URL", "")
POCKET_WS_URLS = [POCKET_WS_URL] if POCKET_WS_URL else POCKET_WS_DEFAULTS
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


def parse_auth(value):
    value = (value or "").strip()
    if not value:
        raise ValueError("PO_AUTH_JSON is empty")
    try:
        obj = json.loads(value)
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list) and len(obj) >= 2 and obj[0] == "auth":
            return obj[1]
    except json.JSONDecodeError:
        pass
    if value.startswith("42"):
        obj = json.loads(value[2:])
        if isinstance(obj, list) and len(obj) >= 2 and obj[0] == "auth":
            return obj[1]
    if value.startswith("40"):
        obj = json.loads(value[2:])
        if isinstance(obj, dict):
            return obj
    raise ValueError("PO_AUTH_JSON is not valid JSON or a supported auth packet")


def feed_worker():
    if not PO_AUTH_JSON:
        print("FEED: missing PO_AUTH_JSON", flush=True)
        return
    while True:
        ws = None
        try:
            auth = parse_auth(PO_AUTH_JSON)
            connected = False
            for url in POCKET_WS_URLS:
                try:
                    print(f"FEED: trying {url}", flush=True)
                    ws = create_connection(
                        url, timeout=20, header=POCKET_WS_HEADERS,
                        origin="https://pocketoption.com", suppress_origin=True
                    )
                    connected = True
                    break
                except Exception as endpoint_exc:
                    print(f"FEED: endpoint failed {type(endpoint_exc).__name__}: {endpoint_exc}", flush=True)
            if not connected or ws is None:
                raise RuntimeError("No Pocket Option WebSocket endpoint accepted the connection")

            first = ws.recv()
            if isinstance(first, bytes):
                first = first.decode("utf-8", errors="ignore")
            first = str(first)
            print(f"FEED: handshake {first[:80]}", flush=True)
            if first.startswith("2"):
                ws.send("3")

            ws.send("40")
            ws.send("42" + json.dumps(["auth", auth], separators=(",", ":")))
            print("FEED: websocket connected; auth sent", flush=True)

            while True:
                msg = ws.recv()
                if not msg:
                    continue
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", errors="ignore")
                msg = str(msg)
                if msg == "2":
                    ws.send("3")
                    continue
                parse_message(msg)

        except Exception as exc:
            with lock:
                state["connected"] = False
            print(f"FEED: connection error: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(3)
        finally:
            try:
                if ws:
                    ws.close()
            except Exception:
                pass


def parse_message(msg):
    if not msg:
        return
    payload = str(msg).strip()

    if payload.startswith("42"):
        payload = payload[2:]
    elif payload.startswith(("40", "41", "44")):
        return
    elif payload.startswith("0"):
        payload = payload[1:]
    elif payload.startswith("4"):
        payload = payload[1:]
    else:
        return

    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
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
                    except (TypeError, ValueError):
                        pass
            for key in ("history", "candles", "data"):
                if key in obj and isinstance(obj[key], list):
                    for item in obj[key]:
                        consume(item)
        elif isinstance(obj, list):
            for item in obj:
                consume(item)

    with lock:
        before = state["last_tick"]
    consume(data)
    with lock:
        if state["last_tick"] != before:
            state["connected"] = True

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
