# Wolf UC Store: bot + sayt (Railway)

Bitta Python xizmati: saytni beradi, API'ni ishlatadi va Telegram botni yuritadi.

## Railway Variables
- `BOT_TOKEN`   - @Wolf_uc_bot tokeni (BotFather)
- `ADMIN_ID`    - asosiy ega/admin Telegram ID (chek va buyurtmalar shunga keladi)
- `OWNER_IDS`   - (ixtiyoriy) qo'shimcha egalar, vergul bilan: `111,222`
- `CARD_NUMBER` - to'lov kartasi (bo'sh qolsa: 5614 6822 0781 4403)
- `CARD_OWNER`  - karta egasi ismi (bo'sh qolsa: Wolf UC Store)
- `WEBAPP_URL`  - Railway bergan https manzil (menyudagi ko'k tugma shu saytni ochadi)
- `DATA_DIR`    - `/data` (Railway Volume mount path ham `/data`)

## Karta orqali to'lov oqimi
1. Mijoz saytda PUBG ID ni kiritib "Tekshirish"ni bosadi, paketni tanlaydi, "sotib olish".
2. Karta va summa chiqadi -> "Karta to'lovni qildim" -> chek rasmini joylaydi -> "Yuborish".
3. Egaga chek rasmi + PUBG ID + username + Telegram ID + Tasdiqlash/Rad etish keladi.
4. Tasdiqlansa mijozga: "UC yaqinda tushadi ✅". Rad etilsa rad xabari.
5. Ega UC ni PUBG ID ga tashlaydi va botga `/yuborildi MIJOZ_TELEGRAM_ID` yozadi
   (yoki tugmani bosadi) -> mijozga: "UC tushdi ✅".
   `/yuborildi` (idsiz) UC kutayotganlar ro'yxatini ko'rsatadi.

Balans (To'ldirish, Profil, Buyurtmalar tarixi) avvalgidek ishlaydi; balansi yetarli mijoz
buyurtma oynasida "Balansdan to'lash" tugmasini ham ko'radi.

## Muhim
- Sayt Railway'ning o'zidan ochiladi (`/api` shu yerda). Vercel'dagi nusxada API yo'q.
- Volume qo'shmasangiz, har deploydan keyin baza o'chadi.
- Narxlar: `botboshliq.py` dagi `PACKAGES` va `static/index.html` ikkalasida.
