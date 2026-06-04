"""
xpanel.py — Unified VPN panel adapter

Supports two server types:
  - "ssh"  : legacy SSH xpanel API (token-based, /api/adduser etc.)
  - "xray" : 3X-UI API with xray core (session-cookie, VLESS/VMess clients)
"""
import json
import logging
import os
import threading
import time
import uuid as _uuid_mod
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse, urlencode

import urllib3
import requests
from urllib.parse import urlparse as _urlparse_check

# Suppress SSL warnings for plain-IP servers (self-signed certs)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


def _ssl_verify(server) -> bool:
    """Return False for bare-IP URLs (no valid cert possible), True otherwise."""
    try:
        host = _urlparse_check(server.xpanel_url).hostname or ""
        # If hostname is a raw IP address, skip SSL verification
        import ipaddress
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True

DEFAULT_EXP_DAYS = int(os.getenv("DEFAULT_EXP_DAYS", "30"))

# ══════════════════════════════════════════════════════════════════════════════
# ── SSH (legacy xpanel) backend ───────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _ssh_post(server, endpoint: str, data: dict) -> dict:
    url = f"{server.xpanel_url.rstrip('/')}/api/{endpoint}"
    data["token"] = server.xpanel_token
    resp = requests.post(url, data=data, timeout=25, verify=_ssl_verify(server))
    if resp.text.strip().startswith("<"):
        raise RuntimeError(f"SSH xpanel HTML response on {endpoint} (status={resp.status_code}, url={url}, snippet={resp.text.strip()[:120]!r})")
    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text[:200]}
    if not resp.ok:
        raise RuntimeError(f"SSH xpanel [{resp.status_code}] {endpoint}: {resp.text[:200]}")
    return body


def _ssh_adduser(server, pkg, username: str, password: str, customer) -> str:
    days = int(getattr(pkg, "duration_days", None) or DEFAULT_EXP_DAYS)
    expdate = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
    tg_id = customer.get("telegram_id", "") if isinstance(customer, dict) else getattr(customer, "telegram_id", "")
    body = _ssh_post(server, "adduser", {
        "username": username, "password": password,
        "email": "", "mobile": (customer.get("phone") if isinstance(customer, dict) else "") or "",
        "multiuser": int(os.getenv("DEFAULT_MULTIUSER", "1")),
        "traffic": pkg.traffic_amount, "type_traffic": pkg.traffic_unit,
        "expdate": expdate, "connection_start": "",
        "desc": f"tg={tg_id} pkg={pkg.name}",
    })
    if isinstance(body, dict) and "exist" in body.get("message", "").lower():
        return password
    return password


def _ssh_get_user(server, username: str) -> Optional[dict]:
    url = f"{server.xpanel_url.rstrip('/')}/api/{server.xpanel_token}/user/{username}"
    try:
        resp = requests.get(url, timeout=10, verify=_ssl_verify(server))
        if resp.ok:
            data = resp.json()
            if isinstance(data, list) and data and isinstance(data[0], dict):
                u = data[0]
                traffics = u.get("traffics", [])
                used_mb = float(traffics[0].get("total", 0) or 0) if traffics else 0.0
                total_mb = float(u.get("traffic", 0) or 0)
                return {
                    "username": u.get("username"), "status": u.get("status"),
                    "password": u.get("password") or u.get("passwd") or "",
                    "total_mb": total_mb, "used_mb": used_mb,
                    "expdate": u.get("end_date") or "—",
                }
    except Exception as e:
        logger.warning("ssh_get_user error: %s", e)
    return None


def _ssh_renewal(server, username: str, duration_days: int = None, carry_over_mb: float = 0, pkg=None) -> None:
    days = int(duration_days or DEFAULT_EXP_DAYS)
    data = {"username": username, "day_date": days, "re_date": "yes", "re_traffic": "yes"}
    if pkg:
        data["traffic"]      = pkg.traffic_amount
        data["type_traffic"] = pkg.traffic_unit
    _ssh_post(server, "renewal", data)
    if carry_over_mb > 1:
        _ssh_add_traffic(server, username, int(carry_over_mb), "mb")


