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
NIGHT_KEEPALIVE_MINUTES = 60  # นอกเวลาทำงาน เข้าเว็บแค่ชั่วโมงละครั้งเพื่อไม่ให้ session ขาด
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
    code, body, location, changed = _http(jar, path, data)
    # เว็บหมุนคุกกี้ล็อกอินเป็นระยะ ต้องบันทึกทันที ไม่อย่างนั้นถ้างานถูกหยุดกลางรอบ ชุดใหม่จะหายและชุดเก่าใช้ไม่ได้
    if code == 200 and "cs.member" in changed:
        save_jar(jar, os.environ.get("CRYSTAL_COOKIE", "").strip())
    return code, body, location, changed


def _http(jar, path, data=None):
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


def _attr(tag, name):
    m = re.search(r'\b' + name + r'\s*=\s*"([^"]*)"', tag)
    return html.unescape(m.group(1)) if m else None


def form_fields(form):
    """ค่าที่เบราว์เซอร์จะส่งเมื่อกดยืนยัน โดยเลือกจ่ายด้วย Wallet และไม่เลือกโค้ช

    คืน (รายการคู่ชื่อ-ค่า ที่ยังไม่รวมช่องติ๊ก, รายการช่องติ๊กที่ยังไม่ได้ติ๊ก [(ชื่อ, ค่า)])
    """
    fields, checkboxes = [], []
    for tag in re.findall(r"<input\b[^>]*>", form):
        name, kind = _attr(tag, "name"), (_attr(tag, "type") or "text").lower()
        if not name or re.search(r"\bdisabled\b", tag):
            continue
        value = _attr(tag, "value")
        if kind == "checkbox":
            if re.search(r"\bchecked\b", tag):
                fields.append((name, value if value is not None else "on"))
            else:  # เก็บตำแหน่งไว้ เพื่อส่งตามลำดับเดียวกับที่เบราว์เซอร์ส่ง
                checkboxes.append((name, value if value is not None else "on"))
                fields.append((name, None))
        elif kind == "radio":
            if name == "method":
                if value == "WALLET":
                    fields.append((name, value))
            elif re.search(r"\bchecked\b", tag):
                fields.append((name, value or "on"))
        elif kind not in ("submit", "button", "image", "file", "reset"):
            fields.append((name, value or ""))
    for tag, body in re.findall(r"(<select\b[^>]*>)(.*?)</select>", form, re.S):
        name = _attr(tag, "name")
        if not name or re.search(r"\bdisabled\b", tag):
            continue
        if name.startswith("coach_"):
            fields.append((name, ""))  # ไม่เลือกโค้ช
            continue
        opts = re.findall(r"<option\b[^>]*>", body)
        chosen = next((o for o in opts if re.search(r"\bselected\b", o)), opts[0] if opts else "")
        fields.append((name, _attr(chosen, "value") or ""))
    return fields, checkboxes


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
    fields, checkboxes = form_fields(form)
    return {"fields": fields, "checkboxes": checkboxes,
            "price": int(m.group(1)), "sel": sel.group(1), "token": token.group(1),
            "court": html.unescape(title.group(1)).strip(), "when": html.unescape(title.group(2)).strip(),
            "wallet": 'name="method" value="WALLET"' in form,
            "items": len(re.findall(r'name="coach_', form))}


