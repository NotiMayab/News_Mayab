-- =============================================================================
-- News_Mayab — Esquema Inicial SQLite (Fase B: Núcleo MVP + Previsión Fase G)
-- =============================================================================
-- Nota: PRAGMA foreign_keys = ON debe ejecutarse en cada conexión en Python.

-- 1. Grupos propietarios / editoriales (Anti-eco por concentración de propiedad)
CREATE TABLE IF NOT EXISTS ownership_groups (
    ownership_group_id TEXT PRIMARY KEY CHECK (length(trim(ownership_group_id)) > 0),
    display_name TEXT NOT NULL,
    group_type TEXT NOT NULL CHECK (group_type IN (
        'PRIVATE_MEDIA_GROUP',
        'INDEPENDENT_OUTLET',
        'PUBLIC_STATE_ENTITY',
        'ACADEMIC_INSTITUTION',
        'WIRE_AGENCY',
        'CIVIL_SOCIETY'
    )),
    transparency_notes TEXT NOT NULL,
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;

-- 2. Catálogo cerrado de fuentes RSS/Atom
CREATE TABLE IF NOT EXISTS media_sources (
    source_id TEXT PRIMARY KEY CHECK (length(trim(source_id)) > 0),
    name TEXT NOT NULL,
    rss_url TEXT NOT NULL UNIQUE CHECK (rss_url LIKE 'https://%' OR rss_url LIKE 'http://%'),
    website_url TEXT NOT NULL CHECK (website_url LIKE 'https://%' OR website_url LIKE 'http://%'),
    source_type TEXT NOT NULL CHECK (source_type IN (
        'INDEPENDENT_MEDIA',
        'STATE_OFFICIAL',
        'WIRE_AGENCY',
        'ACADEMIC',
        'CIVIL_ORG'
    )),
    ownership_group_id TEXT NOT NULL REFERENCES ownership_groups(ownership_group_id) ON UPDATE CASCADE ON DELETE RESTRICT,
    editorial_stance_note TEXT NOT NULL,
    geographic_scope TEXT NOT NULL CHECK (geographic_scope IN (
        'YUCATAN',
        'REGIONAL_PENINSULA',
        'NATIONAL',
        'GLOBAL'
    )),
    fetch_mode TEXT NOT NULL DEFAULT 'ci_and_local' CHECK (fetch_mode IN ('ci_and_local', 'local_isp_only')),
    headline_and_link_only INTEGER NOT NULL DEFAULT 0 CHECK (headline_and_link_only IN (0, 1)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    http_etag TEXT,
    http_last_modified TEXT,
    last_checked_at_utc TEXT,
    last_success_at_utc TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    last_error_kind TEXT CHECK (last_error_kind IS NULL OR last_error_kind IN (
        'TIMEOUT',
        'DNS_OR_CONNECTION',
        'HTTP_4XX',
        'HTTP_5XX',
        'WAF_OR_GEO_BLOCK',
        'INVALID_XML_OR_XXE',
        'PAYLOAD_TOO_LARGE',
        'SSRF_BLOCKED'
    ))
) STRICT;

-- 3. Eventos ("Una historia, varias versiones")
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY CHECK (length(trim(event_id)) > 0),
    slug TEXT NOT NULL UNIQUE CHECK (length(trim(slug)) > 0),
    canonical_headline TEXT NOT NULL CHECK (length(trim(canonical_headline)) > 0 AND length(canonical_headline) <= 300),
    topic_category TEXT NOT NULL CHECK (topic_category IN (
        'POLITICA_GOBIERNO',
        'ECONOMIA_EMPLEO',
        'SEGURIDAD_JUSTICIA',
        'CIENCIA_TECNOLOGIA',
        'ARTE_CULTURA_HUMANIDADES',
        'SALUD_BIENESTAR',
        'MEDIO_AMBIENTE_CLIMA',
        'INFRAESTRUCTURA_TRANSPORTE',
        'EDUCACION',
        'DERECHOS_HUMANOS_SOCIEDAD'
    )),
    geographic_scope TEXT NOT NULL CHECK (geographic_scope IN (
        'YUCATAN',
        'REGIONAL_PENINSULA',
        'NATIONAL',
        'GLOBAL'
    )),
    first_seen_at_utc TEXT NOT NULL,
    last_updated_at_utc TEXT NOT NULL,
    is_sensitive INTEGER NOT NULL DEFAULT 0 CHECK (is_sensitive IN (0, 1)),
    sensitivity_reasons_json TEXT NOT NULL DEFAULT '[]',
    verification_state TEXT NOT NULL DEFAULT 'SINGLE_SOURCE' CHECK (verification_state IN (
        'CONFIRMED',
        'UNCONFIRMED_REPORT',
        'SINGLE_SOURCE',
        'DISPUTED',
        'RETRACTED'
    )),
    review_status TEXT NOT NULL DEFAULT 'PENDING_HUMAN' CHECK (review_status IN (
        'PENDING_HUMAN',
        'AUTO_PUBLISHED',
        'HUMAN_APPROVED',
        'REJECTED'
    )),
    review_priority INTEGER NOT NULL DEFAULT 3 CHECK (review_priority BETWEEN 1 AND 4),
    independent_origins_count INTEGER NOT NULL DEFAULT 1 CHECK (independent_origins_count >= 0),
    media_count INTEGER NOT NULL DEFAULT 1 CHECK (media_count >= 0),
    clustering_confidence REAL NOT NULL DEFAULT 1.0 CHECK (clustering_confidence BETWEEN 0.0 AND 1.0),
    tfidf_terms_json TEXT NOT NULL DEFAULT '{}',
    key_entities_json TEXT NOT NULL DEFAULT '[]',
    rule_engine_version TEXT NOT NULL DEFAULT 'v1.0.0',
    -- INVARIANTE DE SEGURIDAD EDITORIAL (ADR-003 / ADR-005):
    -- Prohibido publicar automáticamente (AUTO_PUBLISHED) si el evento es sensible
    -- o si está en estado DISPUTED / RETRACTED.
    CHECK (NOT (is_sensitive = 1 AND review_status = 'AUTO_PUBLISHED')),
    CHECK (NOT (verification_state IN ('DISPUTED', 'RETRACTED') AND review_status = 'AUTO_PUBLISHED'))
) STRICT;

-- 4. Artículos individuales ingeridos desde RSS/Atom
CREATE TABLE IF NOT EXISTS articles (
    article_id TEXT PRIMARY KEY CHECK (length(trim(article_id)) > 0),
    source_id TEXT NOT NULL REFERENCES media_sources(source_id) ON UPDATE CASCADE ON DELETE RESTRICT,
    event_id TEXT REFERENCES events(event_id) ON UPDATE CASCADE ON DELETE SET NULL,
    canonical_url TEXT NOT NULL CHECK (canonical_url LIKE 'https://%' OR canonical_url LIKE 'http://%'),
    url_hash TEXT NOT NULL UNIQUE CHECK (length(url_hash) = 64),
    title_plain TEXT NOT NULL CHECK (length(trim(title_plain)) > 0 AND length(title_plain) <= 350),
    -- INVARIANTE DE DERECHOS DE AUTOR (S4 / ADR-005):
    -- Extracto literal truncado estrictamente a <= 240 caracteres en texto plano.
    excerpt_plain TEXT NOT NULL DEFAULT '' CHECK (length(excerpt_plain) <= 240),
    author_raw TEXT NOT NULL DEFAULT '',
    published_at_utc TEXT NOT NULL,
    ingested_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    simhash_hex TEXT NOT NULL CHECK (length(simhash_hex) = 16),
    duplicate_of_article_id TEXT REFERENCES articles(article_id) ON UPDATE CASCADE ON DELETE SET NULL,
    similarity_to_event REAL NOT NULL DEFAULT 1.0 CHECK (similarity_to_event BETWEEN 0.0 AND 1.0),
    CHECK (duplicate_of_article_id IS NULL OR duplicate_of_article_id != article_id)
) STRICT;

-- 5. Orígenes independientes detectados por evento (Contar orígenes != contar medios)
CREATE TABLE IF NOT EXISTS independent_origins (
    origin_id TEXT PRIMARY KEY CHECK (length(trim(origin_id)) > 0),
    event_id TEXT NOT NULL REFERENCES events(event_id) ON UPDATE CASCADE ON DELETE CASCADE,
    origin_type TEXT NOT NULL CHECK (origin_type IN (
        'OFFICIAL_BULLETIN',
        'WIRE_AGENCY',
        'ORIGINAL_MEDIA_REPORTING',
        'PUBLIC_DOCUMENT_OR_DATA',
        'DIRECT_ACTOR_STATEMENT',
        'ACADEMIC_PUBLICATION'
    )),
    actor_normalized_key TEXT NOT NULL CHECK (length(trim(actor_normalized_key)) > 0),
    actor_display_name TEXT NOT NULL CHECK (length(trim(actor_display_name)) > 0),
    detection_method TEXT NOT NULL CHECK (detection_method IN (
        'RULE_REGEX_ATTRIBUTION',
        'OWNERSHIP_GROUP_COLLAPSE',
        'WIRE_SIGNATURE',
        'HUMAN_VERIFIED'
    )),
    created_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (event_id, origin_type, actor_normalized_key)
) STRICT;

-- 6. Trazabilidad de atribuciones por artículo (Evidencia de por qué se agrupó el origen)
CREATE TABLE IF NOT EXISTS article_attributions (
    attribution_id TEXT PRIMARY KEY CHECK (length(trim(attribution_id)) > 0),
    article_id TEXT NOT NULL REFERENCES articles(article_id) ON UPDATE CASCADE ON DELETE CASCADE,
    origin_id TEXT NOT NULL REFERENCES independent_origins(origin_id) ON UPDATE CASCADE ON DELETE CASCADE,
    extracted_evidence_snippet TEXT NOT NULL CHECK (length(extracted_evidence_snippet) <= 200),
    rule_id_matched TEXT NOT NULL,
    UNIQUE (article_id, origin_id)
) STRICT;

-- 7. Historial inmutable de estados de verificación (Trazabilidad pública)
CREATE TABLE IF NOT EXISTS verification_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES events(event_id) ON UPDATE CASCADE ON DELETE CASCADE,
    previous_state TEXT CHECK (previous_state IS NULL OR previous_state IN (
        'CONFIRMED',
        'UNCONFIRMED_REPORT',
        'SINGLE_SOURCE',
        'DISPUTED',
        'RETRACTED'
    )),
    new_state TEXT NOT NULL CHECK (new_state IN (
        'CONFIRMED',
        'UNCONFIRMED_REPORT',
        'SINGLE_SOURCE',
        'DISPUTED',
        'RETRACTED'
    )),
    changed_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    changed_by TEXT NOT NULL CHECK (changed_by IN ('PIPELINE_RULE', 'EDITOR_HUMAN')),
    rule_engine_version TEXT NOT NULL,
    reason_components_json TEXT NOT NULL
) STRICT;

