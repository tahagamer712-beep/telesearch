# FINAL STATUS — PRIVATE DATABASE vFINAL (Patient Record Linkage)
**Automated tests (Phase 9, 2026-10-27): 206 ran, 202 passed, 0 failed, 3 errors, 1 skipped** (Galaxy Termux / Python 3.14.8 / SQLite 3.54.1 / aarch64، مع `-W default`). الأخطاء الثلاثة كلها ملفات `test_data` غائبة عن شجرة الاختبار المؤقتة (بيئة، لا كود). **Nokia 6.1 ما زال NEEDS_TERMUX_TEST** (إصلاح ingest الخاص بها مُثبت باختبار دخان معزول بدل الحزمة الكاملة).
مفاتيح: VERIFIED=اختُبر فعلاً هنا • UNVERIFIED • NEEDS_TERMUX_TEST • OPTIONAL • UNSUPPORTED • MISSING • PARTIAL • (اختبار: PASS/FAIL/SKIPPED/NEEDS_ENVIRONMENT).

## الفحص الاولي للمشروع (EXISTS / WORKING / PARTIAL / MISSING / BROKEN / NEEDS_TEST)
| الجزء | قبل هذه النسخة | الآن |
|---|---|---|
| Ingestion (ارشيفات/صيغ/multipart/streaming/hash/duplicate) | EXISTS+WORKING | كما هو + **XLS** + ragged CSV (VERIFIED) |
| Search (exact/prefix/FTS5/fuzzy/Arabic/phone/date) | EXISTS+WORKING | كما هو + تجميع النتائج بالكيان الموحد |
| Roles | 4 ادوار (owner-only) | **استُبدلت بـ ADMIN/USER** |
| Admin Panel / User management | MISSING | **جديد** (PARTIAL: انظر الاستثناءات) |
| Entity Resolution | MISSING | **جديد** |
| Unified Excel | MISSING | **جديد** |
| OpenRouter / Jobs / Reports / Backup / Audit | EXISTS+WORKING | كما هي (+ job ER/Export، + manifest SHA في النسخ) |

