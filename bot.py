"""
bot.py — CrabVPN Telegram Bot (Multi-Server)
"""
import asyncio
import base64
import io
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import qrcode
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

from models import (
    Package, Server, create_order, create_payment, customer_accepted_tos,
    customer_has_account_on_server, customer_used_free_trial, delete_package,
    delete_server, expire_old_payments, generate_password, get_active_order_on_server,
    get_active_orders_for_customer, get_all_servers, get_customer_by_tg,
    get_customer_by_token, get_latest_tos, get_package, get_packages_for_server,
    get_payment_by_authority, get_server, get_setting, get_stored_password, init_db,
    log_event, mark_free_trial_used, mark_tos_accepted, now_iso, save_donation,
    set_order_active, set_setting, try_match_donation, try_match_donation_global,
    upsert_customer, upsert_package, upsert_server,
    alloc_sms_code, release_sms_code,
    PAYMENT_EXPIRE_MIN,
)

load_dotenv()

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Env ────────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID    = int(os.getenv("ADMIN_CHAT_ID", "0"))
_WL_RAW          = os.getenv("ADMIN_WHITELIST", "")
ADMIN_WHITELIST  = set(int(x.strip()) for x in _WL_RAW.split(",") if x.strip().isdigit())
CONFIG_PREFIX    = os.getenv("CONFIG_REMARKS_PREFIX", "CrabVPN")
DEFAULT_EXP_DAYS = int(os.getenv("DEFAULT_EXP_DAYS", "30"))
DEFAULT_MULTIUSER= int(os.getenv("DEFAULT_MULTIUSER", "1"))
WEBHOOK_API_PORT   = int(os.getenv("WEBHOOK_API_PORT", "8765"))
_PORTAL_BASE_URL_ENV = os.getenv("PORTAL_BASE_URL", "http://localhost:8765")


def _portal_base_url() -> str:
    """Reads portal URL from DB first so admin can change it without restart."""
    return get_setting("portal_base_url", "") or _PORTAL_BASE_URL_ENV

# ── States ─────────────────────────────────────────────────────────────────────
ST_MAIN, ST_PHONE, ST_WAIT_PAYER, ST_BUY_CONFIRM, ST_SELECT_SERVER, ST_WAIT_NPVT, ST_SELECT_PAYMENT = range(7)

# ══════════════════════════════════════════════════════════════════════════════
# PANEL API (dispatches to SSH or Xray backend via xpanel module)
# ══════════════════════════════════════════════════════════════════════════════

import xpanel


def _make_qr(text: str) -> io.BytesIO:
    buf = io.BytesIO()
    qrcode.make(text).save(buf, format="PNG")
    buf.seek(0)
    return buf


