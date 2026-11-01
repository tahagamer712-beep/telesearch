# COMPATIBILITY — ما تحقق منه وما يحتاج Nokia
المفاتيح: **VERIFIED** (اختُبر فعلاً بيئة التطوير: Linux x86_64 / Python 3.12.3 / SQLite 3.45.1) • **UNVERIFIED** (مكتوب لكن لم يُشغَّل) • **OPTIONAL** • **UNSUPPORTED** • **NEEDS_TERMUX_TEST** (لا يمكن اثباته الا على Nokia 6.1 / Android 10 / Termux / Python 3.14.6 / SQLite 3.53.4 / aarch64).

| البند | الحالة | ملاحظة |
|---|---|---|
| منطق المشروع (206 اختبار آلي) على Galaxy Termux / Python 3.14.8 | VERIFIED | 202 pass / 0 fail / 3 errors (ملفات test_data غائبة) / 1 skipped — Phase 9 2026-10-27 |
| Python 3.14.6 التشغيل الفعلي | **NEEDS_TERMUX_TEST** | فحص ساكن فقط: لا استخدام لواجهات محذوفة (imp/cgi/utcnow/asyncio...) والاختبارات تمر مع `-W error::DeprecationWarning` على 3.12. لا مكتبات pip ⇒ لا خطر تجميع. |
| SQLite 3.53.4 | **NEEDS_TERMUX_TEST** | يلزم >= 3.35 فقط (RETURNING، window functions). مُختبر على 3.45.1. |
| FTS5 في libsqlite الخاصة بـTermux | **NEEDS_TERMUX_TEST** | `./status.sh` يفحصه. اذا غاب: يعمل البحث بـLIKE (ابطأ) |
| aarch64 / Android 10 | **NEEDS_TERMUX_TEST** | كود Python صرف؛ لا ملحقات C خاصة |
| ZIP, TAR, TAR.GZ, GZ, BZ2, XZ, multipart `.001` | VERIFIED | stdlib |
| 7Z | **UNVERIFIED** + OPTIONAL | يحتاج `pkg install p7zip`. غير مثبت في بيئة التطوير ولا عندك حالياً. مُختبر: غيابه (الملف يُحفظ unsupported برسالة واضحة) ومحلل قائمة الملفات وكشف المسارات الخبيثة. الاستخراج الفعلي NEEDS_TERMUX_TEST |
| RAR | **UNVERIFIED** + OPTIONAL | يحتاج `7z` (مع دعم rar) او `unrar`. توفر RAR في p7zip على Termux غير مؤكد |
| Multipart لـ7Z/RAR | **UNVERIFIED** | يُمرَّر الجزء الاول للأداة (تتولى هي التجميع) |
| CSV, TSV, JSON(stream), JSONL, SQL, SQLite, TXT | VERIFIED | |
| XLSX | VERIFIED | streaming، مُختبر بملفات openpyxl/اصطناعية |
| **XLS (Excel 97-2003)** | VERIFIED (3.12) / NEEDS_TERMUX_TEST (3.14) | قارئ BIFF8 مكتوب بالـstdlib (بدون xlrd لأن توافق xlrd مع 3.14 غير قابل للتحقق هنا). مُختبر على ملف XLS حقيقي من LibreOffice (2005 سجل، ورقتان، تواريخ، نصوص طويلة عبر CONTINUE). غير مدعوم (يُبلَّغ بوضوح): BIFF5/95 وملفات بكلمة مرور |
| PDF نصي | VERIFIED مع `pdftotext` | يحتاج `pkg install poppler` (OPTIONAL). بدونها: "Text extraction unavailable" |
| PDF ممسوح (صور) | `OCR_REQUIRED` | لا OCR مضمّن؛ يُسجَّل "Text extraction unavailable / OCR_REQUIRED" ولا يُدّعى انه قابل للبحث. OCR اختياري مستقبلاً (ثقيل على Nokia) |
| تقارير HTML/TXT | VERIFIED | |
| تقارير PDF عربي | VERIFIED (تحقق بصري) | خط DejaVu مرفق (assets/fonts). التشكيل يُحذف؛ bidi مبسّط |
| تشفير النسخ (openssl AES-256) | VERIFIED مع openssl هنا / NEEDS_TERMUX_TEST | `pkg install openssl-tool` |
| SQLCipher | UNVERIFIED — غير مستخدم | انظر SECURITY.md |
| قراءة CPU/RAM من /proc | NEEDS_TERMUX_TEST | Android قد يمنع `/proc/stat`؛ يوجد fallback (loadavg ثم None) والنظام لا ينهار |
| حرارة الجهاز | NEEDS_TERMUX_TEST | `/sys/class/thermal` قد لا يكون مقروءاً؛ اذا غاب نستعمل CPU/RAM فقط |
| wake-lock / tmux / Termux:Boot | OPTIONAL | `termux-wake-lock` من حزمة termux-api (اختياري) |
| اتصال Telegram الحقيقي | NEEDS_TERMUX_TEST | المنطق مُختبر بـFakeAPI وخادم HTTP محاكٍ؛ لم يُجرَّب على api.telegram.org |
| اتصال OpenRouter الحقيقي واسم الموديل | NEEDS_TERMUX_TEST | مُختبر بخادم محاكٍ. تأكد ان الموديل المجاني متاح وقت التشغيل |
| حدّا Telegram (تنزيل ~20MB، رفع ~50MB) | UNVERIFIED | من المعرفة العامة؛ قابلة للتعديل بـ.env |
| سرعة/ذاكرة ملف 4GB حقيقي على Nokia | **NEEDS_TERMUX_TEST** | التصميم streaming (لا `read()` كامل؛ ذاكرة مقاسة ثابتة ~30-50MB)، لكن الزمن الفعلي على Nokia غير معروف |

