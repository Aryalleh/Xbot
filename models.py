"""
models.py — CrabVPN Multi-Server DB Schema & Helpers
"""
import json
import os
import random
import secrets
import sqlite3
import string
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

DB_PATH = Path(__file__).resolve().parent / "bot.sqlite3"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def log_event(event_type: str, payload: dict) -> None:
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT INTO audit_logs (event_type,payload,created_at) VALUES (?,?,?)",
            (event_type, json.dumps(payload, ensure_ascii=False), now_iso()),
        )
        conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# DATACLASSES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Server:
    id: int
    name: str
    flag: str
    location: str
    xpanel_url: str
    xpanel_token: str          # SSH mode: API token
    ssh_host: str
    ssh_port: int
    reymit_url: str
    sales_open: int
    renewal_open: int
    free_trial: int
    is_active: int
    display_order: int
    # server_type: "ssh" (legacy) | "xray" (3X-UI / xray core)
    server_type: str = "ssh"
    # Xray-mode credentials (unused for SSH servers)
    xpanel_username: str = ""
    xpanel_password: str = ""
    xpanel_inbound_id: int = 1
    xpanel_webbasepath: str = "/"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "flag": self.flag,
            "location": self.location, "xpanel_url": self.xpanel_url,
            "xpanel_token": self.xpanel_token, "ssh_host": self.ssh_host,
            "ssh_port": self.ssh_port, "reymit_url": self.reymit_url,
            "sales_open": self.sales_open, "renewal_open": self.renewal_open,
            "free_trial": self.free_trial, "is_active": self.is_active,
            "display_order": self.display_order,
            "server_type": self.server_type,
            "xpanel_username": self.xpanel_username,
            "xpanel_password": self.xpanel_password,
            "xpanel_inbound_id": self.xpanel_inbound_id,
            "xpanel_webbasepath": self.xpanel_webbasepath,
        }


@dataclass
class Package:
    id: int
    server_id: int
    name: str
    traffic_amount: int
    traffic_unit: str    # mb / gb
    price_irr: Optional[float]
    is_active: int
    display_order: int
    duration_days: int = 30  # مدت زمان اشتراک به روز

    @property
    def traffic_label(self) -> str:
        return f"{self.traffic_amount}{self.traffic_unit.upper()}"

    @property
    def price_label(self) -> str:
        if self.price_irr is None:
            return "دستی"
        return f"{int(self.price_irr):,} تومان"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "server_id": self.server_id, "name": self.name,
            "traffic_amount": self.traffic_amount, "traffic_unit": self.traffic_unit,
            "price_irr": self.price_irr, "is_active": self.is_active,
            "display_order": self.display_order,
            "duration_days": self.duration_days,
        }


# ══════════════════════════════════════════════════════════════════════════════
# INIT DB
# ══════════════════════════════════════════════════════════════════════════════