async def _send_both_configs(bot, tg_id: int, config_primary: str, config_secondary: str, server: Server = None) -> None:
    if get_setting("link_enabled", "1") == "1":
        return  # configs are available on the portal; not sent directly in bot
    is_xray = server and getattr(server, "server_type", "ssh") == "xray"
    if config_primary:
        label = "📲 *کانفیگ VPN* (تپ برای کپی):" if is_xray else "📲 *کانفیگ NPV* (تپ برای کپی):"
        await bot.send_message(tg_id, label, parse_mode="Markdown")
        await bot.send_message(tg_id, f"`{config_primary}`", parse_mode="Markdown")
        try:
            loop = asyncio.get_event_loop()
            qr = await loop.run_in_executor(None, _make_qr, config_primary)
            await bot.send_photo(tg_id, photo=qr, caption="QR کد کانفیگ")
        except Exception as e:
            logger.warning("QR send error: %s", e)
    if config_secondary:
        await bot.send_message(tg_id, "📲 *کانفیگ Netmod* (تپ برای کپی):", parse_mode="Markdown")
        await bot.send_message(tg_id, f"`{config_secondary}`", parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# PROVISION
# ══════════════════════════════════════════════════════════════════════════════

async def do_provision(bot, payment_row, donor_name: str = "", donated_amount: float = 0) -> None:
    pay        = payment_row
    tg_id      = pay["telegram_id"]
    order_id   = pay["order_id"]
    order_type = pay["order_type"]
    server_id  = int(pay["server_id"])
    server     = get_server(server_id)
    username   = str(tg_id)

    # approve
    with closing(__import__("models").get_conn()) as conn:
        conn.execute("UPDATE payments SET status='approved',updated_at=? WHERE id=?", (now_iso(), pay["id"]))
        conn.commit()

    provision_error  = None
    config_text      = None
    config_netmod_val = None

    try:
        with closing(__import__("models").get_conn()) as conn:
            _order    = conn.execute(
                "SELECT o.*,p.traffic_amount,p.traffic_unit,p.name AS pkg_name,p.id AS pkg_id "
                "FROM orders o JOIN packages p ON p.id=o.package_id WHERE o.id=?", (order_id,)
            ).fetchone()
            _customer = conn.execute("SELECT * FROM customers WHERE telegram_id=?", (tg_id,)).fetchone()

        pkg = get_package(_order["pkg_id"])

        # برای renewal/add_traffic username و password واقعی رو از order فعال بخون
        if order_type in ("renewal", "add_traffic"):
            existing = get_active_order_on_server(tg_id, server_id)
            if existing and existing["xpanel_username"]:
                username = existing["xpanel_username"]

        if order_type == "add_traffic":
            xpanel.add_traffic(server, username, _order["traffic_amount"], _order["traffic_unit"])
            xpanel.activate(server, username)
            with closing(__import__("models").get_conn()) as conn:
                conn.execute("UPDATE orders SET status='active',updated_at=? WHERE id=?", (now_iso(), order_id))
                conn.commit()
        else:
            new_pass = generate_password()
            if order_type == "renewal":
                user_info   = xpanel.get_user(server, username) or {}
                pre_total   = float(user_info.get("total_mb", 0) or 0)
                pre_used    = float(user_info.get("used_mb",  0) or 0)
                carry_over  = max(0.0, pre_total - pre_used)
                xpanel.renewal(server, username, getattr(pkg, "duration_days", None),
                               carry_over_mb=carry_over, pkg=pkg)
                active_pass = get_stored_password(tg_id, server_id) or new_pass
            else:
                active_pass = xpanel.adduser(server, pkg, username, new_pass, _customer)
            config_text, config_netmod_val = xpanel.build_config(server, _order["pkg_name"], username, active_pass)
            set_order_active(order_id, username, active_pass, config_text, config_netmod_val)
            # order های قبلی همین کاربر روی همین سرور رو expire کن
            if order_type == "renewal":
                with closing(__import__("models").get_conn()) as conn:
                    conn.execute(
                        "UPDATE orders SET status='expired',updated_at=? WHERE customer_id=? AND server_id=? AND status='active' AND id!=?",
                        (now_iso(), _customer["id"], server_id, order_id),
                    )
                    conn.commit()

        log_event("provision_ok", {"order_id": order_id, "server_id": server_id})
    except Exception as exc:
        provision_error = str(exc)
        logger.error("provision_failed order=%s server=%s: %s", order_id, server_id, exc)
        log_event("provision_failed", {"order_id": order_id, "error": provision_error})

    # notify user
    amount_str = f"{int(donated_amount):,} تومان" if donated_amount > 0 else "—"
    try:
        if provision_error:
            await bot.send_message(tg_id,
                f"✅ پرداخت دریافت شد ولی ساخت اکانت با خطا مواجه شد.\nمبلغ: {amount_str}\nادمین بررسی می‌کنه 🙏")
            if ADMIN_CHAT_ID:
                try:
                    await bot.send_message(ADMIN_CHAT_ID,
                        f"⚠️ *Provision failed*\norder\\_id=`{order_id}` | server\\_id=`{server_id}`\n`{provision_error[:300]}`",
                        parse_mode="Markdown")
                except Exception:
                    pass
        else:
            await bot.send_message(tg_id, f"✅ پرداخت تأیید شد!\nمبلغ: {amount_str}\nسرور: {server.flag} {server.name}")
            if order_type == "add_traffic":
                await bot.send_message(tg_id, "➕ حجم اضافه شد و تاریخ تمدید شد. کانفیگ تغییری نکرده.")
            c = get_customer_by_tg(tg_id)
            portal_url = f"{_portal_base_url()}/portal/{c['portal_token']}" if c and c.get("portal_token") else None
            if config_text and get_setting("link_enabled", "1") != "1":
                await _send_both_configs(bot, tg_id, config_text, config_netmod_val or "", server=server)
            if portal_url:
                await bot.send_message(tg_id,
                    f"🌐 برای دریافت کانفیگ، لینک زیر رو *در مرورگر* باز کن:\n{portal_url}",
                    parse_mode="Markdown")
    except Exception as e:
        logger.warning("notify user failed: %s", e)

    # notify admin
    if ADMIN_CHAT_ID:
        status_label = f"error: {provision_error}" if provision_error else "ok"
        try:
            await bot.send_message(ADMIN_CHAT_ID,
                f"payment={pay['id']} | server={server.name}\n"
                f"donor={donor_name} | amount={int(donated_amount):,}T\n"
                f"customer={pay['full_name']} | tg={tg_id}\n"
                f"order_type={order_type} | {status_label}")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# DONATION LISTENER (global — یک WebSocket برای همه سرورها، بدون مرورگر)
# ══════════════════════════════════════════════════════════════════════════════

_last_donation_key: dict = {"key": None}


def _parse_amount(detail: str) -> float:
    fa_map = str.maketrans("۰۱۲۳۴۵۶۷۸۹٬،", "0123456789  ")
    normalized = detail.translate(fa_map).replace(",", "").replace(" ", "")
    m = re.search(r"(\d+)", normalized)
    return float(m.group(1)) if m else 0.0


def _unwrap_pusher(data):
    """Pusher پیام‌ها رو به فرمت {event, data:json-string} میفرسته — unwrap کن"""
    if isinstance(data, dict) and isinstance(data.get("data"), str):
        try:
            return json.loads(data["data"])
        except Exception:
            return data
    return data


def _extract_donation(raw_msg: str) -> tuple[str, float, str]:
    """از پیام WS، اسم + مبلغ دونیت رو درمیاره (best-effort)"""
    try:
        data = json.loads(raw_msg)
    except Exception:
        return "", 0.0, raw_msg[:200]

    data = _unwrap_pusher(data)
    if not isinstance(data, dict):
        return "", 0.0, raw_msg[:200]

    # ممکنه خود payload چند لایه باشه
    for _ in range(2):
        if isinstance(data.get("donation"), dict):
            data = data["donation"]
        elif isinstance(data.get("payload"), dict):
            data = data["payload"]
        else:
            break

    name = (data.get("name") or data.get("donor") or data.get("donor_name")
            or data.get("user") or data.get("username") or "").strip()

    amount_raw = (data.get("amount") or data.get("price") or data.get("value")
                  or data.get("money") or 0)
    try:
        amount = float(amount_raw)
    except Exception:
        amount = _parse_amount(str(amount_raw))

    detail = data.get("detail") or data.get("message") or json.dumps(data, ensure_ascii=False)[:200]
    return name, amount, detail


async def _handle_donation(bot_app: Application, raw_msg: str) -> None:
    name, amount, detail = _extract_donation(raw_msg)
    if not name:
        return

    key = f"{name}|{amount}|{detail[:80]}"
    if _last_donation_key["key"] == key:
        return
    _last_donation_key["key"] = key

    logger.info("🎁 donation: %s — %s (%s)", name, amount, detail[:80])
    notif_id = save_donation(0, name, amount, detail)
    log_event("donation_received", {"name": name, "amount": amount})

    if amount <= 0:
        if ADMIN_CHAT_ID:
            try:
                await bot_app.bot.send_message(ADMIN_CHAT_ID,
                    f"🎁 دونیت بدون مبلغ\nاز: {name}\n{detail}")
            except Exception:
                pass
        return

    match = try_match_donation_global(name, amount)
    if match:
        with closing(__import__("models").get_conn()) as conn:
            conn.execute("UPDATE donation_notifications SET status='matched' WHERE id=?", (notif_id,))
            conn.commit()
        await do_provision(bot_app.bot, match["payment"], donor_name=name, donated_amount=amount)
    else:
        if ADMIN_CHAT_ID:
            try:
                await bot_app.bot.send_message(ADMIN_CHAT_ID,
                    f"🎁 دونیت بی‌match\nاز: {name} | {int(amount):,} تومان\n{detail[:200]}")
            except Exception:
                pass


STALE_TIMEOUT = 90   # ثانیه — اگه هیچ پیامی نیاد، WS مرده است


async def _donation_listener(bot_app: Application) -> None:
    """
    WebSocket listener واحد برای همه سرورها.
    - URL از runtime_settings (`donation_url`) خونده میشه
    - health check: اگه بیش از STALE_TIMEOUT ثانیه پیامی نیاد، reconnect
    """
    import aiohttp

    while True:
        ws_url = get_setting("donation_url", "").strip()
        if not ws_url:
            await asyncio.sleep(30)
            continue

        if not ws_url.startswith(("ws://", "wss://")):
            logger.warning("donation_url باید wss:// یا ws:// باشه: %s", ws_url)
            await asyncio.sleep(60)
            continue

        logger.info("🎧 donation WS connecting: %s", ws_url)
        last_msg_ts = [datetime.utcnow()]
        closed_flag = {"val": False}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url, heartbeat=30, timeout=30) as ws:
                    logger.info("✅ donation WS connected")

                    async def recv_loop():
                        async for msg in ws:
                            last_msg_ts[0] = datetime.utcnow()
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    await _handle_donation(bot_app, msg.data)
                                except Exception as e:
                                    logger.warning("handle donation err: %s", e)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
                        closed_flag["val"] = True

                    async def watchdog():
                        while not closed_flag["val"] and not ws.closed:
                            await asyncio.sleep(10)
                            # 1) stale check
                            if (datetime.utcnow() - last_msg_ts[0]).total_seconds() > STALE_TIMEOUT:
                                logger.warning("donation WS stale (%ss) — closing", STALE_TIMEOUT)
                                await ws.close()
                                break
                            # 2) URL changed
                            fresh_url = get_setting("donation_url", "").strip()
                            if fresh_url != ws_url:
                                logger.info("donation_url changed — reconnecting")
                                await ws.close()
                                break

                    await asyncio.gather(recv_loop(), watchdog(), return_exceptions=True)

        except Exception as exc:
            logger.error("donation WS error: %s", exc)

        await asyncio.sleep(5)   # کمی صبر قبل از reconnect


