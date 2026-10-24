# Jarvis uslubidagi Autohabar bot

## Ishga tushirish
1. @BotFather'da bot oching, tokenni oling. https://my.telegram.org dan API_ID va API_HASH oling.
2. `.env.example` dagi o'zgaruvchilarni Render -> Environment ga kiriting (SECRET_KEY ni generatsiya qiling).
3. GitHub'ga yuklang (`.env`, `*.db`, `*.session` yuklamang!).
4. Render -> New Web Service: Build `pip install -r requirements.txt`, Start `python main.py`.
5. Render bergan URL ni WEBAPP_URL ga yozing, qayta deploy qiling.
6. Botda /start bosing. Pastki chapda "Cabinet" tugmasi paydo bo'ladi.

## Fayllar
`main.py` (ildiz papkada) va `webapp/index.html` (Mini App). Eski baza bo'lsa ham, yangi ustunlar ishga tushganda o'zi qo'shiladi.

## Cabinet bo'limlari
- **📨 Avtohabar** — xabar matni + guruhlarni tanlash. Free: har 2 daqiqada, Pro: har 1 daqiqada (tsikl boshidan boshigacha).
- **📣 Kanal** (alohida) — 1) kanal, 2) kanalga yoziladigan xabar(lar), 3) qachon: har N daqiqada / har kuni belgilangan soatda / bir marta (sana va soat), nechta marta, 4) guruhlarga forward qilinsinmi? Ha bo'lsa, nechta guruh va guruhlar ro'yxatidan tanlanadi. Forward guruhlari avtohabar guruhlaridan mustaqil.

## Pro berish
To'lov kelgach, admin (ADMIN_ID) botga yozadi: `/pro 123456789 30` (user ID, kun). Bekor qilish: `/pro 123456789 0`.
Foydalanuvchi ID'sini u Pro tarif sahifasida ko'radi.

## Muhim
- Render bepul tarifi 15 daqiqa so'rovsiz qolsa uxlaydi -> avtohabar to'xtaydi. UptimeRobot bilan `/health` ni har 5 daqiqada pinglang yoki VPS ishlating.
- Render bepul tarifida fayl tizimi vaqtinchalik: SQLite (akkaunt sessiyalari) qayta ishga tushganda o'chadi. Render Disk (pullik) yoki VPS tavsiya etiladi.
- Userbot Telegram qoidalariga zid bo'lishi mumkin: alohida raqam ishlating, intervalni kichraytirmang.