def init_db() -> None:
    with closing(get_conn()) as conn:
        cur = conn.cursor()

        # ── TOS ───────────────────────────────────────────────────────────────
        cur.execute("""CREATE TABLE IF NOT EXISTS tos_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")

        # ── Customers ─────────────────────────────────────────────────────────
        cur.execute("""CREATE TABLE IF NOT EXISTS customers (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id          INTEGER NOT NULL UNIQUE,
            telegram_username    TEXT,
            full_name            TEXT,
            phone                TEXT,
            portal_token         TEXT UNIQUE,       -- hex 16 byte = 32 chars
            tos_accepted_version INTEGER,
            free_trial_used      INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL
        )""")

        # ── Servers ───────────────────────────────────────────────────────────
        cur.execute("""CREATE TABLE IF NOT EXISTS servers (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            flag          TEXT NOT NULL DEFAULT '🌍',
            location      TEXT NOT NULL DEFAULT '',
            xpanel_url    TEXT NOT NULL,
            xpanel_token  TEXT NOT NULL,
            ssh_host      TEXT NOT NULL DEFAULT '127.0.0.1',
            ssh_port      INTEGER NOT NULL DEFAULT 22,
            reymit_url    TEXT NOT NULL DEFAULT '',
            sales_open    INTEGER NOT NULL DEFAULT 1,
            renewal_open  INTEGER NOT NULL DEFAULT 1,
            free_trial    INTEGER NOT NULL DEFAULT 0,
            is_active     INTEGER NOT NULL DEFAULT 1,
            display_order INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )""")

        # ── Packages (per-server) ─────────────────────────────────────────────
        cur.execute("""CREATE TABLE IF NOT EXISTS packages (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id      INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
            name           TEXT NOT NULL,
            traffic_amount INTEGER NOT NULL,
            traffic_unit   TEXT NOT NULL CHECK(traffic_unit IN ('mb','gb')),
            price_irr      REAL,
            is_active      INTEGER NOT NULL DEFAULT 1,
            display_order  INTEGER NOT NULL DEFAULT 0,
            notes          TEXT,
            updated_at     TEXT NOT NULL DEFAULT '',
            UNIQUE(server_id, name)
        )""")

        # ── Orders ────────────────────────────────────────────────────────────
        cur.execute("""CREATE TABLE IF NOT EXISTS orders (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id      INTEGER NOT NULL REFERENCES customers(id),
            package_id       INTEGER NOT NULL REFERENCES packages(id),
            server_id        INTEGER NOT NULL REFERENCES servers(id),
            status           TEXT NOT NULL,
            order_type       TEXT NOT NULL DEFAULT 'new',
            xpanel_username  TEXT,
            xpanel_password  TEXT,
            config_text      TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )""")

        # ── Payments ──────────────────────────────────────────────────────────
        cur.execute("""CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id    INTEGER NOT NULL REFERENCES orders(id),
            server_id   INTEGER NOT NULL,
            amount      REAL,
            currency    TEXT NOT NULL DEFAULT 'IRR',
            payer_name  TEXT,
            status      TEXT NOT NULL,
            expires_at  TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )""")

        # ── Donation notifications (per-server reymit) ────────────────────────
        cur.execute("""CREATE TABLE IF NOT EXISTS donation_notifications (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id   INTEGER NOT NULL DEFAULT 0,
            donor_name  TEXT NOT NULL,
            amount      REAL NOT NULL,
            detail_raw  TEXT,
            status      TEXT NOT NULL DEFAULT 'unmatched',
            created_at  TEXT NOT NULL
        )""")

        # ── Runtime settings (global) ─────────────────────────────────────────
        cur.execute("""CREATE TABLE IF NOT EXISTS runtime_settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")

        # ── Audit logs ────────────────────────────────────────────────────────
        cur.execute("""CREATE TABLE IF NOT EXISTS audit_logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload    TEXT,
            created_at TEXT NOT NULL
        )""")

        # ── Migrations ────────────────────────────────────────────────────────
        for stmt in [
            "ALTER TABLE orders ADD COLUMN config_netmod TEXT",
            "ALTER TABLE servers ADD COLUMN server_type TEXT NOT NULL DEFAULT 'ssh'",
            "ALTER TABLE servers ADD COLUMN xpanel_username TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE servers ADD COLUMN xpanel_password TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE servers ADD COLUMN xpanel_inbound_id INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE servers ADD COLUMN xpanel_webbasepath TEXT NOT NULL DEFAULT '/'",
            "ALTER TABLE payments ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'donate'",
            "ALTER TABLE payments ADD COLUMN authority TEXT",
            "ALTER TABLE payments ADD COLUMN sms_code INTEGER",
            "ALTER TABLE payments ADD COLUMN sms_amount INTEGER",
            "ALTER TABLE packages ADD COLUMN duration_days INTEGER NOT NULL DEFAULT 30",
            "ALTER TABLE orders ADD COLUMN renew_notified INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE customers ADD COLUMN portal_token TEXT",
        ]:
            try:
                cur.execute(stmt)
            except Exception:
                pass

        # ── Backfill portal_token for existing customers ───────────────────────
        nulls = cur.execute("SELECT id FROM customers WHERE portal_token IS NULL").fetchall()
        for row in nulls:
            cur.execute("UPDATE customers SET portal_token=? WHERE id=?",
                        (secrets.token_hex(16), row["id"]))

        # ── Seed TOS ──────────────────────────────────────────────────────────
        if not cur.execute("SELECT id FROM tos_versions LIMIT 1").fetchone():
            cur.execute(
                "INSERT INTO tos_versions (content,created_at) VALUES (?,?)",
                (
                    "📋 *شرایط استفاده از CrabVPN*\n\n"
                    "🦀 خدمات ما تا زمانی که زیرساخت فعال باشد ارائه می‌شود.\n\n"
                    "⚠️ ضمانتی برای اتصال دائمی وجود ندارد.\n\n"
                    "✅ اشتراک‌ها تا زمانی که سرویس فعال است قابل استفاده‌اند.\n\n"
                    "🔒 اطلاعات شما کاملاً محرمانه است.\n\n"
                    "با زدن دکمه *قبول می‌کنم* شرایط فوق را پذیرفته‌اید.",
                    now_iso(),
                ),
            )

        conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# RUNTIME SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

