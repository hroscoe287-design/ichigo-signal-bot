import asyncio
import json
import logging
import struct
import time
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import websockets

log = logging.getLogger("alucard.feed")
# Keep high-volume feed diagnostics off on the Render free tier.
log.setLevel(logging.WARNING)


class PocketOptionFeed:
    """Signal-only Pocket Option market feed."""

    def __init__(self, url, auth_json, on_tick, on_history=None, asset="EURUSD_otc", period=60, assets=None, on_history_asset=None):
        self.url = url
        self.auth_json = auth_json or ""
        self.on_tick = on_tick
        self.on_history = on_history
        self.asset = asset
        self.assets = set(str(x).lstrip("#") for x in (assets or [asset]))
        self.assets.add(str(asset).lstrip("#"))
        self.period = int(period)
        self.on_history_asset = on_history_asset
        self.running = False
        self.connected = False
        self.authenticated = False
        self.last_tick = 0
        self.last_market_ts = 0.0
        self.last_tick_latency_ms = None
        self.last_tick_source = ""
        self.last_error = ""
        self.ws = None
        self._update_stream_samples = 0
        self._update_stream_rejected_samples = 0
        self._update_assets_samples = 0
        self._region_index = 0

    def _url(self):
        raw = self.url.strip()
        if not raw:
            raw = "wss://api-us-south.po.market/socket.io/?EIO=4&transport=websocket"
        parsed = urlparse(raw)
        hosts = ["api-eu.po.market", "api-msk.po.market", "api-spb.po.market", "api-us-north.po.market", "api-us-south.po.market"]
        if parsed.netloc in hosts:
            host = hosts[self._region_index % len(hosts)]
            raw = urlunparse((parsed.scheme or "wss", host, parsed.path or "/socket.io/", "", parsed.query, ""))
        p = urlparse(raw)
        q = parse_qs(p.query)
        q["EIO"] = ["4"]
        q["transport"] = ["websocket"]
        return urlunparse((p.scheme or "wss", p.netloc, p.path or "/socket.io/", "", urlencode(q, doseq=True), ""))

    def _auth_payload(self):
        if not self.auth_json.strip():
            return None
        raw = self.auth_json.strip()
        if raw.startswith("42"):
            try:
                packet = json.loads(raw[2:])
                if isinstance(packet, list) and len(packet) >= 2 and packet[0] == "auth":
                    return packet[1]
            except Exception:
                pass
        try:
            data = json.loads(raw)
        except Exception:
            data = {"session": raw}
        if isinstance(data, list):
            if len(data) >= 2 and data[0] == "auth":
                return data[1]
            return None
        if isinstance(data, dict) and "command" in data:
            data = data.get("data") or {}
        if not isinstance(data, dict):
            return None

        # Normalize the current Pocket Option auth protocol while preserving
        # the captured session, uid, and demo/real fields.
        payload = dict(data)
        payload.setdefault("platform", 2)
        payload.setdefault("isFastHistory", True)
        payload.setdefault("isOptimized", True)
        return payload

    def _auth_packets(self):
        """Return auth frames in safest-to-most-normalized order."""
        packets = []
        raw = self.auth_json.strip()
        if raw.startswith("42"):
            try:
                packet = json.loads(raw[2:])
                if isinstance(packet, list) and len(packet) >= 2 and packet[0] == "auth":
                    packets.append(raw)
            except Exception:
                pass
        payload = self._auth_payload()
        if payload is not None:
            rebuilt = "42" + json.dumps(["auth", payload], separators=(",", ":"))
            if rebuilt not in packets:
                packets.append(rebuilt)
        return packets

    def auth_packet(self):
        packets = self._auth_packets()
        return packets[0] if packets else None

    def _event_packet(self, event, payload):
        return "42" + json.dumps([event, payload], separators=(",", ":"))

    def _wire_asset(self, asset=None):
        """Return the symbol format expected by Pocket Option's wire protocol."""
        name = str(asset or self.asset)
        upper = name.upper().lstrip("#")
        stock_symbols = {"AAPL", "MSFT", "AMZN", "TSLA", "GOOGL", "META", "NFLX", "NVDA", "VISA", "BA", "AMD", "INTC", "PFE", "COIN", "BABA", "MCD", "PYPL", "CSCO", "JPM", "JNJ", "XOM", "AXP", "FB", "VIX", "CITI", "GME", "PLTR", "MARA"}
        if upper in stock_symbols or (upper.endswith("_OTC") and upper[:-4] in stock_symbols):
            return "#" + name.lstrip("#")
        return name.lstrip("#")

    @staticmethod
    def _display_asset(asset):
        return str(asset).lstrip("#") if asset is not None else asset

    async def _subscribe(self, ws):
        for asset in sorted(self.assets):
            wire_asset = self._wire_asset(asset)
            await ws.send(self._event_packet("subscribeSymbol", {"asset": wire_asset}))
            await ws.send(self._event_packet("changeSymbol", {
                "asset": wire_asset,
                "period": self.period,
            }))
            await ws.send(self._event_packet("subfor", {"asset": wire_asset}))

    async def change_subscription(self, asset, period):
        """Switch the live Pocket Option subscription without restarting the service."""
        try:
            period = int(period)
        except (TypeError, ValueError):
            raise ValueError("Invalid timeframe period")

        if not asset:
            raise ValueError("Asset is required")

        self.asset = str(asset).lstrip("#")
        self.period = period
        # Keep exactly one active subscription: the currently selected asset.
        # This prevents stale subscriptions from consuming the feed and makes
        # Apply reliably switch the live stream to the selected instrument.
        self.assets = {self.asset}

        if self.ws and self.connected and self.authenticated:
            await self._subscribe(self.ws)
            log.info(
                "Pocket Option subscription changed to %s/%ss",
                self.asset,
                self.period,
            )
            return True

        log.info(
            "Pocket Option subscription queued for reconnect: %s/%ss",
            self.asset,
            self.period,
        )
        return False

    async def _keepalive(self, ws):
        while self.running:
            try:
                await ws.send(self._event_packet("ps", {}))
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                raise
            except Exception:
                return

    def _number(self, value):
        try:
            value = float(value)
            return value if value > 0 else None
        except (TypeError, ValueError):
            return None

    def _replace_placeholders(self, value, attachments):
        if isinstance(value, dict):
            if value.get("_placeholder") is True and isinstance(value.get("num"), int):
                idx = value["num"]
                if 0 <= idx < len(attachments):
                    return attachments[idx]
                return value
            return {k: self._replace_placeholders(v, attachments) for k, v in value.items()}
        if isinstance(value, list):
            return [self._replace_placeholders(v, attachments) for v in value]
        return value

    def _decode_socket_packet(self, msg):
        if isinstance(msg, bytes):
            return None
        if not isinstance(msg, str) or (not msg.startswith("42") and not msg.startswith("45")):
            return None
        raw = msg[2:]
        attachments = 0
        if msg.startswith("45"):
            dash = raw.find("-")
            if dash < 1:
                return None
            try:
                attachments = int(raw[:dash])
            except ValueError:
                return None
            raw = raw[dash + 1:]
        try:
            packet = json.loads(raw)
        except Exception:
            return None
        if not isinstance(packet, list) or len(packet) < 2:
            return None
        return str(packet[0]), packet[1], attachments

    async def _handshake(self, ws):
        first = await asyncio.wait_for(ws.recv(), timeout=15)
        if isinstance(first, bytes):
            first = first.decode("utf-8", "ignore")
        if not str(first).startswith("0"):
            raise RuntimeError(f"unexpected Engine.IO handshake: {str(first)[:120]}")
        await ws.send("40")

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            msg = await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.monotonic()))
            if isinstance(msg, bytes):
                continue
            if str(msg) == "40" or str(msg).startswith("40"):
                return
            if str(msg) == "2":
                await ws.send("3")

        raise RuntimeError("Socket.IO namespace handshake timed out")

    async def _authenticate(self, ws):
        packets = self._auth_packets()
        if not packets:
            raise RuntimeError("PO_AUTH_JSON is not configured")
        await ws.send(packets[0])
        auth_attempt = 0

        auth_deadline = time.monotonic() + 45
        while time.monotonic() < auth_deadline:
            msg = await asyncio.wait_for(ws.recv(), timeout=max(1, auth_deadline - time.monotonic()))
            if isinstance(msg, bytes):
                continue

            text_msg = str(msg)
            if text_msg == "2":
                await ws.send("3")
                continue
            if text_msg.startswith("41"):
                raise RuntimeError(f"Pocket Option authorization rejected: {text_msg[:200]}")

            decoded = self._decode_socket_packet(text_msg)
            if decoded is None:
                continue
            event, body, count = decoded

            if count:
                attachments = []
                for _ in range(count):
                    attachment = await asyncio.wait_for(
                        ws.recv(), timeout=max(1, auth_deadline - time.monotonic())
                    )
                    attachments.append(attachment)
                body = self._replace_placeholders(body, attachments)

            if event == "successauth":
                self.authenticated = True
                log.info("Pocket Option authorization accepted")
                return
            if event == "updateAssets":
                log.info("Pocket Option auth-stage assets received; continuing authorization wait")
                if len(packets) > 1 and auth_attempt == 0:
                    auth_attempt = 1
                    await ws.send(packets[1])
                    log.info("Pocket Option auth retry: normalized session payload")

        raise RuntimeError("Pocket Option authorization response not received")

    def _price_bounds(self):
        name = self.asset.upper().lstrip("#")
        if "XAU" in name or "GOLD" in name:
            return 100.0, 10000.0
        if "XAG" in name or "SILVER" in name:
            return 5.0, 200.0
        if any(x in name for x in ("BTC", "ETH", "LTC", "XRP", "BCH", "DOGE", "ADA", "SOL", "DOT", "LINK", "AVAX", "BNB")):
            return 0.0000001, 1_000_000_000.0
        if any(x in name for x in ("US30", "NAS", "SPX", "DAX", "CAC", "FTSE", "100GBP", "E50", "D30")):
            return 10.0, 100_000.0
        if any(x in name for x in ("AAPL", "MSFT", "AMZN", "TSLA", "GOOGL", "META", "NFLX", "NVDA", "VISA", "BA", "AMD", "INTC", "PFE", "COIN", "BABA", "MCD", "PYPL", "CSCO", "JPM", "JNJ", "XOM")):
            return 0.01, 1_000_000.0
        return 0.00001, 10.0

    def _valid_price(self, value, asset=None):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False
        old_asset = self.asset
        try:
            if asset is not None:
                self.asset = str(asset).lstrip("#")
            low, high = self._price_bounds()
        finally:
            self.asset = old_asset
        return low <= value <= high

    def _extract_binary_tick(self, data):
        """Decode the compact Pocket Option stream frame."""
        if not isinstance(data, (bytes, bytearray)) or len(data) < 5:
            return None

        raw = bytes(data)

        try:
            decoded = json.loads(raw.decode("utf-8"))
            if isinstance(decoded, list):
                for item in decoded:
                    if not isinstance(item, (list, tuple)) or len(item) < 3:
                        continue
                    asset = self._display_asset(item[0]) if item[0] is not None else self.asset
                    try:
                        stamp = float(item[1])
                        price = float(item[2])
                    except (TypeError, ValueError):
                        continue
                    if self._valid_price(price, asset) and (not asset or asset.lower() in {str(x).lower() for x in self.assets}):
                        if stamp > 10_000_000_000:
                            stamp /= 1000.0
                        return self._display_asset(asset) or self.asset, price, stamp
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            pass

        if len(raw) < 36:
            return None

        try:
            values = struct.unpack("<IdIfffff", raw[:36])
            if self._valid_price(values[1], self.asset):
                stamp = float(values[2])
                if stamp > 10_000_000_000:
                    stamp /= 1000.0
                return self._display_asset(self.asset), float(values[1]), stamp
        except struct.error:
            pass

        for offset in range(1, min(17, len(raw) - 35)):
            try:
                values = struct.unpack("<IdIfffff", raw[offset:offset + 36])
            except struct.error:
                continue
            if not self._valid_price(values[1], self.asset):
                continue
            stamp = float(values[2])
            if stamp <= 0:
                continue
            if stamp > 10_000_000_000:
                stamp /= 1000.0
            return self._display_asset(self.asset), float(values[1]), stamp

        return None

    def _extract_history(self, body):
        source = body
        if isinstance(source, (bytes, bytearray)):
            try:
                source = json.loads(bytes(source).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return []
        if not isinstance(source, dict):
            return []
        asset = self._display_asset(source.get("asset") or source.get("symbol") or self.asset)
        raw = source.get("candles") or source.get("history") or []
        if isinstance(raw, dict):
            raw = raw.get("candles") or raw.get("history") or []
        out = []
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                continue
            try:
                ts = float(row[0])
                o, c, h, l = map(float, row[1:5])
            except (TypeError, ValueError):
                continue
            if ts > 10_000_000_000:
                ts /= 1000.0
            if asset.lower() in {str(v).lower() for v in self.assets} and self._valid_price(o, asset) and self._valid_price(h, asset) and self._valid_price(l, asset) and self._valid_price(c, asset) and h >= max(o, c) and l <= min(o, c):
                out.append({"timestamp": ts, "open": o, "close": c, "high": h, "low": l})
        return out

    def _extract_event(self, event, body):
        candidates = []

        def walk(value, asset=None, timestamp=None):
            if isinstance(value, dict):
                current_asset = asset
                current_ts = timestamp
                for key, child in value.items():
                    lk = str(key).lower()
                    if lk in {"asset", "symbol", "pair", "active", "instrument"}:
                        current_asset = self._display_asset(child)
                    elif lk in {"time", "timestamp", "ts", "at"}:
                        try:
                            current_ts = float(child)
                        except (TypeError, ValueError):
                            pass
                    elif lk in {"price", "rate", "quote", "close", "value", "bid", "ask", "close_value"}:
                        number = self._number(child)
                        if number is not None and self._valid_price(number):
                            candidates.append((current_asset, number, current_ts))
                    walk(child, current_asset, current_ts)
            elif isinstance(value, list):
                for child in value:
                    walk(child, asset, timestamp)
            elif isinstance(value, (bytes, bytearray)):
                parsed = self._extract_binary_tick(bytes(value))
                if parsed:
                    candidates.append(parsed)

        walk(body)
        preferred = [x for x in candidates if x[0] is None or str(x[0]).lower() in {str(v).lower() for v in self.assets}]
        if event == "updateStream" and preferred:
            asset, price, ts = preferred[0]
            return asset or self.asset, price, ts
        for asset, price, ts in preferred:
            if asset in self.assets:
                return asset, price, ts
        return None

    @staticmethod
    def _safe_body_summary(body, limit=1600):
        if isinstance(body, (bytes, bytearray)):
            raw = bytes(body)
            preview = raw[:limit]
            try:
                text = preview.decode("utf-8")
                return f"bytes={len(raw)} utf8={text!r}"
            except UnicodeDecodeError:
                return f"bytes={len(raw)} hex={preview[:96].hex()}"
        try:
            return json.dumps(body, separators=(",", ":"), default=str)[:limit]
        except Exception:
            return repr(body)[:limit]

    async def run(self):
        self.running = True
        delay = 2

        while self.running:
            try:
                url = self._url()
                log.info("connecting to Pocket Option websocket")
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=16 * 1024 * 1024,
                    max_queue=32,
                    compression=None,
                    additional_headers={
                        "Origin": "https://pocketoption.com",
                        "User-Agent": "Mozilla/5.0",
                    },
                ) as ws:
                    self.ws = ws
                    self.connected = True
                    self.authenticated = False
                    self.last_error = ""
                    self._update_stream_samples = 0
                    self._update_stream_rejected_samples = 0
                    self._update_assets_samples = 0
                    delay = 2

                    await self._handshake(ws)
                    await self._authenticate(ws)
                    await self._subscribe(ws)
                    log.info(
                        "Pocket Option feed authenticated; subscribed to %s/%ss",
                        self.asset,
                        self.period,
                    )

                    keepalive_task = asyncio.create_task(self._keepalive(ws))
                    try:
                        while self.running:
                            msg = await ws.recv()

                            if isinstance(msg, bytes):
                                parsed_binary = self._extract_binary_tick(msg)
                                if parsed_binary:
                                    asset, price, ts = parsed_binary
                                    received_at = time.time()
                                    self.last_tick = received_at
                                    self.last_market_ts = float(ts or 0)
                                    self.last_tick_latency_ms = max(0.0, (received_at - self.last_market_ts) * 1000.0) if self.last_market_ts else None
                                    self.last_tick_source = "binary"
                                    self.on_tick(asset, price, ts)
                                    if self._update_stream_samples < 5:
                                        log.debug("Pocket Option binary market tick received: %s %.8f latency_ms=%.1f", asset, price, self.last_tick_latency_ms or 0.0)
                                else:
                                    log.debug("Pocket Option binary frame received: %d bytes hex=%s", len(msg), msg[:32].hex())
                                continue

                            text_msg = str(msg)
                            if text_msg == "2":
                                await ws.send("3")
                                continue
                            if text_msg == "3":
                                continue
                            if text_msg.startswith("1"):
                                raise RuntimeError(
                                    f"Pocket Option websocket closed: {text_msg[:200]}"
                                )

                            decoded = self._decode_socket_packet(text_msg)
                            if decoded is None:
                                if text_msg:
                                    log.debug("Pocket Option non-event message: %s", text_msg[:180])
                                continue

                            event, body, count = decoded

                            if count:
                                attachments = []
                                for _ in range(count):
                                    attachments.append(await ws.recv())
                                body = self._replace_placeholders(body, attachments)

                            if event == "updateAssets" and self._update_assets_samples < 2:
                                self._update_assets_samples += 1
                                log.debug(
                                    "Pocket Option updateAssets diagnostic %d: %s",
                                    self._update_assets_samples,
                                    self._safe_body_summary(body, 2200),
                                )

                            if event == "updateStream" and self._update_stream_samples < 5:
                                self._update_stream_samples += 1
                                log.debug(
                                    "Pocket Option updateStream sample %d: %s",
                                    self._update_stream_samples,
                                    self._safe_body_summary(body, 2200),
                                )

                            if event != "updateStream":
                                log.debug("Pocket Option event received: %s body_type=%s attachments=%d", event, type(body).__name__, count)

                            if event == "updateHistoryNewFast" and (self.on_history or self.on_history_asset):
                                history = self._extract_history(body)
                                if history:
                                    if self.on_history_asset:
                                        history_asset = self._display_asset(body.get("asset") or body.get("symbol") or self.asset) if isinstance(body, dict) else self.asset
                                        self.on_history_asset(history_asset, history)
                                    elif self.on_history:
                                        self.on_history(history)
                                    log.debug("Pocket Option historical candles loaded: %d for %s", len(history), self._display_asset(body.get("asset") or body.get("symbol") or self.asset) if isinstance(body, dict) else self.asset)

                            parsed = self._extract_event(event, body)
                            if parsed:
                                asset, price, ts = parsed
                                stamp = float(ts) if ts else time.time()
                                if stamp > 10_000_000_000:
                                    stamp /= 1000.0
                                received_at = time.time()
                                self.last_tick = received_at
                                self.last_market_ts = stamp
                                self.last_tick_latency_ms = max(0.0, (received_at - stamp) * 1000.0) if stamp else None
                                self.last_tick_source = event
                                self.on_tick(asset, price, stamp)
                                if self._update_stream_samples <= 5:
                                    log.debug("Pocket Option market tick received: %s %.8f latency_ms=%.1f", asset, price, self.last_tick_latency_ms or 0.0)
                            elif event == "updateStream":
                                if self._update_stream_rejected_samples < 5:
                                    self._update_stream_rejected_samples += 1
                                    log.debug(
                                        "Pocket Option rejected updateStream for %s: %s",
                                        self.asset,
                                        self._safe_body_summary(body, 1800),
                                    )
                            elif event in {"updateHistoryNewFast", "successauth"}:
                                log.debug("Pocket Option market event had no valid price: %s", event)
                    finally:
                        keepalive_task.cancel()
                        try:
                            await keepalive_task
                        except asyncio.CancelledError:
                            pass

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.authenticated = False
                self.last_error = repr(exc)
                self._region_index = (self._region_index + 1) % 5
                log.warning("feed disconnected: %s (%s); rotating websocket region", exc, type(exc).__name__)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
            finally:
                self.connected = False
                self.authenticated = False
                self.ws = None

    async def stop(self):
        self.running = False
        if self.ws:
            await self.ws.close()
