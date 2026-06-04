"""
api.py — CrabVPN Web API (Admin Dashboard + User Portal)
Run via: python main.py  (or directly)
"""
import asyncio
import json
import logging
import os
import secrets
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from models import (
    Server, Package,
    create_order, create_payment, delete_package, delete_server, expire_old_payments,
    get_all_servers, get_conn, get_customer_by_token, get_package,
    get_packages_for_server, get_server, get_setting, init_db, log_event,
    now_iso, set_setting, upsert_package, upsert_server,
    get_active_orders_for_customer, get_payment_by_sms_amount, release_sms_code,
    alloc_sms_code, PAYMENT_EXPIRE_MIN,
)
import sms_parser

load_dotenv()

logger            = logging.getLogger(__name__)
DASHBOARD_SECRET  = os.getenv("DASHBOARD_SECRET", "admin")
WEBHOOK_API_PORT  = int(os.getenv("WEBHOOK_API_PORT", "8765"))
ADMIN_CHAT_ID     = int(os.getenv("ADMIN_CHAT_ID", "0"))
PORTAL_BASE_URL   = os.getenv("PORTAL_BASE_URL", "http://localhost:8765")

BASE_DIR = Path(__file__).resolve().parent

# ── Session store ──────────────────────────────────────────────────────────────
_otp_store: dict = {}
_sessions:  dict = {}


def _valid_session(token: str) -> bool:
    exp = _sessions.get(token)
    if not exp or now_iso() > exp:
        _sessions.pop(token, None)
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# PANEL HELPER (delegates to xpanel module — supports SSH and Xray)
# ══════════════════════════════════════════════════════════════════════════════

import xpanel


# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

