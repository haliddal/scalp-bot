import asyncio, json, logging, hmac, hashlib, time
from datetime import datetime
from aiohttp import web
import aiohttp

import os
MEXC_API_KEY    = os.environ.get("MEXC_API_KEY", "")
MEXC_SECRET_KEY = os.environ.get("MEXC_SECRET_KEY", "")
WEBHOOK_PORT    = 8080
WEBHOOK_SECRET  = "scalp2025"
LEVERAGE        = 15
MARGIN_TYPE     = "CROSSED"
RISK_PERCENT    = 2.5
MAX_OPEN_TRADES = 4

ALLOWED = {
    "BTCUSDT": ["1", "3", "5", "10", "15", "30", "45", "60", "120", "180", "240"],
    "ETHUSDT": ["1", "3", "5", "10", "15", "30", "45", "60", "120", "180", "240"],
    "SOLUSDT": ["1", "3", "5", "10", "15", "30", "45", "60", "120", "180", "240"],
    "BNBUSDT": ["1", "3", "5", "10", "15", "30", "45", "60", "120", "180", "240"],
},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ScalpBot")

open_trades = []
stats = {"wins": 0, "losses": 0, "total": 0}

BASE = "https://contract.mexc.com"

async def futures_post(path: str, body: dict) -> dict:
    ts = str(int(time.time() * 1000))
    sign_str = MEXC_API_KEY + ts + json.dumps(body)
    signature = hmac.new(MEXC_SECRET_KEY.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    headers = {
        "ApiKey": MEXC_API_KEY,
        "Request-Time": ts,
        "Signature": signature,
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(BASE + path, json=body, headers=headers) as r:
            return await r.json()

async def futures_get(path: str, params: dict = None) -> dict:
    params = params or {}
    ts = str(int(time.time() * 1000))
    param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sign_str = MEXC_API_KEY + ts + param_str
    signature = hmac.new(MEXC_SECRET_KEY.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    headers = {
        "ApiKey": MEXC_API_KEY,
        "Request-Time": ts,
        "Signature": signature,
    }
    async with aiohttp.ClientSession() as s:
        async with s.get(BASE + path, params=params, headers=headers) as r:
            return await r.json()

async def get_usdt_balance() -> float:
    data = await futures_get("/api/v1/private/account/assets")
    for asset in data.get("data", []):
        if asset.get("currency") == "USDT":
            return float(asset.get("availableBalance", 0))
    return 0.0

async def get_price(symbol: str) -> float:
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{BASE}/api/v1/contract/ticker", params={"symbol": symbol}) as r:
            d = await r.json()
            return float(d["data"]["lastPrice"])

async def set_leverage(symbol: str):
    await futures_post("/api/v1/private/position/change_leverage", {
        "symbol": symbol,
        "leverage": LEVERAGE,
        "openType": 2,
    })
    log.info(f"⚙️  {symbol} kaldıraç: {LEVERAGE}x Cross")

async def place_futures_order(symbol, side, qty, tp, sl):
    open_type = 1 if side == "BUY" else 3
    body = {
        "symbol": symbol,
        "price": 0,
        "vol": qty,
        "side": open_type,
        "type": 5,
        "openType": 2,
        "leverage": LEVERAGE,
        "stopLossPrice": round(sl, 4),
        "takeProfitPrice": round(tp, 4),
    }
    result = await futures_post("/api/v1/private/order/submit", body)
    if result.get("success"):
        log.info(f"✅ {side} açıldı — {symbol} | qty={qty} | TP={tp:.4f} | SL={sl:.4f}")
        return result
    else:
        log.error(f"❌ Order hatası: {result}")
        return None

def is_allowed(symbol, tf):
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    symbol = symbol.upper()
    if symbol not in ALLOWED:
        log.info(f"⛔ {symbol} izin listesinde yok.")
        return False
    allowed_tfs = ALLOWED[symbol]
    if allowed_tfs and tf not in allowed_tfs:
        log.info(f"⛔ {symbol} {tf} zaman dilimi filtreli.")
        return False
    return True

def winrate():
    t = stats["total"]
    return round(stats["wins"] / t * 100, 2) if t else 0.0

async def handle_signal(signal: dict):
    action   = signal.get("action", "").upper()
    raw_sym  = signal.get("symbol", "")
    tf       = str(signal.get("timeframe", ""))
    sl_price = float(signal.get("sl", 0))
    tp_price = float(signal.get("tp", 0))
    symbol = raw_sym.upper().replace(".PUSDT", "USDT").replace(".P", "")
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"

    log.info(f"📡 Sinyal → {action} {symbol} TF={tf}")

    if not is_allowed(symbol, tf): return
    if action not in ("BUY", "SELL"): return
    if len(open_trades) >= MAX_OPEN_TRADES:
        log.info("⚠️  Max işlem doldu, atlandı."); return
    if any(t["symbol"] == symbol for t in open_trades):
        log.info(f"⚠️  {symbol} zaten açık."); return

    balance   = await get_usdt_balance()
    price     = await get_price(symbol)
    risk_usdt = balance * (RISK_PERCENT / 100)
    qty       = round((risk_usdt * LEVERAGE) / price, 3)

    log.info(f"💰 Bakiye: {balance:.2f} | Risk: {risk_usdt:.2f} | Qty: {qty}")

    await set_leverage(symbol)
    order = await place_futures_order(symbol, action, qty, tp_price, sl_price)
    if not order: return

    open_trades.append({
        "symbol": symbol, "action": action,
        "entry": price, "sl": sl_price, "tp": tp_price,
        "qty": qty, "opened_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M"),
    })
    stats["total"] += 1
    log.info(f"📊 Toplam:{stats['total']} Kazanç:{stats['wins']} Kayıp:{stats['losses']} Winrate:%{winrate()} Açık:{len(open_trades)}")

async def webhook_handler(request: web.Request):
    
    try:
        body = await request.json()
    except:
        return web.Response(status=400, text="Bad JSON")
    asyncio.create_task(handle_signal(body))
    return web.json_response({"status": "ok", "winrate": winrate()})

async def status_handler(request: web.Request):
    return web.json_response({
        "acik_islemler": len(open_trades),
        "islemler": open_trades,
        "istatistik": stats,
        "winrate": f"%{winrate()}",
    })

app = web.Application()
app.router.add_post("/webhook", webhook_handler)
app.router.add_get("/status", status_handler)

if __name__ == "__main__":
    log.info("🤖 Scalp Pro Futures Bot başlatılıyor...")
    log.info(f"   Kaldıraç: {LEVERAGE}x Cross | Risk: %{RISK_PERCENT} | Max: {MAX_OPEN_TRADES}")
    web.run_app(app, port=WEBHOOK_PORT)