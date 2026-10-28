# Registro de Decisiones de Arquitectura (ADR) — Fase A

## ADR-001: Lenguaje del pipeline (Python 3.12) + Generador Estático (Hugo)
* **Contexto:** Un único operador con conocimientos de Linux/Docker necesita implementar ingesta RSS, normalización de texto en español, TF-IDF/BM25, MinHash/SimHash, extracción de entidades/atribuciones por reglas y generación de un sitio estático rápido.
* **Opciones:**
  1. Todo en Go.
  2. Todo en Python (Jinja2/Pelican).
  3. Híbrido: Pipeline en Python + Sitio generado con Hugo.
* **Decisión:** Opción 3 (Python 3.12+ con tipado estricto y estructuras eficientes + Hugo).
* **Consecuencias:** Python provee el ecosistema determinista más maduro en español (`scikit-learn`, `datasketch`, `defusedxml`, `feedparser`, `urllib3`). Hugo compila miles de páginas en milisegundos desde un único binario estático sin depender del ecosistema Node.js/npm.

## ADR-002: Persistencia de estado en arquitectura efímera ($0 USD, sin tarjeta)
* **Contexto:** Los runners de GitHub Actions son efímeros y el operador requiere costo $0 USD sin registrar tarjeta de crédito en la etapa inicial.
* **Opciones:**
  1. SQLite local durante el job, persistido como archivo `.sqlite` en una rama huérfana (`data-state`) o repositorio privado auxiliar + volcados JSON/Markdown para eventos publicados.
  2. Cloudflare D1 / R2 (requiere método de pago en cuenta para ciertos recursos).
  3. Archivos planos sueltos en Git.
* **Decisión:** Opción 1 (SQLite en modo WAL durante ejecución + persistencia en Git con `commit --amend` horario y snapshots semanales).
* **Consecuencias:** Cero costo y cero dependencia de tarjetas bancarias. Migrar en el futuro a un VPS con Docker consiste únicamente en montar el archivo `state.sqlite` en un volumen persistente local y cambiar el cron de GitHub Actions a un `systemd.timer`.

## ADR-003: Política de publicación de doble vía y revisión desde CLI en Linux
* **Contexto:** El operador dispone de 30–90 minutos diarios desde su PC con Linux mediante línea de comandos y requiere que el sitio no quede congelado si un día no puede revisar, sin violar la restricción de revisión humana obligatoria en temas sensibles.
* **Opciones:**
  1. Bloqueo total (nada se publica sin revisión humana).
  2. Doble vía (*Dual-Track Gate*) con CLI local (`./bin/revisar`).
* **Decisión:** Opción 2 (Doble vía + CLI local en Linux).
  * **Vía Automática Acotada (1 a 4 cortes al día):** Solo eventos con `is_sensitive == False`, sin disputas ni retractaciones, y con similitud fuera de la zona gris se exportan a Hugo con el sello visible: `"Agrupación automática — Pendiente de validación editorial"`.
  * **Vía Bloqueada (Revisión Humana Obligatoria):** Todo evento con `is_sensitive == True` (crimen, imputaciones a personas, menores, salud crítica, elecciones), o en estado `Disputado`/`Retractado`, o en zona gris de similitud, queda retenido en la cola. El operador ejecuta en su PC `./bin/revisar` (interfaz TUI con `textual`/`rich` en Python que descarga la cola, permite aprobar/fusionar/separar/reclasificar con atajos de teclado, firma el commit con SSH/GPG y hace `git push`).
* **Consecuencias:** El sitio se mantiene vivo (máximo 4 builds/día = ~120 builds/mes, muy por debajo del límite de 500 builds/mes de Cloudflare Pages), se anula la exposición de paneles web y se protege legalmente al operador ante temas delicados.

## ADR-004: Protección de la cola sensible en Repositorio Público (Cifrado de Estado / Repo Privado de Estado)
* **Contexto:** El operador eligió repositorio **público** para contar con minutos ilimitados de GitHub Actions y transparencia metodológica. Sin embargo, guardar notas sensibles no verificadas (ej. acusaciones penales o nombres de particulares) en texto plano dentro de una rama pública de estado equivale a publicarlas antes de la revisión humana.
* **Opciones:**
  1. Guardar `state.sqlite` en texto plano en el repo público (Riesgo alto: cualquiera puede clonar la rama `data-state`).
  2. Cifrar el archivo `state.sqlite` en reposo con `age` (Chacha20-Poly1305) o `cryptography.fernet` usando un secreto en GitHub Actions (`STATE_ENCRYPTION_KEY`) antes de hacer commit en la rama `data-state`, o empujar `state.sqlite` a un segundo repositorio **privado** (`news-mayab-state`) vía Deploy Key SSH.
