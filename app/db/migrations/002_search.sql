-- فهرس على القيمة المطبّعة (بحث exact/prefix)
CREATE INDEX idx_rv_norm ON record_values(normalized_value);

-- vocab: قاموس الكلمات الفريدة (للبحث التقريبي fuzzy بدون مسح كل السجلات)
CREATE TABLE vocab (
    token TEXT PRIMARY KEY,
    n     INTEGER NOT NULL DEFAULT 1
) WITHOUT ROWID;

-- search_hits: نتائج كل بحث (للـpagination والتصدير الكامل بدون RAM)
CREATE TABLE search_hits (
    search_id  INTEGER NOT NULL REFERENCES search_history(id) ON DELETE CASCADE,
    rank       INTEGER NOT NULL,
    record_id  INTEGER NOT NULL,
    match_type TEXT NOT NULL,
    score      REAL,
    reason     TEXT,
    PRIMARY KEY (search_id, rank)
) WITHOUT ROWID;

ALTER TABLE jobs ADD COLUMN payload TEXT;
ALTER TABLE files ADD COLUMN warnings TEXT;
ALTER TABLE files ADD COLUMN source_path TEXT;
