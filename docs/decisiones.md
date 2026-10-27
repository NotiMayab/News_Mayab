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

