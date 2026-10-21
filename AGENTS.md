# AGENTS.md — fc_molduplastic_frappe_ia

App Frappe (`Molduplastic IA`) sobre bench en `/home/frappe/frappe-bench`, site `molduplastic.jarritohost.net` (Frappe 15.83). Asistente IA con tenant `Aseguradora`. Rama `develop`.

## Estructura (todo dentro de esta app)

- `molduplastic_ia/`: `whitelist.py`, `validate_spec.py`, `query_executor.py`, `llm_factory.py`, `api.py` (endpoints), `permissions.py` (tenant).
- Proveedores LLM en `Molduplastic IA Settings` (tabla `IA Proveedor`, sin tocar código): rol Económico/Profundo + orden de fallback; modo **Opencode API** (gateway local por HTTP, sin keys) o **API HTTP (OpenAI)**.
- Opencode Zen = `https://opencode.ai/zen/v1/chat/completions` (OpenAI-compatible) con el Console API Key del Settings. Sin OpenCode local: nada de `opencode serve`, subprocess ni /proc. Modelos: `deepseek-v4.1-flash` (eco, $0.30/$1.20 por 1M), `glm-5.3` (pro, $1.40/$4.40 por 1M); más (Qwen, Gemini, GPT, Claude) en la misma base con su endpoint (`/responses`, `/messages`, `/models/*`). Respuesta trae `usage` → tokens y costo reales en el log.
- `molduplastic_ia/doctype/`: `Molduplastic IA Settings` (Single, solo System Manager; con las tablas hijas `IA Proveedor` y `IA Sugerencias`), `IA API Log` (solo admin), `IA Conversacion` (+ hija `IA Mensaje`), `IA Reporte`.
- `molduplastic_ia/page/asistente_ia/`: Page Desk `asistente-ia` en JS plano + `frappe.Chart` nativo (el Desk v15 **no expone `Vue` global**: una página con `Vue.createApp` queda en blanco). Sin build.
- `molduplastic_ia/patches/`: `crear_rol_y_permisos_ia` (rol `Molduplastic IA User` + Custom DocPerms, no toca la otra app), `crear_workspace_ia`. Registrados en `patches.txt`.
- `seed_gnp_dev.py`: crea usuario `pruebas.gnp@molduplastic.test` + User Permission GNP. **Solo dev, nunca producción.**

## Comandos (desde `/home/frappe/frappe-bench`)

```bash
bench --site molduplastic.jarritohost.net migrate
bench --site molduplastic.jarritohost.net run-tests --app fc_molduplastic_frappe_ia
bench restart   # obligatorio tras tocar hooks.py (workers cachean hooks)
```

## Tenant: cómo funciona aquí

- Fuente de verdad: `User Permission` (`allow=Aseguradora`) por usuario. Admins (System Manager) ven todo.
- Listados: `permission_query_conditions` en `hooks.py`. **No cubren `get_doc`**: por eso hay hooks `has_permission` para Pieza/Recoleccion/Taller en `permissions.py`.
- `Pieza.aseguradora` es directo. La columna guarda el docname ("6"); si el modelo filtra por el nombre que leyó en la pregunta ("GNP"), `query_executor._spec_con_aseguradora` lo traduce antes del SQL (`like "%GNP%"` incluido). Sin eso la consulta salía vacía sin error. El prompt además pide no filtrar por `aseguradora`: el universo ya lo aplica la app. `Recoleccion` se filtra vía `Pieza.id_recoleccion`. `Taller` **solo vía `Pieza`** (`taller_asignado` + `aseguradora` en subconsulta `IN (SELECT DISTINCT ...)`; no se usa `Taller-Aseguradora`). El `EXISTS` correlacionado es inviable (probes de cientos de miles de filas).
- Índices covering creados directo en BD (no van en migrate; recrearlos si se reinstala la BD):
  `ALTER TABLE tabPieza ADD INDEX idx_ia_aseg_taller (aseguradora, taller_asignado);`
  `ALTER TABLE tabPieza ADD INDEX idx_ia_aseg_recoleccion (aseguradora, id_recoleccion);`
  Sin ellos, los listados de Taller/Recolección por tenant tardan minutos; con ellos ~1s.
- Ojo con nombres no estándar en la app hermana: `Fila_Archivo` se renombró a `Fila Archivo`; `Taller-Aseguradora` lleva guion. `get_controller` deriva el nombre de clase quitando solo espacios/guiones: cualquier rename con `_` rompe el import y el migrate **borra el DocType** (ver abajo).

## Executor SQL (desviación consciente del plan)

`tabPieza` tiene 3,3M filas (GNP: 1,9M): agregaciones en Python inviables. `query_executor.py` construye SQL parametrizado **desde el spec ya validado** (identificadores solo de whitelist, valores siempre `%s`, tenant inyectado). El LLM nunca toca SQL. El `COUNT(*)` del universo sobre el tenant tarda ~20-40s: es el costo conocido de la cobertura.