def _ssh_add_traffic(server, username: str, traffic: int, unit: str) -> None:
    _ssh_post(server, "traffic", {"username": username, "traffic": traffic, "type_traffic": unit})


def _ssh_activate(server, username: str) -> None:
    _ssh_post(server, "active", {"username": username})


def _ssh_online_count(server) -> int:
    try:
        url = f"{server.xpanel_url.rstrip('/')}/api/{server.xpanel_token}/online"
        resp = requests.get(url, timeout=5, verify=_ssl_verify(server))
        if resp.ok:
            d = resp.json()
            return len(d) if isinstance(d, list) else int(d.get("count", 0))
    except Exception:
        pass
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# ── XRAY (3X-UI) backend ─────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

_xray_sessions: dict = {}
_xray_lock = threading.Lock()


def _xray_base(server) -> str:
    base = (getattr(server, "xpanel_webbasepath", None) or "/").rstrip("/")
    return f"{server.xpanel_url.rstrip('/')}{base}"


def _xray_login_url(server) -> str:
    """Build the correct login URL.

    Some panels store the full path including /login in xpanel_webbasepath
    (e.g. /yToWTpSjYio94KhOn0/login). In that case _xray_base() already
    points at the login endpoint — do NOT append /login again.
    """
    base = _xray_base(server).rstrip("/")
    if base.endswith("/login"):
        return base + "/"   # already the login endpoint
    return base + "/login/"


def _xray_do_login(server) -> requests.Session:
    """Login to 3X-UI panel.

    3X-UI v3.0+ requires a CSRF token fetched from /csrf-token before login.
    Older versions work without it.  We try CSRF-aware login first; if the
    endpoint returns 404 we fall back to the plain POST flow.
    """
    session = requests.Session()
    verify = _ssl_verify(server)
    base = _xray_base(server).rstrip("/")

    # ── 1. Determine login URL ────────────────────────────────────────────────
    # webbasepath may already include "/login" (e.g. /secret/login)
    if base.endswith("/login"):
        login_url = base + "/"
        api_base  = base[: -len("/login")]   # strip /login for CSRF endpoint
    else:
        login_url = base + "/login/"
        api_base  = base

    # ── 2. Fetch CSRF token (3X-UI v3+) ──────────────────────────────────────
    csrf_token = None
    try:
        csrf_resp = session.get(
            f"{api_base}/csrf-token",
            timeout=10, verify=verify,
        )
        if csrf_resp.ok:
            csrf_body = csrf_resp.json()
            csrf_token = csrf_body.get("data") or csrf_body.get("token") or csrf_body.get("obj")
    except Exception:
        pass   # older panel — proceed without CSRF

    # ── 3. POST login ─────────────────────────────────────────────────────────
    headers = {}
    if csrf_token:
        headers["X-Csrf-Token"] = csrf_token

    login_data = {
        "username": server.xpanel_username,
        "password": server.xpanel_password,
    }

    try:
        resp = session.post(
            login_url, data=login_data, headers=headers,
            timeout=15, verify=verify, allow_redirects=True,
        )
    except requests.exceptions.SSLError as exc:
        raise RuntimeError(
            f"3X-UI SSL error for {server.xpanel_url} — "
            f"use a domain instead of bare IP, or check cert. Detail: {exc}"
        )

    if resp.status_code == 404:
        # Try without trailing slash
        try:
            resp = session.post(
                login_url.rstrip("/"), data=login_data, headers=headers,
                timeout=15, verify=verify,
            )
        except Exception:
            pass

    if resp.status_code == 403:
        # Retry without trailing slash before giving up
        try:
            resp2 = session.post(
                login_url.rstrip("/"), data=login_data, headers=headers,
                timeout=15, verify=verify,
            )
            if resp2.status_code not in (403, 404):
                resp = resp2
            else:
                resp = resp2  # use latest for error message
        except Exception:
            pass
    if resp.status_code == 403:
        raise RuntimeError(
            f"3X-UI login 403 Forbidden — "
            "check xpanel_username / xpanel_password / webbasepath. "
            f"URL tried: {login_url} | body: {resp.text[:200]!r}"
        )
    if resp.status_code == 404:
        raise RuntimeError(
            f"3X-UI login endpoint not found at {login_url} — "
            "check xpanel_webbasepath on server record"
        )

    resp.raise_for_status()

    try:
        body = resp.json()
    except Exception:
        raise RuntimeError(
            f"3X-UI login non-JSON response ({resp.status_code}): {resp.text[:200]}"
        )
    if not body.get("success"):
        raise RuntimeError(f"3X-UI login failed: {body.get('msg', 'unknown')}")

    # Store CSRF token on the session so _xray_req can attach it to every POST
    post_csrf = csrf_token
    for _cname in ("XSRF-TOKEN", "csrf_token", "X-CSRF-TOKEN"):
        _cv = session.cookies.get(_cname)
        if _cv:
            post_csrf = _cv
            break
    session._xray_csrf_token = post_csrf or ""

    return session