| Entity Resolution (منطق + SQL: window functions, partial unique index, json_insert, RETURNING) | VERIFIED على SQLite 3.45.1 / NEEDS_TERMUX_TEST على 3.53.4 | المتطلب >= 3.35؛ `json_insert` يلزم JSON1 (مضمّن افتراضياً) |
| Unified Excel (writer مكتوب بالـstdlib) | VERIFIED (يُقرأ بـopenpyxl في الاختبارات فقط) / لم يُفتح في Excel/WPS على Android | |
| اوامر Telegram `setMyCommands` بنطاق chat لكل ادمن | UNVERIFIED على Telegram الحقيقي | مكتوب حسب Bot API؛ اذا رفضه Telegram يبقى `/admin` محمياً server-side |
| Force Subscription: `getChatMember` الحقيقي (البوت مشرف في القناة، قنوات خاصة، restricted) | **UNVERIFIED / NEEDS_TERMUX_TEST** | المنطق مختبر بـFake API |
| Scheduled notifications على Telegram الحقيقي (الارسال، حد الرسائل، تغيّر ساعة الجهاز) | **UNVERIFIED / NEEDS_TERMUX_TEST** | المنطق مختبر بمرسل وهمي |
| ER على ملايين السجلات / Nokia | **UNVERIFIED / NEEDS_TERMUX_TEST** | المقاس: 27k سجل = 95 ثانية على جهاز التطوير (التصادم عالٍ في بيانات الاختبار) |

## قياسات (جهاز التطوير، ليست Nokia! سيكون Nokia ابطأ غالباً 5-10×)
- 10k سجل: فهرسة 0.3s • بحث <0.02s • RAM 28MB
- 100k: 7.9s (12.7k rec/s) • بحث prefix 0.11s • DB 71MB • RAM 29MB
- 1M: 102s (9.8k rec/s) • بحث exact 0.001s • prefix (234k نتيجة) 1.4s • fuzzy 0.8s • DB 716MB • RAM 29MB
- استخراج zip + فهرسة 300k سجل: 20s • RAM 49MB • FTS ≈ 42MB من DB 235MB
- تقرير HTML 300k نتيجة: 8.4s (307MB) • TXT: 7s (138MB) • **PDF 111k نتيجة (Evidence): 142s** (بطيء: لا تصدّر PDF لمئات الآلاف؛ استخدم HTML/TXT)
## ما يجب تشغيله على Nokia
انظر آخر `STATUS.md` ("Required actions on Nokia").
