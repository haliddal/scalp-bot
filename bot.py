import os, time, json, hmac, hashlib, logging, asyncio
from aiohttp import web
import aiohttp

MEXC_API_KEY = os.environ.get("MEXC_API_KEY", "")
MEXC_SECRET_KEY = os.environ.get("MEXC_SECRET_KEY", "")

BASE = "https://contract.mexc.com"
PORT = int(os.environ.get("PORT", 8080))

MAX_OPEN_TRADES = 5

BOT_CONFIG = {
    ("BTCUSDT", "3"):  {"leverage": 10, "risk": 3.5},
    ("BTCUSDT", "5"):  {"leverage": 15, "risk": 6.0},
    ("ETHUSDT", "5"):  {"leverage": 15, "risk": 3.8},
    ("PTBUSDT", "5"):  {"leverage": 10, "risk": 6.0},
    ("HYPEUSDT", "3"): {"leverage": 10, "risk": 5.0},
    ("APEUSDT", "3"):  {"leverage": 10, "risk": 5.0},
}

open_trades = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("MEXC-BOT-V2")


def contract_symbol(symbol):
    symbol = symbol.upper().replace(".P", "")
    if symbol.endswith("USDT"):
        return symbol.replace("USDT", "_USDT")
    return symbol


def sign_body(body):
    ts = str(int(time.time() * 1000))
    body_str = json.dumps(body, separators=(",", ":"))
    sign_str = MEXC_API_KEY + ts + body_str
    signature = hmac.new(MEXC_SECRET_KEY.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    return ts, signature


async def mexc_post(path, body):
    ts, signature = sign_body(body)
    headers = {
        "ApiKey": MEXC_API_KEY,
        "Request-Time": ts,
        "Signature": signature,
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(BASE + path, data=json.dumps(body, separators=(",", ":")), headers=headers) as r:
            text = await r.text()
            log.info(f"MEXC RESPONSE [{r.status}]: {text}")

            try:
                return json.loads(text)
            except Exception:
                return {"success": False, "code": "NON_JSON", "message": text[:300]}


async def mexc_get(path):
    ts = str(int(time.time() * 1000))
    sign_str = MEXC_API_KEY + ts
    signature = hmac.new(MEXC_SECRET_KEY.encode(), sign_str.encode(), hashlib.sha256).hexdigest()

    headers = {
        "ApiKey": MEXC_API_KEY,
        "Request-Time": ts,
        "Signature": signature,
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(BASE + path, headers=headers) as r:
            text = await r.text()
            log.info(f"MEXC GET [{r.status}]: {text}")

            try:
                return json.loads(text)
            except Exception:
                return {"success": False, "code": "NON_JSON", "message": text[:300]}


async def get_balance():
    data = await mexc_get("/api/v1/private/account/assets")
    if not data.get("success"):
        return 0.0

    for item in data.get("data", []):
        if item.get("currency") == "USDT":
            return float(item.get("availableBalance", 0))

    return 0.0


async def set_leverage(symbol, leverage):
    body = {
        "symbol": contract_symbol(symbol),
        "leverage": leverage,
        "openType": 2
    }
    return await mexc_post("/api/v1/private/position/change_leverage", body)


async def place_order(symbol, action, price, leverage, risk_percent):
    balance = await get_balance()

    if balance <= 0:
        log.error("Bakiye 0 görünüyor. API yetkisi, futures hesabı veya whitelist kontrol edilmeli.")
        return None

    margin_usdt = balance * (risk_percent / 100)
    position_usdt = margin_usdt * leverage
    qty = round(position_usdt / price, 3)

    if qty <= 0:
        log.error(f"Qty 0 çıktı. Balance={balance}, Price={price}")
        return None

    side = 1 if action == "BUY" else 3

    body = {
        "symbol": contract_symbol(symbol),
        "price": 0,
        "vol": qty,
        "side": side,
        "type": 5,
        "openType": 2,
        "leverage": leverage
    }

    log.info(f"ORDER → {action} {symbol} | qty={qty} | lev={leverage}x | risk=%{risk_percent}")
    return await mexc_post("/api/v1/private/order/create", body)


async def handle_signal(data):
    action = str(data.get("action", "")).upper()
    symbol = str(data.get("symbol", "")).upper().replace(".P", "")
    timeframe = str(data.get("timeframe", ""))
    price = float(data.get("price", 0))

    if action not in ["BUY", "SELL"]:
        log.warning(f"Geçersiz action: {action}")
        return

    if price <= 0:
        log.warning(f"Geçersiz price: {price}")
        return

    config = BOT_CONFIG.get((symbol, timeframe))

    if not config:
        log.warning(f"Bu bot listede yok: {symbol} {timeframe}m")
        return

    if len(open_trades) >= MAX_OPEN_TRADES:
        log.warning("Max 5 açık işlem dolu, sinyal atlandı.")
        return

    if symbol in open_trades:
        log.warning(f"{symbol} zaten açık görünüyor, tekrar açılmadı.")
        return

    await set_leverage(symbol, config["leverage"])
    order = await place_order(symbol, action, price, config["leverage"], config["risk"])

    if order and order.get("success"):
        open_trades[symbol] = {
            "action": action,
            "price": price,
            "timeframe": timeframe,
            "leverage": config["leverage"],
            "risk": config["risk"],
            "time": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        log.info(f"✅ İşlem açıldı: {symbol}")
    else:
        log.error(f"❌ İşlem açılamadı: {order}")


async def webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Bad JSON"}, status=400)

    log.info(f"WEBHOOK GELDİ: {data}")
    asyncio.create_task(handle_signal(data))

    return web.json_response({"ok": True, "received": data})


async def status(request):
    return web.json_response({
        "status": "running",
        "max_open_trades": MAX_OPEN_TRADES,
        "open_trades": open_trades,
        "configs": BOT_CONFIG
    })


app = web.Application()
app.router.add_get("/", status)
app.router.add_get("/status", status)
app.router.add_post("/webhook", webhook)

if __name__ == "__main__":
    log.info("MEXC BOT V2 başlıyor...")
    log.info(f"Max açık işlem: {MAX_OPEN_TRADES}")
    web.run_app(app, host="0.0.0.0", port=PORT)