-- 8. Fe de erratas y correcciones editoriales públicas
CREATE TABLE IF NOT EXISTS editorial_corrections (
    correction_id TEXT PRIMARY KEY CHECK (length(trim(correction_id)) > 0),
    event_id TEXT NOT NULL REFERENCES events(event_id) ON UPDATE CASCADE ON DELETE CASCADE,
    issued_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    correction_type TEXT NOT NULL CHECK (correction_type IN (
        'CLUSTERING_SPLIT_OR_MERGE',
        'SOURCE_RETRACTION',
        'ORIGIN_COUNT_ADJUSTMENT',
        'FACTUAL_CLARIFICATION'
    )),
    public_explanation TEXT NOT NULL CHECK (length(trim(public_explanation)) > 0)
) STRICT;

-- 9. Observabilidad sin servidor: Ejecuciones del pipeline y salud de fuentes
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY CHECK (length(trim(run_id)) > 0),
    started_at_utc TEXT NOT NULL,
    finished_at_utc TEXT,
    run_mode TEXT NOT NULL CHECK (run_mode IN ('CI_HOURLY', 'LOCAL_CLI', 'PUBLISH_EXPORT')),
    sources_checked INTEGER NOT NULL DEFAULT 0,
    sources_failed INTEGER NOT NULL DEFAULT 0,
    articles_ingested INTEGER NOT NULL DEFAULT 0,
    events_created INTEGER NOT NULL DEFAULT 0,
    events_updated INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'SUCCESS', 'PARTIAL_FAILURE', 'ABORTED'))
) STRICT;

