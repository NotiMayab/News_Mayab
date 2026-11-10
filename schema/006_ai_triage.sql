-- =============================================================================
-- News_Mayab — Migración 006: Triaje Asistido por IA (Consultivo, sin autoridad)
-- =============================================================================
-- Invariante (ADR-003 / ADR-013): La IA produce únicamente dictámenes de triaje
-- consultivo para el operador humano. Por diseño y construcción, esta tabla vive
-- aislada y no altera review_status ni verification_history en events.

CREATE TABLE IF NOT EXISTS ai_triage_dictamenes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES events(event_id) ON UPDATE CASCADE ON DELETE CASCADE,
    input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
    model_name TEXT NOT NULL CHECK (length(trim(model_name)) > 0),
    model_digest TEXT NOT NULL CHECK (length(trim(model_digest)) > 0),
    prompt_version TEXT NOT NULL CHECK (length(trim(prompt_version)) > 0),
    status TEXT NOT NULL CHECK (status IN ('OK', 'ERROR', 'STALE')),
    error_code TEXT CHECK (error_code IS NULL OR error_code IN (
        'TIMEOUT',
        'BAD_JSON',
        'SCHEMA_VIOLATION',
        'EVIDENCE_MISMATCH',
        'CONN_REFUSED',
        'NON_LOOPBACK_HOST',
        'INSTRUCTION_IN_CONTENT',
        'MODEL_ERROR'
    )),
    suggested_action TEXT CHECK (suggested_action IS NULL OR suggested_action IN (
        'SUGGEST_APPROVE',
        'SUGGEST_HOLD',
        'SUGGEST_REJECT'
    )),
    priority INTEGER CHECK (priority IS NULL OR (priority BETWEEN 1 AND 5)),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    signals_json TEXT CHECK (signals_json IS NULL OR json_valid(signals_json)),
    deterministic_flags_json TEXT NOT NULL CHECK (json_valid(deterministic_flags_json)),
    justification TEXT CHECK (justification IS NULL OR length(justification) <= 300),
    created_at_utc TEXT NOT NULL,
    UNIQUE (event_id, input_hash, model_digest, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_triage_event ON ai_triage_dictamenes(event_id, created_at_utc);
CREATE INDEX IF NOT EXISTS idx_triage_priority ON ai_triage_dictamenes(status, priority, created_at_utc);
