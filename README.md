### Molduplastic IA

Aplicación de frappe para Molduplastic para integrar funcionalidades relacionadas con IA.

### Installation

Requiere la app hermana `fc_molduplastic_frappe_app` (define `Pieza`, `Recoleccion`,
`Taller` y los catálogos que consulta el asistente). Está declarada en
`required_apps`, así que tiene que estar instalada primero.

```bash
cd $PATH_TO_BENCH
bench get-app $URL_DE_LA_APP_HERMANA --branch excel-files
bench get-app $URL_DE_ESTA_APP --branch develop
bench --site <site> install-app fc_molduplastic_frappe_app
bench --site <site> install-app fc_molduplastic_frappe_ia
```

Después de instalar o actualizar hay que crear los índices de `tabPieza` (ver
"Base de datos del asistente") y capturar la API key en `Molduplastic IA Settings`.

### Actualizar otra instalación que ya tiene el asistente (rama `develop`)

Runbook para un servidor que **ya tiene una versión anterior** de la app. El orden
importa: migrar, índices, configuración, caché y reinicio.

#### 0. Antes de tocar nada

- Respaldo de la base y del sitio (desde el bench):
  ```bash
  cd /home/frappe/frappe-bench
  bench --site <site> backup --with-files
  ```
- Copia de `sites/<site>/site_config.json`, en especial **`encryption_key`**. Si
  más adelante restauras un dump en un site con otra clave, los campos `Password`
  (API key de Zen, `IA Proveedor.api_key`, `sync_source_password`) no se descifran
  y el asistente responde "sin API key".
- Confirma que la **app hermana** tiene la base de fecha y el universo de BI:
  ```bash
  cd /home/frappe/frappe-bench
  grep -c '"fecha_hora_carga"' apps/fc_molduplastic_frappe_app/fc_molduplastic_frappe_app/doctype/pieza/pieza.json
  grep -c '"rutas_validas"'   apps/fc_molduplastic_frappe_app/fc_molduplastic_frappe_app/doctype/pieza/pieza.json
  ```
  Las dos deben dar 1 o más. En el repo de la app hermana eso vive en la rama
  **`excel-files`** (commit `ad9a685` o posterior); `develop` **no** los tiene y
  sin ellos cualquier consulta de Pieza falla con `Unknown column`.
- Confirma las dependencias de la app hermana **en el env de ese servidor**:
  ```bash
  cd /home/frappe/frappe-bench
  ./env/bin/python -c "import thefuzz, unidecode; print('deps ok')" \
    || ./env/bin/pip install -r apps/fc_molduplastic_frappe_app/requirements.txt
  ```
  Si faltan, `remove_orphan_doctypes` (el último paso de `bench migrate`) borra el
  registro de `Pieza` y `Carga Archivo` porque su controlador no importa. Desde
  esta versión `bench migrate` **aborta antes** con un mensaje claro, pero la
  solución es instalar las dependencias.
- Espacio y motor: deja **≥ 4 GB libres** (los índices de `tabPieza` quedan en
  ~3.3 GB), MariaDB 10.11 y `innodb_buffer_pool_size=1G`.

#### 1. Bajar el código

```bash
cd /home/frappe/frappe-bench/apps/fc_molduplastic_frappe_ia
git fetch origin
git checkout develop
git pull --ff-only origin develop
```

Si la app hermana no tiene los dos campos del paso 0, actualízala primero desde
`excel-files` y migra; si no, el asistente queda instalado pero sin poder consultar.

#### 2. Migrar

```bash
cd /home/frappe/frappe-bench
bench --site <site> migrate
```

Esto crea/actualiza los 13 DocTypes del asistente (incluye `IA Calificacion`),
los campos nuevos de `IA Mensaje`/`IA API Log`, las 4 Pages, los 2 Workspaces, el
rol `Molduplastic IA User` con sus 7 permisos y las semillas (glosario,
proveedores, sugerencias, evaluación e indicadores). El parche nuevo
`sembrar_tokens_respuesta` deja `max_tokens_respuesta` en **4000** si estaba
vacío o en el default viejo (2000); un valor puesto a mano se respeta. El último
parche, `normalizar_configuracion_ia`, completa los valores de análisis que un
Single viejo no tenía (objetivo, presupuesto, umbrales, límites de contexto):
Frappe no aplica defaults a un Single que ya existía.

Si `migrate` se quedó a medias con `El objetivo de efectividad debe estar entre 0
y 100`, es exactamente eso: un Single viejo con `objetivo_efectividad` en 0. Esta
versión lo corrige; vuelve a ejecutar `bench --site <site> migrate` y retoma desde
el parche que falló.

