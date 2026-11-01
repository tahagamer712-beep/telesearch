-- 009: ER v2 — mother_name + support_json on the derived identity layer (raw data untouched).
ALTER TABLE er_records ADD COLUMN mother_name TEXT;
ALTER TABLE er_records ADD COLUMN support_json TEXT;
CREATE INDEX IF NOT EXISTS idx_er_mother ON er_records(mother_name);