def site_message(page):
    """ข้อความแจ้งเตือนที่เว็บแสดงบนหน้า (toast / ข้อความผิดพลาด) ตัดให้สั้น"""
    parts = []
    m = re.search(r'class="toast-stack"[^>]*>(.*?)<div class="ball-loader"', page, re.S)
    if m:
        parts.append(m.group(1))
    parts += re.findall(r'<[a-z]+[^>]*class="[^"]*(?:alert|error|danger|invalid|validation)[^"]*"[^>]*>(.*?)</', page, re.S)
    clean = (re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", p))).strip() for p in parts)
    return " | ".join(dict.fromkeys(t for t in clean if t))[:300]


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
    boxes = dict(rv["checkboxes"])
    tick = bool(cfg.get("accept_terms")) and len(rv["checkboxes"]) == 1
    data = [(n, boxes[n] if v is None else v) for n, v in rv["fields"] if v is not None or tick]
    if rv["checkboxes"]:
        # หน้ายืนยันมีช่องให้ติ๊กยอมรับข้อตกลงของสนาม บอทติ๊กแทนได้เมื่อเจ้าของบัญชีอนุญาตไว้ใน config เท่านั้น
        info["terms_box"] = ", ".join(n for n, _ in rv["checkboxes"])
        if len(rv["checkboxes"]) > 1:
            return "skipped", f"หน้ายืนยันมีช่องติ๊กหลายช่อง ({info['terms_box']}) บอทไม่ติ๊กเอง", info
        if not cfg.get("accept_terms"):
            return "skipped", "หน้ายืนยันต้องติ๊กยอมรับข้อตกลงของสนาม แต่ยังไม่ได้อนุญาตให้บอทติ๊กแทน (accept_terms)", info
    if cfg.get("dry_run", True):
        return "dry", "โหมดซ้อม: ผ่านทุกขั้น หยุดก่อนกดยืนยันการจอง" + (
            f" (จะติ๊กยอมรับข้อตกลง: {info['terms_box']})" if rv["checkboxes"] else ""), info

    code, page, location, _ = http(jar, "/booking/confirm", data)
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
    info["site_msg"] = site_message(page or "")
    after = wallet_balance(jar)
    info["after"] = after
    if after is not None and after == before - rv["price"]:
        return "booked", "จองและจ่ายด้วย Wallet แล้ว", info
    if after == before and code == 200 and 'action="/booking/confirm"' in (page or ""):
        # เว็บพากลับมาหน้ายืนยันเดิมและไม่ได้ตัดเงิน = ไม่รับการจอง
        return "rejected", "เว็บไม่รับการจอง และไม่ได้ตัดเงิน", info
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
        print(f"จองอัตโนมัติ [{result}] {label} · {msg} · {info.get('site_msg', '')}")
        if result == "booked":
            state["rejects"] = 0
            booked.append({"date": date, "time": s["time"], "loc": loc})
            telegram(f"✅ จองให้แล้ว\n{label}\n{detail}\nWallet ฿{info['balance']} → ฿{info['after']}\n"
                     f"ยกเลิกได้ที่ {BASE}/bookings (ต้องล่วงหน้าอย่างน้อย 24 ชม.)")
        elif result == "dry":
            telegram(f"🧪 ซ้อมจอง (ยังไม่ได้จองจริง)\n{label}\n{detail}\nWallet ฿{info['balance']}\n{msg}")
        elif result == "rejected":
            state["rejects"] = state.get("rejects", 0) + 1
            if state["rejects"] >= 3:  # โดนปฏิเสธซ้ำ ๆ น่าจะมีสาเหตุประจำ หยุดไว้ให้ตรวจ
                state["paused_reset"] = reset
            telegram(f"❌ จองไม่สำเร็จ ไม่มีการตัดเงิน\n{label}\n{detail}\n"
                     f"เว็บแจ้งว่า: {info.get('site_msg') or '(ไม่มีข้อความ)'}"
                     + ("\nโดนปฏิเสธ 3 ครั้งแล้ว บอทหยุดจองอัตโนมัติไว้ก่อน" if state["rejects"] >= 3 else ""))
        elif result == "uncertain":
            state["paused_reset"] = reset
            telegram(f"⚠️ ผลการจองไม่ชัดเจน บอทหยุดจองอัตโนมัติไว้ก่อน\n{label}\n{detail}\n{msg}\n"
                     f"Wallet ก่อน ฿{info.get('balance', '?')} หลัง ฿{info.get('after', '?')}\n"
                     f"เส้นทาง: {info.get('trail', '-')}\nหน้าสุดท้าย: {info.get('title', '-')}\n"
                     f"เว็บแจ้งว่า: {info.get('site_msg') or '(ไม่มีข้อความ)'}\n"
                     f"ตรวจที่ {BASE}/bookings")
        else:
            telegram(f"ℹ️ ไม่ได้จองอัตโนมัติ\n{label}\n{detail}\n{msg}")
        save(state)
        return  # รอบละไม่เกินหนึ่งครั้ง


EXPIRED_NOTIFY_AFTER = 3  # แจ้งเมื่อถูกเด้งไปหน้าล็อกอินติดกันกี่รอบ (เว็บมีอาการชั่วคราวได้)


def notify_expired(state):
    """นับรอบที่ถูกเด้งไปหน้าล็อกอิน แจ้งครั้งเดียวเมื่อเกิดติดกันหลายรอบ แล้วลองต่อทุกรอบ"""
    state["expired_streak"] = state.get("expired_streak", 0) + 1
    if state["expired_streak"] == EXPIRED_NOTIFY_AFTER:
        telegram("⚠️ บอทเข้าเว็บ Crystal Sports ไม่ได้ (ถูกส่งไปหน้าล็อกอิน) มา 15 นาทีแล้ว\n"
                 "บอทจะลองต่อเองทุก 5 นาที และจะแจ้งเมื่อกลับมาใช้ได้\n"
                 "ถ้าไม่กลับมาเอง ให้ล็อกอินใหม่ในคอมแล้วอัปเดต secret CRYSTAL_COOKIE "
                 "(มีผลภายในราว 30 นาที)")
    save(state)
    print(f"ถูกส่งไปหน้าล็อกอิน (ติดกัน {state['expired_streak']} รอบ)")


def session_ok(state):
    if state.get("expired_streak", 0) >= EXPIRED_NOTIFY_AFTER:
        telegram("✅ บอทกลับมาเข้าเว็บ Crystal Sports ได้แล้ว")
    state["expired_streak"] = 0


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


# ---------- ตอบคำถามใน Telegram ----------

DAY_WORDS = [("จันทร์", 0), ("อังคาร", 1), ("พุธ", 2), ("พฤหัส", 3), ("ศุกร์", 4), ("เสาร์", 5), ("อาทิตย์", 6)]
HELP = ("พิมพ์ได้ดังนี้\n"
        "• วันนี้ / พรุ่งนี้ / เสาร์ / 15/11 : ดูช่องที่ว่างของวันนั้น\n"
        "• ข้าม เสาร์ อาทิตย์ / ข้าม 20/11 : ไม่ต้องหาและไม่จองวันนั้น\n"
        "• ยกเลิกข้าม / ยกเลิกข้าม 20/11 : กลับมาหาตามปกติ\n"
        "• สถานะ : ดูว่าบอททำงานอยู่ไหม\n"
        "• wallet : ดูยอดเงินคงเหลือ")


def parse_days(text, today):
    """หาทุกวันที่ที่ข้อความพูดถึง (ใช้กับคำสั่งข้าม) เรียงตามวันที่"""
    days = set()
    for dd, mm in re.findall(r"(\d{1,2})\s*/\s*(\d{1,2})", text):
        try:
            d = dt.date(today.year, int(mm), int(dd))
        except ValueError:
            continue
        days.add(d if d >= today else d.replace(year=today.year + 1))
    for word, offset in (("วันนี้", 0), ("พรุ่งนี้", 1), ("มะรืน", 2)):
        if word in text:
            days.add(today + dt.timedelta(days=offset))
    for word, wd in DAY_WORDS:
        if word in text:
            days.add(today + dt.timedelta(days=(wd - today.weekday()) % 7))
    return sorted(days)


def fmt_days(isos):
    out = []
    for iso in sorted(isos):
        d = dt.date.fromisoformat(iso)
        out.append(f"{DAYS_TH[d.weekday()]} {d.day}/{d.month}")
    return ", ".join(out) or "ไม่มี"


def skipped_dates(config, state, today):
    """วันที่ไม่ต้องหาและไม่จอง รวมจาก config และที่สั่งผ่าน Telegram ตัดวันที่ผ่านไปแล้วออก"""
    iso = today.isoformat()
    return {d for d in list(config.get("skip_dates", [])) + list(state.get("skip_dates", [])) if d >= iso}


def parse_day(text, today):
    """หาวันที่ที่ผู้ใช้ถามถึง คืน None ถ้าไม่พบ"""
    m = re.search(r"(\d{1,2})\s*/\s*(\d{1,2})", text)
    if m:
        try:
            d = dt.date(today.year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        return d if d >= today else d.replace(year=today.year + 1)
    if "วันนี้" in text or "today" in text:
        return today
    if "พรุ่งนี้" in text:
        return today + dt.timedelta(days=1)
    if "มะรืน" in text:
        return today + dt.timedelta(days=2)
    for word, wd in DAY_WORDS:
        if word in text:
            return today + dt.timedelta(days=(wd - today.weekday()) % 7)
    return None


def answer(text, config, state, jar, now):
    """สร้างคำตอบสำหรับข้อความหนึ่งข้อความ"""
    t = text.strip().lower()
    names = config.get("loc_names", {"LOC001": "LOC001"})
    if "ข้าม" in t:
        today = now.date()
        mine = {d for d in state.get("skip_dates", []) if d >= today.isoformat()}
        days = {d.isoformat() for d in parse_days(t, today)}
        if "ยกเลิก" in t or "ไม่ข้าม" in t or "เลิกข้าม" in t:
            mine = mine - days if days else set()
            head = "กลับมาหาตามปกติแล้ว"
        elif days:
            mine |= days
            head = "รับทราบ จะไม่หาและไม่จองวันที่ข้าม"
        else:
            head = "ไม่พบวันที่ในข้อความ ลองพิมพ์เช่น: ข้าม เสาร์ อาทิตย์ หรือ ข้าม 20/11"
        state["skip_dates"] = sorted(mine)
        return f"{head}\nวันที่ข้ามอยู่ตอนนี้: {fmt_days(skipped_dates(config, state, today))}"
    if "สถานะ" in t or "status" in t:
        ab = config.get("auto_book") or {}
        mode = "ปิด" if not ab.get("enabled") else ("โหมดซ้อม" if ab.get("dry_run", True) else "จองจริง")
        if state.get("paused_reset") == ab.get("reset", 0) and ab.get("enabled"):
            mode += " (หยุดไว้ รอตรวจผลการจอง)"
        web = "เข้าไม่ได้ กำลังลองใหม่" if state.get("expired_streak") else "ปกติ"
        return (f"🤖 บอททำงานอยู่\nเช็กล่าสุด: {state.get('last_check', 'ยังไม่มี')}\n"
                f"เว็บสนาม: {web}\nจองอัตโนมัติ: {mode}\n"
                f"วันที่ข้าม: {fmt_days(skipped_dates(config, state, now.date()))}")
    if "wallet" in t or "วอลเล็ต" in t or "ยอดเงิน" in t:
        bal = wallet_balance(jar)
        return f"Wallet คงเหลือ ฿{bal:,}" if bal is not None else "อ่านยอด Wallet ไม่ได้ตอนนี้"
    day = parse_day(t, now.date())
    if day is None:
        return HELP
    lines = [f"สนามว่าง {DAYS_TH[day.weekday()]} {day.day}/{day.month}"]
    for loc in names:
        status, body, _ = fetch(day.isoformat(), loc, jar)
        if status != "ok":  # ลองซ้ำหนึ่งครั้ง เผื่อเว็บสะดุดชั่วคราว
            time.sleep(3)
            status, body, _ = fetch(day.isoformat(), loc, jar)
        if status != "ok":
            why = "ถูกส่งไปหน้าล็อกอิน" if status == "expired" else body
            return f"เข้าเว็บสนามไม่ได้ตอนนี้ ({why}) ลองใหม่อีกสักครู่"
        slots = parse_slots(body)
        free = [f"{x['time']} ({x['count']})" for x in slots if x["free"]]
        if not slots:
            lines.append(f"\n{names[loc]}: ยังไม่เปิดให้จองหรือไม่มีข้อมูล")
        elif not free:
            lines.append(f"\n{names[loc]}: เต็มทุกช่อง")
        else:
            lines.append(f"\n{names[loc]}: " + ", ".join(free) +
                         f"\n{BASE}/booking?date={day.isoformat()}&loc={loc}&time=0&sel=")
    lines.append("\nตัวเลขในวงเล็บคือจำนวนสนามที่ว่าง")
    return "\n".join(lines)


def tg_updates(offset, timeout):
    token = os.environ["TG_TOKEN"]
    q = urllib.parse.urlencode({"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'})
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getUpdates?{q}", timeout=timeout + 15) as r:
        return json.load(r).get("result", [])


def listen(seconds):
    """รอจนครบเวลา ระหว่างนั้นตอบข้อความจากเจ้าของบอทใน Telegram"""
    end = time.time() + seconds
    if not (os.environ.get("TG_TOKEN") and os.environ.get("TG_CHAT_ID")):
        time.sleep(max(seconds, 0))
        return
    secret = os.environ.get("CRYSTAL_COOKIE", "").strip()
    while time.time() < end:
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        try:
            updates = tg_updates(state.get("tg_offset", 0), int(max(1, min(25, end - time.time()))))
        except Exception as e:
            print(f"ฟัง Telegram ไม่ได้: {type(e).__name__}", flush=True)
            time.sleep(max(0, min(10, end - time.time())))
            continue
        for u in updates:
            state["tg_offset"] = u["update_id"] + 1
            msg = u.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != os.environ["TG_CHAT_ID"]:
                continue  # ตอบเฉพาะเจ้าของบอท
            if time.time() - msg.get("date", 0) > 600 or not msg.get("text"):
                continue  # ข้ามข้อความเก่าที่ค้างอยู่
            try:
                with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
                    config = json.load(f)
                jar = load_jar(secret)
                reply = answer(msg["text"], config, state, jar, dt.datetime.now(BKK))
                save_jar(jar, secret)
            except Exception as e:
                reply = f"ตอบไม่ได้ตอนนี้ ({type(e).__name__})"
            telegram(reply)
            print("ตอบข้อความใน Telegram 1 ข้อความ", flush=True)
        if updates:
            save(state)


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

    hours = config.get("active_hours") or {}
    if not hours.get("from", 8) <= now.hour < hours.get("to", 23) and not os.environ.get("FORCE_FULL"):
        # นอกเวลาทำงาน: ไม่เช็กสนาม เปิดหน้าเดียวชั่วโมงละครั้งเพื่อไม่ให้ session ขาด
        last = state.get("last_keepalive")
        if last and (now - dt.datetime.fromisoformat(last)).total_seconds() < NIGHT_KEEPALIVE_MINUTES * 60 - 150:
            print("กลางคืน: พัก")
            return 0
        state["last_keepalive"] = now.isoformat()
        save(state)
        status, body, changed = fetch(now.date().isoformat(), "LOC001", jar)
        if status == "expired":
            notify_expired(state)
            return EXPIRED
        if status == "ok":
            session_ok(state)
            save(state)
            save_jar(jar, secret)
        print(f"กลางคืน: ต่ออายุ session ({status}) · คุกกี้ที่เว็บออกใหม่: {', '.join(changed) or '-'}")
        return 0

    jobs = {}  # (date, loc) -> ช่วงเวลาที่ต้องการ
    for w in config["watches"]:
        for d in target_dates(w, now.date()):
            for loc in w.get("locs", ["LOC001"]):
                jobs.setdefault((d, loc), []).append((w.get("from", "00:00"), w.get("to", "24:00")))

    skip = skipped_dates(config, state, now.date())
    state["skip_dates"] = sorted(d for d in state.get("skip_dates", []) if d >= now.date().isoformat())
    jobs = {k: v for k, v in jobs.items() if k[0] not in skip}
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

    session_ok(state)
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
    state["last_check"] = (f"{now:%d/%m %H:%M} · ว่างตรงเงื่อนไข {len(new_seen)} ช่อง"
                           + (f" · อ่านไม่ได้ {len(errors)} หน้า" if errors else ""))
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

        def interval_seconds():
            """ระยะห่างระหว่างรอบ อ่านจาก config ทุกรอบ จึงแก้แล้วมีผลโดยไม่ต้องเริ่มงานใหม่"""
            try:
                with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
                    return max(60, int(json.load(f).get("interval_seconds", os.environ.get("INTERVAL", "300"))))
            except (OSError, ValueError):
                return 300

        expired_rounds = 0
        while True:
            started = time.time()
            print(dt.datetime.now(BKK).strftime("--- %d/%m %H:%M ---"), flush=True)
            subprocess.run(["git", "pull", "-q", "--ff-only"], cwd=HERE, capture_output=True)  # รับ config ที่แก้ใหม่
            try:
                expired_rounds = expired_rounds + 1 if main() == EXPIRED else 0
            except Exception as e:
                print(f"รอบนี้ผิดพลาด: {type(e).__name__}: {e}", flush=True)
            sys.stdout.flush()
            wait = interval_seconds() - (time.time() - started)
            # เข้าเว็บไม่ได้ครบ 30 นาที: จบรอบนี้เพื่อให้รอบใหม่อ่านคุกกี้ที่อาจถูกอัปเดต
            if time.time() + max(wait, 0) >= deadline or expired_rounds >= 6:
                break
            listen(max(wait, 0))
    else:
        sys.exit(0 if main() in (0, EXPIRED) else 1)