## التقرير النهائي
ARCHITECTURE: Telegram(UI) → authorize(.env ADMIN | DB USER) → Application → {Ingestion, Search, ER, AI(DSL only)} → SQLite+FTS5 → ملفات اصلية. **VERIFIED (منطق) / NEEDS_TERMUX_TEST**
DATABASE: migrations 001–010 (users ADMIN/USER، ER: field_mappings, schema_synonyms, er_records, er_blocks, entities, entity_records, entity_matches, entity_merge_history, match_decisions, matcher_versions, search_groups، reports xlsx، 009: mother_name، 010: ER build indexes). ترقية من 001 تحافظ على البيانات. **VERIFIED**
INGESTION: ZIP/TAR/GZ/BZ2/XZ/multipart/CSV/TSV/JSON/JSONL/SQL/SQLite/XLSX/**XLS**/TXT/PDF نصي **VERIFIED**؛ 7Z/RAR **UNVERIFIED (تحتاج p7zip/unrar)**؛ PDF ممسوح = OCR_REQUIRED.
SCHEMA DETECTION: مرادفات عربي/انجليزي + اضافات الادمن، ثقة HIGH/MEDIUM/NEEDS_REVIEW/NONE مع سبب، ولا استخدام للمنخفض بلا تأكيد الادمن، الاعمدة المجهولة محفوظة. **VERIFIED** (على المخطط المرفوع والمخطط النصي). بلا headers: يعتمد على القيم فقط (PARTIAL).
NORMALIZATION: original+normalized، عربي/هاتف/تاريخ. **VERIFIED**
DEDUPLICATION: 4 مستويات (ملف SHA-256 / صف مكرر تام / تكرار مطبّع احصائياً / كيان محتمل). **VERIFIED** (تكرار مطبّع = احصاء فقط، لا اجراء).
ENTITY RESOLUTION + MATCHING + MATCH CONFIDENCE + MATCH EVIDENCE + CONFLICTS: قواعد موثقة (docs/MATCHING.md)، حالات المطابقة السبع، ثقة = قيمة قاعدة موثقة (ليست احتمالاً معايراً)، ادلة وتعارضات لكل زوج، حراس (تعارض متعدٍّ، غموض، NEVER MATCH). **VERIFIED على بيانات اصطناعية**. دقة حقيقية: **UNVERIFIED**.
HUMAN REVIEW: Review/Conflicts/Confirm/Reject(NEVER MATCH)/Mark Conflict/Unmerge/Merge History. **VERIFIED (منطق + FakeAPI)**، لم يُجرَّب على Telegram الحقيقي.
UNIFIED DATASET: كيان موحد = عرض فوق الخام، مصدر لكل قيمة، قيم متعددة (Known Values) وسياسة preferred، لا فقدان. **VERIFIED**
SEARCH: بحث على الخام + تجميع بالكيان + 📚 مصادر + 🧠 لماذا (محلي حتمي). **VERIFIED**. PARTIAL: شرط يجمع سجلات مختلفة للكيان نفسه غير مدعوم.
TELEGRAM / ADMIN / USERS: /start مختلف للادمن واليوزر، /admin محمي server-side، قائمة اوامر بنطاق، ادارة مستخدمين كاملة (Add/List/Search/Edit perms/Disable/Enable/Remove/Activity). **VERIFIED بـFakeAPI**؛ `setMyCommands` الحقيقي **UNVERIFIED**.
OPENROUTER: اختياري، DSL فقط (يقبل صيغة spec: terms/filters/field/operator/fuzzy/limit)، حجب الارقام والايميلات، fallback. **VERIFIED (خادم محاكٍ)**؛ شبكة حقيقية NEEDS_TERMUX_TEST. شاشة OpenRouter في الادمن = عرض حالة فقط (المفتاح لا يُدخل من Telegram: **بالتصميم**).
UNIFIED RECORD PROFILE: **VERIFIED** (مصدر ووقت استيراد لكل قيمة، السجلات الاصلية، الادلة، audit، بلا شبكة). NAME VARIANTS: **VERIFIED** (بحث بأي variant، توضيح القيمة والمصدر والصف، بلا aliases مخمّنة). SCHEDULED NOTIFICATIONS: **PARTIAL بالتصميم**: stats / ops_summary / task_events فقط (VERIFIED)؛ **Saved Search / Query / تقارير بسجلات مجدولة = UNSUPPORTED عمداً**؛ التنفيذ كـTask مع صلاحيات المستلم الحالية وaudit وretry. شاشة الاشعارات في الادمن موجودة. MODULES: **MISSING** (غير منفَّذ). MANDATORY CHANNELS (Force Subscription كوحدة access-control عامة ومستقلة): **VERIFIED بـFake API (25 اختبار)** / Telegram الحقيقي NEEDS_TERMUX_TEST (انظر docs/FORCE_SUBSCRIPTION.md). NOTIFICATIONS screen / SCHEDULED TASKS: **موجودة** (تصحيح تدقيق 2026-10-27: `app/notify/service.py` + شاشات `nt_*` في الادمن + اختباراتها؛ السطر القديم "MISSING" كان قديماً). RANDOM RECORD: **EXCLUDED بالاتفاق** (لا إرسال تلقائي لسجلات). إشعارات انتهاء المعالجة/المطابقة/التصدير تعمل.
JOBS: queued/running/paused/interrupted/done/failed/cancelled + checkpoint + استئناف (ER ايضاً) + retry. **VERIFIED**
EXPORT / EXCEL: HTML/TXT/PDF (عربي، تحقق بصري) + Unified Excel (7 اوراق، streaming، تقسيم الاوراق عند 1M صف). **VERIFIED**؛ فتحه في Excel/WPS على Android **UNVERIFIED**. Auto Unified Export موجود (اعداد Auto Excel) **VERIFIED بالمنطق**.
BACKUP: openssl AES-256 + SHA-256 + manifest + رفض التالف/غير المتوافق + عدم استعادة جزئية. **VERIFIED**
SECURITY / AUDIT: انظر docs/SECURITY.md. **VERIFIED** (IDOR، callbacks، admin-only، SQLi، archive، secrets).
DIAGNOSTICS: status.sh + شاشة Diagnostics (+ Users/ER/Schema checks). **VERIFIED**. Developer: وضع المطور + Diagnostics (PARTIAL: لا أدوات اضافية).
TERMUX: **NEEDS_TERMUX_TEST** لكل شيء (docs/COMPATIBILITY.md). Python 3.14: فحص ساكن + deprecation فقط.
TESTS (Phase 9 Galaxy 2026-10-27): PASS 202 • FAIL 0 • ERRORS 3 (ملفات test_data غائبة) • SKIPPED 1 • NEEDS_ENVIRONMENT: 7z/rar الفعلي، Telegram/OpenRouter الحقيقي، Nokia Termux.