async def handle_otp_request(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    if data.get("secret") != DASHBOARD_SECRET:
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    otp = str(secrets.randbelow(900000) + 100000)
    exp = (datetime.utcnow() + timedelta(minutes=5)).isoformat(timespec="seconds")
    _otp_store[otp] = exp
    bot_app = request.app.get("bot_app")
    if ADMIN_CHAT_ID and bot_app:
        try:
            await bot_app.bot.send_message(ADMIN_CHAT_ID,
                f"🔐 کد ورود داشبورد:\n\n`{otp}`\n\nاعتبار: ۵ دقیقه", parse_mode="Markdown")
        except Exception:
            pass
    return web.json_response({"ok": True})


async def handle_otp_verify(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    otp = str(data.get("otp", "")).strip()
    exp = _otp_store.get(otp)
    if not exp or now_iso() > exp:
        _otp_store.pop(otp, None)
        return web.json_response({"ok": False, "error": "OTP نامعتبر"}, status=401)
    _otp_store.pop(otp, None)
    token = secrets.token_hex(32)
    _sessions[token] = (datetime.utcnow() + timedelta(hours=8)).isoformat(timespec="seconds")
    return web.json_response({"ok": True, "token": token})


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — SERVERS CRUD
# ══════════════════════════════════════════════════════════════════════════════

async def handle_list_servers(request: web.Request) -> web.Response:
    if not _valid_session(request.query.get("token", "")):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    servers = get_all_servers(active_only=False)
    return web.json_response({"ok": True, "servers": [s.to_dict() for s in servers]})


async def handle_upsert_server(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    if not _valid_session(data.get("token", "")):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    stype = data.get("server_type", "ssh")
    if stype == "xray":
        required = ["name", "xpanel_url", "xpanel_username", "xpanel_password", "xpanel_inbound_id"]
    else:
        required = ["name", "xpanel_url", "xpanel_token"]
    for f in required:
        if not data.get(f):
            return web.json_response({"ok": False, "error": f"{f} required"}, status=400)
    sid = upsert_server(data)
    log_event("server_upsert", {"id": sid, "name": data["name"]})
    return web.json_response({"ok": True, "id": sid})


async def handle_delete_server(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    if not _valid_session(data.get("token", "")):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    ok = delete_server(int(data.get("id", 0)))
    if not ok:
        return web.json_response({"ok": False, "error": "سرور با سفارش فعال حذف نمیشه"}, status=400)
    log_event("server_delete", {"id": data.get("id")})
    return web.json_response({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — PACKAGES CRUD
# ══════════════════════════════════════════════════════════════════════════════

async def handle_list_packages(request: web.Request) -> web.Response:
    if not _valid_session(request.query.get("token", "")):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    server_id = int(request.query.get("server_id", 0))
    pkgs = get_packages_for_server(server_id, active_only=False) if server_id else []
    return web.json_response({"ok": True, "packages": [p.to_dict() for p in pkgs]})


async def handle_upsert_package(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    if not _valid_session(data.get("token", "")):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    pid = upsert_package(data)
    log_event("package_upsert", {"id": pid})
    return web.json_response({"ok": True, "id": pid})


async def handle_delete_package(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    if not _valid_session(data.get("token", "")):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    ok, err = delete_package(int(data.get("id", 0)))
    if not ok:
        return web.json_response({"ok": False, "error": err}, status=400)
    return web.json_response({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — TOGGLE SERVER SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

async def handle_server_toggle(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    if not _valid_session(data.get("token", "")):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    server_id = int(data.get("server_id", 0))
    key       = data.get("key", "")
    if key not in ("sales_open", "renewal_open", "free_trial", "is_active"):
        return web.json_response({"ok": False, "error": "invalid key"}, status=400)
    value = 1 if data.get("value") else 0
    with closing(get_conn()) as conn:
        conn.execute(f"UPDATE servers SET {key}=?,updated_at=? WHERE id=?", (value, now_iso(), server_id))
        conn.commit()
    log_event("server_toggle", {"server_id": server_id, "key": key, "value": value})
    return web.json_response({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN — DASHBOARD DATA
# ══════════════════════════════════════════════════════════════════════════════

async def handle_dashboard_data(request: web.Request) -> web.Response:
    if not _valid_session(request.query.get("token", "")):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    with closing(get_conn()) as conn:
        totals = conn.execute("""SELECT
            (SELECT COUNT(*) FROM customers) AS total_customers,
            (SELECT COUNT(*) FROM orders WHERE status='active') AS active_orders,
            (SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved') AS total_revenue,
            (SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved'
             AND strftime('%Y-%m',created_at)=strftime('%Y-%m','now')) AS this_month_revenue
        """).fetchone()

        monthly = conn.execute("""SELECT strftime('%Y-%m',created_at) AS month,
            SUM(amount) AS revenue, COUNT(*) AS count FROM payments WHERE status='approved'
            GROUP BY month ORDER BY month DESC LIMIT 12""").fetchall()

        daily = conn.execute("""SELECT strftime('%Y-%m-%d',created_at) AS day,
            SUM(amount) AS revenue, COUNT(*) AS count FROM payments WHERE status='approved'
            AND created_at >= date('now','-30 days')
            GROUP BY day ORDER BY day DESC""").fetchall()

        users = conn.execute("""SELECT c.full_name,c.telegram_username,c.phone,c.portal_token,
            o.xpanel_username,o.server_id,o.id AS order_id,
            pkg.name AS pkg_name,pkg.traffic_amount,pkg.traffic_unit
            FROM orders o JOIN customers c ON c.id=o.customer_id
            JOIN packages pkg ON pkg.id=o.package_id
            WHERE o.status='active' AND o.xpanel_username IS NOT NULL ORDER BY o.id DESC""").fetchall()

        pending = conn.execute("""SELECT p.id,p.amount,p.payer_name,p.status,p.expires_at,p.created_at,
            p.server_id,c.full_name,c.telegram_username,pkg.name AS pkg_name
            FROM payments p JOIN orders o ON o.id=p.order_id
            JOIN customers c ON c.id=o.customer_id JOIN packages pkg ON pkg.id=o.package_id
            ORDER BY p.id DESC LIMIT 100""").fetchall()

    servers = {s.id: s for s in get_all_servers(active_only=False)}

    # build user list from DB only — no live xpanel calls
    def _get_user_info(u):
        sv = servers.get(u["server_id"])
        if not sv:
            return None
        t = float(u["traffic_amount"])
        total_mb = t if u["traffic_unit"] == "mb" else t * 1024
        return {
            "name":         u["full_name"],
            "username":     u["telegram_username"] or "—",
            "phone":        u["phone"] or "—",
            "portal_token": u["portal_token"] or "—",
            "xpanel_user":  u["xpanel_username"],
            "pkg":          u["pkg_name"],
            "server_id":    u["server_id"],
            "server_name":  sv.name,
            "server_flag":  sv.flag,
            "expdate":      "—",
            "used_mb":      0,
            "total_mb":     total_mb,
            "remaining_mb": total_mb,
        }

    users_info = [r for r in (_get_user_info(u) for u in users) if r]

    return web.json_response({
        "ok": True,
        "totals":        dict(totals),
        "monthly":       [dict(r) for r in monthly],
        "daily":         [dict(r) for r in daily],
        "users":         users_info,
        "pending":       [dict(r) for r in pending],
        "servers":       [s.to_dict() for s in servers.values()],
        "online_counts": {},
    })


_BOOL_KEYS = {"link_enabled", "payment_donate_enabled", "payment_tetra_enabled", "payment_card_enabled"}
_STR_KEYS  = {"donation_url", "tetra_api_key", "portal_base_url", "card_number", "card_owner"}


def _get_portal_base_url() -> str:
    """DB setting overrides env var so admin can change domain/port without restart."""
    return get_setting("portal_base_url", "") or PORTAL_BASE_URL


async def handle_get_settings(request: web.Request) -> web.Response:
    if not _valid_session(request.query.get("token", "")):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    base = _get_portal_base_url()
    return web.json_response({
        "ok": True,
        "settings": {
            "link_enabled":            get_setting("link_enabled", "1") == "1",
            "donation_url":            get_setting("donation_url", ""),
            "payment_donate_enabled":  get_setting("payment_donate_enabled", "1") == "1",
            "payment_card_enabled":    get_setting("payment_card_enabled",  "0") == "1",
            "payment_tetra_enabled":   get_setting("payment_tetra_enabled", "0") == "1",
            "card_number":             get_setting("card_number", ""),
            "card_owner":              get_setting("card_owner",  ""),
            "tetra_api_key":           get_setting("tetra_api_key", ""),
            "portal_base_url":         get_setting("portal_base_url", ""),
            "callback_url":            f"{base}/api/payment/callback",
        },
    })


async def handle_set_setting(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    if not _valid_session(data.get("token", "")):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    key   = data.get("key", "")
    value = data.get("value")
    if key in _BOOL_KEYS:
        set_setting(key, "1" if value else "0")
    elif key in _STR_KEYS:
        set_setting(key, str(value or "").strip())
    else:
        return web.json_response({"ok": False, "error": "invalid key"}, status=400)
    log_event("setting_change", {"key": key, "value": value})
    return web.json_response({"ok": True})


async def handle_tetra_callback(request: web.Request) -> web.Response:
    """Receives Tetra98 payment callback, verifies with Tetra, then provisions."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)

    status    = str(data.get("status", ""))
    authority = str(data.get("authority") or data.get("Authority", ""))
    amount    = data.get("amount") or data.get("Amount")

    if status != "100" or not authority:
        logger.warning("tetra callback: bad status=%s authority=%s", status, authority)
        return web.json_response({"ok": False, "error": "payment failed or cancelled"}, status=200)

    from models import get_payment_by_authority
    pay = get_payment_by_authority(authority)
    if not pay:
        logger.warning("tetra callback: unknown authority %s", authority)
        return web.json_response({"ok": False, "error": "unknown authority"}, status=404)

    api_key = get_setting("tetra_api_key", "")
    try:
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as session:
            async with session.post(
                "https://tetra98.com/api/verify_order",
                json={"ApiKey": api_key, "Authority": authority, "Amount": int(amount or 0)},
                timeout=_aiohttp.ClientTimeout(total=15),
            ) as resp:
                verify = await resp.json(content_type=None)
        if str(verify.get("status")) != "100":
            raise RuntimeError(f"verify failed: {verify}")
    except Exception as exc:
        logger.error("tetra verify error: %s", exc)
        return web.json_response({"ok": False, "error": "verify failed"}, status=200)

    bot_app = request.app.get("bot_app")
    if bot_app:
        try:
            await bot_app.bot_data  # ensure bot is running
            from bot import do_provision
            await do_provision(bot_app.bot, pay)
        except Exception as exc:
            logger.error("tetra provision error: %s", exc)

    log_event("tetra_payment_verified", {"authority": authority, "payment_id": pay["id"]})
    return web.json_response({"ok": True})


async def handle_manual_approve(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    if not _valid_session(data.get("token", "")):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    payment_id = int(data.get("payment_id", 0))
    with closing(get_conn()) as conn:
        pay = conn.execute(
            """SELECT p.*,o.id AS order_id,COALESCE(o.order_type,'new') AS order_type,
                      o.server_id,c.telegram_id,c.full_name
               FROM payments p JOIN orders o ON o.id=p.order_id
               JOIN customers c ON c.id=o.customer_id WHERE p.id=?""",
            (payment_id,),
        ).fetchone()
    if not pay:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    if pay["status"] == "approved":
        return web.json_response({"ok": False, "error": "already approved"}, status=400)
    bot_app = request.app.get("bot_app")
    from bot import do_provision
    await do_provision(bot_app.bot if bot_app else None, pay,
                       donor_name=pay["payer_name"] or "manual",
                       donated_amount=float(pay["amount"] or 0))
    log_event("manual_approve", {"payment_id": payment_id})
    return web.json_response({"ok": True})


async def handle_manual_traffic(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    if not _valid_session(data.get("token", "")):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    server_id = int(data.get("server_id", 0))
    username  = str(data.get("username", "")).strip()
    traffic   = int(data.get("traffic", 0))
    unit      = str(data.get("unit", "gb")).lower()
    server    = get_server(server_id)
    if not server or not username or not traffic:
        return web.json_response({"ok": False, "error": "server_id, username, traffic required"}, status=400)
    try:
        xpanel.add_traffic(server, username, traffic, unit)
        log_event("manual_traffic", {"server_id": server_id, "username": username, "traffic": traffic, "unit": unit})
        return web.json_response({"ok": True})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


# ══════════════════════════════════════════════════════════════════════════════
# USER PORTAL API
# ══════════════════════════════════════════════════════════════════════════════

async def handle_portal_data(request: web.Request) -> web.Response:
    """GET /api/portal/{token}"""
    token    = request.match_info.get("token", "")
    customer = get_customer_by_token(token)
    if not customer:
        return web.json_response({"ok": False, "error": "invalid token"}, status=404)

    tg_id    = customer["telegram_id"]
    with closing(get_conn()) as conn:
        orders = conn.execute(
            """SELECT o.*,p.name AS pkg_name,p.traffic_amount,p.traffic_unit,
                      COALESCE(p.duration_days,30) AS duration_days,
                      s.name AS server_name,s.flag,s.location
               FROM orders o JOIN packages p ON p.id=o.package_id
               JOIN servers s ON s.id=o.server_id
               WHERE o.customer_id=? AND o.status='active' AND o.xpanel_username IS NOT NULL
               ORDER BY o.id DESC""",
            (customer["id"],),
        ).fetchall()

    servers = {s.id: s for s in get_all_servers()}

    hide_config = get_setting("link_enabled", "1") != "1"

    def _enrich(o):
        sv = servers.get(o["server_id"])
        if not sv:
            return None
        t = float(o["traffic_amount"])
        total_mb = t if o["traffic_unit"] == "mb" else t * 1024
        duration_days = int(o["duration_days"] or 30)

        # estimate expiry from purchase date + package duration
        expdate = "—"
        days_left = None
        try:
            created = datetime.fromisoformat(o["created_at"].replace("Z", "").split("T")[0])
            exp_dt  = created + timedelta(days=duration_days)
            expdate = exp_dt.strftime("%Y-%m-%d")
            days_left = (exp_dt - datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)).days
        except Exception:
            pass

        can_renew_time = days_left is not None and days_left <= int(duration_days * 0.25)
        can_renew = can_renew_time  # traffic unknown until refreshed

        renew_msg = "" if can_renew else (
            f"⏳ تمدید هنوز ممکن نیست — کمتر از {int(duration_days * 0.25)} روز مانده باشه"
            + (f" (الان {days_left} روز)" if days_left is not None else "")
        )

        return {
            "order_id":        o["id"],
            "server_id":       o["server_id"],
            "server_name":     o["server_name"],
            "server_flag":     o["flag"],
            "server_location": o["location"],
            "pkg_name":        o["pkg_name"],
            "config_text":     "" if hide_config else (o["config_text"] or ""),
            "config_netmod":   "" if hide_config else (o["config_netmod"] or "" if "config_netmod" in o.keys() else ""),
            "expdate":         expdate,
            "total_mb":        total_mb,
            "used_mb":         0,
            "remaining_mb":    total_mb,
            "used_pct":        0,
            "created_at":      o["created_at"],
            "can_renew":       can_renew,
            "renew_msg":       renew_msg,
        }

    subs = [s for s in (_enrich(o) for o in orders) if s]

    return web.json_response({
        "ok": True,
        "customer": {
            "name":  customer["full_name"],
            "phone": customer["phone"] or "—",
        },
        "subscriptions": subs,
        "config_hidden": hide_config,
    })


async def handle_portal_packages(request: web.Request) -> web.Response:
    """GET /api/portal/{token}/packages/{server_id}"""
    token    = request.match_info.get("token", "")
    customer = get_customer_by_token(token)
    if not customer:
        return web.json_response({"ok": False, "error": "invalid token"}, status=404)
    server_id = int(request.match_info.get("server_id", 0))
    pkgs = get_packages_for_server(server_id, active_only=True)
    return web.json_response({"ok": True, "packages": [p.to_dict() for p in pkgs]})


async def handle_portal_initiate_payment(request: web.Request) -> web.Response:
    """POST /api/portal/{token}/payment — create renewal order + payment, return method data."""
    token    = request.match_info.get("token", "")
    customer = get_customer_by_token(token)
    if not customer:
        return web.json_response({"ok": False, "error": "invalid token"}, status=404)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "bad json"}, status=400)

    order_id   = int(data.get("order_id", 0))
    package_id = int(data.get("package_id", 0))

    with closing(get_conn()) as conn:
        existing = conn.execute(
            "SELECT * FROM orders WHERE id=? AND customer_id=? AND status='active'",
            (order_id, customer["id"]),
        ).fetchone()
    if not existing:
        return web.json_response({"ok": False, "error": "order not found"}, status=404)

    pkg = get_package(package_id)
    if not pkg or pkg.server_id != existing["server_id"]:
        return web.json_response({"ok": False, "error": "invalid package"}, status=400)

    server = get_server(existing["server_id"])
    if not server:
        return web.json_response({"ok": False, "error": "server not found"}, status=404)
    if not server.renewal_open:
        return web.json_response({"ok": False, "error": "تمدید در حال حاضر بسته است"}, status=400)

    new_order_id = create_order(customer["id"], package_id, existing["server_id"], order_type="renewal")
    payment_id   = create_payment(new_order_id, existing["server_id"], pkg.price_irr,
                                  payer_name=customer["full_name"] or "")

    with closing(get_conn()) as conn:
        pay = conn.execute("SELECT expires_at FROM payments WHERE id=?", (payment_id,)).fetchone()
    expires_at = pay["expires_at"] if pay else ""

    donate_on = get_setting("payment_donate_enabled", "1") == "1"
    card_on   = get_setting("payment_card_enabled",   "0") == "1"
    tetra_on  = get_setting("payment_tetra_enabled",  "0") == "1"

    response: dict = {
        "ok":          True,
        "payment_id":  payment_id,
        "expires_at":  expires_at,
        "amount_toman": int(pkg.price_irr or 0),
        "methods":     {},
    }

    if card_on:
        _, sms_amount = alloc_sms_code(payment_id, pkg.price_irr or 0)
        response["methods"]["card"] = {
            "card_number": get_setting("card_number", ""),
            "card_owner":  get_setting("card_owner",  ""),
            "sms_amount":  sms_amount,
        }

    if donate_on:
        response["methods"]["donate"] = {
            "donation_url": get_setting("donation_url", ""),
        }

    if tetra_on:
        api_key      = get_setting("tetra_api_key", "")
        callback_url = f"{_get_portal_base_url()}/api/payment/callback"
        try:
            import aiohttp as _aiohttp
            async with _aiohttp.ClientSession() as sess:
                async with sess.post(
                    "https://tetra98.com/api/create_order",
                    json={
                        "ApiKey":      api_key,
                        "Amount":      int(pkg.price_irr or 0),
                        "CallbackUrl": callback_url,
                        "Description": f"CrabVPN - {pkg.name}",
                    },
                    timeout=_aiohttp.ClientTimeout(total=10),
                ) as resp:
                    result = await resp.json(content_type=None)
            if str(result.get("status")) == "100":
                authority = result.get("authority") or result.get("Authority", "")
                pay_url   = result.get("url") or f"https://tetra98.com/pay/{authority}"
                with closing(get_conn()) as conn:
                    conn.execute(
                        "UPDATE payments SET authority=?,payment_method='tetra',updated_at=? WHERE id=?",
                        (authority, now_iso(), payment_id),
                    )
                    conn.commit()
                response["methods"]["tetra"] = {"url": pay_url}
            else:
                response["methods"]["tetra"] = {"error": "gateway error"}
        except Exception as exc:
            logger.error("tetra create order (portal): %s", exc)
            response["methods"]["tetra"] = {"error": str(exc)}

    # اطلاع‌رسانی به ادمین
    bot_app = request.app.get("bot_app")
    if bot_app and ADMIN_CHAT_ID:
        try:
            from telegram import InlineKeyboardMarkup, InlineKeyboardButton
            methods_label = " + ".join(response["methods"].keys())
            admin_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تأیید دستی", callback_data=f"admin_approve:{payment_id}")
            ]])
            card_info = ""
            if "card" in response["methods"]:
                sms_amt = response["methods"]["card"].get("sms_amount", 0)
                card_info = f"\n💰 sms\\_amount=`{sms_amt:,}` ریال"
            await bot_app.bot.send_message(
                ADMIN_CHAT_ID,
                f"🌐 *پورتال* — پرداخت جدید [{methods_label}]\n"
                f"📦 {pkg.name} / {pkg.price_label}\n"
                f"👤 {customer['full_name']}\n"
                f"🔢 payment\\_id=`{payment_id}`{card_info}",
                parse_mode="Markdown",
                reply_markup=admin_kb,
            )
        except Exception as exc:
            logger.warning("portal payment admin notify: %s", exc)

    log_event("portal_payment_initiated", {"payment_id": payment_id, "customer_id": customer["id"]})
    return web.json_response(response)


async def handle_portal_payment_status(request: web.Request) -> web.Response:
    """GET /api/portal/{token}/payment/{payment_id}/status"""
    token    = request.match_info.get("token", "")
    customer = get_customer_by_token(token)
    if not customer:
        return web.json_response({"ok": False, "error": "invalid token"}, status=404)
    payment_id = int(request.match_info.get("payment_id", 0))
    with closing(get_conn()) as conn:
        pay = conn.execute(
            """SELECT p.status, p.expires_at FROM payments p
               JOIN orders o ON o.id=p.order_id
               WHERE p.id=? AND o.customer_id=?""",
            (payment_id, customer["id"]),
        ).fetchone()
    if not pay:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    return web.json_response({"ok": True, "status": pay["status"], "expires_at": pay["expires_at"]})


async def handle_portal_order_stats(request: web.Request) -> web.Response:
    """GET /api/portal/{token}/stats/{order_id} — on-demand live stats from xpanel"""
    token    = request.match_info.get("token", "")
    customer = get_customer_by_token(token)
    if not customer:
        return web.json_response({"ok": False, "error": "invalid token"}, status=404)
    order_id = int(request.match_info.get("order_id", 0))
    with closing(get_conn()) as conn:
        o = conn.execute(
            """SELECT o.xpanel_username, o.server_id, o.traffic_amount, o.traffic_unit
               FROM orders o WHERE o.id=? AND o.customer_id=? AND o.status='active'""",
            (order_id, customer["id"]),
        ).fetchone()
    if not o:
        return web.json_response({"ok": False, "error": "not found"}, status=404)
    sv = next((s for s in get_all_servers() if s.id == o["server_id"]), None)
    if not sv:
        return web.json_response({"ok": False, "error": "server not found"}, status=404)

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=1) as pool:
        info = await loop.run_in_executor(pool, xpanel.get_user, sv, o["xpanel_username"])
    info = info or {}

    t = float(o["traffic_amount"])
    total_mb = float(info.get("total_mb", 0) or 0) or (t if o["traffic_unit"] == "mb" else t * 1024)
    used_mb  = float(info.get("used_mb", 0) or 0)
    remaining_mb = max(0.0, round(total_mb - used_mb, 2))
    pct = min(100, int(used_mb / total_mb * 100)) if total_mb else 0
    return web.json_response({
        "ok":           True,
        "total_mb":     total_mb,
        "used_mb":      used_mb,
        "remaining_mb": remaining_mb,
        "used_pct":     pct,
        "expdate":      info.get("expdate", "—"),
    })


# ══════════════════════════════════════════════════════════════════════════════
# STATIC HTML PAGES
# ══════════════════════════════════════════════════════════════════════════════

async def handle_dashboard_html(request: web.Request) -> web.Response:
    html_path = BASE_DIR / "dashboard.html"
    if not html_path.exists():
        return web.Response(text="dashboard.html not found", status=404)
    return web.Response(text=html_path.read_text(encoding="utf-8"), content_type="text/html")


async def handle_portal_html(request: web.Request) -> web.Response:
    html_path = BASE_DIR / "portal.html"
    if not html_path.exists():
        return web.Response(text="portal.html not found", status=404)
    return web.Response(text=html_path.read_text(encoding="utf-8"), content_type="text/html")


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ══════════════════════════════════════════════════════════════════════════════

async def _expire_notifier(bot_app) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            now = now_iso()
            with closing(get_conn()) as conn:
                rows = conn.execute(
                    "SELECT p.id,c.telegram_id FROM payments p "
                    "JOIN orders o ON o.id=p.order_id JOIN customers c ON c.id=o.customer_id "
                    "WHERE p.status='pending_review' AND p.expires_at < ?", (now,)
                ).fetchall()
            for row in rows:
                with closing(get_conn()) as conn:
                    conn.execute("UPDATE payments SET status='expired',sms_code=NULL,updated_at=? WHERE id=?", (now, row["id"]))
                    conn.commit()
                try:
                    await bot_app.bot.send_message(row["telegram_id"],
                        "⏰ مهلت پرداخت شما منقضی شد.\nبرای خرید مجدد /start بزن.")
                except Exception:
                    pass
        except Exception as e:
            logger.warning("expire notifier: %s", e)


async def _renew_notifier(bot_app) -> None:
    """هر ساعت یک بار اشتراک‌های فعال رو بررسی می‌کنه؛ اولین باری که واجد تمدید شدن پیام می‌فرسته."""
    while True:
        await asyncio.sleep(3600)
        try:
            with closing(get_conn()) as conn:
                orders = conn.execute(
                    """SELECT o.id, o.xpanel_username, o.server_id, o.package_id,
                              c.telegram_id, c.full_name,
                              p.name AS pkg_name, COALESCE(p.duration_days,30) AS duration_days,
                              s.flag, s.name AS server_name
                       FROM orders o
                       JOIN customers c ON c.id = o.customer_id
                       JOIN packages  p ON p.id = o.package_id
                       JOIN servers   s ON s.id = o.server_id
                       WHERE o.status='active' AND o.xpanel_username IS NOT NULL
                         AND o.renew_notified=0""",
                ).fetchall()

            servers = {s.id: s for s in get_all_servers()}

            def _check(o):
                sv = servers.get(o["server_id"])
                if not sv:
                    return None
                info     = xpanel.get_user(sv, o["xpanel_username"]) or {}
                total_mb = float(info.get("total_mb", 0) or 0)
                used_mb  = float(info.get("used_mb",  0) or 0)
                if total_mb < 0.0001:
                    return None
                pct           = min(100, int(used_mb / total_mb * 100))
                duration_days = int(o["duration_days"] or 30)
                can_traffic   = pct >= 75
                can_time      = False
                days_left     = None
                expdate       = info.get("expdate", "")
                if expdate and expdate != "—":
                    try:
                        exp_dt    = datetime.fromisoformat(expdate.split("T")[0])
                        days_left = (exp_dt - datetime.utcnow().replace(
                            hour=0, minute=0, second=0, microsecond=0)).days
                        can_time  = days_left <= int(duration_days * 0.25)
                    except Exception:
                        pass
                if can_traffic or can_time:
                    return {"order_id": o["id"], "telegram_id": o["telegram_id"],
                            "full_name": o["full_name"], "pkg_name": o["pkg_name"],
                            "flag": o["flag"], "server_name": o["server_name"],
                            "pct": pct, "days_left": days_left}
                return None

            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor(max_workers=16) as pool:
                results = list(await loop.run_in_executor(pool, lambda: [_check(o) for o in orders]))

            for res in results:
                if not res:
                    continue
                # mark notified
                with closing(get_conn()) as conn:
                    conn.execute(
                        "UPDATE orders SET renew_notified=1,updated_at=? WHERE id=?",
                        (now_iso(), res["order_id"]),
                    )
                    conn.commit()
                if bot_app:
                    try:
                        days_line = f"\n⏳ روزهای باقی‌مانده: {res['days_left']}" if res["days_left"] is not None else ""
                        await bot_app.bot.send_message(
                            res["telegram_id"],
                            f"🔔 *وقت تمدیده!*\n\n"
                            f"اشتراک {res['flag']} *{res['server_name']}* — {res['pkg_name']} "
                            f"الان واجد شرایط تمدیده.\n"
                            f"📊 مصرف: {res['pct']}٪{days_line}\n\n"
                            f"برای تمدید /start بزن یا از پورتال اقدام کن.",
                            parse_mode="Markdown",
                        )
                        log_event("renew_notify_sent", {"order_id": res["order_id"],
                                                        "telegram_id": res["telegram_id"]})
                    except Exception as exc:
                        logger.warning("renew notify send failed tg=%s: %s", res["telegram_id"], exc)
        except Exception as exc:
            logger.warning("renew notifier error: %s", exc)


async def handle_sms_webhook(request: web.Request) -> web.Response:
    """
    GET /api/sms?msg=<url-encoded SMS text>
    Parses the SMS, matches by sms_amount, auto-confirms payment.
    """
    msg = request.query.get("msg", "").strip()
    if not msg:
        return web.json_response({"ok": False, "error": "no msg"}, status=400)

    amount_rials, paid_at = sms_parser.parse_sms(msg)
    if not amount_rials:
        logger.info("sms_webhook: could not parse msg")
        return web.json_response({"ok": False, "error": "could not parse amount"})

    pay = get_payment_by_sms_amount(amount_rials)
    if not pay:
        logger.info("sms_webhook: no matching payment for amount=%s", amount_rials)
        return web.json_response({"ok": False, "error": "no matching payment"})

    release_sms_code(pay["id"])

    bot_app = request.app.get("bot_app")
    if bot_app:
        try:
            from bot import do_provision
            await do_provision(bot_app.bot, pay)
        except Exception as exc:
            logger.error("sms provision error: %s", exc)

    log_event("sms_payment_matched", {
        "payment_id": pay["id"],
        "amount_rials": amount_rials,
        "paid_at": paid_at,
    })
    return web.json_response({"ok": True, "payment_id": pay["id"], "amount": amount_rials})


async def run_web_server(bot_app=None) -> None:
    from bot import _reymit_supervisor
    app = web.Application()
    app["bot_app"] = bot_app

    # Auth
    app.router.add_post("/api/auth/otp",   handle_otp_request)
    app.router.add_post("/api/auth/login", handle_otp_verify)
    # Admin - servers
    app.router.add_get ("/api/admin/servers",        handle_list_servers)
    app.router.add_post("/api/admin/servers/upsert", handle_upsert_server)
    app.router.add_post("/api/admin/servers/delete", handle_delete_server)
    app.router.add_post("/api/admin/servers/toggle", handle_server_toggle)
    # Admin - packages
    app.router.add_get ("/api/admin/packages",        handle_list_packages)
    app.router.add_post("/api/admin/packages/upsert", handle_upsert_package)
    app.router.add_post("/api/admin/packages/delete", handle_delete_package)
    # Admin - dashboard
    app.router.add_get ("/api/admin/data",             handle_dashboard_data)
    app.router.add_post("/api/admin/approve",          handle_manual_approve)
    app.router.add_post("/api/admin/traffic",          handle_manual_traffic)
    app.router.add_get ("/api/admin/settings",         handle_get_settings)
    app.router.add_post("/api/admin/settings/set",     handle_set_setting)
    # Payment callbacks
    app.router.add_post("/api/payment/callback", handle_tetra_callback)
    app.router.add_get ("/api/sms",              handle_sms_webhook)
    # User portal
    app.router.add_get ("/api/portal/{token}",                              handle_portal_data)
    app.router.add_get ("/api/portal/{token}/packages/{server_id}",         handle_portal_packages)
    app.router.add_post("/api/portal/{token}/payment",                      handle_portal_initiate_payment)
    app.router.add_get ("/api/portal/{token}/payment/{payment_id}/status",  handle_portal_payment_status)
    app.router.add_get ("/api/portal/{token}/stats/{order_id}",             handle_portal_order_stats)
    # HTML pages
    app.router.add_get("/dashboard",         handle_dashboard_html)
    app.router.add_get("/portal/{token}",    handle_portal_html)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEBHOOK_API_PORT).start()
    logger.info("Web server on :%s", WEBHOOK_API_PORT)

    if bot_app:
        asyncio.create_task(_expire_notifier(bot_app))
        asyncio.create_task(_reymit_supervisor(bot_app))
        asyncio.create_task(_renew_notifier(bot_app))

    while True:
        await asyncio.sleep(3600)