* **Decisión:** Opción 2 (Cifrado en reposo con `age` / clave simétrica en `data-state`, o repo privado sólo para el binario `.sqlite`). En el repositorio público solo existen en texto plano el código fuente, los diccionarios/reglas, la metodología y los eventos ya aprobados o aptos para publicación en `content/`.
* **Consecuencias:** Se conserva el 100% de gratuidad y minutos ilimitados de ejecución en el repositorio público sin filtrar datos no revisados ni exponer cabeceras HTTP internas.

## ADR-005: Invariantes de seguridad y derechos de autor en el motor SQLite (Fase B)
* **Contexto:** Un error lógico en el pipeline de Python nunca debe permitir que: (a) se publique automáticamente una nota sensible sin revisión humana, (b) se almacene un extracto mayor a 240 caracteres, o (c) se inyecte un esquema de URL peligroso (`javascript:`, `data:`).
* **Opciones:**
  1. Validar únicamente en el código Python.
  2. Imponer invariantes mediante restricciones `CHECK`, `FOREIGN KEY` y `TRIGGER` directamente en el motor SQLite 3.54+, además de la validación en Python, usando migraciones SQL puras sin ORM.
* **Decisión:** Opción 2 (Defensa en profundidad en el DDL de SQLite + migrador SQL nativo en Python).
* **Consecuencias:** Aunque un módulo de Python tuviese un bug, SQLite abortará la transacción (`sqlite3.IntegrityError`) si se intenta insertar `is_sensitive = 1` con `review_status = 'AUTO_PUBLISHED'`, un `excerpt_plain` de >240 caracteres o una URL que no empiece por `http://` o `https://`.

## ADR-006: Ingesta RSS/Atom sobre `defusedxml` puro y sanitización sin dependencias de terceros (Fase C)
* **Contexto:** El módulo de ingesta procesa XML no confiable proveniente de internet. Librerías históricas como `feedparser` arrastran dependencias adicionales y lógica permisiva, mientras que un cliente HTTP estándar sigue redirecciones sin validar si el destino resuelve a una IP privada (SSRF).
* **Opciones:**
  1. Usar `requests` + `feedparser` + `beautifulsoup4`.
  2. Implementar el cliente HTTP sobre `urllib.request` con validación DNS/IP anti-SSRF en cada salto, parseo RSS 2.0 / Atom 1.0 directamente con `defusedxml.ElementTree` (`forbid_dtd=True`, `forbid_entities=True`, `forbid_external=True`) y sanitizador HTML a texto plano con `html.parser` de la librería estándar.
* **Decisión:** Opción 2.
* **Consecuencias:** Cero dependencias externas nuevas que mantener o auditar para la Fase C; bloqueo garantizado de XXE, bombas XML (`Billion Laughs`), SSRF hacia `127.0.0.0/8` o `169.254.169.254`, y detección explícita de bloqueos WAF (`WAF_OR_GEO_BLOCK`) en portales `.gob.mx`.

## ADR-007: Agrupación Determinista Híbrida (TF-IDF + Bigramas + Entidades Ancla) y Colapso de Eco (Fase D)
* **Contexto:** Agrupar notas del mismo suceso regional en ventana de 72 horas sin LLMs y distinguir entre "10 medios repitiendo un boletín" (`independent_origins_count = 1`) y "confirmación cruzada real" (`independent_origins_count >= 2`).
* **Opciones:**
  1. Embeddings neuronales pesados (`sentence-transformers` / `scikit-learn`).
  2. Similitud coseno TF-IDF sublineal sobre unigramas y bigramas ponderados en Python puro + intersección de entidades ancla (municipios de Yucatán, siglas institucionales, números de ruta/kilómetro y nombres propios) + motor declarativo `config/rules.yaml` para atribución de orígenes, sensibilidad y disputa.