# alias برای حفظ compat با api.py
async def _reymit_supervisor(bot_app: Application) -> None:
    await _donation_listener(bot_app)


# ══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════════════════

def kb_tos():
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ قبول می‌کنم", callback_data="tos:accept")]])


def kb_main_with_subscriptions(has_subs: bool, has_free_trial_available: bool):
    rows = []
    if has_free_trial_available:
        rows.append([InlineKeyboardButton("🎁 تست رایگان", callback_data="menu:freetrial")])
    rows.append([InlineKeyboardButton("🛒 خرید اشتراک جدید", callback_data="menu:buy")])
    if has_subs:
        rows.append([InlineKeyboardButton("📊 اشتراک‌های من", callback_data="menu:mysubs")])
        rows.append([InlineKeyboardButton("➕ افزایش حجم / تمدید", callback_data="menu:addtraffic")])
    if get_setting("link_enabled", "1") == "1":
        rows.append([InlineKeyboardButton("🔗 لینک کردن کانفیگ", callback_data="menu:linkcfg")])
    rows.append([InlineKeyboardButton("🌐 پورتال شخصی", callback_data="menu:portal")])
    return InlineKeyboardMarkup(rows)


def kb_servers(servers: list[Server], action: str):
    rows = [[InlineKeyboardButton(f"{s.flag} {s.name} — {s.location}", callback_data=f"{action}:{s.id}")] for s in servers]
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)


def kb_packages(pkgs: list[Package]):
    rows = [[InlineKeyboardButton(f"{p.name}  —  {p.price_label}", callback_data=f"pkg:{p.id}")] for p in pkgs]
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)


def kb_confirm():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تأیید", callback_data="buy:yes"),
        InlineKeyboardButton("❌ لغو",   callback_data="buy:no"),
    ]])


def kb_phone():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 ارسال شماره تماس", request_contact=True)],
         [KeyboardButton("رد کردن ⏭")]],
        resize_keyboard=True, one_time_keyboard=True)


# ══════════════════════════════════════════════════════════════════════════════
# BOT HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _buyable_servers(mode: str) -> list[Server]:
    """سرورهایی که فروش/تمدید برایشان باز است"""
    servers = get_all_servers()
    if mode == "buy":
        return [s for s in servers if s.sales_open]
    if mode == "addtraffic":
        return [s for s in servers if s.renewal_open]
    if mode == "freetrial":
        return [s for s in servers if s.free_trial]
    return servers


def _subs_text(orders) -> str:
    if not orders:
        return "📭 هیچ اشتراک فعالی نداری."
    lines = ["📡 *اشتراک‌های فعال شما*\n━━━━━━━━━━━━━━━━━━━━"]
    for o in orders:
        lines.append(f"\n{o['flag']} *{o['server_name']}* — {o['pkg_name']}")
    return "\n".join(lines)


def _fmt_mb(mb: float) -> str:
    if mb < 1024:
        return f"{mb:.0f} MB"
    return f"{mb / 1024:.2f} GB"


