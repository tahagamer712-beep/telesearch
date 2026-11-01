# TROUBLESHOOTING
| المشكلة | الحل |
|---|---|
| `Bot already running` | نسخة تعمل فعلاً. `./status.sh` ثم `./stop.sh`. (هذا يمنع Telegram Conflict) |
| البوت لا يرد | `tail -30 logs/app.log` • تأكد TOKEN صحيح ورقمك في AUTHORIZED_USER_IDS • الانترنت متصل؟ |
| يرد `⛔ Access Denied` لك | رقمك في `.env` غلط. استخدم userinfobot ثم أعد التشغيل |
| ملف 7z/RAR يظهر unsupported | `pkg install p7zip unrar` ثم من المصادر اضغط Re-index |
| PDF يظهر "Text extraction unavailable" | اما ممسوح (OCR_REQUIRED) او ينقص `pkg install poppler` |
| XLS فشل | قد يكون BIFF5 قديم او بكلمة مرور؛ افتحه بـExcel واحفظه xlsx |
| `مساحة غير كافية` / المهمة paused | حرّر مساحة او خفّض MIN_FREE_STORAGE_GB ثم من المهام اضغط استئناف |
| الهاتف يسخن/يبطئ | قلّل BATCH_SIZE وMAX_RAM_PERCENT/MAX_CPU_PERCENT وزد THROTTLE_DELAY_SEC |
| البحث بطيء جداً | `./status.sh` : اذا FTS5 غير متوفر فالبحث بـLIKE. استعمل كلمة اطول/سنة لتضييق البحث |
| تقرير PDF لا يصل | اكبر من حد Telegram؛ مساره المحلي يظهر لك. استخدم HTML/TXT للتقارير الكبيرة |
| PDF العربي مربعات | اضبط `PDF_FONT_PATH` لخط TTF فيه Arabic Presentation Forms (مثل DejaVuSans المرفق) |
| فشل الاستعادة | `restore` ترفض الملف التالف/كلمة المرور الخاطئة عمداً؛ جرّب نسخة اقدم. قاعدتك الحالية محفوظة كـpre_restore_* |
| نسيت كلمة مرور النسخ | لا يمكن استرجاعها (تشفير حقيقي) |
| توقف الجهاز اثناء الفهرسة | شغّل `./start.sh`؛ المهام تُستأنف من آخر checkpoint بدون تكرار |
| أريد اضافة موظف | `/admin` → 👥 المستخدمون → ➕ Add User (ID + اسم) ثم حدد صلاحياته |
| مطابقة خاطئة / ربط مشكوك | `/admin` → 🔗 Entity Resolution → 🔍 Review: Confirm / Reject (NEVER MATCH) / Conflict؛ او ✂️ Unmerge من شاشة الكيان |
| عمود لم يُصنَّف | `/admin` → 🧬 Schema Mapping: اختر الحقل القياسي او أضف مرادفاً ثم Run Matching |
الفحص الشامل: `python -m app.cli health --deep`
