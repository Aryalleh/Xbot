import asyncio
import json
import re
import threading
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright
from websockets.server import serve

URL = "https://reymit.ir/overlay/?aparatfollowalert=off&aparatsubalert=off&aparatraidalert=off&aparatrubyalert=off&key=YzM4ZDEwNDg0ZGU0NzUxZC9qUVRYQU1zbDZuWXYwbCt0TkdoMFpPS29DZXk3Sys3ZlRLaW5yRTZoZFpTdmJHTkh0RmVmdDVzK3F5azRjMTc%3D"

LOCAL_WS_HOST = "127.0.0.1"
LOCAL_WS_PORT = 8765

clients = set()
loop_ref: asyncio.AbstractEventLoop | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_amounts(detail_text: str) -> tuple[int | None, int | None, str | None]:
    """
    متن‌هایی شبیه:
    مبلغ ۱,۰۰۰ تومان حمایت کرد!
    را parse می‌کند.
    خروجی: (amount_toman, amount_rial, currency)
    """
    if not detail_text:
        return None, None, None

    trans = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    text = detail_text.translate(trans)

    m = re.search(r"(\d[\d,]*)", text)
    if not m:
        return None, None, None

    try:
        amount = int(m.group(1).replace(",", ""))
    except ValueError:
        return None, None, None

    if "تومان" in detail_text or "toman" in text.lower():
        return amount, amount * 10, "Toman"

    if "ریال" in detail_text or "rial" in text.lower():
        return amount // 10, amount, "Rial"

    return amount, amount * 10, "Unknown"


async def ws_handler(websocket):
    clients.add(websocket)
    try:
        hello = {
            "type": "hello",
            "ts": now_iso(),
            "message": "connected to local donation relay"
        }
        await websocket.send(json.dumps(hello, ensure_ascii=False))
        await websocket.wait_closed()
    finally:
        clients.discard(websocket)


async def broadcast(payload: dict):
    if not clients:
        return
    message = json.dumps(payload, ensure_ascii=False)
    dead = []
    for ws in list(clients):
        try:
            await ws.send(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


def publish(payload: dict):
    global loop_ref
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    if loop_ref is not None:
        asyncio.run_coroutine_threadsafe(broadcast(payload), loop_ref)


def handle_console(msg):
    text = msg.text
    if not text.startswith("DONATION_EVENT:"):
        return

    raw = text.replace("DONATION_EVENT:", "", 1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return

    name = (data.get("name") or "").strip()
    detail = (data.get("detail") or "").strip()

    if not name:
        return

    # رویداد تستی اولیه را رد کن
    if name.lower() == "test":
        return

    amount_toman, amount_rial, currency = parse_amounts(detail)

    payload = {
        "type": "donation",
        "ts": now_iso(),
        "name": name,
        "detail": detail,
        "amount_toman": amount_toman,
        "amount_rial": amount_rial,
        "currency": currency,
    }
    publish(payload)


def playwright_worker():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("console", handle_console)

        page.goto(URL, wait_until="load")

        frame = page.frame(name="tool-holder")
        if not frame:
            raise SystemExit("iframe tool-holder پیدا نشد")

        frame.wait_for_selector("#dname", timeout=15000)
        frame.wait_for_selector("#ddetail", timeout=15000)

        frame.evaluate("""
            () => {
                const nameEl = document.querySelector("#dname");
                const detailEl = document.querySelector("#ddetail");

                if (!nameEl || !detailEl) {
                    console.log("DONATION_EVENT:" + JSON.stringify({
                        name: "",
                        detail: ""
                    }));
                    return;
                }

                let lastSent = "";

                function sendUpdate() {
                    const data = {
                        name: nameEl.innerText.trim(),
                        detail: detailEl.innerText.trim()
                    };

                    const current = JSON.stringify(data);
                    if (current === lastSent) return;
                    lastSent = current;

                    console.log("DONATION_EVENT:" + current);
                }

                const observer = new MutationObserver(() => {
                    sendUpdate();
                });

                observer.observe(nameEl, { childList: true, subtree: true, characterData: true });
                observer.observe(detailEl, { childList: true, subtree: true, characterData: true });

                sendUpdate();
            }
        """)

        print("Playwright listener started.", flush=True)
        page.wait_for_timeout(24 * 60 * 60 * 1000)
        browser.close()


async def main():
    global loop_ref
    loop_ref = asyncio.get_running_loop()

    thread = threading.Thread(target=playwright_worker, daemon=True)
    thread.start()

    async with serve(ws_handler, LOCAL_WS_HOST, LOCAL_WS_PORT):
        print(f"Local WebSocket server: ws://{LOCAL_WS_HOST}:{LOCAL_WS_PORT}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())