La gramática de `filters` vive en dos lugares que deben coincidir: el bloque de ejemplos en `SYSTEM_SPEC` (`llm_factory.py`) y `_validar_condicion` (`validate_spec.py`). Si divergen, el modelo emite una forma que el validador rechaza y la pregunta muere con "No pude construir una consulta válida". `between` canónico = `["between", desde, hasta]` plano (el anidado `["between", [desde, hasta]]` se aplana por tolerancia); `in` canónico = `["in", [valores]]`. El prompt inyecta la fecha actual para que "este mes"/"mes pasado" no se resuelvan con un mes inventado.

## Lecciones del entorno (no revertir)

- `bench migrate` ejecuta `remove_orphan_doctypes`: si un controlador no importa (dependencia faltante, clase mal nombrada), **borra el registro DocType y sus archivos**. Verificar siempre tras migrar.
- Deps instaladas al env por este motivo: `thefuzz==0.22.1`, `unidecode==1.3.8` (estaban en requirements de la app hermana pero no instaladas) y `setuptools==80.9.0` (el dropbox SDK 11.36 necesita `pkg_resources`).
- `allow_tests` activado en el site (`bench set-config allow_tests true`).
- pre-commit/ruff no están instalados en este entorno: estilo a mano (tabs, comillas dobles, línea 110).

## Gramática: comparación entre campos y varias condiciones por campo

`{"campo": "otro"}` como valor solo con `=,!=,>,<,>=,<=`; `["mes_distinto", {"campo": "otro"}]` y `["mes_igual", {"campo": "otro"}]` comparan `YYYY-MM` (rezagadas y recolectadas en su mes de ingreso). Varias condiciones sobre el mismo campo = lista de condiciones (AND, máx 3). **Cuatro lugares que deben coincidir**: `SYSTEM_SPEC`, `_validar_condicion` (+`_es_referencia`), `_condicion`/`_params_de` (+`_refs_de` en cobertura) y tests (`TestComparacionEntreCampos`, `TestRezagadas`, `TestRecolectadasEstrictas`). `MAX_AGGS = 8` (el resumen completo: 6 categorías con el período sobre `fechaingreso` + 2 rezagadas sobre `fecha_hora_recoleccion`). Ojo: `DATE_FORMAT` lleva `%%` en el SQL porque frappe hace `query % args` y un `%Y` pelado truena con `ValueError` (verificado).

Vocabulario registrado en el prompt: CDR=taller; **"tipo CDR"=siempre `tipo_carga`** (nunca `tipo`, que es el modelo del vehículo); tipo_carga por id (10=Taller, 11=Agencia, 12=Audavales, 13=Equipo Pesado); el id nunca sale: `_tipo_carga_legible` sustituye el valor por su nombre (`_mapa_tipo_carga`, respaldo `TIPOS_CARGA`) antes de mandar los datos al modelo; totales/efectividad del mes sobre `fechaingreso`; rezagada = recolección del mes posterior al ingreso y de distinto mes; inconsistente = recolección del mes anterior al ingreso.

## Pendiente (decisiones del dueño)
- Cargar API keys de DeepSeek/GLM en `Molduplastic IA Settings`; sin keys, `ask()` responde "servicio no disponible" (verificado en tests con mocks).
- Purga de `IA API Log`: diseñada pero no activada (prompt+respuesta completos crecen rápido).

## Preguntas que ya responde el LLM (antes eran módulos fijos)

Hubo una tanda de instrucciones deterministas (`siniestros.py`, `montos.py`, `efectividad.py` + base `instrucciones.py`) que quedaron **eliminadas**: al ampliar la gramática quedaron absorbidas por `ask()`.

| Pregunta | Cómo la responde hoy | Verificado contra |
|---|---|---|
| "nº de siniestros de este mes" | `count_distinct(siniestro)` con `fechaingreso` del mes | 15 siniestros / 40 piezas |
| "¿cuánto valen las piezas recolectadas y las pendientes?" | dos `sum(preciovale)`, cada una con su `filtros` (ver abajo) | $36,980 / $14,775 |
| "¿por encima o por debajo del 75% de efectividad?" | dos `count(*)` con `filtros` distintos, una por fecha | 40 y 11 → 27.5% |
| "porcentaje de efectividad en el año" | los mismos dos conteos con `between` del 1 de enero a hoy | recolectadas / ingresadas |

**Las cifras de "recolectadas" cambiaron de definición el 2026-10-17** (decisión del dueño). Antes bastaba con que la pieza tuviera `fecha_hora_recoleccion`; ahora exige las tres condiciones de abajo. Ver "Definición estricta de recolectada".

**Las dos reglas de campo de fecha del prompt no son opcionales** (sin ellas el modelo devuelve el histórico entero y da un número plausible pero equivocado):

- "piezas que entraron este mes" → `fechaingreso`; "piezas recolectadas este mes" → `fecha_hora_recoleccion`.
- Cuando las dos medidas de una pregunta se definen por **campos de fecha distintos**, `filters` global va **vacío** y cada agregación lleva su condición completa. Si no, ambos montos comparten población.

