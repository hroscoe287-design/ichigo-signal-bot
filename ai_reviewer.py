import json
import os
import urllib.request

class AIReviewer:
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.model = os.getenv("OPENAI_MODEL", "gpt-5").strip()
        self.enabled = os.getenv("AI_CONFIRMATION_ENABLED", "true").lower() not in ("0", "false", "no")
        self.timeout = float(os.getenv("AI_CONFIRMATION_TIMEOUT", "3.5"))

    def review(self, asset, timeframe, engine_result, indicators, candles):
        if not self.enabled or not self.api_key:
            return {"enabled": False, "decision": "NO_REVIEW", "reason": "AI confirmation is not configured"}

        candidate = engine_result.get("signal", "WAIT")
        if candidate not in ("CALL", "PUT"):
            return {"enabled": True, "decision": "WAIT", "reason": "ALUCARD did not produce a CALL/PUT candidate"}

        compact = {k: indicators.get(k) for k in (
            "price", "ema9", "ema20", "ema50",
            "alligator_jaw", "alligator_teeth", "alligator_lips",
            "psar", "macd_hist", "rsi", "cci", "bb_pct", "bb_width",
            "adx", "plus_di", "minus_di", "stoch_k", "stoch_d",
            "atr", "atr_baseline", "atr_ratio",
            "momentum_1", "momentum_2", "momentum_3"
        )}

        recent = []
        for c in (candles or [])[-8:]:
            recent.append({k: c.get(k) for k in ("ts", "open", "high", "low", "close") if k in c})

        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "You are the independent confirmation layer for ALUCARD V4. "
                        "Review only the supplied market snapshot. Do not invent data. "
                        "Return JSON with decision CALL, PUT, or WAIT and a short reason. "
                        "Confirm only when the candidate direction is coherent and current. "
                        "If evidence is insufficient or conflicting, return WAIT. "
                        "This is signal analysis, not order execution."
                    )
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "asset": asset,
                        "timeframe": timeframe,
                        "engine_candidate": candidate,
                        "engine": engine_result,
                        "indicators": compact,
                        "recent_candles": recent
                    }, separators=(",", ":"))
                }
            ],
            "max_output_tokens": 120
        }

        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json"
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))

            output_text = ""
            for item in data.get("output", []):
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        output_text += part.get("text", "")

            parsed = json.loads(output_text)
            decision = str(parsed.get("decision", "WAIT")).upper()
            if decision not in ("CALL", "PUT", "WAIT"):
                decision = "WAIT"

            return {
                "enabled": True,
                "decision": decision,
                "reason": str(parsed.get("reason", "AI confirmation complete"))[:300],
                "model": self.model
            }
        except Exception as exc:
            return {
                "enabled": True,
                "decision": "WAIT",
                "reason": "AI review unavailable: " + str(exc)[:180],
                "model": self.model
            }