def get_setting(key: str, default: str = "") -> str:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT value FROM runtime_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT INTO runtime_settings (key,value,updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value, now_iso()),
        )
        conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# TOS
# ══════════════════════════════════════════════════════════════════════════════

def get_latest_tos() -> Optional[sqlite3.Row]:
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM tos_versions ORDER BY id DESC LIMIT 1").fetchone()


def customer_accepted_tos(telegram_id: int) -> bool:
    tos = get_latest_tos()
    if not tos:
        return True
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT tos_accepted_version FROM customers WHERE telegram_id=?", (telegram_id,)
        ).fetchone()
    return row is not None and row["tos_accepted_version"] == tos["id"]


def mark_tos_accepted(telegram_id: int) -> None:
    tos = get_latest_tos()
    if not tos:
        return
    with closing(get_conn()) as conn:
        conn.execute(
            "UPDATE customers SET tos_accepted_version=?,updated_at=? WHERE telegram_id=?",
            (tos["id"], now_iso(), telegram_id),
        )
        conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# CUSTOMERS
# ══════════════════════════════════════════════════════════════════════════════

def generate_portal_token() -> str:
    return secrets.token_hex(16)   # 32-char hex


def upsert_customer(tg_user, phone: Optional[str] = None) -> int:
    full_name = " ".join(x for x in [tg_user.first_name, tg_user.last_name] if x).strip() or str(tg_user.id)
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT id,portal_token FROM customers WHERE telegram_id=?", (tg_user.id,)).fetchone()
        ts = now_iso()
        if row:
            new_token = row["portal_token"] or generate_portal_token()
            conn.execute(
                "UPDATE customers SET telegram_username=?,full_name=?,phone=COALESCE(?,phone),portal_token=COALESCE(portal_token,?),updated_at=? WHERE telegram_id=?",
                (tg_user.username, full_name, phone, new_token, ts, tg_user.id),
            )
            cid = int(row["id"])
        else:
            token = generate_portal_token()
            conn.execute(
                "INSERT INTO customers (telegram_id,telegram_username,full_name,phone,portal_token,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (tg_user.id, tg_user.username, full_name, phone, token, ts, ts),
            )
            cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    return cid


def get_customer_by_tg(telegram_id: int) -> Optional[sqlite3.Row]:
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM customers WHERE telegram_id=?", (telegram_id,)).fetchone()


def get_customer_by_token(token: str) -> Optional[sqlite3.Row]:
    with closing(get_conn()) as conn:
        return conn.execute("SELECT * FROM customers WHERE portal_token=?", (token,)).fetchone()


def customer_used_free_trial(telegram_id: int) -> bool:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT free_trial_used FROM customers WHERE telegram_id=?", (telegram_id,)).fetchone()
    return bool(row and row["free_trial_used"])