CREATE TABLE IF NOT EXISTS source_health_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON UPDATE CASCADE ON DELETE CASCADE,
    source_id TEXT NOT NULL REFERENCES media_sources(source_id) ON UPDATE CASCADE ON DELETE CASCADE,
    checked_at_utc TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    http_status INTEGER,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    error_kind TEXT,
    items_fetched INTEGER NOT NULL DEFAULT 0
) STRICT;

-- =============================================================================
-- PREVISIÓN FASE G (DIFERIDA): Grafo de Confianza para Ciencia, Arte y Humanidades
-- (OpenAlex, Crossref, ORCID, Retraction Watch, Wikidata)
-- =============================================================================

CREATE TABLE IF NOT EXISTS knowledge_entities (
    entity_id TEXT PRIMARY KEY CHECK (length(trim(entity_id)) > 0),
    canonical_name TEXT NOT NULL,
    entity_type TEXT NOT NULL CHECK (entity_type IN (
        'RESEARCHER',
        'INSTITUTION',
        'CULTURAL_AGENT',
        'HERITAGE_SITE'
    )),
    orcid_id TEXT UNIQUE,
    wikidata_qid TEXT UNIQUE,
    ror_id TEXT UNIQUE,
    metadata_json TEXT NOT NULL DEFAULT '{}'
) STRICT;

