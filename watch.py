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


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def http(jar, path, data=None):
    """เรียกเว็บสนามหนึ่งครั้งโดยไม่ตาม redirect คืน (รหัสสถานะ, เนื้อหา, Location, ชื่อคุกกี้ที่เปลี่ยน)"""
    headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in jar.items()),
               "User-Agent": UA, "Accept-Language": "th"}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers.update({"Content-Type": "application/x-www-form-urlencoded",
                        "Origin": BASE, "Referer": BASE + "/booking/review"})
    req = urllib.request.Request(BASE + path, data=body, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace"), "", absorb_set_cookies(jar, r.headers)
    except urllib.error.HTTPError as e:
        return (e.code, e.read().decode("utf-8", "replace"), e.headers.get("Location") or "",
                absorb_set_cookies(jar, e.headers))


def fetch(date, loc, jar):
    """คืน (สถานะ, เนื้อหา, ชื่อคุกกี้ที่เปลี่ยน) โดยสถานะเป็น ok / expired / error"""
    try:
        code, body, location, changed = http(jar, f"/booking?date={date}&loc={loc}&time=0&sel=")
    except Exception as e:  # เครือข่ายล่ม ฯลฯ
        return "error", str(e), []
    if code == 200:
        return "ok", body, changed
    if code in (301, 302, 303, 307) and "/account/login" in location:
        return "expired", "", []
    return "error", f"HTTP {code}", []


# ---------- จองอัตโนมัติด้วย Wallet ----------

COURT_RE = re.compile(r'<a class="court-tile(?P<cls>[^"]*)"(?P<attrs>[^>]*)>.*?'
                      r'<span class="court-name">(?P<name>[^<]+)</span>', re.S)


def parse_courts(page):
    """สนามที่ยังกดเลือกได้ในหน้าขั้นเลือกสนาม: [{name, sel}]"""
    out = []
    for m in COURT_RE.finditer(page):
        sel = re.search(r'href="[^"]*[?&;]sel=(\d{8}-\d+)', m["attrs"])
        if sel and "is-disabled" not in m["cls"] and "is-selected" not in m["cls"]:
            out.append({"name": m["name"].strip(), "sel": sel.group(1)})
    return out


def parse_review(page):
    """อ่านฟอร์มยืนยันการจอง คืน None ถ้าไม่ใช่หน้าที่คาดไว้"""
    m = re.search(r'<form[^>]*id="bkForm"[^>]*action="/booking/confirm"[^>]*data-base="(\d+)".*?</form>', page, re.S)
    if not m:
        return None
    form = m.group(0)
    sel = re.search(r'name="sel" value="([^"]+)"', form)
    token = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', form)
    title = re.search(r'<b>([^<]+)</b><div class="text-muted text-sm">([^<]+)</div>', form)
    if not (sel and token and title):
        return None
    return {"price": int(m.group(1)), "sel": sel.group(1), "token": token.group(1),
            "court": html.unescape(title.group(1)).strip(), "when": html.unescape(title.group(2)).strip(),
            "wallet": 'name="method" value="WALLET"' in form,
            "items": len(re.findall(r'name="coach_', form))}


def wallet_balance(jar):
    code, body, _, _ = http(jar, "/")
    m = re.search(r'class="wallet-card-amount">([\d,]+)<', body) if code == 200 else None
    return int(m.group(1).replace(",", "")) if m else None


def book_slot(jar, date, loc, slot, cfg):
    """พยายามจองหนึ่งช่องด้วย Wallet คืน (ผล, ข้อความ, รายละเอียด)

    ผล: booked = จ่ายแล้ว (ยอด Wallet ลดตามราคา) · dry = โหมดซ้อม หยุดก่อนกดยืนยัน
        skipped = ไม่ได้ส่งคำสั่งจอง · uncertain = ส่งคำสั่งจองแล้วแต่ยืนยันผลไม่ได้
    """
    path = slot["href"][len(BASE):].split("#")[0]
    code, page, _, _ = http(jar, path)
    courts = parse_courts(page) if code == 200 else []
    if not courts:
        return "skipped", "ไม่มีสนามให้เลือกแล้ว (อาจถูกจองไปก่อน)", {}
    court = courts[0]
    code, page, _, _ = http(jar, "/booking/review?sel=" + court["sel"])
    rv = parse_review(page) if code == 200 else None
    if not rv:
        return "skipped", f"เปิดหน้ายืนยันไม่ได้ (HTTP {code})", {}
    info = {"court": rv["court"], "when": rv["when"], "price": rv["price"]}
    hour = slot["time"][:2]
    if rv["sel"] != court["sel"] or rv["items"] != 1 or f"{hour}:00" not in rv["when"]:
        return "skipped", "รายการในหน้ายืนยันไม่ตรงกับช่องที่ตั้งใจจอง", info
    if not rv["wallet"]:
        return "skipped", "หน้ายืนยันไม่มีตัวเลือกจ่ายด้วย Wallet", info
    if rv["price"] > cfg.get("max_price", 700):
        return "skipped", f"ราคา ฿{rv['price']} เกินเพดาน ฿{cfg.get('max_price', 700)}", info
    before = wallet_balance(jar)
    info["balance"] = before
    if before is None:
        return "skipped", "อ่านยอด Wallet ไม่ได้", info
    if before < rv["price"]:
        return "skipped", f"Wallet ไม่พอ (มี ฿{before} ต้องใช้ ฿{rv['price']})", info
    if cfg.get("dry_run", True):
        return "dry", "โหมดซ้อม: ผ่านทุกขั้น หยุดก่อนกดยืนยันการจอง", info

    code, page, location, _ = http(jar, "/booking/confirm", {
        "sel": rv["sel"], f"coach_{rv['sel']}": "", "method": "WALLET",
        "__RequestVerificationToken": rv["token"]})
    trail = [f"{code} {location.split('?')[0]}".strip()]
    for _ in range(3):  # ตาม redirect หลังกดยืนยัน
        if code not in (301, 302, 303, 307) or not location.startswith("/"):
            break
        code, page, location2, _ = http(jar, location)
        trail.append(f"{code} {location2.split('?')[0]}".strip())
        location = location2
    title = re.search(r"<title>([^<]*)</title>", page or "")
    info["trail"] = " → ".join(trail)
    info["title"] = html.unescape(title.group(1)).strip() if title else ""
    after = wallet_balance(jar)
    info["after"] = after
    if after is not None and after == before - rv["price"]:
        return "booked", "จองและจ่ายด้วย Wallet แล้ว", info
    return "uncertain", "ส่งคำสั่งจองแล้ว แต่ยอด Wallet ไม่ได้ลดตามราคา", info


def auto_book(state, config, jar, free_now, now):
    """จองเองไม่เกินหนึ่งช่องต่อรอบ ตามกติกาใน config["auto_book"]"""
    cfg = config.get("auto_book") or {}
    if not cfg.get("enabled"):
        return
    reset = cfg.get("reset", 0)
    if state.get("paused_reset") == reset:
        return  # หยุดไว้หลังผลจองไม่ชัดเจน รอให้เจ้าของตรวจแล้วเปลี่ยนค่า reset
    today = now.date().isoformat()
    booked = [b for b in state.get("booked", []) if b["date"] >= today]
    attempted = {k: v for k, v in state.get("attempted", {}).items() if k[:10] >= today}
    state["booked"], state["attempted"] = booked, attempted
    names = config.get("loc_names", {})
    for date, loc, s in sorted(free_now, key=lambda x: (x[0], x[2]["time"], x[1])):
        key = f"{date}|{loc}|{s['time']}"
        d = dt.date.fromisoformat(date)
        start = dt.datetime.combine(d, dt.time(int(s["time"][:2])), BKK)
        dry = cfg.get("dry_run", True)
        if attempted.get(key) == "live" or (dry and key in attempted) or (start - now).total_seconds() <= cfg.get("min_hours_ahead", 25) * 3600:
            continue
        if sum(b["date"] == date for b in booked) >= cfg.get("max_per_day", 1):
            continue
        week = d.isocalendar()[:2]
        if sum(dt.date.fromisoformat(b["date"]).isocalendar()[:2] == week for b in booked) >= cfg.get("max_per_week", 2):
            continue
        attempted[key] = "dry" if dry else "live"
        save(state)  # จดไว้ก่อนส่งคำสั่งจอง กันจองซ้ำถ้าบอทล้มกลางทาง
        try:
            result, msg, info = book_slot(jar, date, loc, s, cfg)
        except Exception as e:
            result, msg, info = "uncertain", f"ผิดพลาดระหว่างจอง: {type(e).__name__}", {}
        label = f"{DAYS_TH[d.weekday()]} {d.day}/{d.month} เวลา {s['time']} · {names.get(loc, loc)}"
        detail = f"{info.get('court', '')} · {info.get('when', '')} · ฿{info.get('price', '?')}" if info else ""
        print(f"จองอัตโนมัติ [{result}] {label} · {msg}")
        if result == "booked":
            booked.append({"date": date, "time": s["time"], "loc": loc})
            telegram(f"✅ จองให้แล้ว\n{label}\n{detail}\nWallet ฿{info['balance']} → ฿{info['after']}\n"
                     f"ยกเลิกได้ที่ {BASE}/bookings (ต้องล่วงหน้าอย่างน้อย 24 ชม.)")
        elif result == "dry":
            telegram(f"🧪 ซ้อมจอง (ยังไม่ได้จองจริง)\n{label}\n{detail}\nWallet ฿{info['balance']}\n{msg}")
        elif result == "uncertain":
            state["paused_reset"] = reset
            telegram(f"⚠️ ผลการจองไม่ชัดเจน บอทหยุดจองอัตโนมัติไว้ก่อน\n{label}\n{detail}\n{msg}\n"
                     f"Wallet ก่อน ฿{info.get('balance', '?')} หลัง ฿{info.get('after', '?')}\n"
                     f"เส้นทาง: {info.get('trail', '-')}\nหน้าสุดท้าย: {info.get('title', '-')}\n"
                     f"ตรวจที่ {BASE}/bookings")
        else:
            telegram(f"ℹ️ ไม่ได้จองอัตโนมัติ\n{label}\n{detail}\n{msg}")
        save(state)
        return  # รอบละไม่เกินหนึ่งครั้ง


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

    found, new_seen, errors, free_now = [], {}, [], []
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
            free_now.append((date, loc, s))
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
    auto_book(state, config, jar, free_now, now)
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
