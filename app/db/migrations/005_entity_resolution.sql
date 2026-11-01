-- matcher_versions: كل نسخة خوارزمية مطابقة
CREATE TABLE matcher_versions (
    version     INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- schema_synonyms: مرادفات اسماء الاعمدة (builtin + اضافات الادمن)
CREATE TABLE schema_synonyms (
    id             INTEGER PRIMARY KEY,
    standard_field TEXT NOT NULL,
    synonym        TEXT NOT NULL UNIQUE,
    origin         TEXT NOT NULL DEFAULT 'builtin' CHECK (origin IN ('builtin','admin')),
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- field_mappings: تصنيف كل عمود مع الثقة والسبب (لا mapping صامت عند ثقة منخفضة)
CREATE TABLE field_mappings (
    field_id       INTEGER PRIMARY KEY REFERENCES fields(id) ON DELETE CASCADE,
    standard_field TEXT,
    confidence     TEXT NOT NULL CHECK (confidence IN ('HIGH','MEDIUM','LOW','NEEDS_REVIEW','NONE')),
    reason         TEXT,
    status         TEXT NOT NULL DEFAULT 'auto' CHECK (status IN ('auto','confirmed','rejected')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
-- er_records: طبقة الهوية المطبّعة المشتقة من records (لا تعدّل الخام)
CREATE TABLE er_records (
    record_id  INTEGER PRIMARY KEY REFERENCES records(id) ON DELETE CASCADE,
    file_id    INTEGER NOT NULL,
    patient_id TEXT,
    national_id TEXT,
    name_norm  TEXT,
    name_core  TEXT,
    dob        TEXT,
    sex        TEXT,
    phone      TEXT,
    fp_exact   TEXT,
    fp_norm    TEXT,
    dup_of     INTEGER,
    kind       TEXT NOT NULL DEFAULT 'identity',
    matched_ver INTEGER NOT NULL DEFAULT 0,
    built_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_er_pid ON er_records(patient_id);
CREATE INDEX idx_er_nid ON er_records(national_id);
CREATE INDEX idx_er_fpn ON er_records(fp_norm);
CREATE INDEX idx_er_file ON er_records(file_id);
-- er_blocks: مفاتيح blocking لتوليد المرشحين (بدون N x N)
CREATE TABLE er_blocks (
    block_key TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    PRIMARY KEY (block_key, record_id)
) WITHOUT ROWID;
-- entities: الكيانات الموحدة (public id = U-000001)
CREATE TABLE entities (
    id              INTEGER PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','merged_into','retired')),
    merged_into     INTEGER,
    matcher_version INTEGER NOT NULL,
    state           TEXT NOT NULL DEFAULT 'EXACT_MATCH',
    confidence      REAL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT
);
CREATE TABLE entity_records (
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    record_id INTEGER NOT NULL REFERENCES er_records(record_id) ON DELETE CASCADE,
    active    INTEGER NOT NULL DEFAULT 1,
    added_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (entity_id, record_id)
);
CREATE UNIQUE INDEX idx_entity_record_active ON entity_records(record_id) WHERE active = 1;
-- entity_matches: زوج سجلات + الحالة + الثقة + الادلة + التعارضات
CREATE TABLE entity_matches (
    id              INTEGER PRIMARY KEY,
    record_a        INTEGER NOT NULL,
    record_b        INTEGER NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('EXACT_MATCH','HIGH_CONFIDENCE_MATCH','PROBABLE_MATCH','POSSIBLE_MATCH','NEEDS_REVIEW','NO_MATCH','CONFLICT')),
    confidence      REAL NOT NULL,
    matcher_version INTEGER NOT NULL,
    evidence        TEXT NOT NULL,
    conflicts       TEXT,
    decision        TEXT CHECK (decision IN ('confirmed','rejected','conflict')),
    decided_by      INTEGER,
    decided_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (record_a, record_b)
);
CREATE INDEX idx_em_state ON entity_matches(state, decision);
CREATE TABLE entity_merge_history (
    id              INTEGER PRIMARY KEY,
    ts              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    entity_id       INTEGER,
    action          TEXT NOT NULL,
    detail          TEXT,
    matcher_version INTEGER,
    actor           TEXT
);
CREATE INDEX idx_emh_entity ON entity_merge_history(entity_id);
-- قرارات الادمن الدائمة (NEVER MATCH لا يُقترح مرة ثانية)
CREATE TABLE match_decisions (
    record_a INTEGER NOT NULL,
    record_b INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('never_match','confirmed')),
    actor    INTEGER,
    ts       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (record_a, record_b)
);
-- search_groups: نتائج البحث مجمّعة بالكيان الموحد (صفحات/تصدير)
CREATE TABLE search_groups (
    search_id  INTEGER NOT NULL REFERENCES search_history(id) ON DELETE CASCADE,
    rank       INTEGER NOT NULL,
    entity_id  INTEGER,
    record_id  INTEGER NOT NULL,
    match_type TEXT NOT NULL,
    score      REAL,
    hit_count  INTEGER NOT NULL,
    PRIMARY KEY (search_id, rank)
) WITHOUT ROWID;