def _xray_session(server) -> requests.Session:
    with _xray_lock:
        cached = _xray_sessions.get(server.id)
        if cached and cached["expires"] > time.time():
            return cached["session"]
        sess = _xray_do_login(server)
        _xray_sessions[server.id] = {"session": sess, "expires": time.time() + 3600}
        return sess


def _xray_req(server, method: str, path: str, **kwargs) -> dict:
    """Make authenticated request; re-login once on 401/403."""
    url = f"{_xray_base(server)}{path}"
    verify = _ssl_verify(server)
    for attempt in range(2):
        sess = _xray_session(server)
        req_kwargs = dict(kwargs)
        headers = dict(req_kwargs.pop("headers", {}))
        if method.lower() in ("post", "put", "delete", "patch"):
            csrf = getattr(sess, "_xray_csrf_token", None)
            for _cname in ("XSRF-TOKEN", "csrf_token"):
                _cv = sess.cookies.get(_cname)
                if _cv:
                    csrf = _cv
                    break
            if csrf:
                headers.setdefault("X-Csrf-Token", csrf)
        try:
            resp = getattr(sess, method)(url, timeout=25, verify=verify, headers=headers, **req_kwargs)
        except requests.RequestException as exc:
            raise RuntimeError(f"3X-UI request error: {exc}")
        if resp.status_code in (401, 403) and attempt == 0:
            with _xray_lock:
                _xray_sessions.pop(server.id, None)
            continue
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text[:200]}
    raise RuntimeError("3X-UI auth failed after retry")


def _xray_get_client(server, username: str) -> Optional[dict]:
    """Fetch clientStats by email. Returns the stats dict or None."""
    try:
        try:
            body = _xray_req(server, "get", f"/panel/api/clients/traffic/{username}")
            obj = body.get("obj")
            return (obj if isinstance(obj, dict) else (obj[0] if obj else None)) if obj else None
        except Exception as e:
            if "404" not in str(e):
                raise
        # v3.x endpoint not found — fall back to v2.x
        body = _xray_req(server, "get", f"/panel/api/inbounds/getClientTraffics/{username}")
        obj = body.get("obj")
        return (obj if isinstance(obj, dict) else (obj[0] if obj else None)) if obj else None
    except Exception as e:
        logger.warning("xray_get_client error for %s: %s", username, e)
        return None


def _xray_get_inbound(server) -> dict:
    try:
        body = _xray_req(server, "get", f"/panel/api/inbounds/get/{server.xpanel_inbound_id}")
        result = body.get("obj") or {}
        if not result:
            logger.warning("xray_get_inbound: empty obj for server %s body=%s", server.id, str(body)[:200])
        return result
    except Exception as e:
        logger.warning("xray_get_inbound error server %s: %s", server.id, e)
        return {}


