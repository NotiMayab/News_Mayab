# News Mayab — Agregador Determinista de Noticias con Trazabilidad de Orígenes (Yucatán, México)

Medio digital gratuito para Yucatán con cobertura local, peninsular, nacional y mundial. Diseñado para ser operado por **una sola persona** con costo de infraestructura **$0 USD/mes** (Python 3.12+ determinista, SQLite en modo `WAL` cifrado en reposo con `age` y sitio estático generado con Hugo).

## Diferenciadores Técnicos y Editoriales

1. **Contar orígenes independientes, no eco de medios:** Si 10 portales publican el mismo boletín oficial (`OFFICIAL_BULLETIN`), reproducen un cable de agencia (`WIRE_AGENCY`) o pertenecen al mismo consorcio empresarial (`ownership_group_id`), el motor colapsa automáticamente el conteo a **1 solo origen independiente**.
2. **Invariantes de Seguridad y Derechos de Autor en el Motor SQLite (`STRICT`):**
   - Prohibición física (`CHECK` en SQLite + validación en Python) de auto-publicar eventos sensibles (`is_sensitive = 1`: crimen, imputaciones a personas, menores, salud crítica, elecciones) o en estado `DISPUTED` / `RETRACTED`.
   - Extractos literales cortos truncados estrictamente a **$\le 240$ caracteres** en texto plano sin etiquetas HTML (cero XSS) y enlace directo al medio autor original.
3. **Ingesta endurecida anti-SSRF y anti-XXE:** Resolución y bloqueo de rangos IP privados/loopback/link-local/cloud metadata en cada salto de redirección HTTP, límite duro de 2 MiB por respuesta y parseo XML con `defusedxml` (`forbid_dtd=True`, `forbid_entities=True`, `forbid_external=True`).
4. **Grafo de Confianza para Ciencia, Arte y Humanidades:** Vinculación de identificadores abiertos (`DOI`, `OpenAlex`, `ORCID`, `ROR`, `Wikidata`, `Retraction Watch`) con exposición transparente de componentes (sin puntaje único opaco) y bloqueo automático si una nota cita una obra retractada (`CITES_RETRACTED_WORK`).

## Estructura del Repositorio

```text
├── bin/
│   ├── check_feeds      # Diagnóstico de conectividad RSS/Atom y detección WAF
│   ├── run_pipeline     # Ciclo completo: Ingesta -> Agrupación 72h -> Anti-Eco -> Estado
│   ├── revisar          # CLI editorial (queue, show, approve, reject, merge, split, correct, interactive)
│   ├── export_hugo      # Exportador seguro de eventos publicables a Hugo + compilación estática
│   ├── ops_status       # Tablero operativo, continuidad (>48h), auditoría HHI y mantenimiento SQLite
│   └── sync_state       # Sincronización atómica (pull/push) de state.sqlite.age en rama data-state
├── config/
│   ├── sources.yaml     # Catálogo cerrado de fuentes RSS/Atom y grupos propietarios (ownership_groups)
│   └── rules.yaml       # Reglas auditables de sensibilidad, atribución de orígenes, disputa y municipios
├── docs/
│   ├── decisiones.md    # Registro de Decisiones de Arquitectura (ADR-001 a ADR-010)
│   └── runbooks.md      # Rutina diaria de 15-30 min, plan de continuidad y respuesta a incidentes
├── schema/
│   ├── 001_initial.sql  # Esquema SQLite STRICT con invariantes CHECK/FK e índices
│   └── 002_event_aliases.sql
├── site/                # Sitio estático Hugo (layouts, cabeceras CSP/HSTS, metodología pública, PWA)
├── src/mayab_news/
│   ├── db/              # Migraciones SQL verificadas por SHA-256 y cifrado en reposo con `age`
│   ├── ingest/          # Cliente HTTP anti-SSRF y parser XML seguro con `defusedxml`
│   ├── nlp/             # Sanitización HTML->texto, SimHash/Jaccard, TF-IDF 72h, orígenes y sensibilidad
│   ├── editorial/       # Cola de revisión humana, merge/split con redirecciones y fe de erratas
│   ├── export/          # Generador de Front Matter YAML para Hugo y limpieza de obsoletos
│   ├── ops/             # Observabilidad, modo desatendido (>48h) y auditoría HHI de diversidad
│   └── trustgraph/      # Grafo de confianza académico/cultural con CTEs recursivas
└── tests/               # Suite automatizada de pruebas unitarias y de integración
```

## Ejecución Rápida de Pruebas y Comandos Locales

```bash
# Ejecutar toda la suite de pruebas (sin requerir servicios externos)
PYTHONPATH=src python3 -m unittest discover -s tests -v

# Inicializar/actualizar base de datos y correr un ciclo local de ingesta
./bin/run_pipeline --db state.sqlite --mode LOCAL_CLI

# Revisar estado operativo y cola de revisión humana
./bin/ops_status --db state.sqlite
./bin/revisar --db state.sqlite queue

# Exportar eventos verificados/auto-publicados y compilar el sitio Hugo
./bin/export_hugo --db state.sqlite --build
```
