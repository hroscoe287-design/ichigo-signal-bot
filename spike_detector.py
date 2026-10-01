import numpy as np
import pandas as pd

def detect_spike(df, lookback=20):
    """Detect abnormal short-window price/range/volume expansion."""
    n = len(df)
    if n < lookback + 3:
        return {"ready": False, "direction": "WAIT", "strength": 0.0, "exhaustion": False, "volume_spike": False}
    close = df.close.astype(float)
    high = df.high.astype(float)
    low = df.low.astype(float)
    open_ = df.open.astype(float)
    rng = (high - low).clip(lower=0.0)
    returns = close.diff()
    base_rng = rng.iloc[-lookback-1:-1]
    base_abs_move = returns.abs().iloc[-lookback-1:-1]
    med_rng = float(base_rng.median())
    med_move = float(base_abs_move.median())
    cur_rng = float(rng.iloc[-1])
    cur_move = float(returns.iloc[-1])
    cur_abs_move = abs(cur_move)
    range_ratio = cur_rng / med_rng if med_rng > 0 else 1.0
    move_ratio = cur_abs_move / med_move if med_move > 0 else 1.0
    volumes = df.volume.astype(float) if "volume" in df.columns else pd.Series(0.0, index=df.index)
    base_vol = volumes.iloc[-lookback-1:-1]
    vol_available = bool((base_vol > 0).sum() >= max(5, lookback // 3))
    vol_med = float(base_vol[base_vol > 0].median()) if vol_available else 0.0
    cur_vol = float(volumes.iloc[-1])
    volume_ratio = cur_vol / vol_med if vol_med > 0 else 0.0
    volume_spike = bool(vol_available and volume_ratio >= 1.8)
    price_spike = bool(range_ratio >= 1.8 or move_ratio >= 2.0)
    spike = price_spike or volume_spike
    direction = "CALL" if cur_move > 0 else "PUT" if cur_move < 0 else "WAIT"
    strength = 0.0
    if spike:
        strength += min(0.55, max(0.0, (range_ratio - 1.0) * 0.22))
        strength += min(0.35, max(0.0, (move_ratio - 1.0) * 0.14))
        if volume_spike: strength += 0.20
        strength = min(1.0, strength)
    prev_move = float(returns.iloc[-2]) if pd.notna(returns.iloc[-2]) else 0.0
    prev_abs = abs(prev_move)
    prior_move_ratio = prev_abs / med_move if med_move > 0 else 1.0
    prior_spike = bool((rng.iloc[-2] / med_rng >= 1.8 if med_rng > 0 else False) or prior_move_ratio >= 2.0)
    reversal = bool(prior_spike and prev_move != 0 and cur_move != 0 and np.sign(prev_move) != np.sign(cur_move))
    last3 = returns.iloc[-3:].fillna(0.0).to_numpy()
    accel_side = "CALL" if all(x > 0 for x in last3) else "PUT" if all(x < 0 for x in last3) else "WAIT"
    return {"ready": True, "spike": spike, "direction": direction, "strength": round(float(strength), 3), "price_spike": price_spike, "volume_spike": volume_spike, "volume_available": vol_available, "range_ratio": round(float(range_ratio), 2), "move_ratio": round(float(move_ratio), 2), "volume_ratio": round(float(volume_ratio), 2), "exhaustion": reversal, "prior_spike": prior_spike, "acceleration_direction": accel_side}