## Definición estricta de recolectada

Decisión del dueño (2026-10-17): una pieza está **recolectada** solo si cumple las tres:

1. `id_estatus = 100` (en el catálogo `Estatus`, 100 = "Recolectado"; 1.269.093 de 3.313.370 filas).
2. `fecha_hora_recoleccion` en el **mismo mes** que `fechaingreso` (`mes_igual`).
3. `fecha_hora_recoleccion` **igual o posterior** a `fechaingreso`.

El prompt la llama "definición ESTRICTA" y dice explícitamente que nunca use la versión floja ("solo tiene fecha de recolección"). Vive en `SYSTEM_SPEC` y aplica a toda aparición de "recolectada", sumada al período que traiga la pregunta.

**Consecuencia medida: las cifras bajan 1 a 3 órdenes de magnitud.**

| Mes | aseguradora | con fecha de recolección | definición estricta |
|---|---|---|---|
| oct-2026 | Afirme | 36 | **0** |
| sep-2026 | Afirme | 296 | 47 |
| jul-2026 | GNP | 52.273 | **27** |
| abr-2026 | GNP | 49.975 | 1.632 |

No es un bug del filtro: en Afirme octubre las 36 piezas tienen `fecha_hora_recoleccion` de **meses anterior** al ingreso (ej. ingresada 2026-10-01, recolectada 2026-02-24). El dueño lo confirmó como la definición correcta.

Dos efectos colaterales que el prompt deja explícitos:

- **"pendientes" ya no es el complemento de "recolectadas"** (sigue siendo `fecha_hora_recoleccion IS NULL`), así que la suma no da el total. El prompt prohíbe compensar restando.
- **La efectividad del mes** (recolectadas / total) con esta definición da valores muy bajos. Las filas de la tabla de arriba y las de "Verificado contra" quedaron con la definición vieja: revalidar si se usan como referencia.

Tests: `TestRecolectadasEstrictas` en `test_tenant.py` (ejecución contra datos reales) y `TestComparacionEntreCampos` en `test_validate_spec.py` (validación de `mes_igual`).

Lo que se perdió y no se recupera por LLM: los contadores `sin_precio_*` (`SUM` ignora `preciovale` NULL en silencio) y las `advertencias` de población no complementaria.

Tests: los de esas instrucciones se borraron con ellas. `tests/test_validate_spec.py` y `tests/test_tenant.py` cubren ahora la gramática que las reemplazó, y los que comparan contra SQL directo van en `test_tenant.py` porque es el único que tiene datos reales a mano.

## Rendimiento: índices de tabPieza

Creados 2026-10-14 con `ALGORITHM=INPLACE, LOCK=NONE` (185s y 83s), fuera de `migrate` como los demás `idx_ia_aseg_*`. Recrearlos si se reinstala la BD:

```sql
ALTER TABLE tabPieza ADD INDEX idx_ia_aseg_fechaingreso_siniestro (aseguradora, fechaingreso, siniestro);
ALTER TABLE tabPieza ADD INDEX idx_ia_aseg_fecharecoleccion (aseguradora, fecha_hora_recoleccion);
```

Antes de esto no había nada sobre `(aseguradora, fechaingreso)` ni `(aseguradora, fecha_hora_recoleccion)`: GNP caía en `type=ALL` sobre 3.2M filas (~140s por consulta). Ahora todas las lecturas por esas fechas van `range` con **`Using index`** (index-only).

Medido sobre el año en curso:

| Consulta | Afirme | GNP |
|---|---|---|
| `count_distinct` sobre `siniestro` por `fechaingreso` | 0.16s | 1.14s |
| `count(*)` por `fechaingreso` | 0.02s | 0.21s |
| `count(*)` por `fecha_hora_recoleccion` | 0.05s | 0.19s |
| `sum(preciovale)` por `fechaingreso` | 1.46s | 13.73s |

**Ojo con `sum(preciovale)`**: no entra en el índice, así que solo va `Using index condition`, no `Using index`. El rango se estrecha (941k filas en vez de 3.2M de escaneo) pero hay que leer la fila para el precio. Sigue siendo el punto lento de cualquier consulta de montos; si molesta en GNP, la salida es un índice con `preciovale` al final, a costa de más escritura y disco.

La tabla quedó en 3.6 GB con los cuatro `idx_ia_aseg_*`.

## Gramática: `count_distinct`

`FUNCIONES_AGREGACION` incluye `count_distinct`: sin ella el modelo no podía contar cosas únicas y reportaba filas en vez de siniestros (213,989 filas en lugar de 15 siniestros). `AGGS_SQL` la mapea a `COUNT` y es `_agregacion` quien antepone `DISTINCT` al argumento. El validador la acepta sobre cualquier campo de la whitelist (no exige numérico, al contrario que sum/avg) y le rechaza `"*"` vía `FUNCIONES_SIN_STAR`.