async def _subs_text_with_usage(uid: int) -> str:
    orders = get_active_orders_for_customer(uid)
    if not orders:
        return "📭 هیچ اشتراک فعالی نداری."
    servers = {s.id: s for s in get_all_servers()}

    def _enrich(o):
        sv = servers.get(o["server_id"])
        if not sv:
            return None
        info     = xpanel.get_user(sv, o["xpanel_username"]) or {}
        total_mb = float(info.get("total_mb", 0) or 0)
        used_mb  = float(info.get("used_mb", 0) or 0)
        if total_mb < 0.0001:
            t = float(o["traffic_amount"])
            total_mb = t if o["traffic_unit"] == "mb" else t * 1024
        remaining_mb = max(0.0, round(total_mb - used_mb, 2))
        pct = min(100, int(used_mb / total_mb * 100)) if total_mb else 0
        return {
            "flag": o["flag"], "server_name": o["server_name"], "pkg_name": o["pkg_name"],
            "total_mb": total_mb, "used_mb": used_mb, "remaining_mb": remaining_mb,
            "pct": pct, "expdate": info.get("expdate", "—"),
        }

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=8) as pool:
        enriched = list(await loop.run_in_executor(pool, lambda: [_enrich(o) for o in orders]))
    enriched = [e for e in enriched if e]

    if not enriched:
        return "📭 هیچ اشتراک فعالی نداری."

    lines = ["📡 *اشتراک‌های فعال شما*\n━━━━━━━━━━━━━━━━━━━━"]
    for e in enriched:
        lines.append(f"\n{e['flag']} *{e['server_name']}* — {e['pkg_name']}")
        lines.append(f"📊 مصرف: {_fmt_mb(e['used_mb'])} از {_fmt_mb(e['total_mb'])} ({e['pct']}%)")
        lines.append(f"💾 باقی‌مانده: {_fmt_mb(e['remaining_mb'])}")
        if e["expdate"] and e["expdate"] != "—":
            lines.append(f"📅 انقضا: {e['expdate']}")
    return "\n".join(lines)


async def _show_main(update: Update, msg=None) -> int:
    uid  = update.effective_user.id
    msg  = msg or update.message
    c    = get_customer_by_tg(uid)
    subs = get_active_orders_for_customer(uid)
    # تست رایگان: فقط اگه حداقل یه سرور free_trial داشته باشه و کاربر استفاده نکرده
    free_avail = (not customer_used_free_trial(uid)) and bool(_buyable_servers("freetrial"))
    await msg.reply_text(
        _subs_text(subs) if subs else "👋 خوش اومدی! هنوز اشتراک فعالی نداری.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main_with_subscriptions(bool(subs), free_avail),
    )
    return ST_MAIN


# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    upsert_customer(update.effective_user)
    if not customer_accepted_tos(update.effective_user.id):
        tos = get_latest_tos()
        await update.message.reply_text(
            f"👋 سلام *{update.effective_user.first_name}*!\n\n"
            "قبل از شروع شرایط استفاده رو بخون:\n\n" + tos["content"],
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb_tos(),
        )
        return ST_MAIN
    return await _show_main(update)


async def cb_tos(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer()
    mark_tos_accepted(q.from_user.id)
    await q.edit_message_reply_markup(None)
    await q.message.reply_text("✅ شرایط پذیرفته شد.")
    return await _show_main(update, msg=q.message)


async def cb_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer()
    action = q.data.split(":")[1]
    uid    = q.from_user.id

    if action == "back":
        return await _show_main(update, msg=q.message)

    if action == "linkcfg":
        if get_setting("link_enabled", "1") != "1":
            await q.edit_message_text("🔒 لینک کردن کانفیگ فعلاً غیرفعاله.")
            return ST_MAIN
        await q.edit_message_text(
            "📎 *لینک کردن کانفیگ*\n\n"
            "فایل `.npvt` خودت رو بفرست (همون فایلی که ادمین بهت داده).\n\n"
            "نام فایل باید به فرمت `username.npvt` یا `server.username.npvt` باشه.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ST_WAIT_NPVT

    if action == "portal":
        c = get_customer_by_tg(uid)
        if c and c["portal_token"]:
            url = f"{_portal_base_url()}/portal/{c['portal_token']}"
            await q.edit_message_text(f"🌐 *پورتال شخصی شما:*\n\n`{url}`\n\nاین لینک فقط مال توئه — به کسی نده!", parse_mode=ParseMode.MARKDOWN)
        return ST_MAIN

    if action == "mysubs":
        text = await _subs_text_with_usage(uid)
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="menu:back")]]))
        return ST_MAIN

    if action == "freetrial":
        servers = _buyable_servers("freetrial")
        if not servers:
            await q.edit_message_text("تست رایگان در حال حاضر در دسترس نیست.")
            return ST_MAIN
        if customer_used_free_trial(uid):
            await q.edit_message_text("شما قبلاً از تست رایگان استفاده کردید.")
            return ST_MAIN
        ctx.user_data["mode"] = "freetrial"
        await q.edit_message_text("🎁 سرور مورد نظر رو انتخاب کن:", reply_markup=kb_servers(servers, "srv"))
        return ST_MAIN

    if action == "buy":
        servers = _buyable_servers("buy")
        if not servers:
            await q.edit_message_text("🔒 فروش در حال حاضر بسته است.")
            return ST_MAIN
        ctx.user_data["mode"] = "buy"
        await q.edit_message_text("🌍 سرور مورد نظر رو انتخاب کن:", reply_markup=kb_servers(servers, "srv"))
        return ST_MAIN

    if action == "addtraffic":
        servers = _buyable_servers("addtraffic")
        if not servers:
            await q.edit_message_text("🔒 تمدید در حال حاضر بسته است.")
            return ST_MAIN
        ctx.user_data["mode"] = "addtraffic"
        await q.edit_message_text("🌍 سرور مورد نظر رو انتخاب کن:", reply_markup=kb_servers(servers, "srv"))
        return ST_MAIN

    return ST_MAIN