## قياس الدقة (اصطناعي فقط)
Precision 1.000 • Recall 0.878–0.889 • FPR 0.000 • FNR 0.112–0.122 • FP=0 (250/2k/8k مريض). المتبقي بالتصميم (اسم فقط بلا معرّف/ميلاد، ومعرفان مختلفان لنفس الشخص). **REAL-WORLD MATCHING ACCURACY: UNVERIFIED.**

## Known limitations
7z/rar غير مختبرة • لا OCR • الكتل الفرعية الأكبر من 300 سجل تُتخطّى وتُعرض (v2 يقسّم الكتل الكبيرة بالاسم أولاً) • ER على ملايين السجلات UNVERIFIED (27k سجل=95ث هنا) • القاعدة غير مشفرة • لا بحث عابر لسجلات الكيان الواحد بشروط مركّبة • Unmerge لكيان >60 سجلاً يتطلب تحديد السجلات (زر ✂️ Unmerge في شاشة الكيان للادمن) • Modules غير منفذة • لا masking بين المستخدمين (من لديه search يرى كل الحقول).

## Required actions on Nokia
1) فك الملف و`./install.sh --optional` وقراءة الجدول. 2) `nano .env` (TOKEN، رقمك في AUTHORIZED_USER_IDS، BACKUP_PASSPHRASE). 3) `./start.sh` ثم `./status.sh` (FTS5=OK، Python 3.14). 4) انسخ `test_data/hospital_synthetic/*` الى `data/incoming/` وجرب: `/admin` → 🔗 Entity Resolution → Run Matching → Review → Unified Excel. 5) أضف USER تجريبياً بصلاحية search فقط وتأكد أنه لا يرى `/admin`. 6) جرّب `pkg install p7zip` ثم 7z حقيقي. 7) ملف كبير 1–4GB: راقب الذاكرة/الحرارة/الزمن، وراقب `skipped_oversized_blocks` وزمن ER. 8) افتح ملف Unified Excel في تطبيق Excel/WPS. 9) `./backup.sh` ثم جرّب الاستعادة. 10) ان فشل شيء أرسل `./status.sh` وآخر 50 سطراً من `logs/app.log`.