def _xray_adduser(server, pkg, username: str, customer) -> str:
    client_uuid = str(_uuid_mod.uuid4())
    days = int(getattr(pkg, "duration_days", None) or DEFAULT_EXP_DAYS)
    expiry_ms = int((datetime.utcnow() + timedelta(days=days)).timestamp() * 1000)
    total_bytes = pkg.traffic_amount * (1024 ** 3 if pkg.traffic_unit == "gb" else 1024 ** 2)
    tg_id_raw = customer.get("telegram_id", "") if isinstance(customer, dict) else getattr(customer, "telegram_id", "")
    tg_id_int = int(tg_id_raw) if tg_id_raw else 0
    tg_id_str = str(tg_id_raw) if tg_id_raw else ""
    # Use tg_id as email identifier; fall back to username if tg_id is unavailable
    email = tg_id_str if tg_id_str else username
    client = {
        "id": client_uuid, "flow": "",
        "email": email, "limitIp": 0,
        "totalGB": total_bytes, "expiryTime": expiry_ms,
        "enable": True, "tgId": tg_id_int,
        "subId": "", "reset": 0,
        "comment": f"pkg={pkg.name}",
    }
    settings_str = json.dumps({"clients": [client]})

    def _handle_exist(msg: str) -> Optional[str]:
        if "exist" in msg.lower() or "duplicate" in msg.lower():
            existing = _xray_get_client(server, email)
            if existing and existing.get("uuid"):
                return existing["uuid"]
        return None

    # Try v3.x path: {"client": {...}, "inboundIds": [...]}
    try:
        body = _xray_req(server, "post", "/panel/api/clients/add",
                         json={"client": client, "inboundIds": [int(server.xpanel_inbound_id)]})
        if body.get("success"):
            return client_uuid
        msg = body.get("msg", "")
        uid = _handle_exist(msg)
        if uid:
            return uid
        raise RuntimeError(f"addClient failed: {msg}")
    except Exception as e:
        if "404" not in str(e):
            raise

    # Fall back to v2.x path — form-encoded body
    body = _xray_req(server, "post", "/panel/api/inbounds/addClient",
                     data={"id": server.xpanel_inbound_id, "settings": settings_str})
    if not body.get("success"):
        msg = body.get("msg", "")
        uid = _handle_exist(msg)
        if uid:
            return uid
        raise RuntimeError(f"addClient failed: {msg}")
    return client_uuid


def _xray_get_user(server, username: str) -> Optional[dict]:
    try:
        u = _xray_get_client(server, username)
        if not u:
            return None
        up = float(u.get("up", 0) or 0)
        down = float(u.get("down", 0) or 0)
        total = float(u.get("total", 0) or 0)
        expiry_ms = int(u.get("expiryTime", 0) or 0)
        expdate = datetime.utcfromtimestamp(expiry_ms / 1000).strftime("%Y-%m-%d") if expiry_ms else "—"
        return {
            "username": u.get("email", username),
            "uuid": u.get("uuid", ""),
            "status": "active" if u.get("enable") else "disabled",
            "total_mb": total / (1024 * 1024),
            "used_mb": (up + down) / (1024 * 1024),
            "expdate": expdate,
        }
    except Exception as e:
        logger.warning("xray_get_user error for %s: %s", username, e)
    return None


def _xray_reset_traffic(server, username: str) -> None:
    """Reset client traffic counters. Tries v3.x, falls back to v2.x."""
    try:
        _xray_req(server, "post", f"/panel/api/clients/resetTraffic/{username}")
        return
    except Exception as e:
        if "404" not in str(e):
            raise
    # Fall back to v2.x
    _xray_req(server, "post",
              f"/panel/api/inbounds/{server.xpanel_inbound_id}/resetClientTraffic/{username}")