* **Decisión:** Opción 2.
* **Consecuencias:** Ejecución en milisegundos sin arrastrar binarios pesados en CI; penalización explícita cuando dos notas mencionan municipios distintos de Yucatán (evitando mezclar obras o incidentes de Mérida con Valladolid); colapso automático en un único origen cuando múltiples medios citan el mismo actor oficial/agencia o pertenecen al mismo `ownership_group_id`; y retención automática en `PENDING_HUMAN` para eventos sensibles, disputados, retractados o en zona gris de similitud (`[0.24, 0.42)`).

## ADR-008: Revisión Editorial desde CLI sin Dependencias TUI Pesadas y Exportación Segura a Hugo (Fase E)
* **Contexto:** El operador necesita revisar la cola `PENDING_HUMAN` (aprobar, rechazar, fusionar eventos duplicados, separar artículos mal agrupados y emitir fe de erratas) y exportar a Hugo sin arriesgar la publicación de notas sensibles ni depender de paneles web expuestos.
* **Opciones:**
  1. Panel web administrativo con login.
  2. CLI nativo en Python (`./bin/revisar`) con subcomandos deterministas y modo interactivo por teclado sobre `state.sqlite`, acoplado a un exportador estático (`./bin/export_hugo`) que aplica una segunda validación de invariantes antes de escribir Front Matter YAML en `site/content/eventos/` y purga automáticamente archivos `.md` obsoletos.
* **Decisión:** Opción 2.
* **Consecuencias:** Superficie de ataque web igual a cero; todas las acciones humanas registran auditoría en `verification_history` (`changed_by = 'EDITOR_HUMAN'`) y `editorial_corrections`; y si un evento previamente auto-publicado recibe una nota con desmentido (`DISPUTED`), el exportador retira su archivo `.md` hasta que el humano lo revise.

## ADR-009: Degradación Elegante en Modo Desatendido (>48h), Aliases de Fusión y Retención SQLite (Fase F)
* **Contexto:** Si el único operador se enferma o se ausenta varios días, el sistema no debe congelarse por completo ni tampoco auto-publicar agrupaciones dudosas; además, fusionar eventos ya publicados no debe romper enlaces previos (404), y `state.sqlite` debe mantenerse compacto en Git.
* **Opciones:**
  1. Apagar todo el cron si el humano no entra en 48 horas.
  2. Activar automáticamente el modo de continuidad estricto (`is_unattended_degraded_mode`): mantener bloqueado todo lo sensible/disputado y elevar el umbral de confianza de `AUTO_PUBLISHED` a `0.88` (reteniendo el resto en `PENDING_HUMAN`), exportar `site/data/system_status.json`, conservar los slugs absorbidos en `events.aliases_json` para generar redirecciones `aliases` en Hugo, y purgar logs operativos >30 días.
* **Decisión:** Opción 2.
* **Consecuencias:** El medio sobrevive ausencias de una semana publicando únicamente notas no sensibles de altísima certeza; cero enlaces rotos tras un `merge`; y el archivo `state.sqlite.age` en la rama `data-state` se mantiene de pocos megabytes.

## ADR-010: Grafo de Confianza Transparente con CTEs Recursivas en SQLite y Auditoría HHI de Diversidad (Fase G)
* **Contexto:** Cubrir ciencia, arte y humanidades requiere verificar estudios (DOI/OpenAlex), autorías (ORCID), adscripciones (ROR/Wikidata) y retractaciones (Retraction Watch) sin usar un puntaje único opaco ni pagar una base de datos de grafos externa; asimismo, se requiere medir si un solo grupo empresarial monopoliza la agenda temática.
* **Opciones:**
  1. Base de datos de grafos externa (Neo4j) + puntaje compuesto único (0–100).
  2. Tablas relacionales estrictas en SQLite (`knowledge_entities`, `scholarly_works`, `work_authorships`, `claim_evidence_links`) consultadas con CTEs recursivas (`WITH RECURSIVE` acotado a profundidad $\le 3$), exposición pública de cada componente verificable, bloqueo automático cuando se cita una obra retractada (`CITES_RETRACTED_WORK`) y cálculo del índice Herfindahl-Hirschman (HHI) y equidad de Pielou por categoría temática.
* **Decisión:** Opción 2.
* **Consecuencias:** Transparencia metodológica total ante el lector; cero costo de infraestructura adicional; alerta inmediata si se divulga un paper retractado; y métricas cuantitativas para detectar sesgo de concentración de medios por tema.






