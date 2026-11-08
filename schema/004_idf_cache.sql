-- =============================================================================
-- News_Mayab — Migración 004: Caché diaria de IDF histórico (30 días) en SQLite (Tarea C.2)
-- =============================================================================

CREATE TABLE IF NOT EXISTS idf_corpus_cache (
    cache_date_utc TEXT PRIMARY KEY CHECK (length(cache_date_utc) = 10),
    computed_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    window_days INTEGER NOT NULL CHECK (window_days > 0),
    max_docs INTEGER NOT NULL CHECK (max_docs > 0),
    total_docs INTEGER NOT NULL CHECK (total_docs >= 0),
    doc_freq_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(doc_freq_json))
) STRICT;