def _xray_update_client(server, u: dict, **overrides) -> None:
    """Update client settings. Tries v3.x /panel/api/clients/update/{email}, falls back to v2.x."""
    email = u.get("email", "")
    client = {
        "id": u["uuid"], "flow": u.get("flow", ""),
        "email": email, "limitIp": u.get("limitIp", 0),
        "totalGB": int(u.get("total", 0) or 0),
        "expiryTime": u.get("expiryTime", 0),
        "enable": True,
        "tgId": int(u.get("tgId", 0) or 0), "subId": u.get("subId", ""), "reset": 0,
        "comment": u.get("comment", ""),
    }
    client.update(overrides)
    # Try v3.x
    try:
        body = _xray_req(server, "post", f"/panel/api/clients/update/{email}", json=client)
        if body.get("success"):
            return
        raise RuntimeError(f"updateClient failed: {body.get('msg', '')}")
    except Exception as e:
        if "404" not in str(e):
            raise
    # Fall back to v2.x
    payload = {"id": server.xpanel_inbound_id, "settings": json.dumps({"clients": [client]})}
    body = _xray_req(server, "post", f"/panel/api/inbounds/updateClient/{u['uuid']}", json=payload)
    if not body.get("success"):
        raise RuntimeError(f"updateClient failed: {body.get('msg', '')}")


def _xray_renewal(server, username: str, duration_days: int = None, carry_over_mb: float = 0, pkg=None) -> None:
    u = _xray_get_client(server, username)
    if not u or not u.get("uuid"):
        raise RuntimeError(f"Client {username} not found on server {server.id}")
    _xray_reset_traffic(server, username)
    if pkg:
        new_bytes = int(pkg.traffic_amount * (1024**3 if pkg.traffic_unit == "gb" else 1024**2))
    else:
        new_bytes = int(u.get("total", 0) or 0)
    total_bytes = new_bytes + int(carry_over_mb * 1024 * 1024)
    days = int(duration_days or DEFAULT_EXP_DAYS)
    expiry_ms = int((datetime.utcnow() + timedelta(days=days)).timestamp() * 1000)
    _xray_update_client(server, u, expiryTime=expiry_ms, totalGB=total_bytes)


def _xray_add_traffic(server, username: str, traffic: int, unit: str) -> None:
    u = _xray_get_client(server, username)
    if not u or not u.get("uuid"):
        raise RuntimeError(f"Client {username} not found")
    add_bytes = traffic * (1024 ** 3 if unit == "gb" else 1024 ** 2)
    new_total = int(u.get("total", 0) or 0) + add_bytes
    _xray_update_client(server, u, totalGB=new_total)


def _xray_activate(server, username: str) -> None:
    try:
        u = _xray_get_client(server, username)
        if not u or not u.get("uuid") or u.get("enable"):
            return
        _xray_update_client(server, u, enable=True)
    except Exception as e:
        logger.warning("xray_activate error for %s: %s", username, e)


def _xray_online_count(server) -> int:
    try:
        body = _xray_req(server, "post", "/panel/api/inbounds/onlines")
        obj = body.get("obj")
        return len(obj) if isinstance(obj, list) else 0
    except Exception:
        return 0


