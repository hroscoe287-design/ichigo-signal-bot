from datetime import datetime, timezone


class SignalEngine:
    """ALUCARD V4 signal engine.

    Keeps the 10-vote / 100-point model as the baseline, but makes the three
    user-selected directional leaders (Alligator, MACD, CCI) safety gates
    instead of allowing later confirmation bonuses to rescue a stale leader.
    """

    def __init__(self, min_confidence=70):
        self.min_confidence = min_confidence
        self.last_signal = "WAIT"
        self.developing_side = "WAIT"
        self.developing_strength = 0.0
        self.developing_since = 0.0

    @staticmethod
    def _dir(value, positive, negative):
        if value is None:
            return "WAIT"
        if value > positive:
            return "CALL"
        if value < negative:
            return "PUT"
        return "WAIT"

    def evaluate(self, ind):
        now = datetime.now(timezone.utc).timestamp()
        if not ind.get("ready"):
            self.last_signal = "WAIT"
            self.developing_side = "WAIT"
            self.developing_strength = 0.0
            self.developing_since = 0.0
            return {
                "signal": "WAIT", "confidence": 0, "reason": ind.get("reason", "Insufficient data"),
                "votes": [], "developing_signal": "WAIT", "developing_strength": 0
            }

        v = ind["values"]
        call = put = 0.0

        # Spike layer: a separate momentum/exhaustion safety system. It does
        # not alter the 10-vote baseline, so the existing model remains intact.
        spike_detected = bool(v.get("spike_detected", False))
        spike_direction = v.get("spike_direction", "WAIT")
        spike_strength = float(v.get("spike_strength") or 0.0)
        spike_exhaustion = bool(v.get("spike_exhaustion", False))
        spike_acceleration = v.get("spike_acceleration_direction", "WAIT")
        votes = []

        def vote(name, direction, weight):
            nonlocal call, put
            if direction == "CALL":
                call += weight
            elif direction == "PUT":
                put += weight
            votes.append({"name": name, "direction": direction, "weight": round(weight, 1)})

        price = v.get("price")
        atr = v.get("atr") or 0.0

        # ---------- 10 weighted baseline votes ----------
        jaw = v.get("alligator_jaw")
        teeth = v.get("alligator_teeth")
        lips = v.get("alligator_lips")
        if jaw is not None and teeth is not None and lips is not None:
            spread = max(abs(lips - jaw), abs(teeth - jaw), abs(lips - teeth))
            alligator_dir = "CALL" if lips > teeth > jaw else "PUT" if lips < teeth < jaw else "WAIT"
            vote("Alligator", alligator_dir, 20.0)
        else:
            spread = 0.0
            alligator_dir = "WAIT"
            vote("Alligator", "WAIT", 0)

        ajp = v.get("alligator_jaw_prev")
        atp = v.get("alligator_teeth_prev")
        alp = v.get("alligator_lips_prev")
        prev_spread = max(abs(alp - ajp), abs(atp - ajp), abs(alp - atp)) if all(
            x is not None for x in (ajp, atp, alp)
        ) else None
        alligator_expanding = bool(prev_spread is not None and spread > prev_spread)
        width_ratio = spread / atr if atr > 0 else 0.0
        alligator_wide = bool(width_ratio >= 0.75 and atr > 0)
        alligator_compressed = bool(width_ratio < 0.35 and atr > 0)

        alligator_slope_dir = "WAIT"
        if all(x is not None for x in (ajp, atp, alp)):
            slopes = (jaw - ajp, teeth - atp, lips - alp)
            if all(x > 0 for x in slopes):
                alligator_slope_dir = "CALL"
            elif all(x < 0 for x in slopes):
                alligator_slope_dir = "PUT"

        alligator_strong = bool(
            alligator_dir in ("CALL", "PUT")
            and alligator_wide
            and alligator_expanding
            and alligator_slope_dir == alligator_dir
        )
        alligator_weak = bool(
            alligator_dir == "WAIT"
            or alligator_compressed
            or (alligator_slope_dir in ("CALL", "PUT") and alligator_slope_dir != alligator_dir)
        )

        ema9, ema20, ema50 = v.get("ema9"), v.get("ema20"), v.get("ema50")
        ema_dir = (
            "CALL" if None not in (ema9, ema20, ema50) and ema9 > ema20 > ema50
            else "PUT" if None not in (ema9, ema20, ema50) and ema9 < ema20 < ema50
            else "WAIT"
        )
        vote("EMA 9/20/50", ema_dir, 12.0)

        fd = v.get("fractal_down")
        fu = v.get("fractal_up")
        fractal_dir = "CALL" if fd and not fu else "PUT" if fu and not fd else "WAIT"
        vote(f"Fractal ({v.get(\"fractal_span\",2)})", fractal_dir, 4.0)

        psar = v.get("psar")
        psar_dir = "CALL" if price is not None and psar is not None and price > psar else "PUT" if price is not None and psar is not None and price < psar else "WAIT"
        vote("Parabolic SAR", psar_dir, 10.0)

        macd_hist = v.get("macd_hist")
        macd_hist_prev = v.get("macd_hist_prev")
        macd_dir = "CALL" if macd_hist is not None and macd_hist > 0 else "PUT" if macd_hist is not None and macd_hist < 0 else "WAIT"
        vote("MACD 12/26/9", macd_dir, 15.0)

        macd_slope_dir = (
            "CALL" if macd_hist is not None and macd_hist_prev is not None and macd_hist > macd_hist_prev
            else "PUT" if macd_hist is not None and macd_hist_prev is not None and macd_hist < macd_hist_prev
            else "WAIT"
        )

        rsi = v.get("rsi")
        rsi_dir = "CALL" if rsi is not None and rsi > 50 else "PUT" if rsi is not None and rsi < 50 else "WAIT"
        vote("RSI (14)", rsi_dir, 8.0)

        cci = v.get("cci")
        cci_prev = v.get("cci_prev")
        cci_prev2 = v.get("cci_prev2")
        cci_dir = (
            "CALL" if cci is not None and cci > 0
            else "PUT" if cci is not None and cci < 0
            else "WAIT"
        )
        vote("CCI (14)", cci_dir, 10.0)

        bbp, bbmid = v.get("bb_pct"), v.get("bb_mid")
        bb_dir = (
            "CALL" if bbp is not None and bbmid is not None and bbp > 0.50 and price >= bbmid
            else "PUT" if bbp is not None and bbmid is not None and bbp < 0.50 and price <= bbmid
            else "WAIT"
        )
        vote("Bollinger 20/2", bb_dir, 8.0)

        atr_base = v.get("atr_baseline")
        atr_ratio = atr / atr_base if atr_base not in (None, 0) else None
        atr_very_low = atr_ratio is not None and atr_ratio < 0.60
        atr_active = atr_ratio is not None and atr_ratio >= 0.80
        atr_dir = "CALL" if not atr_very_low and price is not None and ema9 is not None and price > ema9 else "PUT" if not atr_very_low and price is not None and ema9 is not None and price < ema9 else "WAIT"
        vote("ATR (14)", atr_dir, 5.0)

        st = v.get("supertrend")
        st_direction = v.get("supertrend_direction")
        super_dir = (
            "CALL" if st is not None and st_direction == 1 and price > st
            else "PUT" if st is not None and st_direction == -1 and price < st
            else "WAIT"
        )
        vote("Supertrend 10/3", super_dir, 8.0)

        # FCB is a structure filter, not an additional weighted vote.
        fcb_upper=v.get("fcb_upper")
        fcb_lower=v.get("fcb_lower")
        fcb_mid=v.get("fcb_mid")
        fcb_mid_prev=v.get("fcb_mid_prev")
        fcb_ready=all(x is not None for x in (fcb_upper,fcb_lower,fcb_mid,price))
        fcb_slope=(fcb_mid-fcb_mid_prev) if fcb_mid_prev is not None else None
        fcb_dir=(
            "CALL" if fcb_ready and price>=fcb_mid and fcb_slope is not None and fcb_slope>0
            else "PUT" if fcb_ready and price<=fcb_mid and fcb_slope is not None and fcb_slope<0
            else "WAIT"
        )

        # ---------- Fractal + DMI veto layer ----------
        adx = v.get("adx")
        plus_di = v.get("plus_di")
        minus_di = v.get("minus_di")
        adx_ready = adx is not None and plus_di is not None and minus_di is not None
        dmi_dir = "CALL" if adx_ready and plus_di > minus_di else "PUT" if adx_ready and minus_di > plus_di else "WAIT"
        fractal_veto_dir = fractal_dir if fractal_dir in ("CALL","PUT") else "WAIT"
        fractal_dmi_conflict = bool(fractal_veto_dir in ("CALL","PUT") and dmi_dir in ("CALL","PUT") and fractal_veto_dir != dmi_dir)
        fractal_dmi_veto = bool(fractal_dmi_conflict and adx_ready and adx >= 20)

        # ---------- confirmation context ----------
        sk, sd = v.get("stoch_k"), v.get("stoch_d")
        stoch_ready = sk is not None and sd is not None
        stoch_dir = "CALL" if stoch_ready and sk > sd else "PUT" if stoch_ready and sk < sd else "WAIT"

        osma_hist = v.get("osma_hist")
        osma_dir = "CALL" if osma_hist is not None and osma_hist > 0 else "PUT" if osma_hist is not None and osma_hist < 0 else "WAIT"

        tenkan, kijun = v.get("ichimoku_tenkan"), v.get("ichimoku_kijun")
        span_a, span_b = v.get("ichimoku_span_a"), v.get("ichimoku_span_b")
        ichimoku_dir = "WAIT"
        if all(x is not None for x in (tenkan, kijun, span_a, span_b, price)):
            top, bottom = max(span_a, span_b), min(span_a, span_b)
            if price > top and tenkan > kijun:
                ichimoku_dir = "CALL"
            elif price < bottom and tenkan < kijun:
                ichimoku_dir = "PUT"
        osma_ichimoku_dir = osma_dir if osma_dir in ("CALL", "PUT") and osma_dir == ichimoku_dir else "WAIT"

        dem, dem_prev = v.get("demarker"), v.get("demarker_prev")
        wma9, wma9_prev = v.get("wma9"), v.get("wma9_prev")
        demarker_wma_dir = "WAIT"
        if all(x is not None for x in (dem, dem_prev, wma9, wma9_prev, price)):
            if dem > 0.50 and dem >= dem_prev and price > wma9 and wma9 >= wma9_prev:
                demarker_wma_dir = "CALL"
            elif dem < 0.50 and dem <= dem_prev and price < wma9 and wma9 <= wma9_prev:
                demarker_wma_dir = "PUT"

        ut_fast = v.get("ut_fast_direction", 0)
        ut_slow = v.get("ut_slow_direction", 0)
        ut_fast_dir = "CALL" if ut_fast == 1 else "PUT" if ut_fast == -1 else "WAIT"
        ut_slow_dir = "CALL" if ut_slow == 1 else "PUT" if ut_slow == -1 else "WAIT"
        ut_confirmation = ut_fast_dir if ut_fast_dir == ut_slow_dir and ut_fast_dir in ("CALL", "PUT") else "WAIT"

        market_structure = v.get("market_structure", "WAIT")
        market_structure_break = v.get("market_structure_break", "WAIT")
        candle_direction = v.get("candle_direction", "WAIT")
        candle_confirmed = bool(v.get("candle_confirmed", False))

        near_support = bool(v.get("near_support", False))
        near_resistance = bool(v.get("near_resistance", False))
        support_break = bool(v.get("support_break", False))
        resistance_break = bool(v.get("resistance_break", False))
        support = v.get("support")
        resistance = v.get("resistance")

        # ---------- reversal detection ----------
        cci_reversal_side = "WAIT"
        cci_extreme = bool(cci is not None and abs(cci) >= 100)
        cci_turning = False
        if cci is not None and cci_prev is not None:
            cci_turning = (cci >= 100 and cci < cci_prev) or (cci <= -100 and cci > cci_prev)
            if cci >= 100 and cci < cci_prev:
                cci_reversal_side = "PUT"
            elif cci <= -100 and cci > cci_prev:
                cci_reversal_side = "CALL"

        cci_reversal_zone = (
            (cci_reversal_side == "PUT" and near_resistance)
            or (cci_reversal_side == "CALL" and near_support)
        )

        # CCI direction uses trend/slope, not merely the sign.
        cci_slope = (cci - cci_prev) if cci is not None and cci_prev is not None else None
        cci_trend_dir = "WAIT"
        if cci is not None and cci_slope is not None:
            if cci >= 50 and cci_slope > 0:
                cci_trend_dir = "CALL"
            elif cci <= -50 and cci_slope < 0:
                cci_trend_dir = "PUT"
            elif cci > 0 and cci_slope > 0:
                cci_trend_dir = "CALL"
            elif cci < 0 and cci_slope < 0:
                cci_trend_dir = "PUT"

        # Strong CCI trend requires two rising/falling CCI steps at an extreme.
        cci_clear = False
        if cci_trend_dir == "CALL" and cci is not None and cci_prev is not None and cci_prev2 is not None:
            cci_clear = cci >= 100 and cci > cci_prev > cci_prev2
        elif cci_trend_dir == "PUT" and cci is not None and cci_prev is not None and cci_prev2 is not None:
            cci_clear = cci <= -100 and cci < cci_prev < cci_prev2

        reversal_evidence = []
        leader_direction = "CALL" if call > put else "PUT" if put > call else "WAIT"
        weighted_margin = abs(call - put)
        leader_score = max(call, put)
        directional_votes = sum(1 for x in votes if x["direction"] == leader_direction)
        confidence = round(directional_votes / 10 * 100, 1)

        if leader_direction in ("CALL", "PUT"):
            opposite = "PUT" if leader_direction == "CALL" else "CALL"
            if alligator_slope_dir == opposite:
                reversal_evidence.append("Alligator")
            if macd_slope_dir == opposite:
                reversal_evidence.append("MACD_Slope")
            if cci_trend_dir == opposite:
                reversal_evidence.append("CCI")
            if cci_reversal_side == opposite:
                reversal_evidence.append("CCI_Extreme_Reversal")
            if adx_ready and adx >= 18 and dmi_dir == opposite:
                reversal_evidence.append("DMI")
            if osma_dir == opposite:
                reversal_evidence.append("OSMA")
            if market_structure == opposite:
                reversal_evidence.append("Structure")
            if market_structure_break == opposite:
                reversal_evidence.append("StructureBreak")
            if candle_confirmed and candle_direction == opposite:
                reversal_evidence.append("Candle")

        hard_reversal_tripwire = False
        if leader_direction in ("CALL", "PUT") and cci_reversal_side in ("CALL", "PUT"):
            opposite = "PUT" if leader_direction == "CALL" else "CALL"
            macd_against = macd_slope_dir == opposite
            alligator_against = alligator_slope_dir == opposite
            if cci_reversal_side == opposite and macd_against and (alligator_against or cci_reversal_zone):
                hard_reversal_tripwire = True

        core_conflicts = sum(
            1 for d in (alligator_dir, macd_dir, cci_trend_dir)
            if d in ("CALL", "PUT") and leader_direction in ("CALL", "PUT") and d != leader_direction
        )
        core_agreement = sum(
            1 for d in (alligator_dir, macd_dir, cci_trend_dir)
            if d == leader_direction and leader_direction in ("CALL", "PUT")
        )

        # HARD DIRECTIONAL SAFETY:
        # The old engine could choose a weighted leader and then use bonuses to
        # rescue it. That is the source of the stale CALL/PUT behavior. The new
        # gate is immediate and does not add a candle delay.
        core_direction_block = False
        if leader_direction in ("CALL", "PUT"):
            opposite = "PUT" if leader_direction == "CALL" else "CALL"
            if core_conflicts >= 2:
                core_direction_block = True
            if cci_reversal_side == opposite and macd_slope_dir == opposite:
                core_direction_block = True
            if hard_reversal_tripwire:
                core_direction_block = True

        # Extreme CCI at resistance/support must not be chased when momentum is
        # turning back. A clean break can override the location warning.
        exhaustion_block = False
        if leader_direction == "CALL" and cci_reversal_side == "PUT" and near_resistance and not resistance_break:
            if macd_slope_dir == "PUT" or alligator_slope_dir == "PUT":
                exhaustion_block = True
        elif leader_direction == "PUT" and cci_reversal_side == "CALL" and near_support and not support_break:
            if macd_slope_dir == "CALL" or alligator_slope_dir == "CALL":
                exhaustion_block = True

        # Confirmation score is secondary. It can strengthen a valid candidate,
        # but it can never reverse the core direction or rescue a blocked one.
        confirmation_bonus = 0.0
        conflict_penalty = 0.0

        def confirm(direction, amount):
            nonlocal confirmation_bonus, conflict_penalty
            if leader_direction not in ("CALL", "PUT"):
                return
            if direction == leader_direction:
                confirmation_bonus += amount
            elif direction in ("CALL", "PUT"):
                conflict_penalty += amount

        confirm(dmi_dir if adx_ready and adx >= 18 else "WAIT", 2.0)
        if stoch_ready and ((leader_direction == "CALL" and sk < 85) or (leader_direction == "PUT" and sk > 15)):
            confirm(stoch_dir, 1.5)
        confirm(market_structure, 2.5)
        confirm(market_structure_break, 1.5)
        if candle_confirmed:
            confirm(candle_direction, 1.5)
        confirm(ut_confirmation, 2.0)
        confirm(demarker_wma_dir, 1.5)
        confirm(osma_ichimoku_dir, 1.5)

        if leader_direction == "CALL" and near_support:
            confirmation_bonus += 1.0
        elif leader_direction == "PUT" and near_resistance:
            confirmation_bonus += 1.0
        elif leader_direction == "CALL" and near_resistance and not resistance_break:
            conflict_penalty += 1.5
        elif leader_direction == "PUT" and near_support and not support_break:
            conflict_penalty += 1.5

        momentum = [
            v.get("momentum_1") or 0.0,
            v.get("momentum_2") or 0.0,
            v.get("momentum_3") or 0.0,
        ]
        momentum_side = "CALL" if all(x > 0 for x in momentum) else "PUT" if all(x < 0 for x in momentum) else "WAIT"
        momentum_same_count = sum(1 for x in momentum if (x > 0 if momentum_side == "CALL" else x < 0)) if momentum_side != "WAIT" else 0
        momentum_bonus = 0.0
        if momentum_side == leader_direction and momentum_same_count >= 2:
            momentum_bonus = min(3.0, 1.0 + 0.7 * momentum_same_count)

        adjusted_margin = weighted_margin + confirmation_bonus + momentum_bonus - conflict_penalty
        trend_votes = sum(1 for d in (alligator_dir, ema_dir, super_dir) if d == leader_direction)
        alligator_conflict = leader_direction in ("CALL", "PUT") and alligator_dir in ("CALL", "PUT") and alligator_dir != leader_direction
        trend_aligned = leader_direction in ("CALL", "PUT") and alligator_dir == leader_direction and trend_votes >= 2 and not alligator_conflict

        # A wide/expanding Alligator is preferred, but a compressed Alligator
        # does not by itself delay a signal if the core direction is clean.
        if alligator_strong and alligator_dir == leader_direction:
            confirmation_bonus += 2.0

        # Persistence remains short so the engine stays responsive.
        raw_candidate = leader_direction if leader_direction in ("CALL", "PUT") else "WAIT"
        if raw_candidate == "WAIT":
            self.developing_strength = max(0.0, self.developing_strength - 0.10)
            if self.developing_strength <= 0.05:
                self.developing_side = "WAIT"
                self.developing_since = 0.0
        elif raw_candidate != self.developing_side:
            self.developing_side = raw_candidate
            self.developing_strength = 0.25
            self.developing_since = now
        else:
            self.developing_strength = min(1.0, self.developing_strength + 0.10)

        developing_age = now - self.developing_since if self.developing_since else 0.0
        candidate_persistent = (
            self.developing_side == raw_candidate
            and developing_age >= 0.8
            and self.developing_strength >= 0.40
        )

        spike_block = False
        if leader_direction in ("CALL", "PUT"):
            opposite = "PUT" if leader_direction == "CALL" else "CALL"
            # Do not chase a strong move that is already reversing.
            if spike_exhaustion:
                spike_block = True
            # A fresh, unusually large move directly against the proposed
            # direction is treated as a fast veto; aligned spikes do not delay.
            elif spike_detected and spike_direction == opposite and spike_strength >= 0.45:
                spike_block = True

        # Lightweight confirmation layer: Supertrend + DMI/ADX + FCB are not
        # additional weighted votes. FCB only joins a veto when the other
        # independent trend checks already disagree, preserving responsiveness.
        confirmation_direction_block = False
        if leader_direction in ("CALL", "PUT"):
            opposite = "PUT" if leader_direction == "CALL" else "CALL"
            strong_adx = bool(adx_ready and adx >= 20)
            super_conflict = super_dir == opposite
            dmi_conflict = dmi_dir == opposite
            fcb_conflict = fcb_dir == opposite
            # FCB only participates in the veto when trend strength is already
            # confirmed by ADX and both Supertrend and DMI disagree.
            if strong_adx and super_conflict and dmi_conflict and fcb_conflict:
                confirmation_direction_block = True

        safety_block = (
            core_direction_block
            or exhaustion_block
            or fractal_dmi_veto
            or spike_block
            or confirmation_direction_block
        )
        effective_min_confidence = self.min_confidence

        # Probability-style quality gate. This is NOT a claimed win-rate or
        # statistically calibrated probability; it combines independent
        # agreement/confirmation dimensions into a conservative setup estimate.
        probability_threshold = 78.0
        margin_quality = max(0.0, min(100.0, 50.0 + adjusted_margin * 4.0))
        setup_probability = round(
            0.45 * confidence
            + 0.25 * (core_agreement / 3.0 * 100.0)
            + 0.20 * (trend_votes / 3.0 * 100.0)
            + 0.10 * margin_quality,
            1,
        )

        full_ok = (
            raw_candidate != "WAIT"
            and candidate_persistent
            and confidence >= effective_min_confidence
            and setup_probability >= probability_threshold
            and adjusted_margin >= 6.0
            and trend_aligned
            and not safety_block
            and not (cci_clear and cci_trend_dir != leader_direction)
            and not (cci_trend_dir in ("CALL", "PUT") and cci_trend_dir != leader_direction and abs(cci or 0) >= 100)
        )

        signal = raw_candidate if full_ok else "WAIT"

        if signal in ("CALL", "PUT"):
            reason = (
                f"{signal} confirmation: {directional_votes}/10 agree ({confidence:.0f}%), "
                f"core {core_agreement}/3, trend {trend_votes}/3, margin {adjusted_margin:.1f}"
            )
        elif spike_block:
            reason = f"WAIT: spike protection blocked {leader_direction}; abnormal momentum is opposite or reversing"
        elif fractal_dmi_veto:
            reason = f"WAIT: Fractal + DMI veto — fractal {fractal_veto_dir}, DMI {dmi_dir}"
        elif confirmation_direction_block:
            reason = f"WAIT: Supertrend + DMI/ADX conflict with {leader_direction}; trend confirmation veto active"
        elif safety_block:
            reason = f"WAIT: reversal protection blocked stale {leader_direction}; Alligator/MACD/CCI evidence turned"
        elif not trend_aligned:
            reason = f"WAIT: {directional_votes}/10 agree ({confidence:.0f}%); Alligator/trend alignment is not sufficient"
        elif leader_direction == "WAIT":
            reason = "WAIT: weighted indicators are evenly split"
        elif confidence < effective_min_confidence:
            reason = f"WAIT: {directional_votes}/10 agree ({confidence:.0f}%); confidence threshold not met"
        else:
            reason = f"WAIT: {directional_votes}/10 agree ({confidence:.0f}%); entry confirmation not complete"

        self.last_signal = signal
        return {
            "signal": signal,
            "confidence": confidence if signal != "WAIT" else 0.0,
            "agreement_count": directional_votes if signal != "WAIT" else 0,
            "agreement_total": 10,
            "call_score": round(call, 1),
            "put_score": round(put, 1),
            "max_score": 100,
            "atr_active": atr_active,
            "atr_ratio": round(atr_ratio, 2) if atr_ratio is not None else None,
            "atr_very_low": atr_very_low,
            "alligator_width_ratio": round(width_ratio, 3),
            "alligator_wide": alligator_wide,
            "alligator_expanding": alligator_expanding,
            "alligator_compressed": alligator_compressed,
            "alligator_slope_direction": alligator_slope_dir,
            "alligator_strong": alligator_strong,
            "alligator_weak": alligator_weak,
            "alligator_conflict": alligator_conflict,
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "stoch_k": sk,
            "stoch_d": sd,
            "dmi_direction": dmi_dir,
            "adx_period": 14,
            "adx_smoothing": 7,
            "dmi_plus_color": "green",
            "dmi_minus_color": "red",
            "show_adx_line": False,
            "fractal_span": v.get("fractal_span", 2),
            "fractal_veto_direction": fractal_veto_dir,
            "fractal_dmi_conflict": fractal_dmi_conflict,
            "fractal_dmi_veto": fractal_dmi_veto,
            "fcb_direction": fcb_dir,
            "fcb_upper": fcb_upper,
            "fcb_lower": fcb_lower,
            "fcb_mid": fcb_mid,
            "fcb_slope": round(fcb_slope,8) if fcb_slope is not None else None,
            "stoch_direction": stoch_dir,
            "ut_fast_direction": ut_fast_dir,
            "ut_slow_direction": ut_slow_dir,
            "ut_confirmation": ut_confirmation,
            "osma_direction": osma_dir,
            "osma_hist": osma_hist,
            "ichimoku_direction": ichimoku_dir,
            "osma_ichimoku_direction": osma_ichimoku_dir,
            "demarker": dem,
            "wma9": wma9,
            "demarker_wma_direction": demarker_wma_dir,
            "confirmation_bonus": round(confirmation_bonus, 1),
            "conflict_penalty": round(conflict_penalty, 1),
            "adjusted_margin": round(adjusted_margin, 1),
            "market_structure": market_structure,
            "market_structure_pattern": v.get("market_structure_pattern", "INSUFFICIENT"),
            "market_structure_break": market_structure_break,
            "candle_confirmation": candle_direction if candle_confirmed else "WAIT",
            "support": support,
            "resistance": resistance,
            "near_support": near_support,
            "near_resistance": near_resistance,
            "support_break": support_break,
            "resistance_break": resistance_break,
            "candle_direction": candle_direction,
            "candle_body_ratio": round(float(v.get("candle_body_ratio") or 0.0), 3),
            "candle_confirmed": candle_confirmed,
            "momentum_bonus": round(momentum_bonus, 1),
            "momentum_side": momentum_side,
            "momentum_same_count": momentum_same_count,
            "spike_detected": spike_detected,
            "spike_direction": spike_direction,
            "spike_strength": round(spike_strength, 3),
            "spike_exhaustion": spike_exhaustion,
            "spike_acceleration_direction": spike_acceleration,
            "spike_range_ratio": v.get("spike_range_ratio"),
            "spike_move_ratio": v.get("spike_move_ratio"),
            "spike_volume_ratio": v.get("spike_volume_ratio"),
            "spike_volume_available": bool(v.get("spike_volume_available", False)),
            "spike_protection": spike_block,
            "confirmation_direction_block": confirmation_direction_block,
            "reversal_conflict": safety_block,
            "reversal_safety": safety_block,
            "reversal_direction": cci_reversal_side,
            "major_reversal_block": safety_block,
            "reversal_evidence": reversal_evidence,
            "cci_direction": cci_trend_dir,
            "cci_clear": cci_clear,
            "cci_hard_conflict": bool(cci_clear and cci_trend_dir != leader_direction),
            "cci_reversal_side": cci_reversal_side,
            "cci_extreme": cci_extreme,
            "cci_turning": cci_turning,
            "cci_reversal_zone": cci_reversal_zone,
            "cci_reversal_conflict": bool(cci_reversal_side in ("CALL", "PUT") and cci_reversal_side != leader_direction),
            "hard_reversal_tripwire": hard_reversal_tripwire,
            "effective_min_confidence": effective_min_confidence,
            "probability_threshold": probability_threshold,
            "setup_probability": setup_probability,
            "probability_gate": bool(setup_probability >= probability_threshold),
            "core_agreement": core_agreement,
            "core_conflicts": core_conflicts,
            "developing_signal": self.developing_side,
            "developing_strength": round(self.developing_strength, 2),
            "developing_age": round(developing_age, 1),
            "early_confirmation": False,
            "votes": votes,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