async def cb_server(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer()
    server_id = int(q.data.split(":")[1])
    server    = get_server(server_id)
    uid       = q.from_user.id
    mode      = ctx.user_data.get("mode", "buy")

    if not server or not server.is_active:
        await q.edit_message_text("این سرور در دسترس نیست.")
        return ST_MAIN

    ctx.user_data["server_id"] = server_id

    if mode == "freetrial":
        from models import get_conn
        with closing(get_conn()) as conn:
            trial_pkg = conn.execute(
                "SELECT id FROM packages WHERE server_id=? AND name='Test-50MB' AND is_active=1 LIMIT 1", (server_id,)
            ).fetchone()
        if not trial_pkg:
            await q.edit_message_text("بسته تست برای این سرور در دسترس نیست.")
            return ST_MAIN
        ctx.user_data["package_id"] = trial_pkg["id"]
        ctx.user_data["is_freetrial"] = True
        await q.edit_message_text(
            f"🎁 تست رایگان 50MB\nسرور: {server.flag} {server.name}\n\nفعال‌سازی کنم؟",
            reply_markup=kb_confirm(),
        )
        return ST_BUY_CONFIRM

    pkgs = get_packages_for_server(server_id)
    if not pkgs:
        await q.edit_message_text("هیچ بسته‌ای برای این سرور تعریف نشده.")
        return ST_MAIN
    await q.edit_message_text(f"{server.flag} *{server.name}*\n\nبسته مورد نظر رو انتخاب کن:",
                              parse_mode=ParseMode.MARKDOWN, reply_markup=kb_packages(pkgs))
    return ST_MAIN


async def cb_pkg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q   = update.callback_query; await q.answer()
    pkg = get_package(int(q.data.split(":")[1]))
    if not pkg or not pkg.is_active:
        await q.edit_message_text("این بسته در دسترس نیست.")
        return ST_MAIN
    server = get_server(pkg.server_id)
    ctx.user_data["package_id"] = pkg.id
    ctx.user_data["server_id"]  = pkg.server_id
    mode   = ctx.user_data.get("mode", "buy")
    label  = "➕ افزایش حجم" if mode == "addtraffic" else "🛒 خرید"
    await q.edit_message_text(
        f"{label}: *{pkg.name}*\n"
        f"سرور: {server.flag} {server.name}\n"
        f"💰 قیمت: *{pkg.price_label}*\n\nتأیید می‌کنی؟",
        parse_mode=ParseMode.MARKDOWN, reply_markup=kb_confirm(),
    )
    return ST_BUY_CONFIRM


async def cb_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer()
    if q.data == "buy:no":
        ctx.user_data.clear()
        return await _show_main(update, msg=q.message)

    uid       = q.from_user.id
    server_id = ctx.user_data.get("server_id", 0)
    pkg_id    = ctx.user_data.get("package_id", 0)
    server    = get_server(server_id)
    pkg       = get_package(pkg_id)
    customer  = get_customer_by_tg(uid)

    if not server or not pkg or not customer:
        await q.edit_message_text("خطا. /start بزن.")
        return ConversationHandler.END

    # free trial
    if ctx.user_data.get("is_freetrial"):
        if customer_used_free_trial(uid):
            await q.edit_message_text("قبلاً از تست رایگان استفاده کردی.")
            return ST_MAIN
        uname = str(uid)
        pw    = generate_password()
        try:
            if customer_has_account_on_server(uid, server_id):
                xpanel.renewal(server, uname, getattr(pkg, "duration_days", None))
                prev = get_active_order_on_server(uid, server_id)
                pw   = get_stored_password(uid, server_id) or pw
                cfg, cfg_sec = xpanel.build_config(server, "Test", uname, pw)
                if prev and prev["config_text"]:
                    cfg = prev["config_text"]
                    cfg_sec = prev.get("config_netmod") or cfg_sec
            else:
                pw = xpanel.adduser(server, pkg, uname, pw, customer)
                cfg, cfg_sec = xpanel.build_config(server, "Test", uname, pw)
            from models import create_order as _co
            oid = _co(customer["id"], pkg.id, server_id, "new")
            set_order_active(oid, uname, pw, cfg, cfg_sec)
            mark_free_trial_used(uid)
            await q.edit_message_text(f"✅ تست رایگان فعال شد!\nسرور: {server.flag} {server.name}")
            await _send_both_configs(ctx.bot, uid, cfg, cfg_sec, server=server)
        except Exception as exc:
            await q.edit_message_text("خطا در فعال‌سازی.")
            if ADMIN_CHAT_ID:
                await ctx.bot.send_message(ADMIN_CHAT_ID, f"free trial error [{server.name}]: {exc}")
        ctx.user_data.pop("is_freetrial", None)
        return ConversationHandler.END

    if customer["phone"]:
        return await _go_to_payment(update, ctx, q.message)

    await q.edit_message_text("📱 شماره تماست رو ارسال کن (اختیاری — فقط برای پشتیبانی استفاده میشه):", reply_markup=None)
    await q.message.reply_text("👇", reply_markup=kb_phone())
    return ST_PHONE


async def get_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message and update.message.contact:
        upsert_customer(update.effective_user, phone=update.message.contact.phone_number)
        await update.message.reply_text("✅ شماره ثبت شد.", reply_markup=ReplyKeyboardRemove())
    else:
        # user skipped — proceed without phone
        await update.message.reply_text("⏭ رد شد.", reply_markup=ReplyKeyboardRemove())
    return await _go_to_payment(update, ctx, update.message)


async def _go_to_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE, msg) -> int:
    uid      = update.effective_user.id
    pkg      = get_package(ctx.user_data.get("package_id", 0))
    server   = get_server(ctx.user_data.get("server_id", 0))
    customer = get_customer_by_tg(uid)
    mode     = ctx.user_data.get("mode", "buy")

    if not pkg or not server or not customer:
        await msg.reply_text("خطا. /start بزن.")
        return ConversationHandler.END

    is_renewal = customer_has_account_on_server(uid, server.id)
    order_type = "add_traffic" if mode == "addtraffic" else ("renewal" if is_renewal else "new")

    # شرط تمدید: ≥75% حجم مصرف شده یا ≤25% زمان مونده
    if order_type in ("renewal", "add_traffic"):
        existing = get_active_order_on_server(uid, server.id)
        if existing and existing.get("xpanel_username"):
            loop = asyncio.get_event_loop()
            user_info = await loop.run_in_executor(None, xpanel.get_user, server, existing["xpanel_username"])
            if user_info:
                total_mb = float(user_info.get("total_mb", 0) or 0)
                used_mb  = float(user_info.get("used_mb",  0) or 0)
                pct = min(100, int(used_mb / total_mb * 100)) if total_mb > 0.001 else 0
                # duration از پکیج فعلی کاربر
                with closing(__import__("models").get_conn()) as conn:
                    _dur = conn.execute(
                        "SELECT COALESCE(duration_days,30) AS dur FROM packages WHERE id=?",
                        (existing["package_id"],),
                    ).fetchone()
                duration_days  = int(_dur["dur"]) if _dur else 30
                threshold_days = int(duration_days * 0.25)
                can_renew_traffic = pct >= 75
                can_renew_time    = False
                days_left         = None
                expdate = user_info.get("expdate", "")
                if expdate and expdate != "—":
                    try:
                        exp_dt    = datetime.fromisoformat(expdate.split("T")[0])
                        days_left = (exp_dt - datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)).days
                        can_renew_time = days_left <= threshold_days
                    except Exception:
                        pass
                if not (can_renew_traffic or can_renew_time):
                    remaining_pct = 100 - pct
                    time_line = f"\n📅 روزهای باقی‌مانده: {days_left}" if days_left is not None else ""
                    await msg.reply_text(
                        f"⏳ *هنوز نمیتونی تمدید کنی*\n\n"
                        f"شرط تمدید: حداقل *۷۵٪* حجم مصرف کرده باشی "
                        f"**یا** کمتر از *{threshold_days} روز* به انقضا مونده باشه.\n\n"
                        f"📊 مصرف فعلی: {pct}٪ (باقی‌مانده: {remaining_pct}٪){time_line}",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    return ConversationHandler.END

    order_id   = create_order(customer["id"], pkg.id, server.id, order_type)
    payment_id = create_payment(order_id, server.id, pkg.price_irr, "")

    ctx.user_data["order_id"]   = order_id
    ctx.user_data["payment_id"] = payment_id

    donate_on = get_setting("payment_donate_enabled", "1") == "1"
    card_on   = get_setting("payment_card_enabled",   "0") == "1"
    tetra_on  = get_setting("payment_tetra_enabled",  "0") == "1"

    active = []
    if tetra_on:  active.append(("tetra",  "⚡ درگاه آنلاین Tetra98"))
    if card_on:   active.append(("card",   "💳 کارت به کارت (تأیید خودکار SMS)"))
    if donate_on: active.append(("donate", "🎁 دونیت استریم (تأیید دستی)"))

    if len(active) > 1:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(label, callback_data=f"paymethod:{key}")]
            for key, label in active
        ])
        await msg.reply_text(
            f"📦 *{pkg.name}* — {server.flag} {server.name}\n💰 {pkg.price_label}\n\nروش پرداخت رو انتخاب کن:",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )
        return ST_SELECT_PAYMENT
    elif active:
        return await _route_payment(active[0][0], update, ctx, msg)
    else:
        await msg.reply_text("⚠️ در حال حاضر هیچ روش پرداختی فعال نیست. با ادمین تماس بگیر.")
        return ConversationHandler.END


