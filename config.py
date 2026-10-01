import os
from dataclasses import dataclass

APP_NAME="ICHIGO SIGNAL BOT"
TIMEFRAMES={"5s":5,"15s":15,"30s":30,"1m":60,"2m":120,"3m":180,"5m":300,"15m":900,"30m":1800,"1h":3600,"4h":14400,"1d":86400}

FOREX=["EURUSD","GBPUSD","USDJPY","USDCHF","AUDUSD","USDCAD","NZDUSD","EURGBP","EURJPY","GBPJPY","AUDCAD","AUDCHF","AUDJPY","AUDNZD","CADCHF","CADJPY","CHFJPY","EURAUD","EURCAD","EURCHF","EURNZD","GBPAUD","GBPCAD","GBPCHF","GBPNZD","NZDCAD","NZDCHF"]
OTC_EXTRA=["USDINR","USDBRL","USDIDR","USDMXN","USDCLP","USDPKR","USDCOP","USDARS","USDVND","USDTHB","USDCNH"]
ASSETS={
 "Forex":FOREX,
 "Forex OTC":[x+"_otc" for x in FOREX+OTC_EXTRA],
 "Commodities":["XAUUSD","XAGUSD","USOIL","UKOIL","NATGAS"],
 "Commodities OTC":["XAUUSD_otc","XAGUSD_otc","USOIL_otc","UKOIL_otc","NATGAS_otc"],
 "Crypto":["BTCUSD","ETHUSD","LTCUSD","XRPUSD","BCHUSD","DOGEUSD","ADAUSD","SOLUSD","DOTUSD","LINKUSD","AVAXUSD","BNB"],
 "Crypto OTC":["BTCUSD_otc","ETHUSD_otc","LTCUSD_otc","XRPUSD_otc","BCHUSD_otc","DOGEUSD_otc","ADAUSD_otc","SOLUSD_otc","DOTUSD_otc","LINKUSD_otc","AVAXUSD_otc","BNB_otc"],
 "Stocks":["AAPL","MSFT","AMZN","TSLA","GOOGL","META","NFLX","NVDA"],
 "Stocks OTC":["AAPL_otc","MSFT_otc","AMZN_otc","TSLA_otc","GOOGL_otc","META_otc","NFLX_otc","NVDA_otc"],
 "Indices":["US30","US100","SP500","GER30","UK100","JPN225"],
 "Indices OTC":["US30_otc","US100_otc","SP500_otc","GER30_otc","UK100_otc","JPN225_otc"]
}
REGION_URLS={"EU":"wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket","MSK":"wss://api-msk.po.market/socket.io/?EIO=4&transport=websocket","SPB":"wss://api-spb.po.market/socket.io/?EIO=4&transport=websocket","US-N":"wss://api-us-north.po.market/socket.io/?EIO=4&transport=websocket","US-S":"wss://api-us-south.po.market/socket.io/?EIO=4&transport=websocket"}

@dataclass(frozen=True)
class Settings:
 ws_url:str=os.getenv("PO_WS_URL",REGION_URLS["EU"])
 auth_json:str=os.getenv("PO_AUTH_JSON","")
 asset:str=os.getenv("PO_ASSET","EURUSD_otc")
 timeframe:str=os.getenv("PO_TIMEFRAME","1m")
 payout:int=int(os.getenv("DEFAULT_PAYOUT","85"))
 expiry_minutes:int=int(os.getenv("DEFAULT_EXPIRY_MINUTES","5"))
 min_confidence:float=float(os.getenv("MIN_CONFIDENCE","60"))
 stale_seconds:float=float(os.getenv("STALE_SECONDS","8"))
 entry_seconds:int=int(os.getenv("ENTRY_SECONDS","12"))
 history_size:int=int(os.getenv("HISTORY_SIZE","250"))
 adx_period:int=int(os.getenv("ADX_PERIOD","14"))
 adx_smoothing:int=int(os.getenv("ADX_SMOOTHING","7"))
 fractal_span:int=int(os.getenv("FRACTAL_SPAN","2"))
settings=Settings()