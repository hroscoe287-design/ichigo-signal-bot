import warnings
warnings.filterwarnings("ignore", category=FutureWarning)
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from indicators import ema, cci, macd, psar

_MODEL_CACHE = {}

# Directly adapted from VitalySvyatyuk/pocket_option_trading_bot/po_bot_ml.py.
# The original model uses binary EMA(3/8), Awesome Oscillator, PSAR reversal,
# CCI and MACD features, with the latest 200 candles and a 400-tree RF.
FEATURES = ["ema_cross", "awesome_oscillator", "psar_reversal", "cci", "macd"]

def _frame(candles):
    df = pd.DataFrame(candles)
    if df.empty:
        return df
    for c in ("open", "high", "low", "close"):
        df.loc[:, c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)

def _psar_reversal(df):
    sar = psar(df)
    close = df["close"].astype(float)
    return (
        ((close > sar) & (close.shift(1) <= sar.shift(1))) |
        ((close < sar) & (close.shift(1) >= sar.shift(1)))
    ).astype(int)

def _features(candles):
    df = _frame(candles)
    if len(df) < 60:
        return pd.DataFrame(), df

    close = df["close"]
    fast = ema(close, 3)
    slow = ema(close, 8)

    # Awesome Oscillator: median price, 5-period SMA minus 34-period SMA.
    median = (df["high"] + df["low"]) / 2.0
    ao = median.rolling(5).mean() - median.rolling(34).mean()

    psar_rev = _psar_reversal(df)
    cc = cci(df, 14)
    macd_line, macd_signal, _ = macd(close)

    out = pd.DataFrame(index=df.index)
    out["ema_cross"] = (
        (fast.shift(1) > slow.shift(1)) &
        (fast < slow) &
        (close < close.shift(1)) &
        (close.shift(1) < close.shift(2))
    ).astype(int)
    out["awesome_oscillator"] = (ao >= 0).astype(int)
    out["psar_reversal"] = psar_rev
    out["cci"] = (cc <= 0).astype(int)
    out["macd"] = (macd_line >= macd_signal).astype(int)
    return out, df

def _psar_strategy(candles):
    df = _frame(candles)
    if len(df) < 20:
        return {"signal": "WAIT", "reason": "PSAR strategy needs more candle history"}

    sar = psar(df)
    close = df["close"]

    # The scanner needs PSAR confirmation, not necessarily a fresh reversal.
    # Requiring a brand-new crossing made valid ongoing PSAR trends disappear
    # between reversal candles. Use the current PSAR side plus a 2-candle
    # persistence check so the confirmation remains directional and stable.
    above = close > sar
    below = close < sar

    if bool(above.iloc[-1]) and bool(above.iloc[-2]):
        if bool(above.iloc[-3]) if len(above) >= 3 else False:
            reason = "PSAR bullish alignment (3 candles)"
        else:
            reason = "PSAR bullish alignment (2 candles)"
        return {"signal": "CALL", "reason": reason}

    if bool(below.iloc[-1]) and bool(below.iloc[-2]):
        if bool(below.iloc[-3]) if len(below) >= 3 else False:
            reason = "PSAR bearish alignment (3 candles)"
        else:
            reason = "PSAR bearish alignment (2 candles)"
        return {"signal": "PUT", "reason": reason}

    return {"signal": "WAIT", "reason": "PSAR is not directionally aligned"}

