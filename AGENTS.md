# AGENTS.md — fc_molduplastic_frappe_ia

App Frappe (`Molduplastic IA`) sobre bench en `/home/frappe/frappe-bench`, site `molduplastic.jarritohost.net` (Frappe 15.83). Asistente IA con tenant `Aseguradora`. Rama `develop`.

## Estructura (todo dentro de esta app)

- `molduplastic_ia/`: `whitelist.py`, `validate_spec.py`, `query_executor.py`, `llm_factory.py`, `api.py` (endpoints), `permissions.py` (tenant).
- Proveedores LLM en `Molduplastic IA Settings` (tabla `IA Proveedor`, sin tocar código): rol Económico/Profundo + orden de fallback; modo **Opencode API** (gateway local por HTTP, sin keys) o **API HTTP (OpenAI)**.
- Opencode Zen = `https://opencode.ai/zen/v1/chat/completions` (OpenAI-compatible) con el Console API Key del Settings. Sin OpenCode local: nada de `opencode serve`, subprocess ni /proc. Modelos: `deepseek-v4.1-flash` (eco, $0.30/$1.20 por 1M), `glm-5.3` (pro, $1.40/$4.40 por 1M); más (Qwen, Gemini, GPT, Claude) en la misma base con su endpoint (`/responses`, `/messages`, `/models/*`). Respuesta trae `usage` → tokens y costo reales en el log.
- `molduplastic_ia/doctype/`: `Molduplastic IA Settings` (Single, solo System Manager), `IA API Log` (solo admin), `IA Conversacion` (+ hija `IA Mensaje`), `IA Reporte`.
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
- `Pieza.aseguradora` es directo. `Recoleccion` se filtra vía `Pieza.id_recoleccion`. `Taller` **solo vía `Pieza`** (`taller_asignado` + `aseguradora` en subconsulta `IN (SELECT DISTINCT ...)`; no se usa `Taller-Aseguradora`). El `EXISTS` correlacionado es inviable (probes de cientos de miles de filas).
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

## Pendiente (decisiones del dueño)
- Cargar API keys de DeepSeek/GLM en `Molduplastic IA Settings`; sin keys, `ask()` responde "servicio no disponible" (verificado en tests con mocks).
- Purga de `IA API Log`: diseñada pero no activada (prompt+respuesta completos crecen rápido).

## Universo admin
- `ask()` con usuario admin (sin User Permissions) pregunta primero el universo (`TODAS` o una aseguradora) y lo guarda en `IA Conversacion.universo`; rige esa conversación hasta cambiarlo. No-admin ignora el parámetro y usa su tenant.
- `IA Reporte.alcance` guarda el universo del reporte; re-ejecutar revalida permisos.
- Borrado lógico (nada se borra de BD): `IA Conversacion.estado=Eliminada`, `IA Reporte.estado=Eliminado`; listados los excluyen (`!=`), abrir/cerrar/continuar/reabrir los rechazan. Endpoints `eliminar_conversacion` / `eliminar_reporte` (dueño o System Manager). Restaurar = cambiar estado desde el Desk (admin).
