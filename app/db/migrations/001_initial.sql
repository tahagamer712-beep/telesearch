-- users: المستخدمون المصرح لهم + الدور (RBAC)
CREATE TABLE users (
    telegram_id  INTEGER PRIMARY KEY,
    display_name TEXT,
    role         TEXT NOT NULL CHECK (role IN ('admin','exporter','reader','searcher')),
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- files: كل ملف (اصلي او مستخرج من ارشيف) مع hash وحالته
CREATE TABLE files (
    id             INTEGER PRIMARY KEY,
    parent_file_id INTEGER REFERENCES files(id) ON DELETE RESTRICT,
    original_name  TEXT NOT NULL,
    stored_path    TEXT NOT NULL,
    sha256         TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    detected_type  TEXT,
    mime           TEXT,
    parser         TEXT,
    status         TEXT NOT NULL DEFAULT 'received' CHECK (status IN
                   ('received','queued','processing','indexed','partial','failed','quarantined','unsupported')),
    error          TEXT,
    record_count   INTEGER NOT NULL DEFAULT 0,
    received_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    indexed_at     TEXT
);
CREATE INDEX idx_files_sha256 ON files(sha256);
CREATE INDEX idx_files_status ON files(status);
CREATE INDEX idx_files_parent ON files(parent_file_id);

-- file_parts: اجزاء الارشيف متعدد الاجزاء (.001 .002 ...)
CREATE TABLE file_parts (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    part_index INTEGER NOT NULL,
    path       TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256     TEXT,
    UNIQUE (file_id, part_index)
);

-- archives: نتيجة فحص/استخراج الارشيف
CREATE TABLE archives (
    id                 INTEGER PRIMARY KEY,
    file_id            INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    format             TEXT NOT NULL,
    member_count       INTEGER,
    uncompressed_bytes INTEGER,
    extract_dir        TEXT,
    status             TEXT NOT NULL DEFAULT 'pending',
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- fields: الـSchema الاصلي المكتشف لكل ملف (اسماء الحقول كما هي)
CREATE TABLE fields (
    id            INTEGER PRIMARY KEY,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    position      INTEGER NOT NULL,
    detected_type TEXT,
    UNIQUE (file_id, name)
);

-- records: سجل واحد + مكانه بالملف الاصلي (صف/شيت/جدول) للـprovenance
CREATE TABLE records (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    locator    TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_records_file ON records(file_id);

-- record_values: القيمة الاصلية + المطبّعة لكل حقل (الاصل ما ينعدّل ابداً)
CREATE TABLE record_values (
    id               INTEGER PRIMARY KEY,
    record_id        INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
    field_id         INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
    original_value   TEXT,
    normalized_value TEXT
);
CREATE INDEX idx_rv_record ON record_values(record_id);
CREATE INDEX idx_rv_field_norm ON record_values(field_id, normalized_value);

-- jobs / job_events: طابور العمليات مع checkpoint للاستئناف
CREATE TABLE jobs (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,
    file_id     INTEGER REFERENCES files(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
                ('queued','running','paused','interrupted','done','failed','cancelled')),
    progress    REAL NOT NULL DEFAULT 0,
    checkpoint  TEXT,
    created_by  INTEGER,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE TABLE job_events (
    id      INTEGER PRIMARY KEY,
    job_id  INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    ts      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    level   TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL
);
CREATE INDEX idx_job_events_job ON job_events(job_id);

-- audit_log: كل بحث/فتح/تصدير/تغيير اعدادات (بدون اسرار)
CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    user_id     INTEGER,
    action      TEXT NOT NULL,
    target_type TEXT,
    target_id   TEXT,
    details     TEXT
);
CREATE INDEX idx_audit_ts ON audit_log(ts);
CREATE INDEX idx_audit_user ON audit_log(user_id, ts);

-- search_history: آخر عمليات البحث (مع retention قابل للتعديل)
CREATE TABLE search_history (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    user_id      INTEGER NOT NULL,
    mode         TEXT NOT NULL CHECK (mode IN ('quick','advanced','ai')),
    query_json   TEXT NOT NULL,
    result_count INTEGER
);

-- reports: سجل التقارير المصدّرة
CREATE TABLE reports (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    user_id      INTEGER NOT NULL,
    search_id    INTEGER REFERENCES search_history(id) ON DELETE SET NULL,
    format       TEXT NOT NULL CHECK (format IN ('html','txt','pdf')),
    level        TEXT NOT NULL CHECK (level IN ('results','evidence','metadata')),
    result_count INTEGER,
    path         TEXT NOT NULL
);

-- callback_state: حالة ازرار Telegram (نخزن ID فقط داخل callback_data)
CREATE TABLE callback_state (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- settings: اعدادات وقت التشغيل (بدون مفاتيح API)
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