Con esto el LLM responde "nº de siniestros de este mes" con `count_distinct` sin necesitar el módulo fijo, y el total es exacto aunque el `GROUP BY` se trunque por `limit`: antes dependía de contar las filas devueltas, así que con más de 500 grupos anunciaba "500 siniestros".

Tests: `TestCountDistinct` en `tests/test_validate_spec.py` (validación y rechazos) y 3 de ejecución sobre datos reales en `tests/test_tenant.py`.

**Los tres lugares que deben coincidir** (si divergen, el modelo emite una forma que el validador rechaza y la pregunta muere con "No pude construir una consulta válida"): la lista en `SYSTEM_SPEC` (`llm_factory.py`), el mapa `AGGS_SQL` (`query_executor.py`) y `FUNCIONES_AGREGACION` (`validate_spec.py`). `AGGS_SQL` mapea `count_distinct` a `COUNT` pelado: el `DISTINCT` lo agrega `_agregacion` al argumento, no la función. El prompt lleva además la guía de elegir campo de fecha (`fechaingreso` para "que entraron este mes", `fecha_hora_recoleccion` para "recolectadas este mes"), porque el modelo tendía a usar siempre la de recolección y daba 6 en lugar de 15.

## Gramática: filtro por agregación (`filtros` dentro de `aggregations`)

Cada agregación acepta un `filtros` propio, con las mismas reglas y el mismo recorrido por `_validar_condicion`. Existe por un caso concreto: **dos medidas sobre poblaciones distintas** que un único `WHERE` no puede expresar. Sin él el modelo tiene que elegir una de las dos y se inventa la otra — la pregunta "¿cuánto valen las piezas ya recolectadas y las pendientes?" no cabía en el spec y devolvía el total histórico completo.

```json
{"func": "sum", "field": "preciovale", "alias": "recolectado",
 "filtros": {"fecha_hora_recoleccion": ["is", "not null"]}}
```

`validate_spec._normalizar_agg` valida el filtro anidado contra la misma whitelist y llama a `_validar_condicion`, así que no es una segunda puerta: un operador prohibido ahí también se rechaza. Sin filtro, la normalización deja `"filtros": {}`.

Dos trampas de implementación, ya resueltas y con test que las fija:

- **El `CASE` va dentro del agregado**: `SUM(CASE WHEN filtro THEN col END)`. Escrito al revés (`CASE WHEN filtro THEN SUM(col) END`) el agregado se evalúa sobre todas las filas y el `CASE` solo descarta el resultado entero — devuelve el total sin filtrar, sin error y en silencio.
- **El orden de parámetros**: el SELECT va antes del WHERE en el SQL, así que `_consultar` concatena `agg_params + where_params`. Cruzarlos mete los valores en la columna equivocada sin quejarse. `COUNT(*)` con filtro se reescribe a `COUNT(CASE WHEN filtro THEN 1 END)`, porque `COUNT(*)` no acepta expresión.

Sin coincidencia la celda sale NULL, no 0. Y `_cobertura` ahora cuenta también los campos que solo aparecen en un filtro de agregación: si no, un "monto pendiente" en cero por campo vacío pasaría sin que nadie lo viera.

## Cobertura: el denominador es la población medida, no el tenant

`_cobertura` calcula el % sobre la población de cada agregación (su `WHERE` global + su `filtros`), no sobre todo el universo del tenant. Cuando el modelo arma el resumen con `"filters": {}` y el período dentro de cada agregación, medir sobre el tenant daba 46.9% / 91.3% en vez del 90% real (36 de 40 piezas de octubre) y desacreditaba cifras correctas.

Reglas del denominador, con su motivo:

- **Se descarta la selección del campo medido, pero se conserva su período**. Medir la cobertura de `id_estatus` dentro de la población "pendientes" (`id_estatus IS NULL`) da 0% por construcción; por eso se quitan del `WHERE` las condiciones `=`, `in` e `is null` del propio campo. El `between` **sí** se conserva: es lo que define la población. Sin esa excepción, quitarle el período a `fechaingreso` devolvía el tenant entero y anunciaba 92.1% en vez de 100%.
- **Sin población medible, no hay advertencia**. Si al quitar las condiciones no queda ninguna fila, el campo se omite del detalle y `explain` no recibe cobertura; si no, anunciaba "universo 0" y el modelo dudaba de una cifra correcta (0 recolectadas en octubre).
- **El `universo` informado es el de la agregación más general** (la de menos condiciones: "ingresadas" en el resumen). Si es cero, el mayor de los universos medidos. El JSON de cobertura lleva también el `universo` de cada campo.

Costo: unas 2 queries extra por campo consultado, todas sobre rangos estrechos (`_cobertura` mide ~0.02s). El tiempo real se va en `_consultar`, porque cada `CASE` con el período dentro recorre la población del `WHERE`.

## Presentación: el resumen de piezas va en DOS tablas

`SYSTEM_EXPLAIN` (no `SYSTEM_SPEC`) decide el formato. Para "dame el resumen de piezas" la respuesta son solo tablas, sin preámbulo, sin análisis y sin párrafo de cierre:

