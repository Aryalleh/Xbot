"""
sms_parser.py — Parse Iranian bank SMS messages and extract (amount_rials, paid_at).
Supports: بلو, ResalatBank, خاورمیانه
"""
import re
from datetime import datetime

try:
    import jdatetime
    _HAS_JDATETIME = True
except ImportError:
    _HAS_JDATETIME = False


def normalize_digits(text: str) -> str:
    persian = "۰۱۲۳۴۵۶۷۸۹"
    arabic  = "٠١٢٣٤٥٦٧٨٩"
    for i in range(10):
        text = text.replace(persian[i], str(i))
        text = text.replace(arabic[i],  str(i))
    return text


def _jalali_to_gregorian(jalali_str: str) -> str:
    """'1404.08.13 17:52' → '2025-10-... 17:52:00'"""
    if not _HAS_JDATETIME:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        date_part, time_part = jalali_str.split(" ")
        y, m, d = map(int, date_part.split("."))
        hh, mm  = map(int, time_part.split(":"))
        gdate   = jdatetime.datetime(y, m, d, hh, mm).togregorian()
        return gdate.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_sms(message: str) -> tuple[int | None, str | None]:
    """
    Parse a bank SMS message.
    Returns (amount_rials: int, paid_at_gregorian: str) or (None, None).
    Only deposit (واریز) messages are processed.
    """
    message = message.replace('%2B', '+')
    message = normalize_digits(message.strip())

    # ── بانک بلو ──────────────────────────────────────────────────────────────
    if ("بلو" in message or "واریز پول" in message) and re.search(
        r'ریال\s*به حساب شما نشست', message
    ):
        price_m = re.search(r'([\d,]+)\s*ریال\s*به حساب شما نشست', message)
        date_m  = re.search(r'(\d{4}\.\d{2}\.\d{2})', message)
        time_m  = re.search(r'(\d{1,2}:\d{2})', message)
        if price_m and date_m:
            amount   = int(price_m.group(1).replace(',', ''))
            time_str = time_m.group(1) if time_m else "00:00"
            paid_at  = _jalali_to_gregorian(f"{date_m.group(1)} {time_str}")
            return amount, paid_at

    # ── ResalatBank ───────────────────────────────────────────────────────────
    elif "ResalatBank" in message:
        price_m    = re.search(r'\+?([\d,]{5,})', message)
        datetime_m = re.search(r'(\d{2})/(\d{2})_(\d{2}:\d{2})', message)
        if price_m and datetime_m:
            amount     = int(price_m.group(1).replace(',', ''))
            now_j      = jdatetime.date.today() if _HAS_JDATETIME else None
            year       = now_j.year if now_j else datetime.now().year
            month      = int(datetime_m.group(1))
            day        = int(datetime_m.group(2))
            time_str   = datetime_m.group(3)
            paid_at    = _jalali_to_gregorian(f"{year:04}.{month:02}.{day:02} {time_str}")
            return amount, paid_at

    # ── بانک خاورمیانه ────────────────────────────────────────────────────────
    elif "خاورمیانه" in message:
        if not re.search(r'واریز', message):
            return None, None
        price_m = re.search(r'\+(\d{1,2},\d{3},\d{3,})', message)
        if not price_m:
            price_m = re.search(r'\+(\d[\d,]{5,})', message)
        date_m = re.search(r'(\d{2})/(\d{2})', message)
        time_m = re.search(r'(\d{1,2}:\d{2})', message)
        if price_m and date_m:
            amount     = int(price_m.group(1).replace(',', ''))
            now_j      = jdatetime.date.today() if _HAS_JDATETIME else None
            year       = now_j.year if now_j else datetime.now().year
            month      = int(date_m.group(1))
            day        = int(date_m.group(2))
            time_str   = time_m.group(1) if time_m else "00:00"
            paid_at    = _jalali_to_gregorian(f"{year:04}.{month:02}.{day:02} {time_str}")
            return amount, paid_at

    return None, None