async def _route_payment(method: str, update, ctx, msg) -> int:
    if method == "tetra":  return await _do_tetra_payment(update, ctx, msg)
    if method == "card":   return await _do_card_payment(update, ctx, msg)
    return await _do_donate_payment(update, ctx, msg)


async def cb_select_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    q = update.callback_query; await q.answer()
    return await _route_payment(q.data.split(":")[1], update, ctx, q.message)


async def _do_card_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE, msg) -> int:
    uid        = update.effective_user.id
    pkg        = get_package(ctx.user_data.get("package_id", 0))
    server     = get_server(ctx.user_data.get("server_id", 0))
    payment_id = ctx.user_data.get("payment_id", 0)
    customer   = get_customer_by_tg(uid)

    if not pkg or not server or not customer:
        await msg.reply_text("خطا. /start بزن.")
        return ConversationHandler.END

    card_number = get_setting("card_number", "")
    card_owner  = get_setting("card_owner", "")
    code, sms_amount = alloc_sms_code(payment_id, pkg.price_irr or 0)

    card_line  = f"💳 شماره کارت: `{card_number}`" if card_number else ""
    owner_line = f"👤 به نام: {card_owner}" if card_owner else ""

    await msg.reply_text(
        f"💳 *پرداخت کارت به کارت*\n\n"
        f"📦 {pkg.name} — {server.flag} {server.name}\n"
        + (f"{card_line}\n{owner_line}\n\n" if card_number else "\n")
        + f"💰 مبلغ *دقیق* واریز:\n"
        f"┌─────────────────────\n"
        f"│  *{sms_amount:,} ریال*\n"
        f"└─────────────────────\n"
        f"⚠️ {code} ریال خرده اضافه‌ست — برای شناسایی خودکار پرداخت شماست.\n\n"
        f"✅ بلافاصله بعد از واریز اکانتت فعال میشه 🚀\n"
        f"⏰ {PAYMENT_EXPIRE_MIN} دقیقه فرصت داری.",
        parse_mode=ParseMode.MARKDOWN,
    )

    # اطلاع‌رسانی فوری به ادمین
    if ADMIN_CHAT_ID:
        mode  = ctx.user_data.get("mode", "buy")
        label = "افزایش حجم" if mode == "addtraffic" else (
            "تمدید" if customer_has_account_on_server(uid, server.id) else "خرید جدید")
        try:
            admin_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ تأیید دستی", callback_data=f"admin_approve:{payment_id}")
            ]])
            await ctx.bot.send_message(
                ADMIN_CHAT_ID,
                f"💳 *کارت به کارت* — {label}\n"
                f"{server.flag} {server.name} | {pkg.name} / {pkg.price_label}\n"
                f"👤 {customer['full_name']} | tg=`{uid}`\n"
                f"🔢 payment\\_id=`{payment_id}`\n"
                f"💰 sms\\_amount=`{sms_amount:,}` ریال",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_kb,
            )
        except Exception:
            pass

    return ConversationHandler.END