- Tabla 1, "**Piezas por fecha de ingreso**": las seis categorías cuyo período va sobre `fechaingreso` (ingresadas, recolectadas, no recolectadas, pendientes, inconsistentes recolectadas y no recolectadas).
- Tabla 2, "**Piezas por fecha de recolección**": rezagadas recolectadas y rezagadas no recolectadas.

El modelo deduce la tabla mirando el `between` del spec: sobre `fechaingreso` va a la primera, sobre `fecha_hora_recoleccion` a la segunda. Las dos tablas **no se suman** entre sí. Si la cobertura no es 100%, una sola línea al final; nada más.

Cuentas de una sola cifra (`"¿cuántos siniestros hay?"`) se contestan en una línea, sin piezas, histórico ni desglose: la receta está junto a `count_distinct` en `SYSTEM_SPEC` y la instrucción de brevedad en `SYSTEM_EXPLAIN`.

**Un desglose (`group_by`) va siempre en tabla, aunque tenga una sola fila.** "muestra las piezas por tipo cdr" agrupa por `tipo_carga` y da una sola fila (40 = Taller); sin esta regla el modelo lo contestaba en prosa ("Taller, 40 piezas") porque lo confundía con una cifra única. Para que no dependa de su criterio, `explain()` manda `"desglose": bool(spec["group_by"])` en los datos y `SYSTEM_EXPLAIN` pide: una línea con el período + tabla de dos columnas (dimensión y medida), sin comentarios ni totales al pie. La regla de "una sola cifra en una línea" queda limitada a los totales sin `group_by`. Tests: `TestPromptExplain` en `test_llm.py`.

**La efectividad la divide el código, no el modelo.** "porcentaje de efectividad por taller / región / tipo CDR" fallaba en dos pasos: el modelo devolvía un spec con `fields` y sin `aggregations` (traía filas crudas y se negaba a calcular), y aunque acertara el spec, la división la hacía en prosa. Ahora `SYSTEM_SPEC` lleva el spec exacto escrito (dos `count(*)` con alias `total` y `recolectadas`, `recolectable = 1` en `filters` global, `group_by` con la dimensión, las tres condiciones estrictas en el `filtros` de recolectadas) y `explain()` calcula `datos["porcentajes"]` con `_porcentajes(spec, resultado)` (0.0 cuando `total` es 0, sin dividir entre cero). `SYSTEM_EXPLAIN` solo transcribe: tabla de cinco columnas (dimensión, Ingresadas, Recolectadas, Efectividad y Objetivo) con un decimal, y una línea al final cuando todo da 0% para aclarar que la definición es la estricta. Tests: `TestPorcentajes` en `test_llm.py`.

**El objetivo de efectividad es 75%.** Vive en la constante `llm_factory.OBJETIVO_EFECTIVIDAD` (no escrito a mano en el prompt, para que haya una sola cifra). `SYSTEM_SPEC` lo inyecta con `{objetivo_efectividad}` y dice que "¿estamos por encima o por debajo del objetivo?", "¿cómo vamos contra la meta?" y "¿cumplimos la efectividad?" son la misma consulta de dos cifras del período: el modelo no compara ni filtra por porcentaje en el SQL. `explain()` manda `objetivo_efectividad` en los datos **solo cuando el resultado es de efectividad** (hay columnas `total` + `recolectadas`, o la pregunta menciona efectividad/objetivo). Sin desglose, `SYSTEM_EXPLAIN` pide una línea con la diferencia en puntos (`objetivo − pct`, un decimal); **con desglose la tabla lleva una quinta columna "Objetivo"** con la etiqueta que calcula `_etiqueta_objetivo(pct)` en `_porcentajes` ("Por encima", "Por debajo" o "En el objetivo"): el modelo transcribe, no compara. Verificado: 0 de 40 en octubre → "Está 75.0 puntos por debajo del objetivo de 75%"; la tabla por taller muestra "Por debajo" en las 11 filas; "muestra las piezas por marca" no menciona el objetivo. Tests: `test_prompt_lleva_el_objetivo_de_efectividad` (`TestPromptSpec`), `test_objetivo_solo_en_preguntas_de_efectividad`, `test_objetivo_no_viaja_en_consultas_ajenas`, `test_porcentajes_traen_la_columna_objetivo` y `test_prompt_pide_la_columna_objetivo` (`TestPromptExplain`).

**Comparar grupos no se contesta con el mes en curso.** "¿Las agencias recolectan mejor que los talleres?" no trae período; el default de "este mes" dejaba la respuesta en "no hay datos" (GNP e Inbursa no tienen octubre). `SYSTEM_SPEC` manda usar el año en curso (1 de enero a hoy) cuando la pregunta **compara** dos grupos de CDR o de talleres, y `SYSTEM_EXPLAIN` pide cerrar con una línea de veredicto ("Los talleres recolectan mejor: 1.2% contra 0.7% de las agencias, 0.5 puntos de diferencia"). Sin esa línea la tabla no responde la pregunta. Tests: `TestPromptComparacion` (`test_llm.py`).

