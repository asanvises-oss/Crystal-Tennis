#!/usr/bin/env python3
"""เฝ้าดูสนามว่างของ Crystal Sports แล้วแจ้งเตือนทาง Telegram (อ่านอย่างเดียว ไม่กดจอง)"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://crystalsports-booking.kegroup.co.th"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
BKK = dt.timezone(dt.timedelta(hours=7))
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS_TH = ["จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส.", "อา."]

SLOT_RE = re.compile(
    r'<a class="slot(?P<cls>[^"]*)"(?P<attrs>[^>]*)>\s*'
    r'<span class="slot-time">(?P<time>\d\d:\d\d)</span>\s*'
    r'<span class="slot-meta">(?P<meta>[^<]*)</span>',
    re.S,
)


def parse_slots(page):
    """คืนรายการช่องเวลาทั้งหมดในหน้า: [{time, free, count, href}]"""
    slots = []
    for m in SLOT_RE.finditer(page):
        href = re.search(r'href="([^"]+)"', m["attrs"])
        free = "is-disabled" not in m["cls"] and href is not None
        count = re.search(r"\d+", m["meta"])
        slots.append({
            "time": m["time"],
            "free": free,
            "count": int(count.group()) if free and count else 0,
            "href": BASE + html.unescape(href.group(1)) if free else None,
        })
    return slots


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def fetch(date, loc, cookie):
    """คืน (สถานะ, เนื้อหา) โดยสถานะเป็น ok / expired / error"""
    url = f"{BASE}/booking?date={date}&loc={loc}&time=0&sel="
    req = urllib.request.Request(url, headers={
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        "Accept-Language": "th",
    })
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as r:
            return "ok", r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307) and "/account/login" in (e.headers.get("Location") or ""):
            return "expired", ""
        return "error", f"HTTP {e.code}"
    except Exception as e:  # เครือข่ายล่ม ฯลฯ
        return "error", str(e)


def telegram(text):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print("[ไม่ได้ตั้ง TG_TOKEN/TG_CHAT_ID] " + text)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30).read()


def target_dates(watch, today):
    if watch.get("dates"):
        return [d for d in watch["dates"] if d >= today.isoformat()]
    wanted = [w.lower() for w in watch.get("weekdays", DAYS)]
    out = []
    for i in range(int(watch.get("days_ahead", 14)) + 1):
        d = today + dt.timedelta(days=i)
        if DAYS[d.weekday()] in wanted:
            out.append(d.isoformat())
    return out


def main():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        config = json.load(f)
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    seen = state.get("seen", {})
    cookie = os.environ.get("CRYSTAL_COOKIE", "").strip()
    now = dt.datetime.now(BKK)

    jobs = {}  # (date, loc) -> ช่วงเวลาที่ต้องการ
    for w in config["watches"]:
        for d in target_dates(w, now.date()):
            for loc in w.get("locs", ["LOC001"]):
                jobs.setdefault((d, loc), []).append((w.get("from", "00:00"), w.get("to", "24:00")))

    found, new_seen, errors = [], {}, []
    for (date, loc), ranges in sorted(jobs.items()):
        status, body = fetch(date, loc, cookie)
        if status == "expired":
            if not state.get("expired_notified"):
                telegram("⚠️ Session ของ Crystal Sports หมดอายุ\n"
                         "ล็อกอินใหม่ในเบราว์เซอร์ แล้วอัปเดต secret CRYSTAL_COOKIE ใน GitHub")
            state["expired_notified"] = True
            state["seen"] = seen
            save(state)
            print("session หมดอายุ")
            return 0
        if status == "error":
            errors.append(f"{date} {loc}: {body}")
            for k, v in seen.items():  # เก็บสถานะเดิมไว้ จะได้ไม่เตือนซ้ำ
                if k.startswith(f"{date}|{loc}|"):
                    new_seen[k] = v
            continue
        slots = parse_slots(body)
        if not slots:
            errors.append(f"{date} {loc}: อ่านช่องเวลาไม่ได้ (หน้าเว็บอาจเปลี่ยนรูปแบบ)")
            continue
        for s in slots:
            if not s["free"] or not any(a <= s["time"] < b for a, b in ranges):
                continue
            key = f"{date}|{loc}|{s['time']}"
            new_seen[key] = s["count"]
            if key not in seen:
                found.append((date, loc, s))
        time.sleep(1)

    state["expired_notified"] = False
    state["seen"] = new_seen
    if found:
        names = config.get("loc_names", {})
        lines = ["🎾 มีสนามว่าง!"]
        for date, loc, s in found:
            d = dt.date.fromisoformat(date)
            lines.append(f"\n{DAYS_TH[d.weekday()]} {d.day}/{d.month} เวลา {s['time']} · "
                         f"{names.get(loc, loc)} · ว่าง {s['count']}\n{s['href']}")
        telegram("\n".join(lines))
    # เตือนเมื่อเช็กไม่ได้ติดกัน 3 รอบ และไม่เตือนซ้ำจนกว่าจะกลับมาปกติ
    state["error_streak"] = state.get("error_streak", 0) + 1 if errors else 0
    if state["error_streak"] == 3:
        telegram("⚠️ บอทเช็กสนามไม่ได้ 3 รอบติด:\n" + "\n".join(errors[:5]))
    save(state)
    print(f"เช็ก {len(jobs)} หน้า · ว่างตรงเงื่อนไข {len(new_seen)} · แจ้งใหม่ {len(found)} · ผิดพลาด {len(errors)}")
    for e in errors:
        print("  " + e)
    return 0


def save(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--parse":  # ทดสอบกับไฟล์ HTML ที่บันทึกไว้
        with open(sys.argv[2], encoding="utf-8") as f:
            for s in parse_slots(f.read()):
                print(s["time"], "ว่าง %d" % s["count"] if s["free"] else "-", s["href"] or "")
    else:
        sys.exit(main())
