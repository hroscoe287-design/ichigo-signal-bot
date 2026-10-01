import time


def apply_pro_guards(signal, indicators, *, last_tick, timeframe_seconds, candle_ts=None):
    """Professional-style signal hygiene layer.

    Keeps ALUCARD's existing 10-vote model intact while adding safeguards used
    by mature algo systems: fresh-data gating, confirmed-candle context,
    exhaustion/reversal protection, and one-release-per-candle deduplication.
    """
    out=dict(signal or {})
    direction=out.get("signal","WAIT")
    if direction not in ("CALL","PUT"):
        return out

    now=time.time()
    age=(now-last_tick) if last_tick else 9999.0
    # Feed must be genuinely fresh. This is intentionally independent from
    # the dashboard's health display so stale ticks cannot produce a signal.
    freshness_limit=max(3.0,min(8.0,float(timeframe_seconds)*0.20))
    if age>freshness_limit:
        out.update(signal="WAIT", confidence=0, reason=f"WAIT: market data is stale ({age:.1f}s old)", entry_blocked=True, guard="STALE_DATA")
        return out

    v=indicators or {}
    opposite="PUT" if direction=="CALL" else "CALL"

    # Use the last COMPLETED candle as context without forcing the bot to wait
    # for a new candle before every signal. This preserves fast entries.
    confirmed_core=0
    conflicting_core=0
    if v.get("confirmed_alligator_direction") == direction: confirmed_core += 1
    elif v.get("confirmed_alligator_direction") == opposite: conflicting_core += 1
    if v.get("confirmed_macd_direction") == direction: confirmed_core += 1
    elif v.get("confirmed_macd_direction") == opposite: conflicting_core += 1
    if v.get("confirmed_cci_direction") == direction: confirmed_core += 1
    elif v.get("confirmed_cci_direction") == opposite: conflicting_core += 1

    # A live candle may lead a reversal, but a clean 3/3 conflict on the
    # completed candle is treated as a veto unless there is a fresh structure
    # break in the candidate direction.
    structure_break=v.get("market_structure_break","WAIT")
    if conflicting_core>=3 and structure_break!=direction:
        out.update(signal="WAIT", confidence=0, reason=f"WAIT: completed-candle core trend conflicts with {direction}; reversal protection active", entry_blocked=True, guard="CONFIRMED_CONTEXT_CONFLICT")
        return out

    # Exhaustion filter: avoid chasing an extreme CCI when it has started to
    # turn back, especially at resistance/support. A fresh breakout can override.
    cci=v.get("cci"); cci_prev=v.get("cci_prev")
    near_res=bool(v.get("near_resistance")); near_sup=bool(v.get("near_support"))
    macd_slope=v.get("macd_slope_direction","WAIT")
    exhaustion=False
    if cci is not None and cci_prev is not None:
        if direction=="CALL" and cci>=100 and cci<cci_prev and near_res and macd_slope=="PUT": exhaustion=True
        if direction=="PUT" and cci<=-100 and cci>cci_prev and near_sup and macd_slope=="CALL": exhaustion=True
    if exhaustion and structure_break!=direction:
        out.update(signal="WAIT", confidence=0, reason=f"WAIT: {direction} rejected by CCI exhaustion + MACD turn near {'resistance' if direction=='CALL' else 'support'}", entry_blocked=True, guard="EXHAUSTION")
        return out

    # Do not release an identical direction twice inside the same candle.
    # The caller supplies the previous release marker.
    out["entry_blocked"]=False
    out["guard"]="PASS"
    out["data_age"] = round(age,2)
    out["freshness_limit"] = round(freshness_limit,2)
    out["confirmed_core_agreement"] = confirmed_core
    out["confirmed_core_conflict"] = conflicting_core
    out["candle_ts"] = candle_ts
    return out