**Orden de las tablas: de mayor a menor, salvo que pidan otro.** El orden no puede venir del SQL cuando la medida es un porcentaje (no es columna), así que `explain()` ordena en Python con `_ordenar(spec, filas, porcentajes)`: **con efectividad siempre**, y la dirección la manda la pregunta (`_direccion_por_pregunta`: "peor/peores/menor efectividad" o "más baja" → ascendente; "mejor/mejores" → descendente), no el modelo: si la pregunta no lo dice, se usa el `dir` del `order_by`. El campo que nombre el modelo es un proxy (`recolectadas`), la medida real es el porcentaje. El desempate es por volumen descendente, para que entre varios en 0.0% salga primero el de 266 piezas y no el de 52. En conteos/montos con `group_by` y sin `order_by` ordena por la medida desc, y **no toca nada si el spec trae `order_by`** (ahí manda el SQL). Manda además `"orden"` en los datos y `SYSTEM_EXPLAIN` prohíbe reordenar o alfabetizar. Antes la tabla de efectividad salía por ingresadas y la de marcas podía salir en cualquier orden. Tests: `TestOrdenDeFilas` en `test_llm.py`.

**La efectividad no cabe en el `LIMIT` del SQL.** Como el porcentaje se ordena en Python, `query_executor._limite_sql` usa `max_limit` (500) en el SQL cuando hay dos agregaciones, una se llama `recolectadas` y hay `group_by`, **sin tocar `spec["limit"]`**: el límite pedido sigue siendo el top N ("los 10 peores" → 10 filas) y `explain` recorta a ese N, o a 50 cuando no hubo top. Sin eso, el modelo pedía `limit: 50` y MySQL devolvía 50 grupos **al azar**: en septiembre (210 talleres) la tabla empezaba en 3.8% y dejaba fuera a DITTRICH (5.1%, 9 recolectadas). Con `min_ingresadas` el corte a N va **después** del HAVING (si no, los 10 con menos recolectadas del mes casi no llegan al umbral). `explain` manda `total_filas`, `mostradas` y `corte` (`"top"` o `"presentacion"`), y cierra con "Se muestran los 10 primeros de 20 grupos" o "Se muestran 50 de 210 grupos; acota con una dimensión, un taller o un período". Tests: `TestLimiteSqlEfectividad` (`test_validate_spec.py`), `test_recorta_despues_de_ordenar` y `test_top_n_recorta_al_limite_pedido` (`test_llm.py`).

**Umbral de ingresadas: `min_ingresadas` en el spec.** "Efectividad por taller con 10 o más piezas" es un `HAVING` sobre el conteo, no un `WHERE` sobre la pieza, así que el modelo no lo puede expresar en `filters`. `SYSTEM_SPEC` le pide extraer el número a `"min_ingresadas": N` (siempre `ingresadas >= N`) y `explain()` filtra las filas ya agregadas **después** de ordenar (`zip(filas, porcentajes)`), recalcula `total_filas`/`mostradas` y manda `min_ingresadas` para que la explicación aclare el corte. `validate_spec` lo acepta solo como entero ≥1 y solo en efectividad con `group_by` (si no, `SpecInvalida`: es un no-op silencioso). Sin número no viaja la clave y van todos los grupos (recorte de presentación a 50 con aviso). Verificado en septiembre: con 10+ son 76 talleres (50 mostrados), sin número 210. Los grupos con la dimensión en NULL se muestran con etiqueta legible (`ETIQUETAS_NULO`: "Sin taller asignado", "Sin plaza", "Sin región"), porque el modelo copiaba "None" a la tabla. Tests: `TestMinIngresadas` (`test_validate_spec.py`) y `test_filtra_por_ingresadas_minimas` + `test_dimension_nula_se_muestra_legible` (`test_llm.py`).

**Las dimensiones se piden por su nombre real.** El prompt las lista con el campo al lado (`taller` → `taller_asignado`, `plaza` → `id_plaza`, `región` → `region_pais`, `tipo CDR` → `tipo_carga`), porque **no existe un campo `plaza`** (es `id_plaza`, Link a `Taller-Plazas`) y adivinarlo mataba la pregunta con "No pude construir una consulta válida". `en_plaza` es otro dato (un check, 0 en todo el mes) y no sirve como plaza. Verificado con "dame el porcentaje de efectividad por plaza" (MTY 24, CDMX 8, JUAREZ 6, SALTILLO 2, todas 0.0%) y con "muestra las piezas por plaza". Test: `test_prompt_nombra_los_campos_de_cada_dimension`.

**"Categoría" no es `tipo_carga`.** En "muestra el precio en pesos de las piezas por categoría" el modelo agrupaba por `tipo_carga` (un renglón: Taller $173,224.73). `SYSTEM_SPEC` ahora dice que ahí "categoría" son las **8 categorías del resumen**, con `sum(preciovale)` en vez de `count(*)` y los mismos `filtros`/alias —el máximo de `MAX_AGGS`—, y `explain()` manda `"dinero": bool(_es_dinero(spec))` (todas las agregaciones son `sum(preciovale)`) para que la segunda columna se llame **"Valor"** con formato `$173,224.73` y las sumas nulas salgan `$0.00`. El reparto en dos tablas y la regla de no sumar rezagadas siguen igual. Tests: `TestResumenEnDinero` en `test_llm.py`.