def mark_free_trial_used(telegram_id: int) -> None:
    with closing(get_conn()) as conn:
        conn.execute("UPDATE customers SET free_trial_used=1,updated_at=? WHERE telegram_id=?", (now_iso(), telegram_id))
        conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# SERVERS
# ══════════════════════════════════════════════════════════════════════════════

def get_all_servers(active_only=True) -> list[Server]:
    with closing(get_conn()) as conn:
        q = "SELECT * FROM servers"
        if active_only:
            q += " WHERE is_active=1"
        q += " ORDER BY display_order"
        rows = conn.execute(q).fetchall()
    return [Server(**dict(r)) for r in rows]


def get_server(server_id: int) -> Optional[Server]:
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    return Server(**dict(row)) if row else None


def upsert_server(data: dict) -> int:
    ts = now_iso()
    with closing(get_conn()) as conn:
        if data.get("id"):
            conn.execute(
                "UPDATE servers SET name=?,flag=?,location=?,xpanel_url=?,xpanel_token=?,"
                "ssh_host=?,ssh_port=?,reymit_url=?,sales_open=?,renewal_open=?,free_trial=?,"
                "is_active=?,display_order=?,server_type=?,xpanel_username=?,xpanel_password=?,"
                "xpanel_inbound_id=?,xpanel_webbasepath=?,updated_at=? WHERE id=?",
                (data["name"], data.get("flag","🌍"), data.get("location",""),
                 data["xpanel_url"], data.get("xpanel_token",""),
                 data.get("ssh_host","127.0.0.1"), int(data.get("ssh_port",22)),
                 data.get("reymit_url",""),
                 int(data.get("sales_open",1)), int(data.get("renewal_open",1)),
                 int(data.get("free_trial",0)), int(data.get("is_active",1)),
                 int(data.get("display_order",0)),
                 data.get("server_type","ssh"),
                 data.get("xpanel_username",""), data.get("xpanel_password",""),
                 int(data.get("xpanel_inbound_id",1)), data.get("xpanel_webbasepath","/"),
                 ts, data["id"]),
            )
            sid = data["id"]
        else:
            conn.execute(
                "INSERT INTO servers (name,flag,location,xpanel_url,xpanel_token,ssh_host,ssh_port,"
                "reymit_url,sales_open,renewal_open,free_trial,is_active,display_order,"
                "server_type,xpanel_username,xpanel_password,xpanel_inbound_id,xpanel_webbasepath,"
                "created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (data["name"], data.get("flag","🌍"), data.get("location",""),
                 data["xpanel_url"], data.get("xpanel_token",""),
                 data.get("ssh_host","127.0.0.1"), int(data.get("ssh_port",22)),
                 data.get("reymit_url",""),
                 int(data.get("sales_open",1)), int(data.get("renewal_open",1)),
                 int(data.get("free_trial",0)), int(data.get("is_active",1)),
                 int(data.get("display_order",0)),
                 data.get("server_type","ssh"),
                 data.get("xpanel_username",""), data.get("xpanel_password",""),
                 int(data.get("xpanel_inbound_id",1)), data.get("xpanel_webbasepath","/"),
                 ts, ts),
            )
            sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # Seed default packages for new server
            for row in [
                ("Test-50MB", 50, "mb", None,   1,  5, "free_trial"),
                ("1GB",        1, "gb", 50000,  1, 10, None),
                ("5GB",        5, "gb", 200000, 1, 20, None),
                ("10GB",      10, "gb", 350000, 1, 30, None),
                ("20GB",      20, "gb", 600000, 1, 40, None),
            ]:
                conn.execute(
                    "INSERT OR IGNORE INTO packages (server_id,name,traffic_amount,traffic_unit,price_irr,is_active,display_order,notes,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (sid, *row, ts),
                )
        conn.commit()
    return sid