def scan(candles, horizon=1, min_probability=0.90, cache_key=""):
    """Signal-only adaptation of Vitaly's po_bot_ml.py. Never places orders."""
    feats, df = _features(candles)
    psar_result = _psar_strategy(candles)

    if len(df) < 90:
        return {"ready": False, "signal": "WAIT", "probability": 0.0,
                "call_probability": 0.0, "put_probability": 0.0,
                "accuracy": None, "psar_signal": psar_result["signal"],
                "psar_reason": psar_result["reason"],
                "reason": "Need more candle history"}

    lookback = min(200, len(df))
    feats = feats.iloc[-lookback:].reset_index(drop=True)
    df = df.iloc[-lookback:].reset_index(drop=True)

    # Vitaly's TIME parameter is the number of candles used for estimation.
    horizon = max(1, int(horizon))
    max_i = len(df) - horizon - 1
    if max_i < 40:
        return {"ready": False, "signal": "WAIT", "probability": 0.0,
                "call_probability": 0.0, "put_probability": 0.0,
                "accuracy": None, "psar_signal": psar_result["signal"],
                "psar_reason": psar_result["reason"],
                "reason": "Need more completed candles"}

    # Exact label direction from Vitaly's bot:
    # profit=1 when the future close is <= the current close (PUT).
    future = df["close"].shift(-horizon)
    data = feats.copy()
    data["profit"] = (future <= df["close"]).astype(int)
    data = data.iloc[:max_i + 1].dropna()

    if len(data) < 40 or data["profit"].nunique() < 2:
        return {"ready": False, "signal": "WAIT", "probability": 0.0,
                "call_probability": 0.0, "put_probability": 0.0,
                "accuracy": None, "psar_signal": psar_result["signal"],
                "psar_reason": psar_result["reason"],
                "reason": "Insufficient directional examples"}

    latest_completed = str(len(df) - 1)
    # Reuse each asset model across candles; retraining the RF on every new
    # candle across multiple assets is what was exhausting the free instance.
    cache_id = (cache_key or "default", horizon, "rolling", len(data))
    cached = _MODEL_CACHE.get(cache_id)

    if cached:
        model, accuracy = cached
    else:
        split = max(30, int(len(data) * 0.80))
        train_df = data.iloc[:split]
        valid_df = data.iloc[split:]

        model = RandomForestClassifier(
            n_estimators=50,
            random_state=42,
            n_jobs=1
        )
        model.fit(train_df[FEATURES], train_df["profit"])
        accuracy = (
            float(model.score(valid_df[FEATURES], valid_df["profit"]))
            if len(valid_df) >= 5 else None
        )
        _MODEL_CACHE[cache_id] = (model, accuracy)

    # Vitaly's latest-row calculation uses the current feature state.
    latest = feats.iloc[[-1]][FEATURES]
    if latest.isna().any(axis=None):
        return {"ready": False, "signal": "WAIT", "probability": 0.0,
                "call_probability": 0.0, "put_probability": 0.0,
                "accuracy": accuracy, "psar_signal": psar_result["signal"],
                "psar_reason": psar_result["reason"],
                "reason": "Current feature vector incomplete"}

    probs = model.predict_proba(latest)[0]
    classes = list(model.classes_)
    prob_by_class = {int(c): float(p) for c, p in zip(classes, probs)}

    # class 1 = PUT in Vitaly's training label; class 0 = CALL.
    put_p = prob_by_class.get(1, 0.0)
    call_p = prob_by_class.get(0, 0.0)
    probability = max(put_p, call_p)

    margin = abs(call_p - put_p)
    # Scanner-only quality gate: 90%+ model confidence must also have a
    # decisive probability separation. This does not touch SignalEngine.
    if probability >= min_probability and margin >= 0.80:
        signal = "PUT" if put_p >= call_p else "CALL"
    else:
        signal = "WAIT"

    return {
        "ready": True,
        "signal": signal,
        "probability": round(probability * 100, 1),
        "margin": round(margin * 100, 1),
        "qualified": bool(probability >= min_probability and margin >= 0.80),
        "call_probability": round(call_p * 100, 1),
        "put_probability": round(put_p * 100, 1),
        "accuracy": round(accuracy * 100, 1) if accuracy is not None else None,
        "training_rows": int(len(data)),
        "psar_signal": psar_result["signal"],
        "psar_reason": psar_result["reason"],
        "reason": f"Vitaly RF: {signal if signal != 'WAIT' else 'NO_TRADE'}; probability {probability * 100:.1f}%"
    }