**Cobertura: los nulos que son una categoría no se avisan.** Con las 8 categorías, `_cobertura` reportaba 90% en `id_estatus` y `fecha_hora_recoleccion` porque las 4 pendientes tienen esos campos en NULL — pero ahí el nulo **es** la definición de "pendiente", no un dato faltante. Ahora se omiten los campos que alguna agregación filtra con `["is", "null"]`, y `explain()` solo pasa al modelo los campos por debajo del 100% (los completos eran ruido y el modelo repetía "cobertura 100%" en cada tabla). El resumen, en conteo y en dinero, ya no lleva línea de cobertura; si `preciovale` viniera incompleto, ahí sí aparece. Tests: `test_nulos_que_son_categoria_no_cuentan_como_cobertura` (`test_tenant.py`) y los dos de cobertura en `TestPromptExplain`.

`SYSTEM_SPEC` pide las 8 agregaciones para "dame el resumen de piezas" (a secas) y para "¿cómo va la recolección de mis piezas este mes?" / "cómo vamos con las recolecciones" / "estado de la recolección del mes": es la misma pregunta de negocio y contestar solo con "recolectadas" no dice cómo va nada. Son 8, el máximo de `MAX_AGGS`. Enumerar las 8 categorías en la pregunta en vez de dejarlo al prompt satura el presupuesto de tokens y el modelo devuelve JSON truncado o vacío.

## Presupuesto de tokens y fallback

`max_tokens_respuesta` está en **4000** (antes 2000). DeepSeek v4.1 flash gasta tokens razonando: con un spec largo se quedaba sin presupuesto, devolvía `content` vacío y la pregunta moría con "servicio no disponible" sin explicación. Cambios en `llm_factory.py`:

- El `finish_reason == "length"` con respuesta vacía ahora dice "se agotó el presupuesto de tokens", no "devolvió texto vacío".
- El parseo del JSON se movió **dentro** del `_llamar` del fallback: un JSON inválido o truncado cuenta como fallo de ese proveedor y pasa al siguiente, en vez de abortar la cadena entera.

## Contexto de conversación

Antes del 2026-10-20 el chat no tenía memoria: `ask()` llamaba `generate_spec(pregunta)` y el modelo recibía solo el prompt del sistema y la pregunta actual. La conversación se guardaba en `IA Conversacion`/`IA Mensaje` (con `spec_json` y `datos_json`), pero **solo para mostrarla**: nada de eso volvía al modelo, así que "esos mismos en pesos", "y por taller?" o "y del mes pasado?" se leían como preguntas nuevas.

Ahora hay contexto acotado a referencias, sin volver la app conversacional en el sentido de arrastrar cifras:

- `api._historial_para_llm(conversacion, user)` toma los **últimos 3 turnos** (`MAX_TURNOS_CONTEXTO`) desde los mensajes del propio doc, recorta pregunta y respuesta a 400 caracteres (`MAX_CHARS_CONTEXTO`) y devuelve `[]` si la conversación no existe, es de otro usuario, está eliminada o el usuario no es System Manager.
- `generate_spec(pregunta, historial=None)` y `explain(..., historial=None)` meten un **segundo mensaje system** con `_bloque_contexto(historial)`: la pregunta de cada turno y un recorte de la respuesta. Sin historial los mensajes siguen siendo dos (system + user), así que nada cambia en el camino de una sola pregunta.
- El bloque prohíbe explícitamente reutilizar cifras: **toda cifra sale de la consulta nueva**, que se ejecuta otra vez. El contexto es solo para resolver la referencia. `SYSTEM_SPEC` agrega la regla de seguimientos (heredar período solo si la frase no trae uno; heredar dimensión con "esos mismos"/"lo mismo") y `SYSTEM_EXPLAIN` permite una frase de enlace sin repetir cifras viejas.
- Al agregarlo apareció un fallo que el validador ya frenaba: en un seguimiento el modelo mandaba la dimensión en `fields` **y** en `group_by`. `SYSTEM_SPEC` ahora dice que con `aggregations` el `fields` va vacío `[]`.

Verificado en una misma conversación: "dame el resumen de piezas" → "esos mismos en pesos" (8 `sum(preciovale)`, mismas categorías, $173,224.73) → "y por taller?" (`group_by ["taller_asignado"]`) → "y del mes pasado?" (septiembre, misma dimensión). Tests: `TestHistorialParaLLM` (`test_api.py`) y `TestBloqueContexto` + `test_con_historial_se_agrega_un_system_con_el_contexto` (`test_llm.py`).

## Chat de la app: cómo se pinta la respuesta

