# crystal-watch

บอทเฝ้าดูสนามว่างของ Crystal Sports แล้วแจ้งเตือนทาง Telegram ทุก 10 นาที ช่วง 09:00–23:50 (อ่านอย่างเดียว ไม่กดจอง)

## ตั้งค่า

1. **Telegram**: คุยกับ @BotFather พิมพ์ `/newbot` จะได้ token จากนั้นส่งข้อความอะไรก็ได้ให้บอทของคุณ
   แล้วเปิด `https://api.telegram.org/bot<TOKEN>/getUpdates` ดูเลข `chat.id`
2. **GitHub**: สร้าง repository แบบ Public แล้วอัปโหลดไฟล์ทั้งหมดในโฟลเดอร์นี้ (รวมโฟลเดอร์ `.github`)
3. **Secrets**: ใน repository ไปที่ Settings → Secrets and variables → Actions แล้วเพิ่ม
   - `TG_TOKEN` token จาก BotFather
   - `TG_CHAT_ID` เลข chat id
   - `CRYSTAL_COOKIE` ล็อกอินเว็บสนามในคอม กด F12 → Network → รีเฟรชหน้า `/booking`
     คลิกรายการ `booking` → Request Headers → คัดลอกค่าหลัง `cookie:` ทั้งบรรทัด
4. **ทดสอบ**: แท็บ Actions → watch-courts → Run workflow

## เลือกวันและเวลา

แก้ `config.json` แต่ละรายการใน `watches` คือเงื่อนไขหนึ่งชุด

- `weekdays`: วันในสัปดาห์ (`mon` ถึง `sun`) หรือใช้ `"dates": ["2026-11-21"]` ระบุวันตรง ๆ
- `from` / `to`: ช่วงเวลาเริ่มเล่น เช่น `18:00` ถึง `21:00` คือช่อง 18:00, 19:00, 20:00
- `locs`: `LOC001` (Crystal Sports) หรือ `LOC002` (Crystal Sports G)
- `days_ahead`: เช็กล่วงหน้ากี่วัน

เมื่อบอทแจ้งว่า session หมดอายุ ให้ล็อกอินใหม่แล้วอัปเดต `CRYSTAL_COOKIE`