CREATE TABLE IF NOT EXISTS scholarly_works (
    work_id TEXT PRIMARY KEY CHECK (length(trim(work_id)) > 0),
    doi TEXT UNIQUE,
    openalex_id TEXT UNIQUE,
    title TEXT NOT NULL,
    publication_date TEXT,
    peer_review_status TEXT NOT NULL CHECK (peer_review_status IN (
        'PREPRINT',
        'PEER_REVIEWED',
        'BOOK_OR_MONOGRAPH',
        'RETRACTED',
        'EXPRESSION_OF_CONCERN'
    )),
    retraction_watch_id TEXT,
    journal_or_publisher TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
) STRICT;

CREATE TABLE IF NOT EXISTS work_authorships (
    work_id TEXT NOT NULL REFERENCES scholarly_works(work_id) ON UPDATE CASCADE ON DELETE CASCADE,
    author_entity_id TEXT NOT NULL REFERENCES knowledge_entities(entity_id) ON UPDATE CASCADE ON DELETE CASCADE,
    institution_entity_id TEXT REFERENCES knowledge_entities(entity_id) ON UPDATE CASCADE ON DELETE SET NULL,
    declared_conflicts_of_interest TEXT NOT NULL DEFAULT '',
    funding_sources_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (work_id, author_entity_id)
) STRICT;

CREATE TABLE IF NOT EXISTS claim_evidence_links (
    link_id TEXT PRIMARY KEY CHECK (length(trim(link_id)) > 0),
    event_id TEXT NOT NULL REFERENCES events(event_id) ON UPDATE CASCADE ON DELETE CASCADE,
    work_id TEXT NOT NULL REFERENCES scholarly_works(work_id) ON UPDATE CASCADE ON DELETE CASCADE,
    alignment_type TEXT NOT NULL CHECK (alignment_type IN (
        'PRIMARY_STUDY_CITED',
        'SUPPORTED_BY_CONSENSUS',
        'EXAGGERATED_ EXTRAPOLATION',
        'CONTRADICTED_BY_EVIDENCE',
        'CITES_RETRACTED_WORK'
    )),
    editorial_note TEXT NOT NULL DEFAULT '',
    UNIQUE (event_id, work_id)
) STRICT;

-- =============================================================================
-- ÍNDICES DE RENDIMIENTO PARA VENTANA TEMPORAL (72h) Y COLA DE REVISIÓN
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_articles_published_at ON articles(published_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_articles_event_id ON articles(event_id);
CREATE INDEX IF NOT EXISTS idx_articles_simhash ON articles(simhash_hex);
CREATE INDEX IF NOT EXISTS idx_events_window ON events(last_updated_at_utc DESC, geographic_scope);
CREATE INDEX IF NOT EXISTS idx_events_review_queue ON events(review_status, review_priority ASC, last_updated_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_origins_event ON independent_origins(event_id);