async def _do_donate_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE, msg) -> int:
    pkg    = get_package(ctx.user_data.get("package_id", 0))
    server = get_server(ctx.user_data.get("server_id", 0))
    await msg.reply_text(
        f"🎁 *پرداخت از طریق دونیت استریم*\n\n"
        f"📦 {pkg.name} — {server.flag} {server.name}\n"
        f"💰 مبلغ: *{pkg.price_label}*\n\n"
        f"1️⃣ روی لینک استریم ادمین دونیت کن — مبلغ: *{pkg.price_label}*\n"
        f"2️⃣ اسمت رو اینجا بنویس (همون که با دونیت فرستادی)\n\n"
        f"⏰ {PAYMENT_EXPIRE_MIN} دقیقه فرصت داری.",
        parse_mode=ParseMode.MARKDOWN,
    )
    return ST_WAIT_PAYER


async def _do_tetra_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE, msg) -> int:
    import aiohttp as _aiohttp
    uid        = update.effective_user.id
    pkg        = get_package(ctx.user_data.get("package_id", 0))
    server     = get_server(ctx.user_data.get("server_id", 0))
    payment_id = ctx.user_data.get("payment_id", 0)
    customer   = get_customer_by_tg(uid)

    api_key      = get_setting("tetra_api_key", "")
    callback_url = f"{_portal_base_url()}/api/payment/callback"

    if not api_key:
        await msg.reply_text("⚠️ درگاه آنلاین در دسترس نیست. با ادمین تماس بگیر.")
        return ConversationHandler.END

    try:
        async with _aiohttp.ClientSession() as session:
            async with session.post(
                "https://tetra98.com/api/create_order",
                json={
                    "ApiKey": api_key,
                    "Hash_id": f"invoice-{payment_id}",
                    "Amount": int(pkg.price_irr or 0),
                    "Description": f"خرید {pkg.name} — {server.name}",
                    "Email": "",
                    "Mobile": (customer["phone"] or "") if customer else "",
                    "CallbackURL": callback_url,
                },
                timeout=_aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json(content_type=None)

        if str(data.get("status")) != "100":
            raise RuntimeError(f"Tetra: {data}")

        authority   = data["Authority"]
        pay_url_bot = data.get("payment_url_bot") or data.get("payment_url_web", "")

        with closing(__import__("models").get_conn()) as conn:
            conn.execute(
                "UPDATE payments SET authority=?,payment_method='tetra',updated_at=? WHERE id=?",
                (authority, now_iso(), payment_id),
            )
            conn.commit()

        await msg.reply_text(
            f"💳 *پرداخت آنلاین*\n\n"
            f"📦 {pkg.name} — {server.flag} {server.name}\n"
            f"💰 {pkg.price_label}\n\n"
            f"👇 برای پرداخت روی لینک زیر بزن:",
            parse_mode=ParseMode.MARKDOWN,
        )
        await msg.reply_text(pay_url_bot)
        log_event("tetra_order_created", {"payment_id": payment_id, "authority": authority})

        if ADMIN_CHAT_ID:
            try:
                await ctx.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"💳 Tetra order\ncustomer={customer['full_name'] if customer else uid}\n"
                    f"pkg={pkg.name} / {pkg.price_label}\npayment_id={payment_id}",
                )
            except Exception:
                pass

    except Exception as exc:
        logger.error("tetra create_order error: %s", exc)
        await msg.reply_text("❌ خطا در ایجاد لینک پرداخت. لطفاً با ادمین تماس بگیر.")
        if ADMIN_CHAT_ID:
            try:
                await ctx.bot.send_message(ADMIN_CHAT_ID, f"⚠️ Tetra error: {exc}")
            except Exception:
                pass

    return ConversationHandler.END


