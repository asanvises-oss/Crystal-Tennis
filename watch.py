#!/usr/bin/env python3
"""เฝ้าดูสนามว่างของ Crystal Sports แล้วแจ้งเตือนทาง Telegram (อ่านอย่างเดียว ไม่กดจอง)"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://crystalsports-booking.kegroup.co.th"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
JAR_PATH = os.path.join(HERE, "cookies.enc")
EXPIRED = 2  # ค่าที่ main() คืนเมื่อ session หมดอายุ
ACTIVE_FROM_HOUR = 9  # ก่อนเวลานี้ (เวลาไทย) บอทแค่ต่ออายุ session ไม่เช็กสนาม
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


def parse_cookie_header(text):
    jar = {}
    for part in text.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            jar[name] = value
    return jar


def absorb_set_cookies(jar, headers):
    """อัปเดตคุกกี้ตามที่เว็บส่งกลับมา (เว็บต่ออายุ session ด้วยวิธีนี้) คืนชื่อคุกกี้ที่เปลี่ยน"""
    changed = []
    for line in headers.get_all("Set-Cookie") or []:
        name, sep, value = line.split(";", 1)[0].strip().partition("=")
        if not sep or not name:
            continue
        if value == "":
            if jar.pop(name, None) is not None:
                changed.append(name)
        elif jar.get(name) != value:
            jar[name] = value
            changed.append(name)
    return changed


def _openssl(args, data, key):
    return subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-pass", "env:CW_KEY"] + args,
                          input=data, capture_output=True, env={**os.environ, "CW_KEY": key})


def load_jar(secret):
    """เริ่มจากคุกกี้ใน secret แล้วทับด้วยชุดที่ต่ออายุไว้จากรอบก่อน (ถ้าถอดรหัสได้)

    ไฟล์ที่เก็บถูกเข้ารหัสด้วยกุญแจที่ได้จาก secret เอง เมื่อเปลี่ยน secret ไฟล์เก่าจะถอดไม่ออกและถูกทิ้ง
    """
    jar = parse_cookie_header(secret)
    if os.path.exists(JAR_PATH):
        r = _openssl(["-d", "-in", JAR_PATH], None, hashlib.sha256(secret.encode()).hexdigest())
        if r.returncode == 0:
            try:
                return json.loads(r.stdout)
            except ValueError:
                pass
    return jar


def save_jar(jar, secret):
    if secret:
        _openssl(["-out", JAR_PATH], json.dumps(jar).encode(), hashlib.sha256(secret.encode()).hexdigest())


def fetch(date, loc, jar):
    """คืน (สถานะ, เนื้อหา, ชื่อคุกกี้ที่เปลี่ยน) โดยสถานะเป็น ok / expired / error"""
    url = f"{BASE}/booking?date={date}&loc={loc}&time=0&sel="
    req = urllib.request.Request(url, headers={
        "Cookie": "; ".join(f"{k}={v}" for k, v in jar.items()),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        "Accept-Language": "th",
    })
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as r:
            return "ok", r.read().decode("utf-8", "replace"), absorb_set_cookies(jar, r.headers)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307) and "/account/login" in (e.headers.get("Location") or ""):
            return "expired", "", []
        return "error", f"HTTP {e.code}", []
    except Exception as e:  # เครือข่ายล่ม ฯลฯ
        return "error", str(e), []


def notify_expired(state):
    if not state.get("expired_notified"):
        telegram("⚠️ Session ของ Crystal Sports หมดอายุ\n"
                 "ล็อกอินใหม่ในเบราว์เซอร์ อัปเดต secret CRYSTAL_COOKIE ใน GitHub "
                 "แล้วกด Run workflow ที่แท็บ Actions เพื่อเริ่มบอทใหม่")
    state["expired_notified"] = True
    save(state)
    print("session หมดอายุ")


def telegram(text):
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not token or not chat:
        print("[ไม่ได้ตั้ง TG_TOKEN/TG_CHAT_ID] " + text)
        return
    data = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30).read()
    except Exception as e:
        print(f"ส่ง Telegram ไม่สำเร็จ: {type(e).__name__}")


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
    secret = os.environ.get("CRYSTAL_COOKIE", "").strip()
    jar = load_jar(secret)
    now = dt.datetime.now(BKK)
    renewed = set()

    if now.hour < ACTIVE_FROM_HOUR and not os.environ.get("FORCE_FULL"):
        # กลางคืน: เปิดหน้าเดียวเพื่อไม่ให้ session ขาด
        status, body, changed = fetch(now.date().isoformat(), "LOC001", jar)
        if status == "expired":
            notify_expired(state)
            return EXPIRED
        save_jar(jar, secret)
        print(f"กลางคืน: ต่ออายุ session ({status}) · คุกกี้ที่เว็บออกใหม่: {', '.join(changed) or '-'}")
        return 0

    jobs = {}  # (date, loc) -> ช่วงเวลาที่ต้องการ
    for w in config["watches"]:
        for d in target_dates(w, now.date()):
            for loc in w.get("locs", ["LOC001"]):
                jobs.setdefault((d, loc), []).append((w.get("from", "00:00"), w.get("to", "24:00")))

    found, new_seen, errors = [], {}, []
    for (date, loc), ranges in sorted(jobs.items()):
        status, body, changed = fetch(date, loc, jar)
        renewed.update(changed)
        if status == "expired":
            notify_expired(state)
            return EXPIRED
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
    save_jar(jar, secret)
    print(f"คุกกี้ที่เว็บออกใหม่รอบนี้: {', '.join(sorted(renewed)) or '-'}")
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
    elif os.environ.get("LOOP_SECONDS"):
        # โหมดรันวน: เช็กทุก INTERVAL วินาที จนครบ LOOP_SECONDS แล้วจบเพื่อให้ workflow เริ่มรอบใหม่
        deadline = time.time() + int(os.environ["LOOP_SECONDS"])
        interval = int(os.environ.get("INTERVAL", "300"))
        expired = False
        while True:
            started = time.time()
            print(dt.datetime.now(BKK).strftime("--- %d/%m %H:%M ---"), flush=True)
            subprocess.run(["git", "pull", "-q", "--ff-only"], cwd=HERE, capture_output=True)  # รับ config ที่แก้ใหม่
            try:
                if main() == EXPIRED:
                    expired = True
                    break
            except Exception as e:
                print(f"รอบนี้ผิดพลาด: {type(e).__name__}: {e}", flush=True)
            sys.stdout.flush()
            wait = interval - (time.time() - started)
            if time.time() + max(wait, 0) >= deadline:
                break
            time.sleep(max(wait, 0))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"expired={'true' if expired else 'false'}\n")
    else:
        sys.exit(0 if main() in (0, EXPIRED) else 1)