def _xray_build_config(server, username: str, client_uuid: str) -> str:
    """Build a shareable proxy link from inbound settings."""
    try:
        inbound = _xray_get_inbound(server)
        if not inbound:
            logger.warning("xray_build_config: empty inbound for server %s", server.id)
        port = inbound.get("port", 443)
        protocol = inbound.get("protocol", "vless")
        # streamSettings may be a JSON string (v2.x) or already a dict (v3.x)
        raw_stream = inbound.get("streamSettings") or "{}"
        stream = raw_stream if isinstance(raw_stream, dict) else json.loads(raw_stream)
        network = stream.get("network", "tcp")
        security = stream.get("security", "none")

        host = urlparse(server.xpanel_url).hostname or server.xpanel_url
        params: dict = {"encryption": "none", "security": security, "type": network}

        if security == "tls":
            tls = stream.get("tlsSettings", {})
            if sni := tls.get("serverName"):
                params["sni"] = sni
            if fp := tls.get("fingerprint"):
                params["fp"] = fp

        elif security == "reality":
            real = stream.get("realitySettings", {})
            settings = real.get("settings", {})
            names = real.get("serverNames") or []
            if names:
                params["sni"] = names[0]
            if pbk := settings.get("publicKey"):
                params["pbk"] = pbk
            if fp := settings.get("fingerprint"):
                params["fp"] = fp
            shorts = real.get("shortIds") or []
            if shorts:
                params["sid"] = shorts[0]
            if spx := settings.get("spiderX"):
                params["spx"] = spx

        if network == "ws":
            ws = stream.get("wsSettings", {})
            if path := ws.get("path"):
                params["path"] = path
            if h := (ws.get("headers") or {}).get("Host"):
                params["host"] = h
        elif network == "grpc":
            grpc = stream.get("grpcSettings", {})
            if svc := grpc.get("serviceName"):
                params["serviceName"] = svc

        remark = f"CrabVPN-{server.name}"
        return f"{protocol}://{client_uuid}@{host}:{port}?{urlencode(params)}#{remark}"
    except Exception as e:
        logger.warning("xray_build_config error: %s", e)
        return ""


# ══════════════════════════════════════════════════════════════════════════════
# ── Public dispatcher ─────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

def _is_xray(server) -> bool:
    return getattr(server, "server_type", "ssh") == "xray"


def adduser(server, pkg, username: str, password: str, customer) -> str:
    """Create user/client. Returns credential (password for SSH, UUID for xray)."""
    if _is_xray(server):
        return _xray_adduser(server, pkg, username, customer)
    return _ssh_adduser(server, pkg, username, password, customer)


def get_user(server, username: str) -> Optional[dict]:
    if _is_xray(server):
        return _xray_get_user(server, username)
    return _ssh_get_user(server, username)


def renewal(server, username: str, duration_days: int = None, carry_over_mb: float = 0, pkg=None) -> None:
    if _is_xray(server):
        _xray_renewal(server, username, duration_days, carry_over_mb, pkg)
    else:
        _ssh_renewal(server, username, duration_days, carry_over_mb, pkg)


def add_traffic(server, username: str, traffic: int, unit: str) -> None:
    if _is_xray(server):
        _xray_add_traffic(server, username, traffic, unit)
    else:
        _ssh_add_traffic(server, username, traffic, unit)


def activate(server, username: str) -> None:
    if _is_xray(server):
        _xray_activate(server, username)
    else:
        _ssh_activate(server, username)


def online_count(server) -> int:
    if _is_xray(server):
        return _xray_online_count(server)
    return _ssh_online_count(server)


def build_config(server, pkg_name: str, username: str, credential: str) -> tuple[str, str]:
    """Return (config_primary, config_secondary).

    SSH:  (npvt-ssh link, netmod ssh link)
    Xray: (vless/vmess proxy link, "")
    """
    if _is_xray(server):
        return _xray_build_config(server, username, credential), ""
    # SSH
    import base64, json as _json
    prefix = os.getenv("CONFIG_REMARKS_PREFIX", "CrabVPN")
    payload = {
        "sshConfigType": "SSH-Direct",
        "remarks": f"{prefix}-{server.name}-{pkg_name}",
        "sshHost": server.ssh_host, "sshPort": server.ssh_port,
        "sshUsername": username, "sshPassword": credential,
        "sni": "", "tlsVersion": "DEFAULT", "httpProxy": "",
        "authenticateProxy": False, "proxyUsername": "", "proxyPassword": "",
        "payload": "", "dnsTTMode": "UDP", "dnsServer": "", "nameserver": "",
        "publicKey": "", "udpgwPort": 7300, "udpgwTransparentDNS": True,
    }
    npv = "npvt-ssh://" + base64.b64encode(
        _json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode("ascii")
    netmod = f"ssh://{username}:{credential}@{server.ssh_host}:{server.ssh_port}/#@MR_Crab_VPN"
    return npv, netmod

