# INSTALL — تثبيت خطوة بخطوة (لا تحتاج خبرة)
## 0) ما تحتاجه
- Termux (من F-Droid) • Python (`pkg install python`) • توكن بوت من @BotFather • رقم حسابك في Telegram (ارسل /start لبوت userinfobot).
## 1) انسخ المشروع
انسخ `private_database_FINAL.zip` الى الجهاز ثم:
```
cd ~
unzip private_database_FINAL.zip
cd private_database
```
(انسخ الى مجلد Termux الخاص `~`، لا الى /sdcard، لأن /sdcard قابل للقراءة من تطبيقات اخرى.)
## 2) ثبّت
```
chmod +x install.sh
./install.sh --optional
```
هذا الأمر: يفحص Python وSQLite وFTS5، ينشئ `.env` والمجلدات والقاعدة، يحاول تثبيت p7zip وunrar وpoppler وopenssl-tool وtmux (اذا فشل شيء يكمل ويخبرك)، ويعرض جدول SUPPORTED/MISSING/OPTIONAL/NEEDS_TEST، ثم يشغّل الاختبارات.
## 3) اضبط الاعدادات
```
nano .env
```
املأ: `TELEGRAM_BOT_TOKEN` • `AUTHORIZED_USER_IDS` (رقمك فقط) • `BACKUP_PASSPHRASE` (كلمة مرور قوية؛ احفظها خارج الجهاز ايضاً). اختياري: `OPENROUTER_API_KEY` و`OPENROUTER_MODEL`. احفظ بـCtrl+O ثم Enter ثم Ctrl+X.
## 4) شغّل
```
./start.sh
./status.sh
```
افتح البوت في Telegram واكتب /start. كأدمن ستجد زر ⚙️ ADMIN PANEL (او `/admin`) لإدارة المستخدمين وربط السجلات. للايقاف: `./stop.sh`.
(أول استخدام: `AUTHORIZED_USER_IDS` يجب أن يحتوي رقمك؛ يمكن وضع أكثر من رقم ADMIN مفصولة بفاصلة.)
## 5) اضافة ملفات
- صغيرة (حتى 20MB): ارسلها للبوت من زر ⬆️ إضافة ملفات.
- كبيرة (حتى 4GB+): انسخها الى `~/private_database/data/incoming/` (من USB/شبكة/`cp`). ينتظر النظام حتى يستقر حجم الملف ثم يعالجه ويخبرك. الأجزاء `x.7z.001 .002 ...` انسخها كلها.
## 6) تحديث لاحقاً
فك النسخة الجديدة فوق القديمة **بدون** حذف `.env` و`database/` و`data/`. الـmigrations تُطبّق تلقائياً عند التشغيل.
## 7) بقاء البوت يعمل
`termux-wake-lock` (حزمة termux-api) او `KEEP_AWAKE=true` في `.env`؛ واستثنِ Termux من توفير البطارية في Android. تشغيل تلقائي بعد اعادة الإقلاع يحتاج Termux:Boot (اختياري).