def delete_server(server_id: int) -> bool:
    with closing(get_conn()) as conn:
        in_use = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE server_id=? AND status='active'", (server_id,)
        ).fetchone()[0]
        if in_use:
            return False
        conn.execute("DELETE FROM servers WHERE id=?", (server_id,))
        conn.commit()
    return True


# ══════════════════════════════════════════════════════════════════════════════
# PACKAGES
# ══════════════════════════════════════════════════════════════════════════════

def get_packages_for_server(server_id: int, active_only=True) -> list[Package]:
    with closing(get_conn()) as conn:
        q = "SELECT id,server_id,name,traffic_amount,traffic_unit,price_irr,is_active,display_order,COALESCE(duration_days,30) AS duration_days FROM packages WHERE server_id=?"
        params = [server_id]
        if active_only:
            q += " AND is_active=1 AND name != 'Test-50MB'"
        q += " ORDER BY display_order"
        rows = conn.execute(q, params).fetchall()
    return [Package(**dict(r)) for r in rows]


def get_package(pkg_id: int) -> Optional[Package]:
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT id,server_id,name,traffic_amount,traffic_unit,price_irr,is_active,display_order,COALESCE(duration_days,30) AS duration_days FROM packages WHERE id=?", (pkg_id,)
        ).fetchone()
    return Package(**dict(row)) if row else None


def upsert_package(data: dict) -> int:
    ts = now_iso()
    dur = int(data.get("duration_days") or 30)
    with closing(get_conn()) as conn:
        if data.get("id"):
            conn.execute(
                "UPDATE packages SET name=?,traffic_amount=?,traffic_unit=?,price_irr=?,is_active=?,display_order=?,duration_days=?,updated_at=? WHERE id=?",
                (data["name"], int(data["traffic_amount"]), data["traffic_unit"],
                 data.get("price_irr"), int(data.get("is_active",1)),
                 int(data.get("display_order",0)), dur, ts, data["id"]),
            )
            pid = data["id"]
        else:
            conn.execute(
                "INSERT INTO packages (server_id,name,traffic_amount,traffic_unit,price_irr,is_active,display_order,duration_days,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (data["server_id"], data["name"], int(data["traffic_amount"]),
                 data["traffic_unit"], data.get("price_irr"),
                 int(data.get("is_active",1)), int(data.get("display_order",0)), dur, ts),
            )
            pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    return pid


def delete_package(pkg_id: int) -> tuple[bool, str]:
    with closing(get_conn()) as conn:
        in_use = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE package_id=? AND status='active'", (pkg_id,)
        ).fetchone()[0]
        if in_use:
            return False, f"{in_use} سفارش فعال دارد"
        conn.execute("DELETE FROM packages WHERE id=?", (pkg_id,))
        conn.commit()
    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
# ORDERS
# ══════════════════════════════════════════════════════════════════════════════

def get_active_orders_for_customer(telegram_id: int) -> list[sqlite3.Row]:
    """همه اشتراک‌های فعال کاربر از همه سرورها"""
    with closing(get_conn()) as conn:
        return conn.execute(
            """SELECT o.*, p.name AS pkg_name, p.traffic_amount, p.traffic_unit,
                      s.name AS server_name, s.flag, s.location, s.ssh_host, s.ssh_port
               FROM orders o
               JOIN packages p ON p.id=o.package_id
               JOIN servers  s ON s.id=o.server_id
               WHERE o.customer_id=(SELECT id FROM customers WHERE telegram_id=?)
                 AND o.status='active' AND o.xpanel_username IS NOT NULL
               ORDER BY o.id DESC""",
            (telegram_id,),
        ).fetchall()


def get_active_order_on_server(telegram_id: int, server_id: int) -> Optional[sqlite3.Row]:
    with closing(get_conn()) as conn:
        return conn.execute(
            """SELECT o.*, p.name AS pkg_name, p.traffic_amount, p.traffic_unit
               FROM orders o JOIN packages p ON p.id=o.package_id
               WHERE o.customer_id=(SELECT id FROM customers WHERE telegram_id=?)
                 AND o.server_id=? AND o.status='active' AND o.xpanel_username IS NOT NULL
               ORDER BY o.id DESC LIMIT 1""",
            (telegram_id, server_id),
        ).fetchone()