async def receive_payer(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    payer_name = (update.message.text or "").strip()
    if len(payer_name) < 2:
        await update.message.reply_text("اسم کوتاهه. کامل‌تر بنویس.")
        return ST_WAIT_PAYER

    payment_id = int(ctx.user_data.get("payment_id", 0))
    order_id   = int(ctx.user_data.get("order_id", 0))
    pkg        = get_package(ctx.user_data.get("package_id", 0))
    server     = get_server(ctx.user_data.get("server_id", 0))
    customer   = get_customer_by_tg(update.effective_user.id)

    if not pkg or not server or not customer:
        await update.message.reply_text("خطا. /start بزن.")
        return ConversationHandler.END

    from models import get_conn
    with closing(get_conn()) as conn:
        conn.execute("UPDATE payments SET payer_name=?,updated_at=? WHERE id=?", (payer_name, now_iso(), payment_id))
        conn.commit()

    username = str(update.effective_user.id)
    await update.message.reply_text(
        f"✅ ثبت شد — منتظر دونیت.\nPayment ID: {payment_id}\nبه محض رسیدن، اکانتت فعال میشه 🚀"
    )

    if ADMIN_CHAT_ID:
        mode = ctx.user_data.get("mode", "buy")
        label = "افزایش حجم" if mode == "addtraffic" else ("تمدید" if customer_has_account_on_server(update.effective_user.id, server.id) else "خرید جدید")
        try:
            admin_kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأیید دستی", callback_data=f"admin_approve:{payment_id}")]])
            await ctx.bot.send_message(ADMIN_CHAT_ID,
                f"{label} — {server.flag} {server.name}\n"
                f"pkg={pkg.name} / {pkg.price_label}\n"
                f"customer={customer['full_name']} | tg={update.effective_user.id}\n"
                f"payer={payer_name} | xpanel={username}",
                reply_markup=admin_kb)
        except Exception:
            pass

    return ConversationHandler.END


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("لغو شد.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def cmd_myinfo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    c = get_customer_by_tg(update.effective_user.id)
    if not c:
        await update.message.reply_text("اطلاعاتی نیست. /start")
        return
    portal_url = f"{_portal_base_url()}/portal/{c['portal_token']}" if c["portal_token"] else "—"
    await update.message.reply_text(
        f"نام: {c['full_name']}\nتلفن: {c['phone'] or '—'}\nپورتال: {portal_url}"
    )


async def receive_npvt_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    msg = update.message
    doc = msg.document if msg else None
    if not doc or not (doc.file_name or "").lower().endswith(".npvt"):
        await msg.reply_text("⚠️ فایل با پسوند `.npvt` بفرست.", parse_mode=ParseMode.MARKDOWN)
        return ST_WAIT_NPVT

    # parse filename فقط: username.npvt  یا  server.username.npvt
    stem = doc.file_name[:-5]
    if "." in stem:
        server_prefix, username = stem.split(".", 1)
    else:
        server_prefix, username = None, stem
    username = username.strip()

    if not username:
        await msg.reply_text("❌ نام کاربری از اسم فایل قابل استخراج نیست.")
        return ConversationHandler.END

    servers = get_all_servers(active_only=False)
    if not servers:
        await msg.reply_text("❌ هیچ سروری ثبت نشده.")
        return ConversationHandler.END

    if server_prefix:
        server = next((s for s in servers if s.name.lower().startswith(server_prefix.lower())), None)
        if not server:
            await msg.reply_text(f"❌ سروری با پیشوند `{server_prefix}` پیدا نشد.", parse_mode=ParseMode.MARKDOWN)
            return ConversationHandler.END
    else:
        server = servers[0]   # دیفالت = سرور اول

    if getattr(server, "server_type", "ssh") == "xray":
        await msg.reply_text("❌ لینک کردن فایل `.npvt` فقط برای سرورهای SSH پشتیبانی می‌شه.",
                             parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    # verify + فچ پسورد از xpanel (SSH only)
    xp_info = xpanel.get_user(server, username)
    if not xp_info:
        await msg.reply_text(
            f"❌ یوزر `{username}` روی سرور *{server.name}* پیدا نشد.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    password = xp_info.get("password") or ""
    if not password:
        await msg.reply_text(
            f"⚠️ یوزر تأیید شد ولی پسورد از xpanel قابل فچ نیست.\nبه ادمین اطلاع بده.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    uid      = update.effective_user.id
    customer = get_customer_by_tg(uid)
    if not customer:
        await msg.reply_text("ابتدا /start بزن.")
        return ConversationHandler.END

    pkgs = get_packages_for_server(server.id, active_only=False)
    pkg  = next((p for p in pkgs if p.name != "Test-50MB"), pkgs[0] if pkgs else None)
    if not pkg:
        await msg.reply_text("❌ بسته‌ای برای این سرور تعریف نشده.")
        return ConversationHandler.END

    config_npv, config_netmod = xpanel.build_config(server, pkg.name, username, password)

    existing = get_active_order_on_server(uid, server.id)
    if existing:
        order_id = existing["id"]
    else:
        from models import create_order as _co
        order_id = _co(customer["id"], pkg.id, server.id, "new")

    set_order_active(order_id, username, password, config_npv, config_netmod)

    await msg.reply_text(
        f"✅ کانفیگ لینک شد!\nسرور: {server.flag} *{server.name}*\nیوزر: `{username}`",
        parse_mode=ParseMode.MARKDOWN,
    )
    await _send_both_configs(ctx.bot, uid, config_npv, config_netmod, server=server)

    log_event("link_npvt", {"tg_id": uid, "server_id": server.id, "username": username})
    return ConversationHandler.END


async def _is_admin(bot, user_id: int) -> bool:
    if not ADMIN_CHAT_ID:
        return False
    if ADMIN_WHITELIST:
        return user_id in ADMIN_WHITELIST
    try:
        m = await bot.get_chat_member(ADMIN_CHAT_ID, user_id)
        return m.status in ("administrator", "creator", "member")
    except Exception:
        return user_id == ADMIN_CHAT_ID


async def cb_admin_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query; await q.answer()
    if not await _is_admin(ctx.bot, q.from_user.id):
        await q.answer("فقط ادمین.", show_alert=True); return

    payment_id = int(q.data.split(":")[1])
    from models import get_conn
    with closing(get_conn()) as conn:
        pay = conn.execute(
            """SELECT p.*,o.id AS order_id,COALESCE(o.order_type,'new') AS order_type,
                      o.server_id,c.telegram_id,c.full_name
               FROM payments p JOIN orders o ON o.id=p.order_id
               JOIN customers c ON c.id=o.customer_id WHERE p.id=?""",
            (payment_id,),
        ).fetchone()
    if not pay:
        await q.message.reply_text(f"payment #{payment_id} نیست."); return
    if pay["status"] == "approved":
        await q.message.reply_text(f"payment #{payment_id} قبلاً تأیید شده."); return

    await do_provision(ctx.bot, pay,
                       donor_name=pay["payer_name"] or "manual",
                       donated_amount=float(pay["amount"] or 0))
    await q.edit_message_reply_markup(None)
    await q.message.reply_text(f"✅ payment #{payment_id} تأیید شد.")


async def unknown_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("❓ /start بزن.")


async def unknown_msg(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("/start بزن.")


# ══════════════════════════════════════════════════════════════════════════════
# BUILD
# ══════════════════════════════════════════════════════════════════════════════

def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN تنظیم نشده")
    app = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ST_MAIN: [
                CallbackQueryHandler(cb_tos,    pattern=r"^tos:accept$"),
                CallbackQueryHandler(cb_menu,   pattern=r"^menu:"),
                CallbackQueryHandler(cb_server, pattern=r"^srv:\d+$"),
                CallbackQueryHandler(cb_pkg,    pattern=r"^pkg:\d+$"),
            ],
            ST_BUY_CONFIRM: [
                CallbackQueryHandler(cb_confirm, pattern=r"^buy:(yes|no)$"),
            ],
            ST_PHONE: [
                MessageHandler(filters.CONTACT, get_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone),
            ],
            ST_WAIT_PAYER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_payer),
            ],
            ST_WAIT_NPVT: [
                MessageHandler(filters.Document.ALL, receive_npvt_file),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_npvt_file),
            ],
            ST_SELECT_PAYMENT: [
                CallbackQueryHandler(cb_select_payment, pattern=r"^paymethod:(tetra|card|donate)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True, per_message=False,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("myinfo", cmd_myinfo))
    app.add_handler(CallbackQueryHandler(cb_admin_approve, pattern=r"^admin_approve:\d+$"))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_msg))
    return app