La página `asistente-ia` es JS plano y el Desk **no renderiza Markdown**. Antes el texto iba con `.text()`, así que las tablas del resumen se veían como pipes crudos, y encima `addTabla` agregaba otra tabla HTML con los alias del resultado (`inconsistentes_recolectadas`, etc.). Dos tablas y una lista de pipes para la misma cifra.

- `mdAHtml()` convierte el Markdown del asistente a HTML con **Showdown** usando un converter propio (`frappe.iaMd2html = new frappe.md2html.constructor()`), porque activar `tables` en el converter compartido cambiaría el resto del Desk. `tables` viene apagado por defecto en Showdown 2.1.
- `limpiarHtml()` sanea con `DOMParser` y una whitelist de etiquetas (`TABLE`, `TH`, `TD`, `P`, `STRONG`...), quitando **todos** los atributos. El contenido viene del LLM y no se inyecta HTML crudo.
- `traeTabla()` detecta si la respuesta ya trae tabla Markdown; en ese caso `addTabla` no pinta la tabla del resultado, pero **sí** conserva los botones "Graficar" y "Guardar reporte".
- La cobertura (`textoCobertura`) solo se añade como bloque aparte si el texto de la respuesta no la menciona ya; si no, salía dos veces.
- `.ia-pre.ia-md` quita el `white-space: pre-wrap` (el HTML generado trae saltos de línea) y hay estilos propios para las tablas.

`esCifraUnica()` evita la tabla del resultado cuando es una sola celda (una fila, una columna): "15 siniestros" no necesita un cuadro de una casilla al lado.

Los mensajes del usuario siguen con `.text()`: nunca se renderizan.

## Respuestas sin consulta (`respuesta_directa`)

Algunas preguntas no necesitan la BD: "muestrame los estados por region" se contesta con el mapa que ya vive en `regiones.ESTADOS`. Para eso el modelo devuelve `{{"respuesta_directa": "..."}}` en vez de un Query Spec, y `ask()` corta antes de `ejecutar()`.

- `regiones.resumen_regiones()` arma el texto **desde `ESTADOS`**. El mapa no está escrito a mano en el prompt a propósito: dos copias del mismo mapa se separan en silencio.
- Para el mapa completo el modelo devuelve el **token** `{{"respuesta_directa": "REGIONES"}}` y `ask()` lo expande con `regiones.tabla_regiones()`, una tabla Markdown de 7 filas. El LLM no escribe la tabla: podría omitir estados o mover una región. El chat la renderiza como tabla HTML (`mdAHtml`). Frases: "dame las regiones del país y sus estados", "muéstrame los estados por región". Los estados con volumen siguen siendo consulta normal (`group_by ["region_pais"]`), no respuesta directa.
- Test: `TestTablaRegiones` en `test_tenant.py` (7 filas y los 32 estados, sin repetidos).
- `validate_spec._validar_respuesta_directa` acepta la clave **sola**; si viene con `doctype`, `filters` o `aggregations` la rechaza. Ese es el freno: sin él, `respuesta_directa` serviría para colar cifras inventadas sin pasar por el executor.
- Log: estado `Respuesta directa` (agregado al Select de `IA API Log` **vía `bench execute`, no por migrate**). Si `meta.save()` parece no surtir efecto en el proceso que sigue, es caché: `frappe.clear_cache()`.
- `IA Mensaje` guarda `spec` y `resultado` = `None` en este camino, igual que `fuera_de_alcance`.

Tests: `TestRespuestaDirecta` en `test_validate_spec.py`.

## Universo admin
- `ask()` con usuario admin (sin User Permissions) pregunta primero el universo (`TODAS` o una aseguradora) y lo guarda en `IA Conversacion.universo`; rige esa conversación hasta cambiarlo. No-admin ignora el parámetro y usa su tenant.
- `IA Reporte.alcance` guarda el universo del reporte; re-ejecutar revalida permisos.
- Borrado lógico (nada se borra de BD): `IA Conversacion.estado=Eliminada`, `IA Reporte.estado=Eliminado`; listados los excluyen (`!=`), abrir/cerrar/continuar/reabrir los rechazan. Endpoints `eliminar_conversacion` / `eliminar_reporte` (dueño o System Manager). Restaurar = cambiar estado desde el Desk (admin).


## Sugerencias del chat

`Molduplastic IA Settings > Sugerencias` (tabla `IA Sugerencias`: `sugerencia`, `activo`, `orden`) alimenta los chips que se pintan sobre la caja de texto de `asistente-ia`. `api.sugerencias()` devuelve solo las activas, en orden (lee el Single con `get_cached_doc`: únicamente salen los textos, no datos sensibles, así que un usuario de tenant también puede pedirlas). En `asistente_ia.js`, `cargarSugerencias()` crea un botón por sugerencia y el clic lanza la pregunta (`enviarPregunta`), no la copia al input; en la vista de Reportes se ocultan. El patch `sembrar_sugerencias_ia` deja la primera puesta al migrar (idempotente), para que el chat no arranque sin chips. Test: `TestSugerencias` (`test_api.py`).