def customer_has_account_on_server(telegram_id: int, server_id: int) -> bool:
    return get_active_order_on_server(telegram_id, server_id) is not None


def get_stored_password(telegram_id: int, server_id: int) -> Optional[str]:
    order = get_active_order_on_server(telegram_id, server_id)
    return order["xpanel_password"] if order else None


def create_order(customer_id: int, package_id: int, server_id: int, order_type: str = "new") -> int:
    ts = now_iso()
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT INTO orders (customer_id,package_id,server_id,status,order_type,created_at,updated_at) VALUES (?,?,?,'pending_payment',?,?,?)",
            (customer_id, package_id, server_id, order_type, ts, ts),
        )
        oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    return oid


def set_order_active(order_id: int, username: str, password: str, config_text: str, config_netmod: str = "") -> None:
    with closing(get_conn()) as conn:
        conn.execute(
            "UPDATE orders SET xpanel_username=?,xpanel_password=?,config_text=?,config_netmod=?,status='active',updated_at=? WHERE id=?",
            (username, password, config_text, config_netmod, now_iso(), order_id),
        )
        conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# PAYMENTS
# ══════════════════════════════════════════════════════════════════════════════

PAYMENT_EXPIRE_MIN = int(os.getenv("PAYMENT_EXPIRE_MIN", "30"))


def create_payment(order_id: int, server_id: int, amount: Optional[float], payer_name: str = "") -> int:
    ts      = now_iso()
    expires = (datetime.utcnow() + timedelta(minutes=PAYMENT_EXPIRE_MIN)).isoformat(timespec="seconds")
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT INTO payments (order_id,server_id,amount,currency,payer_name,status,expires_at,created_at,updated_at) VALUES (?,?,?,?,?,'pending_review',?,?,?)",
            (order_id, server_id, amount, "IRR", payer_name, expires, ts, ts),
        )
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    return pid


def expire_old_payments() -> None:
    now = now_iso()
    with closing(get_conn()) as conn:
        conn.execute(
            "UPDATE payments SET status='expired',updated_at=? WHERE status='pending_review' AND expires_at < ?",
            (now, now),
        )
        conn.commit()


def save_donation(server_id: int, donor_name: str, amount: float, detail_raw: str) -> int:
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT INTO donation_notifications (server_id,donor_name,amount,detail_raw,status,created_at) VALUES (?,?,?,?,'unmatched',?)",
            (server_id, donor_name, amount, detail_raw, now_iso()),
        )
        nid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    return nid


def get_payment_by_authority(authority: str) -> Optional[sqlite3.Row]:
    with closing(get_conn()) as conn:
        return conn.execute(
            """SELECT p.*,o.id AS order_id,COALESCE(o.order_type,'new') AS order_type,
                      o.server_id,c.telegram_id,c.full_name
               FROM payments p JOIN orders o ON o.id=p.order_id
               JOIN customers c ON c.id=o.customer_id
               WHERE p.authority=? AND p.status='pending_review'""",
            (authority,),
        ).fetchone()


def alloc_sms_code(payment_id: int, price_irr: float) -> tuple[int, int]:
    """
    Assign a unique 4-digit code (1000-9999) to a pending payment.
    sms_amount = price_irr (Tomans) * 10 + code  →  exact Rial amount user must transfer.
    Returns (code, sms_amount).
    """
    with closing(get_conn()) as conn:
        used = {
            row[0]
            for row in conn.execute(
                "SELECT sms_code FROM payments WHERE sms_code IS NOT NULL AND status='pending_review'"
            ).fetchall()
        }
    all_codes = set(range(1000, 10000))
    free = list(all_codes - used)
    if not free:
        free = list(all_codes)  # all in use — allow reuse as last resort
    code = random.choice(free)
    sms_amount = int(price_irr) * 10 + code
    with closing(get_conn()) as conn:
        conn.execute(
            "UPDATE payments SET sms_code=?,sms_amount=?,updated_at=? WHERE id=?",
            (code, sms_amount, now_iso(), payment_id),
        )
        conn.commit()
    return code, sms_amount