Los hooks `before_migrate`/`after_migrate` de la app protegen los DocTypes de la
app hermana: si su controlador no importa (faltan `thefuzz`/`unidecode`), el
migrate se corta con un mensaje accionable **antes** de que
`remove_orphan_doctypes` borre `Pieza`; si ya se había borrado (el chat dice
`DocType Pieza not found`), el `after_migrate` lo reimporta desde el JSON y
vuelve a crear los índices analíticos que falten. La tabla y los datos de
`tabPieza` no se tocan en ningún caso.

#### 3. Índices de `tabPieza`

El `after_migrate` crea los índices que falten, así que un `migrate` normal deja
los 12 puestos. El script es la vía explícita para una base nueva, para
verificarlos o para correrlos a mano si se prefiere no esperar al hook:

```bash
cd /home/frappe/frappe-bench
bench --site <site> mariadb < apps/fc_molduplastic_frappe_ia/scripts/indices_ia.sql
```

Sin ellos el executor falla con `ERROR 1176: Key 'idx_ia_…' doesn't exist` y el
chat muestra "Error de conexión".

```bash
cd /home/frappe/frappe-bench
bench --site <site> mariadb < apps/fc_molduplastic_frappe_ia/scripts/indices_ia.sql
```

- Es re-ejecutable: crea solo los que faltan y al final lista los 12 como `OK`.
- Crea en línea (`ALGORITHM=INPLACE, LOCK=NONE`), pero en 3.3 M filas tarda del
  orden de **30–60 min** y consume I/O: hazlo en ventana de mantenimiento y no
  abras el chat hasta que los 12 digan `OK`.
- Si restauraste un dump completo de esta base, los índices vienen incluidos;
  igual ejecuta el script para verificar.

#### 4. Configuración en el Desk

`Molduplastic IA Settings`:

- **API key de Opencode Zen** (o la de cada proveedor en modo "API HTTP").
  Sin clave el chat responde "El servicio de IA no está disponible: … sin API key".
- `Máx. tokens de respuesta` = **4000**, `Timeout LLM` = **55**,
  `Presupuesto de tiempo del análisis` = **240**, `Días máximos del análisis` = 31.
- `Modo de respuesta` (`Breve` / `Analítico`), `Modo de sugerencias` y el
  `Objetivo de efectividad` (75). Los parches respetan lo que ya estaba elegido.
- Si van a usar la sincronización manual, captura `sync_source_*` y confirma que
  el worker `long` esté corriendo (`bench worker --queue long`).

#### 5. Usuarios

El parche crea el rol y los permisos, pero **no asigna el rol a nadie**. Para cada
usuario que use el asistente:

- Rol `Molduplastic IA User` (para ver el Workspace y la Page).
- `User Permission` con `Allow = Aseguradora` y el valor de su aseguradora
  (sin esto el asistente responde "Tu usuario no tiene una aseguradora asignada").

#### 6. Invalidar el JavaScript cacheado

El Desk guarda el JS de cada Page en `localStorage` y solo lo invalida cuando
cambia `tabPage.modified`. Un `git pull` no cambia ese campo, así que sin este
paso los navegadores siguen con el chat viejo:

```bash
bench --site <site> mariadb -e "UPDATE tabPage SET modified = NOW() WHERE name IN ('asistente-ia','sugerencias-ia','tokens-ia');"
bench --site <site> clear-cache
```

#### 7. Reiniciar

```bash
bench restart
```

Obligatorio: gunicorn mantiene los módulos Python cargados en memoria y sin el
restart el site sigue ejecutando el código anterior. Si cambiaste nginx o
supervisor, recarga también esos servicios (ver "Tiempos de espera").

#### 8. Verificación

Pregunta en el chat algo simple ("¿cuántas piezas hay?"). Debe salir:

- el encabezado con título y período,
- la barra "Información de … del … al …" (prueba `idx_ia_aseg_creation`/`_modified`),
- una fila `Éxito` con tokens en `IA API Log`.

Y estos conteos deben coincidir:

```bash
bench --site <site> mariadb -e "
SELECT 'doctypes' t, COUNT(*) n FROM tabDocType WHERE module='Molduplastic IA'
UNION ALL SELECT 'pages', COUNT(*) FROM tabPage WHERE module='Molduplastic IA'
UNION ALL SELECT 'workspaces', COUNT(*) FROM tabWorkspace WHERE name IN ('Molduplastic IA','Administración Molduplastic IA')
UNION ALL SELECT 'docperms', COUNT(*) FROM \`tabCustom DocPerm\` WHERE role='Molduplastic IA User'
UNION ALL SELECT 'glosario', COUNT(*) FROM \`tabIA Glosario\`
UNION ALL SELECT 'proveedores', COUNT(*) FROM \`tabIA Proveedor\`
UNION ALL SELECT 'sugerencias', COUNT(*) FROM \`tabIA Sugerencias\`
UNION ALL SELECT 'evaluacion', COUNT(*) FROM \`tabIA Evaluacion\`
UNION ALL SELECT 'indicadores', COUNT(*) FROM \`tabIA Indicador\` WHERE plantilla=1;"
```

Esperado: `13, 4, 2, 7, 10, 2, 9, 12, 6`.

#### 9. Rollback

Los cambios de esta versión son aditivos (DocTypes, campos, Pages y semillas), así
que volver al commit anterior y reiniciar deja el asistente funcionando como
antes; los índices y los campos nuevos no estorban. No revertir `migrate`.

### Tiempos de espera del asistente IA

Una respuesta analítica puede tardar hasta 240 s. Cada capa que atiende la petición tiene que aguantar ese tiempo, o el chat muestra "Error de conexión" aunque el análisis haya terminado. Al mover la app a otro servidor, revisa estos valores en orden:

| Capa | Dónde se configura | Valor requerido | Hoy |
|---|---|---|---|
| Balanceador o proxy externo (si existe, p. ej. HAProxy) | Su configuración (`timeout server`, `timeout client`) | ≥ 240 s | No está en este servidor; revisar en el balanceador |
| nginx | `config/nginx.conf` del bench, bloque `location @webserver`: `proxy_read_timeout`, `proxy_send_timeout` | 240 | 240 / 240 |
| gunicorn | `config/supervisor.conf` (`-t`) y `http_timeout` en `sites/common_site_config.json` | ≥ 240 | `-t 6000` |
| Presupuesto del análisis | Molduplastic IA Settings > Presupuesto de tiempo del análisis | ≤ 240 | 240 |
| Reserva para la respuesta de respaldo | `RESERVA_RESPALDO_SEG` en `molduplastic_ia/api.py` | 35 s | 35 s |
| Llamada al modelo | Molduplastic IA Settings > Timeout LLM (tope duro 55 s en código) | ≤ 55 s | 55 s |

Notas:
- El análisis termina antes de `presupuesto − reserva` (205 s con el valor por defecto), así que el tiempo útil para el análisis es menor que 240 s. La reserva deja tiempo para la respuesta breve de respaldo.
- Si el balanceador externo corta antes (pasó el 2026-11-11: cerró la petición a los ~50 s con un 499 en nginx), el presupuesto del servidor no sirve de nada: hay que subir también ese límite, y no está en este servidor.
- El análisis solo corre en modo Analítico y solo para periodos de un mes o menos. Los rangos más largos (trimestre, cuatrimestre, año) responden en breve con un aviso.
- Después de cambiar nginx: `sudo nginx -t && sudo nginx -s reload`. Después de cambiar supervisor: `sudo supervisorctl reread && sudo supervisorctl update`.

### Base de datos del asistente

El asistente mide lo mismo que los tableros de BI (`VW_PIEZAS` / `vwPiezas`): el período de Pieza es **`fecha_hora_carga`** (fecha del lote de carga) y el universo es **`recolectable = 1 AND rutas_validas = '1'`**. La reconciliación es exacta: `SELECT COUNT(*) FROM vwPiezas WHERE Aseguradora = 'GNP' AND FechaIngreso BETWEEN …` debe coincidir con "piezas ingresadas" del asistente para ese mes.

Los índices de `tabPieza` **no** son `search_index` de un DocType: el script
`scripts/indices_ia.sql` los crea y el hook `after_migrate` de la app asegura los
que falten en cada `migrate` (al recrear `Pieza`, `updatedb` borra los de una sola
columna, `idx_ia_parte` e `idx_ia_tipo`, y el hook los vuelve a crear). Los
detalles de cada índice están en `AGENTS.md` (sección "Rendimiento: índices de
tabPieza").

Tuning recomendado del host de BD (2 GB de RAM); en este host ya está aplicado:

```ini
innodb_buffer_pool_size       = 1G
innodb_buffer_pool_instances  = 1
max_connections               = 50
tmp_table_size                = 64M
max_heap_table_size           = 64M
userstat                      = ON
```

Requiere reinicio de MariaDB (el usuario de BD de la app no tiene `SUPER` global, así que no se aplica desde la app).

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/fc_molduplastic_frappe_ia
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
