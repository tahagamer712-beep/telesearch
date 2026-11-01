-- 010: ER build/match speed — the build's chunk scan had no record_id index
-- (each 1000-record chunk scanned ~910k value rows); the dup check scanned
-- er_records per chunk; block lookups had no index at all.
CREATE INDEX IF NOT EXISTS idx_rv_record ON record_values(record_id, field_id, original_value, normalized_value);
CREATE INDEX IF NOT EXISTS idx_er_fp ON er_records(file_id, fp_exact);
CREATE INDEX IF NOT EXISTS idx_er_blocks_key ON er_blocks(block_key, record_id);