def get_payment_by_sms_amount(amount_rials: int) -> Optional[sqlite3.Row]:
    """Find a pending_review donate payment whose sms_amount matches the parsed SMS amount."""
    expire_old_payments()
    with closing(get_conn()) as conn:
        return conn.execute(
            """SELECT p.*,o.id AS order_id,COALESCE(o.order_type,'new') AS order_type,
                      o.server_id,c.telegram_id,c.full_name
               FROM payments p JOIN orders o ON o.id=p.order_id
               JOIN customers c ON c.id=o.customer_id
               WHERE p.sms_amount=? AND p.status='pending_review'
                 AND p.payment_method='donate'""",
            (amount_rials,),
        ).fetchone()


def release_sms_code(payment_id: int) -> None:
    """Clear sms_code after payment confirmed or expired so it can be reused."""
    with closing(get_conn()) as conn:
        conn.execute(
            "UPDATE payments SET sms_code=NULL,updated_at=? WHERE id=?",
            (now_iso(), payment_id),
        )
        conn.commit()


def try_match_donation_global(donor_name: str, donated_amount: float) -> Optional[dict]:
    """Match دونیت با هر pending payment (فارغ از سرور)"""
    expire_old_payments()
    with closing(get_conn()) as conn:
        payments = conn.execute(
            """SELECT p.*, o.id AS order_id, COALESCE(o.order_type,'new') AS order_type,
                      o.package_id, o.server_id, c.telegram_id, c.full_name
               FROM payments p
               JOIN orders o ON o.id=p.order_id
               JOIN customers c ON c.id=o.customer_id
               WHERE p.status='pending_review'
                 AND p.payer_name IS NOT NULL AND p.payer_name != ''
               ORDER BY p.id ASC"""
        ).fetchall()

    donor_lower = donor_name.lower().strip()
    for pay in payments:
        payer      = (pay["payer_name"] or "").lower().strip()
        exp_amount = float(pay["amount"] or 0)
        name_ok    = donor_lower in payer or payer in donor_lower
        tolerance  = max(exp_amount * 0.05, 1000)
        amount_ok  = abs(exp_amount - donated_amount) <= tolerance
        if name_ok and amount_ok:
            return {"payment": pay}
    return None


def try_match_donation(server_id: int, donor_name: str, donated_amount: float) -> Optional[dict]:
    """Match دونیت با pending payment روی همون سرور"""
    expire_old_payments()
    with closing(get_conn()) as conn:
        payments = conn.execute(
            """SELECT p.*, o.id AS order_id, COALESCE(o.order_type,'new') AS order_type,
                      o.package_id, c.telegram_id, c.full_name
               FROM payments p
               JOIN orders o ON o.id=p.order_id
               JOIN customers c ON c.id=o.customer_id
               WHERE p.status='pending_review' AND p.server_id=?
                 AND p.payer_name IS NOT NULL AND p.payer_name != ''
               ORDER BY p.id ASC""",
            (server_id,),
        ).fetchall()

    donor_lower = donor_name.lower().strip()
    for pay in payments:
        payer      = (pay["payer_name"] or "").lower().strip()
        exp_amount = float(pay["amount"] or 0)
        name_ok    = donor_lower in payer or payer in donor_lower
        tolerance  = max(exp_amount * 0.05, 1000)
        amount_ok  = abs(exp_amount - donated_amount) <= tolerance
        if name_ok and amount_ok:
            return {"payment": pay}
    return None


# ══════════════════════════════════════════════════════════════════════════════
# MISC HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def generate_password(length: int = 12) -> str:
    return "".join(random.SystemRandom().choice(string.ascii_letters + string.digits) for _ in range(length))