## ملاحظات تدقيق 2026-10-27 (Phase 9 — إصلاحات مطبّقة)
- **F1 (P0):** إعادة الفهرسة/حذف الملف كانت تترك `entity_matches`/`match_decisions`/`er_blocks` قديمة (بلا FK) فتتعطل كل `recluster` لاحقة بخطأ FK — بما فيها إعادة البناء بعد قرار على حافة قديمة. الآن: الحذف يطهّر الحواف مع السجلات بنفس المعاملة، و`recluster` يتخطى أي حافة لمعرّفات محذوفة (دفاع بالعمق) مع تحذير بالسجل.
- **CPU على Android (P0):** `ResourceMonitor.cpu_percent` كان ينهار بـ`AttributeError` (لا `getloadavg` على bionic) عندما يُمنع `/proc/stat`، فيفشل ingest كاملاً على Nokia. الآن يرجع `None` (CPU مجهول = لا ضغط).
- **Master Excel (P1):** كان متاحاً لأي حامل صلاحية `export` (تفريغ كامل للقاعدة) — الآن للأدمن فقط (`manage`) ولا يظهر في شاشات غير الأدمن.
- **ملفات الكيانات (P1):** غير الأدمن يفتح الآن فقط الكيانات الظاهرة في بحوثه الخاصة (`search_groups` الخاصة به)؛ `entw`/`entm` بلا قيم فبقيت كما هي.
- **Re-index لملف غير موجود (P1):** فحص وجود قبل إرسال المهمة (رسالة "غير موجود" نظيفة بدل IntegrityError).
- **Ingest (P1):** ملف CSV بعناوين فقط = 0 سجلات (لا سجل وهمي بالعنوان)؛ `.xlsx` تالف بمحتوى نصي = مرفوض لا مفهرس كنص؛ رسالة أجزاء CSV المقطعة لا تذكر "الأرشيف الأب" بعد؛ أجزاء ناقصة (001+003) = رفض واضح لا فهرسة نصفية صامتة؛ أسماء الملفات العربية الطويلة تُقطَّع بالبايت (حد 255 مع بادئة SHA).
- **FTS5:** عقد "النص المطبّع فقط" موثّق في README (استعلام خام بعربية غير مطبّعة يرجع صفراً).
- **WAL (P1):** الاستعادة تفرّغ WAL أولاً (`wal_checkpoint(TRUNCATE)`) قبل نسخ `pre_restore_*`، وترفض بدل إتلاف البيانات إن تعذّر التفريغ.
- **Export dedup (P1):** ضغطات `exf` المتكررة لنفس (بحث، مستوى، صيغ) مع مهمة معلقة تُعاد بدل التكرار.
- **حذف الجدولة (P1):** `nt:rm` يطلب تأكيداً الآن مثل `fs:rm`/`usr:rm`.
- **P2:** فشل استيراد `app.reports` يفشل عند الإقلاع بوضوح (لا `None` ينفجر وقت التصدير)؛ اعتماديات الاختبار معلنة (`requirements-test.txt` + خطوة install.sh)؛ اختبار `no_secrets` مقصور على أشجار التوزيع (أشجار النشر الحية فيها `.env` بالتصميم)؛ إغلاق اتصالات SQLite في `health.py`/`backup.py`/`compat.py` (مسارات الخطأ كانت تسرّب)؛ تعطيل/حذف المستخدم يمسح حالته الحية؛ نسبة ضغط الأرشيفات الافتراضية 2000→500 (أقل من سقف deflate ~1032)؛ `sdh` يتطلب `sources`؛ قائمة `adv` تتطلب `search` عند الفتح؛ رجوع `diag` يعود لشاشة المتصل (`set`/`ap`)؛ `setp` قائمة بيضاء (10/25/50/100) مع حماية للقيم المسمومة قديماً؛ `ANALYZE` بعد فهرسة ≥1000 سجل وعند اكتمال بناء ER.
- **P3:** رسائل المستخدم بلا مسارات مطلقة (اسم الملف فقط)؛ تفاصيل التدقيق تُحجب فيها القيم بشكل التوكنات أيضاً؛ `idx` يتيم بالتصميم (بلا زر، محمي للأدمن) والاستعادة طرفية فقط — كلاهما موثّق هنا لا محذوف؛ `app/ai/organize.py` مستقل غير موصول (حمولاته تُحجب فيها الإيميلات والهواتف والمعرّفات الرقمية) ولا يُوصَل إلا بأمر صريح من المستخدم.
- **اختبارات قديمة (P1):** توقع إصدار المطابقة v2 وترحيلات 001..010 (القديم 1 و001..008 كان قديماً لا نقصاً بالكود).
