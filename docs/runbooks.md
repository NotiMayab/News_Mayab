# Runbooks Operativos y Plan de Continuidad — News_Mayab (Fase F)

## 1. Rutina Diaria del Operador Único (15 a 30 minutos en PC Linux)

Ejecuta esta secuencia una vez al día desde tu estación de trabajo para procesar las fuentes locales que bloquean IPs de datacenter (`local_isp_only`) y despachar la cola de revisión humana (`PENDING_HUMAN`):

```bash
# 1. Descargar y descifrar el estado más reciente desde la rama data-state
./bin/sync_state pull

# 2. Correr ingesta en modo LOCAL_CLI (incluye portales .gob.mx / académicos de Yucatán)
./bin/run_pipeline --db state.sqlite --mode LOCAL_CLI

# 3. Ver salud operativa y revisar la cola PENDING_HUMAN (prioridad P1 y P2)
./bin/ops_status --db state.sqlite
./bin/revisar --db state.sqlite interactive

# 4. Exportar eventos aprobados a Hugo, compilar y hacer git commit + push a origin/main
./bin/export_hugo --db state.sqlite --build --push

# 5. Volver a cifrar state.sqlite y subir a la rama huérfana data-state
./bin/sync_state push
```

---

## 2. Plan de Continuidad: ¿Qué pasa si te enfermas o ausentas una semana?

El sistema está diseñado para **degradarse con elegancia sin intervención manual**:

1. **Detección automática (>48 horas sin acción `EDITOR_HUMAN`):**
   - `enforce_unattended_graceful_degradation` en [`observability.py`](file:///home/gabriel/Escritorio/vscode/News_Mayab/src/mayab_news/ops/observability.py#L194-L246) detecta automáticamente cuando existen eventos pendientes con más de 48 horas y no ha habido actividad humana en las últimas 48 horas (o cuando defines `OPERATIONAL_MODE=SAFE_AUTOPILOT_STRICT`).
2. **Comportamiento en modo desatendido (`is_unattended_degraded_mode = true`):**
   - **Jamás** se publican eventos sensibles (`is_sensitive = 1`), ni disputados (`DISPUTED`), ni retractados (`RETRACTED`).
   - El umbral de confianza para `AUTO_PUBLISHED` se endurece automáticamente de `0.75` a **`0.88`** (`min_confidence_strict`): cualquier agrupación con confianza menor queda retenida en `PENDING_HUMAN`.
   - Se actualiza `site/data/system_status.json` con la marca de tiempo de la última revisión humana para transparencia ante los lectores.

---

## 3. Runbooks de Incidentes y Mantenimiento

### 3.1 Fuente Caída o Bloqueada por WAF (`WAF_OR_GEO_BLOCK`)
1. Ejecuta `./bin/check_feeds` desde tu PC para distinguir si el bloqueo ocurre únicamente en la IP de GitHub Actions o también en tu ISP residencial.
2. Si funciona en tu PC pero falla en GitHub Actions con `WAF_OR_GEO_BLOCK`:
   - Edita [`config/sources.yaml`](file:///home/gabriel/Escritorio/vscode/News_Mayab/config/sources.yaml) y cambia `fetch_mode: "local_isp_only"` para esa fuente.
3. Si el feed cambió de URL o está permanentemente caído (`consecutive_failures >= 10`):
   - Actualiza `rss_url` o cambia `is_active: false` en [`config/sources.yaml`](file:///home/gabriel/Escritorio/vscode/News_Mayab/config/sources.yaml).

### 3.2 Fe de Erratas o Retractación Urgente
Si un medio se retracta o detectas una agrupación errónea ya publicada:
```bash
# Emitir corrección pública y pasar el evento a RETRACTED
./bin/revisar --db state.sqlite correct <EVENT_ID> \
  --type SOURCE_RETRACTION \
  --state RETRACTED \
  --text "El medio original publicó una fe de erratas rectificando el dato inicial."

# O si deseas retirar inmediatamente el evento del sitio público:
./bin/revisar --db state.sqlite reject <EVENT_ID> --reason "Información descartada"
./bin/export_hugo --db state.sqlite --build
```

### 3.3 Rotación de la Clave de Cifrado (`AGE_SECRET_KEY`)
1. Genera una nueva clave local: `age-keygen -o ~/.config/news-mayab/state_new.key`.
2. Descifra `state.sqlite.age` con la clave anterior hacia `state.sqlite`.
3. Cifra `state.sqlite` con la nueva clave pública (`age1...`).
4. Actualiza el secreto `AGE_SECRET_KEY` en GitHub Settings -> Secrets and variables -> Actions y reemplaza `~/.config/news-mayab/state.key`.

---

## 4. Checklist de Traspaso Operativo (Bus Factor = 1 -> 2)

- [ ] Acceso de administrador al repositorio GitHub y a Cloudflare Pages (protegidos con 2FA TOTP/WebAuthn).
- [ ] Copia fuera de banda de `~/.config/news-mayab/state.key` entregada en gestor de contraseñas cifrado (ej. KeePassXC / Bitwarden).
- [ ] Verificación en la máquina del nuevo operador de: `python3 >= 3.12`, binario `age`, binario `hugo` y ejecución exitosa de `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
- [ ] Recorrido guiado por [`docs/decisiones.md`](file:///home/gabriel/Escritorio/vscode/News_Mayab/docs/decisiones.md), [`config/sources.yaml`](file:///home/gabriel/Escritorio/vscode/News_Mayab/config/sources.yaml) y [`config/rules.yaml`](file:///home/gabriel/Escritorio/vscode/News_Mayab/config/rules.yaml).
