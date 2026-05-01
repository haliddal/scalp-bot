import os, time, json, hmac, hashlib, logging, asyncio, math
from aiohttp import web
import aiohttp

MEXC_API_KEY = os.environ.get("MEXC_API_KEY", "")
MEXC_SECRET_KEY = os.environ.get("MEXC_SECRET_KEY", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

BASE = "https://contract.mexc.com"
PORT = int(os.environ.get("PORT", 8080))

MAX_OPEN_TRADES = 5

BOT_CONFIG = {
    "BTCUSDT_3":  {"leverage": 10, "risk": 3.5},
    "BTCUSDT_5":  {"leverage": 15, "risk": 6.0},
    "ETHUSDT_5":  {"leverage": 15, "risk": 3.8},
    "PTBUSDT_5":  {"leverage": 10, "risk": 6.0},
    "HYPEUSDT_3": {"leverage": 10, "risk": 5.0},
    "APEUSDT_3":  {"leverage": 10, "risk": 5.0},
}

open_trades = {}
contract_cache = {}

signal_queue = asyncio.Queue()
mexc_lock = asyncio.Lock()
last_mexc_request_time = 0.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("MEXC-BOT-V2")


def normalize_symbol(symbol):
    return str(symbol).upper().replace(".P", "").replace("_", "")


def contract_symbol(symbol):
    symbol = normalize_symbol(symbol)
    if symbol.endswith("USDT"):
        return symbol.replace("USDT", "_USDT")
    return symbol


def sign_body(body):
    ts = str(int(time.time() * 1000))
    body_str = json.dumps(body, separators=(",", ":"))
    sign_str = MEXC_API_KEY + ts + body_str
    signature = hmac.new(MEXC_SECRET_KEY.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    return ts, signature


def sign_get():
    ts = str(int(time.time() * 1000))
    sign_str = MEXC_API_KEY + ts
    signature = hmac.new(MEXC_SECRET_KEY.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    return ts, signature


async def mexc_wait():
    global last_mexc_request_time

    async with mexc_lock:
        now = time.time()
        wait_time = 1.0 - (now - last_mexc_request_time)

        if wait_time > 0:
            await asyncio.sleep(wait_time)

        last_mexc_request_time = time.time()


async def send_telegram_text(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram token/chat_id eksik.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text
            }, timeout=10) as r:
                resp_text = await r.text()
                log.info(f"TELEGRAM RESPONSE [{r.status}]: {resp_text}")

    except Exception as e:
        log.error(f"Telegram send error: {e}")


async def send_telegram_signal(data):
    action = str(data.get("action", "UNKNOWN")).upper()
    symbol = normalize_symbol(data.get("symbol", "UNKNOWN"))
    timeframe = str(data.get("timeframe", "UNKNOWN"))

    entry = data.get("entry", data.get("price", "UNKNOWN"))
    price = data.get("price", entry)
    sl = data.get("sl", "UNKNOWN")
    tp = data.get("tp", "UNKNOWN")
    rr = data.get("rr", "UNKNOWN")
    mode = data.get("mode", "")

    mode_line = f"Mode: {mode}\n" if mode else ""

    text = f"""🚨 NEW SIGNAL

Symbol: {symbol}
Side: {action}
Timeframe: {timeframe}m
{mode_line}
Entry: {entry}
Price: {price}
SL: {sl}
TP: {tp}
RR: {rr}

MEXC auto order mode is active.
"""

    await send_telegram_text(text)


async def mexc_public_get(path):
    await mexc_wait()

    async with aiohttp.ClientSession() as session:
        async with session.get(BASE + path) as r:
            text = await r.text()
            log.info(f"MEXC PUBLIC GET {path} [{r.status}]: {text}")

            try:
                return json.loads(text)
            except Exception:
                return {"success": False, "code": "NON_JSON", "message": text[:300]}


async def mexc_post(path, body):
    await mexc_wait()

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
            log.info(f"MEXC POST {path} [{r.status}]: {text}")

            try:
                return json.loads(text)
            except Exception:
                return {"success": False, "code": "NON_JSON", "message": text[:300]}


async def mexc_get(path):
    await mexc_wait()

    ts, signature = sign_get()
    headers = {
        "ApiKey": MEXC_API_KEY,
        "Request-Time": ts,
        "Signature": signature,
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(BASE + path, headers=headers) as r:
            text = await r.text()
            log.info(f"MEXC GET {path} [{r.status}]: {text}")

            try:
                return json.loads(text)
            except Exception:
                return {"success": False, "code": "NON_JSON", "message": text[:300]}


async def get_contract_info(symbol):
    csymbol = contract_symbol(symbol)

    if csymbol in contract_cache:
        return contract_cache[csymbol]

    data = await mexc_public_get(f"/api/v1/contract/detail/{csymbol}")

    if not data.get("success"):
        log.error(f"Contract detail alınamadı: {symbol} -> {data}")
        return None

    info = data.get("data")

    if not info:
        log.error(f"Contract detail boş geldi: {symbol} -> {data}")
        return None

    contract_cache[csymbol] = info
    return info


def floor_to_step(value, step):
    if step <= 0:
        return value
    return math.floor(value / step) * step


def format_vol(value, vol_scale):
    if vol_scale <= 0:
        return int(value)
    return round(value, vol_scale)


async def calculate_contract_vol(symbol, price, leverage, risk_percent):
    balance = await get_balance()

    if balance <= 0:
        log.error("Bakiye 0 görünüyor. API yetkisi, futures hesabı veya whitelist kontrol edilmeli.")
        return None

    info = await get_contract_info(symbol)

    if not info:
        return None

    contract_size = float(info.get("contractSize", 0))
    min_vol = float(info.get("minVol", 1))
    max_vol = float(info.get("maxVol", 999999999))
    vol_unit = float(info.get("volUnit", 1))
    vol_scale = int(info.get("volScale", 0))

    if contract_size <= 0:
        log.error(f"Geçersiz contractSize: {symbol} -> {contract_size}")
        return None

    margin_usdt = balance * (risk_percent / 100)
    position_usdt = margin_usdt * leverage

    raw_vol = position_usdt / (price * contract_size)
    vol = floor_to_step(raw_vol, vol_unit)

    if vol < min_vol:
        vol = min_vol

    if vol > max_vol:
        vol = max_vol

    vol = format_vol(vol, vol_scale)

    est_notional = float(vol) * contract_size * price

    log.info(
        f"VOL CALC → {symbol} | balance={balance} | risk=%{risk_percent} | "
        f"lev={leverage} | price={price} | contractSize={contract_size} | "
        f"rawVol={raw_vol} | finalVol={vol} | estNotional={est_notional}"
    )

    return {
        "vol": vol,
        "balance": balance,
        "margin_usdt": margin_usdt,
        "position_usdt": position_usdt,
        "contract_size": contract_size,
        "est_notional": est_notional
    }


async def get_balance():
    data = await mexc_get("/api/v1/private/account/assets")
    if not data.get("success"):
        return 0.0

    for item in data.get("data", []):
        if item.get("currency") == "USDT":
            return float(item.get("availableBalance", 0))

    return 0.0


async def get_open_positions(symbol=None):
    if symbol:
        path = f"/api/v1/private/position/open_positions?symbol={contract_symbol(symbol)}"
    else:
        path = "/api/v1/private/position/open_positions"

    data = await mexc_get(path)

    if not data.get("success"):
        return []

    positions = data.get("data", [])
    result = []

    for p in positions:
        try:
            hold_vol = float(p.get("holdVol", 0))
            state = int(p.get("state", 0))
        except Exception:
            hold_vol = 0
            state = 0

        if state == 1 and hold_vol > 0:
            result.append(p)

    return result


async def find_position(symbol, action):
    wanted_symbol = contract_symbol(symbol)
    wanted_type = 1 if action == "BUY" else 2

    positions = await get_open_positions(symbol)

    for p in positions:
        if p.get("symbol") == wanted_symbol and int(p.get("positionType", 0)) == wanted_type:
            return p

    return None


async def wait_for_position(symbol, action, retries=8):
    for _ in range(retries):
        pos = await find_position(symbol, action)
        if pos:
            return pos
        await asyncio.sleep(1.0)

    return None


async def set_leverage(symbol, leverage):
    body = {
        "symbol": contract_symbol(symbol),
        "leverage": leverage,
        "openType": 2
    }

    log.info(f"LEVERAGE → {symbol} | lev={leverage}x")
    return await mexc_post("/api/v1/private/position/change_leverage", body)


async def place_entry_order(symbol, action, price, leverage, risk_percent):
    vol_info = await calculate_contract_vol(symbol, price, leverage, risk_percent)

    if not vol_info:
        return None

    vol = vol_info["vol"]

    if float(vol) <= 0:
        log.error(f"Vol 0 çıktı. Symbol={symbol}, Price={price}, vol_info={vol_info}")
        return None

    side = 1 if action == "BUY" else 3

    body = {
        "symbol": contract_symbol(symbol),
        "price": 0,
        "vol": vol,
        "side": side,
        "type": 5,
        "openType": 2,
        "leverage": leverage
    }

    log.info(
        f"ENTRY ORDER → {action} {symbol} | vol={vol} | "
        f"lev={leverage}x | risk=%{risk_percent}"
    )

    order = await mexc_post("/api/v1/private/order/create", body)

    return {
        "response": order,
        "vol_info": vol_info
    }


async def place_tpsl(symbol, position, tp, sl):
    position_id = position.get("positionId")
    hold_vol = float(position.get("holdVol", 0))

    if not position_id or hold_vol <= 0:
        return {"success": False, "message": "positionId veya holdVol yok"}

    body = {
        "positionId": int(position_id),
        "vol": hold_vol,

        "stopLossPrice": float(sl),
        "takeProfitPrice": float(tp),

        "lossTrend": 1,
        "profitTrend": 1,

        "priceProtect": 0,
        "profitLossVolType": "SAME",
        "volType": 2,

        "takeProfitReverse": 2,
        "stopLossReverse": 2,

        "takeProfitType": 0,
        "stopLossType": 0,
        "takeProfitOrderPrice": 0,
        "stopLossOrderPrice": 0
    }

    log.info(f"TP/SL PLACE → {symbol} | positionId={position_id} | vol={hold_vol} | TP={tp} | SL={sl}")
    return await mexc_post("/api/v1/private/stoporder/place", body)


async def close_position_market(symbol, position):
    position_id = position.get("positionId")
    hold_vol = float(position.get("holdVol", 0))
    position_type = int(position.get("positionType", 0))

    if not position_id or hold_vol <= 0:
        return {"success": False, "message": "Kapatılacak pozisyon bulunamadı"}

    if position_type == 1:
        close_side = 4
    elif position_type == 2:
        close_side = 2
    else:
        return {"success": False, "message": f"Geçersiz positionType: {position_type}"}

    body = {
        "symbol": contract_symbol(symbol),
        "price": 0,
        "vol": hold_vol,
        "side": close_side,
        "type": 5,
        "openType": 2,
        "positionId": int(position_id)
    }

    log.warning(f"EMERGENCY CLOSE → {symbol} | positionId={position_id} | vol={hold_vol} | side={close_side}")
    return await mexc_post("/api/v1/private/order/create", body)


def parse_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


async def handle_signal(data):
    action = str(data.get("action", "")).upper()
    symbol = normalize_symbol(data.get("symbol", ""))
    timeframe = str(data.get("timeframe", ""))

    price = parse_float(data.get("price", data.get("entry", 0)))
    entry = parse_float(data.get("entry", price))
    sl = parse_float(data.get("sl", 0))
    tp = parse_float(data.get("tp", 0))

    if action not in ["BUY", "SELL"]:
        log.warning(f"Geçersiz action: {action}")
        return

    if price <= 0:
        log.warning(f"Geçersiz price/entry: {data}")
        return

    if sl <= 0 or tp <= 0:
        msg = f"""⚠️ ORDER SKIPPED

Symbol: {symbol}
Side: {action}
Reason: TP veya SL yok.

Entry: {entry}
SL: {sl}
TP: {tp}

Güvenlik için işlem açılmadı.
"""
        log.warning(msg)
        await send_telegram_text(msg)
        return

    if action == "BUY":
        if not (sl < price < tp):
            msg = f"""⚠️ ORDER SKIPPED

Symbol: {symbol}
Side: BUY
Reason: TP/SL yönü hatalı.

Entry: {price}
SL: {sl}
TP: {tp}

BUY için SL entry altında, TP entry üstünde olmalı.
"""
            log.warning(msg)
            await send_telegram_text(msg)
            return

    if action == "SELL":
        if not (tp < price < sl):
            msg = f"""⚠️ ORDER SKIPPED

Symbol: {symbol}
Side: SELL
Reason: TP/SL yönü hatalı.

Entry: {price}
SL: {sl}
TP: {tp}

SELL için TP entry altında, SL entry üstünde olmalı.
"""
            log.warning(msg)
            await send_telegram_text(msg)
            return

    config = BOT_CONFIG.get(f"{symbol}_{timeframe}")

    if not config:
        msg = f"""⚠️ ORDER SKIPPED

Symbol: {symbol}
Timeframe: {timeframe}m
Reason: BOT_CONFIG içinde bu coin/timeframe yok.

Bot otomatik işlem açmadı.
"""
        log.warning(msg)
        await send_telegram_text(msg)
        return

    all_positions = await get_open_positions()

    if len(all_positions) >= MAX_OPEN_TRADES:
        msg = f"""⚠️ ORDER SKIPPED

Symbol: {symbol}
Reason: Max {MAX_OPEN_TRADES} açık işlem dolu.
"""
        log.warning(msg)
        await send_telegram_text(msg)
        return

    existing_same_side = await find_position(symbol, action)

    if existing_same_side:
        msg = f"""⚠️ ORDER SKIPPED

Symbol: {symbol}
Side: {action}
Reason: Bu yönde zaten açık pozisyon var.
"""
        log.warning(msg)
        await send_telegram_text(msg)
        return

    await set_leverage(symbol, config["leverage"])

    entry_result = await place_entry_order(symbol, action, price, config["leverage"], config["risk"])
    order = entry_result.get("response") if entry_result else None
    vol_info = entry_result.get("vol_info") if entry_result else None

    if not order or not order.get("success"):
        msg = f"""❌ ENTRY FAILED

Symbol: {symbol}
Side: {action}

MEXC response:
{order}

Vol info:
{vol_info}
"""
        log.error(msg)
        await send_telegram_text(msg)
        return

    await send_telegram_text(f"""✅ ENTRY OPENED

Symbol: {symbol}
Side: {action}
Entry: {price}

Vol info:
{vol_info}

Şimdi TP/SL kuruluyor...
""")

    position = await wait_for_position(symbol, action)

    if not position:
        msg = f"""🚨 WARNING

Symbol: {symbol}
Side: {action}

Entry başarılı göründü ama açık pozisyon bulunamadı.
MEXC ekranından manuel kontrol et.
"""
        log.error(msg)
        await send_telegram_text(msg)
        return

    tpsl = await place_tpsl(symbol, position, tp, sl)

    if tpsl and tpsl.get("success"):
        open_trades[symbol] = {
            "action": action,
            "price": price,
            "sl": sl,
            "tp": tp,
            "timeframe": timeframe,
            "leverage": config["leverage"],
            "risk": config["risk"],
            "positionId": position.get("positionId"),
            "time": time.strftime("%Y-%m-%d %H:%M:%S")
        }

        msg = f"""✅ ORDER SAFE

Symbol: {symbol}
Side: {action}
Timeframe: {timeframe}m

Entry: {price}
SL: {sl}
TP: {tp}

TP/SL başarıyla kuruldu.
"""
        log.info(msg)
        await send_telegram_text(msg)
        return

    msg = f"""🚨 TP/SL FAILED

Symbol: {symbol}
Side: {action}
Entry: {price}
SL: {sl}
TP: {tp}

TP/SL kurulamadı. Pozisyon güvenlik için kapatılıyor.

MEXC TP/SL response:
{tpsl}
"""
    log.error(msg)
    await send_telegram_text(msg)

    close_result = await close_position_market(symbol, position)

    if close_result and close_result.get("success"):
        await send_telegram_text(f"""✅ EMERGENCY CLOSE DONE

Symbol: {symbol}
Side: {action}

TP/SL kurulamadığı için pozisyon marketten kapatıldı.
""")
    else:
        await send_telegram_text(f"""🚨 EMERGENCY CLOSE FAILED

Symbol: {symbol}
Side: {action}

TP/SL kurulamadı ve pozisyon otomatik kapatılamadı.
MEXC'ye girip hemen manuel kontrol et.

Close response:
{close_result}
""")


async def signal_worker():
    log.info("Signal worker başladı.")

    while True:
        data = await signal_queue.get()

        try:
            log.info(f"SIRADAKİ SİNYAL İŞLENİYOR: {data}")
            await handle_signal(data)
        except Exception as e:
            log.error(f"Signal worker error: {e}")
            await send_telegram_text(f"🚨 BOT ERROR\n\n{e}")
        finally:
            signal_queue.task_done()

        await asyncio.sleep(1.0)


async def webhook(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Bad JSON"}, status=400)

    log.info(f"WEBHOOK GELDİ: {data}")

    asyncio.create_task(send_telegram_signal(data))
    await signal_queue.put(data)

    return web.json_response({"ok": True, "queued": True, "received": data})


async def status(request):
    positions = await get_open_positions()

    return web.json_response({
        "status": "running",
        "max_open_trades": MAX_OPEN_TRADES,
        "queue_size": signal_queue.qsize(),
        "mexc_open_positions_count": len(positions),
        "mexc_open_positions": positions,
        "local_open_trades": open_trades,
        "configs": BOT_CONFIG,
        "contract_cache_keys": list(contract_cache.keys())
    })


async def on_startup(app):
    app["signal_worker"] = asyncio.create_task(signal_worker())


async def on_cleanup(app):
    app["signal_worker"].cancel()


app = web.Application()
app.router.add_get("/", status)
app.router.add_get("/status", status)
app.router.add_post("/webhook", webhook)

app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    log.info("MEXC BOT V2 başlıyor...")
    log.info(f"Max açık işlem: {MAX_OPEN_TRADES}")
    web.run_app(app, host="0.0.0.0", port=PORT)
