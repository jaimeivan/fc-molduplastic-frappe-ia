"""Sincronización manual, unidireccional y por lotes desde producción.

La conexión de origen solo ejecuta SELECT. El destino es la base del site
actual; no se crean tablas ni se propagan borrados.
"""

import json
import re
import time
from datetime import timedelta

import frappe
import pymysql
from frappe.utils import get_datetime, now_datetime

VISTAS_ORIGEN = ("VW_PIEZAS", "vwPiezas", "vw_Recoleccion", "vw_Recolecciones")
TABLAS_BASE = ("tabPieza", "tabTaller", "tabRecoleccion")
TAMANO_LOTE_MAXIMO = 2000
PAUSA_MAXIMA_MS = 5000
INTERVALO_SOLAPAMIENTO = timedelta(minutes=5)
LOCK_NAME = "fc_molduplastic_frappe_ia:sync-prod"
REFERENCIA_SQL = re.compile(
	r"\b(?:FROM|JOIN)\s+((?:`[^`]+`|[\w$]+)(?:\s*\.\s*(?:`[^`]+`|[\w$]+))?)",
	re.IGNORECASE,
)


def _exigir_admin():
	if frappe.session.user == "Guest" or "System Manager" not in frappe.get_roles():
		frappe.throw("Solo System Manager puede administrar la sincronización.", frappe.PermissionError)


def _identificador(nombre):
	# Los nombres vienen de information_schema y se delimitan; rechaza solo los
	# caracteres que podrían cerrar el identificador o terminar la sentencia.
	if not isinstance(nombre, str) or not nombre or len(nombre) > 255 or "`" in nombre or "\x00" in nombre:
		frappe.throw("Se encontró un nombre de tabla no permitido.")
	return f"`{nombre}`"


def _serializar(valor):
	return json.dumps(valor, ensure_ascii=False, default=str)


def _json_o(valor, defecto):
	try:
		return json.loads(valor or "")
	except (TypeError, ValueError):
		return defecto


def _nombre_lock():
	return f"{LOCK_NAME}:{frappe.conf.db_name}"[:64]


def _host_canonico(host):
	host = (host or "localhost").strip().lower().rstrip(".")
	if host in ("localhost", "127.0.0.1", "::1"):
		return "local"
	return host


def _password_origen(settings):
	try:
		return settings.get_password("sync_source_password") or ""
	except Exception:
		return ""


def _conectar_origen(settings):
	host = (settings.sync_source_host or "").strip()
	database = (settings.sync_source_database or "").strip()
	user = (settings.sync_source_user or "").strip()
	password = _password_origen(settings)
	port = int(settings.sync_source_port or 3306)
	if not all((host, database, user, password)):
		frappe.throw("Completa host, base, usuario y contraseña de origen en Molduplastic IA Settings.")
	if (
		database.lower() == str(frappe.conf.db_name or "").lower()
		and _host_canonico(host) == _host_canonico(frappe.conf.db_host)
		and port == int(frappe.conf.db_port or 3306)
	):
		frappe.throw(
			"La base de origen coincide con la base de esta instancia; "
			"se cancela para evitar auto-sincronización."
		)

	kwargs = {
		"host": host,
		"port": port,
		"user": user,
		"password": password,
		"database": database,
		"charset": "utf8mb4",
		"connect_timeout": 10,
		"read_timeout": 60,
		"write_timeout": 10,
		"autocommit": True,
		"cursorclass": pymysql.cursors.DictCursor,
	}
	ssl_config = settings.sync_ssl
	use_ssl = True if ssl_config in (None, "") else bool(int(ssl_config))
	if use_ssl:
		kwargs["ssl"] = {"check_hostname": True}
		if settings.sync_ssl_ca:
			kwargs["ssl"]["ca"] = settings.sync_ssl_ca.strip()
	return pymysql.connect(**kwargs)


def _ejecutar_origen(conn, query, params=()):
	with conn.cursor() as cursor:
		cursor.execute(query, params)
		return cursor.fetchall()


def _referencias_vista(definicion):
	"""Extrae candidatos FROM/JOIN; la existencia real se valida en information_schema."""
	for match in REFERENCIA_SQL.finditer(definicion or ""):
		partes = [parte.strip().strip("`") for parte in match.group(1).split(".")]
		if len(partes) == 1:
			yield None, partes[0]
		else:
			yield partes[0], partes[1]


def _tablas_de_vistas(conn, database):
	"""Resuelve recursivamente las tablas base de las cuatro vistas indicadas."""
	marks = ", ".join(["%s"] * len(VISTAS_ORIGEN))
	views = _ejecutar_origen(
		conn,
		f"SELECT TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS "
		f"WHERE TABLE_SCHEMA = %s AND LOWER(TABLE_NAME) IN ({marks})",
		(database, *(name.lower() for name in VISTAS_ORIGEN)),
	)
	por_nombre = {row["TABLE_NAME"].lower(): row for row in views}
	faltantes = [name for name in VISTAS_ORIGEN if name.lower() not in por_nombre]
	if faltantes:
		frappe.throw("No se encontraron estas vistas en producción: " + ", ".join(faltantes))

	dependencias = set()
	visitadas = set()
	tablas_por_vista = {name: set() for name in VISTAS_ORIGEN}

	def visitar(view_name, vista_raiz):
		key = view_name.lower()
		visit_key = (key, vista_raiz)
		if visit_key in visitadas:
			return
		visitadas.add(visit_key)
		definition = por_nombre.get(key, {}).get("VIEW_DEFINITION")
		if not definition:
			frappe.throw(f"No se pudieron resolver dependencias de la vista {view_name}.")
		for schema, dep in _referencias_vista(definition):
			if schema and schema.lower() != database.lower():
				frappe.throw(
					f"La vista {view_name} depende de otra base ({schema}); "
					"esa dependencia no se sincroniza automáticamente."
				)
			objects = _ejecutar_origen(
				conn,
				"SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
				"WHERE TABLE_SCHEMA = %s AND LOWER(TABLE_NAME) = %s LIMIT 1",
				(database, dep.lower()),
			)
			if not objects:
				# Puede ser un CTE, DUAL o una función tabular, no una tabla física.
				continue
			actual_name = objects[0]["TABLE_NAME"]
			if objects[0]["TABLE_TYPE"] == "VIEW":
				dep_view = _ejecutar_origen(
					conn,
					"SELECT TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS "
					"WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s LIMIT 1",
					(database, actual_name),
				)
				if not dep_view:
					frappe.throw(f"No se pudo leer la definición de la vista dependiente {actual_name}.")
				por_nombre[actual_name.lower()] = dep_view[0]
				visitar(actual_name, vista_raiz)
			else:
				dependencias.add(actual_name)
				tablas_por_vista[vista_raiz].add(actual_name)

	for vista in VISTAS_ORIGEN:
		visitar(por_nombre[vista.lower()]["TABLE_NAME"], vista)

	return sorted(set(TABLAS_BASE) | dependencias, key=str.lower), tablas_por_vista


def _validar_tablas_origen(conn, database, tables):
	marks = ", ".join(["%s"] * len(tables))
	rows = _ejecutar_origen(
		conn,
		f"SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
		f"WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN ({marks})",
		(database, *tables),
	)
	remote = {row["TABLE_NAME"]: row["TABLE_TYPE"] for row in rows}
	faltantes = [name for name in tables if remote.get(name) != "BASE TABLE"]
	if faltantes:
		frappe.throw(
			"La sincronización solo admite tablas físicas de origen; valida: " + ", ".join(faltantes)
		)


def _validar_tablas_destino(tablas):
	marks = ", ".join(["%s"] * len(tablas))
	rows = frappe.db.sql(
		f"SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
		f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ({marks})",
		tablas,
		as_dict=True,
	)
	locales = {row.TABLE_NAME: row.TABLE_TYPE for row in rows}
	faltantes = [name for name in tablas if locales.get(name) != "BASE TABLE"]
	if faltantes:
		frappe.throw(
			"Estas tablas base no existen como tablas físicas en el destino; "
			"no se crearán automáticamente: " + ", ".join(faltantes)
		)


def _columnas_comunes(conn, database, table):
	source = _ejecutar_origen(
		conn,
		"SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA "
		"FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
		"ORDER BY ORDINAL_POSITION",
		(database, table),
	)
	target = frappe.db.sql(
		"SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA "
		"FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
		"ORDER BY ORDINAL_POSITION",
		table,
		as_dict=True,
	)
	if not source or not target:
		frappe.throw(f"No se pudo leer el esquema de {table} en ambos lados.")

	source_by_name = {row["COLUMN_NAME"]: row for row in source}
	target_by_name = {row.COLUMN_NAME: row for row in target}
	if "name" not in source_by_name or "name" not in target_by_name:
		frappe.throw(f"La tabla {table} no tiene una columna name en ambos lados.")
	columns = [
		row["COLUMN_NAME"]
		for row in source
		if row["COLUMN_NAME"] in target_by_name
		and "GENERATED" not in (target_by_name[row["COLUMN_NAME"]].EXTRA or "").upper()
	]
	for name in columns:
		if source_by_name[name]["COLUMN_TYPE"].lower() != target_by_name[name].COLUMN_TYPE.lower():
			frappe.throw(
				f"Esquema incompatible en {table}.{name}: "
				f"origen {source_by_name[name]['COLUMN_TYPE']}, "
				f"destino {target_by_name[name].COLUMN_TYPE}."
			)
	missing_required = [
		row.COLUMN_NAME
		for row in target
		if row.COLUMN_NAME not in source_by_name
		and row.IS_NULLABLE == "NO"
		and row.COLUMN_DEFAULT is None
		and "auto_increment" not in (row.EXTRA or "").lower()
		and "generated" not in (row.EXTRA or "").lower()
	]
	if missing_required:
		frappe.throw(
			f"Esquema incompatible en {table}; el destino requiere columnas ausentes en origen: "
			+ ", ".join(missing_required)
		)
	return columns


def _tiene_indice_modified(conn, database, table):
	rows = _ejecutar_origen(
		conn,
		"SELECT 1 FROM information_schema.STATISTICS "
		"WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
		"AND COLUMN_NAME = 'modified' AND SEQ_IN_INDEX = 1 LIMIT 1",
		(database, table),
	)
	return bool(rows)


def _guardar_checkpoint(checkpoints):
	frappe.db.set_single_value(
		"Molduplastic IA Settings",
		"sync_checkpoints_json",
		_serializar(checkpoints),
		update_modified=False,
	)
	frappe.clear_document_cache("Molduplastic IA Settings", "Molduplastic IA Settings")


def _guardar_progreso(log_name, detalle, total):
	frappe.db.set_value(
		"IA Sincronizacion",
		log_name,
		{"detalle_json": _serializar(detalle), "registros_sincronizados": total},
		update_modified=False,
	)


def _consulta_lote(table, columns, batch_size, full_scan, cursor_name=None, desde=None, cursor_modified=None):
	cols = ", ".join(_identificador(name) for name in columns)
	table_sql = _identificador(table)
	if full_scan:
		query = f"SELECT {cols} FROM {table_sql} WHERE `name` > %s ORDER BY `name` LIMIT %s"
		return query, (cursor_name or "", batch_size)
	if cursor_modified is None:
		query = (
			f"SELECT {cols} FROM {table_sql} WHERE `modified` >= %s "
			"ORDER BY `modified`, `name` LIMIT %s"
		)
		return query, (desde, batch_size)
	query = (
		f"SELECT {cols} FROM {table_sql} WHERE `modified` >= %s "
		"AND (`modified` > %s OR (`modified` = %s AND `name` > %s)) "
		"ORDER BY `modified`, `name` LIMIT %s"
	)
	return query, (desde, cursor_modified, cursor_modified, cursor_name or "", batch_size)


def _aplicar_lote(table, columns, rows):
	if not rows:
		return 0, 0, 0
	_names = [row["name"] for row in rows]
	placeholders = ", ".join(["%s"] * len(_names))
	existing = set(
		frappe.db.sql(
			f"SELECT `name` FROM {_identificador(table)} WHERE `name` IN ({placeholders})",
			_names,
			pluck=True,
		)
	)

	cols_sql = ", ".join(_identificador(name) for name in columns)
	value_marks = ", ".join(["%s"] * len(columns))
	updates = [name for name in columns if name != "name"] or ["name"]
	update_sql = ", ".join(f"{_identificador(name)} = VALUES({_identificador(name)})" for name in updates)
	query = (
		f"INSERT INTO {_identificador(table)} ({cols_sql}) VALUES ({value_marks}) "
		f"ON DUPLICATE KEY UPDATE {update_sql}"
	)
	values = [tuple(row.get(name) for name in columns) for row in rows]
	frappe.db._cursor.executemany(query, values)
	actualizados = sum(1 for name in _names if name in existing)
	return len(rows), len(rows) - actualizados, actualizados


def _actualizar_detalle_tabla(log_name, detalle, total):
	_guardar_progreso(log_name, detalle, total)
	frappe.db.commit()


def _sincronizar_tabla(
	conn, database, table, checkpoint, checkpoints, batch_size, pause_ms, log_name, detalle, total
):
	columns = _columnas_comunes(conn, database, table)
	if "name" not in columns:
		frappe.throw(f"La tabla {table} no comparte la clave name entre origen y destino.")
	has_modified = "modified" in columns
	modified_indexed = has_modified and _tiene_indice_modified(conn, database, table)
	can_increment = has_modified and modified_indexed
	full_scan = not can_increment or not checkpoint.get("full_scan_done")
	last_name = checkpoint.get("last_name") or "" if full_scan else ""
	max_modified = checkpoint.get("modified")
	cursor_modified = None
	cursor_name = None
	desde = None
	if not full_scan:
		desde = get_datetime(max_modified) - INTERVALO_SOLAPAMIENTO if max_modified else get_datetime("1970-01-01")

	item = next((row for row in detalle if row["tabla"] == table), None)
	if item is None:
		item = {
			"tabla": table,
			"estado": "Pendiente",
			"procesados": 0,
			"insertados": 0,
			"actualizados": 0,
			"lotes": 0,
		}
		detalle.append(item)
	item["estado"] = "En curso"
	item["columnas"] = len(columns)
	item["modo"] = "Incremental por modified" if can_increment else "Escaneo por name"
	if has_modified and not modified_indexed:
		item["nota"] = (
			"Sin índice de modified en origen; se recorre la tabla por clave primaria "
			"en cada ejecución."
		)
	elif not has_modified:
		item["nota"] = "La tabla no tiene modified; se recorre por clave primaria en cada ejecución."
	_actualizar_detalle_tabla(log_name, detalle, total[0])

	while True:
		query, params = _consulta_lote(
			table,
			columns,
			batch_size,
			full_scan,
			cursor_name=last_name if full_scan else cursor_name,
			desde=desde,
			cursor_modified=cursor_modified,
		)
		rows = _ejecutar_origen(conn, query, params)
		if not rows:
			break

		processed, inserted, updated = _aplicar_lote(table, columns, rows)
		item["procesados"] += processed
		item["insertados"] += inserted
		item["actualizados"] += updated
		item["lotes"] += 1
		total[0] += processed

		if full_scan:
			last_name = rows[-1]["name"]
			checkpoint["last_name"] = last_name
			for row in rows:
				if row.get("modified") and (not max_modified or row["modified"] > get_datetime(max_modified)):
					max_modified = row["modified"]
			checkpoint["modified"] = str(max_modified) if max_modified else None
			checkpoint["full_scan_done"] = False
		else:
			cursor_modified = rows[-1]["modified"]
			cursor_name = rows[-1]["name"]
			for row in rows:
				if row.get("modified") and (not max_modified or row["modified"] > get_datetime(max_modified)):
					max_modified = row["modified"]
			checkpoint["modified"] = str(max_modified) if max_modified else None
			checkpoint["full_scan_done"] = True

		checkpoints[table] = checkpoint
		_guardar_checkpoint(checkpoints)
		_guardar_progreso(log_name, detalle, total[0])
		frappe.db.commit()
		if pause_ms:
			time.sleep(pause_ms / 1000)

	if full_scan:
		checkpoint["full_scan_done"] = True
		checkpoint["last_name"] = None
		checkpoint["modified"] = str(max_modified) if max_modified else None
		checkpoints[table] = checkpoint
		_guardar_checkpoint(checkpoints)
	item["estado"] = "Completada"
	item["checkpoint_modified"] = checkpoint.get("modified")
	_actualizar_detalle_tabla(log_name, detalle, total[0])


def ejecutar_sincronizacion(log_name):
	"""Worker en cola larga. Un lock de MariaDB serializa las ejecuciones."""
	conn = None
	lock_adquirido = False
	inicio = now_datetime()
	detalle = []
	total = [0]
	try:
		estado_log = frappe.db.get_value("IA Sincronizacion", log_name, "estado")
		if estado_log != "En cola":
			return
		lock = frappe.db.sql("SELECT GET_LOCK(%s, 0)", _nombre_lock())
		lock_adquirido = bool(lock and lock[0][0] == 1)
		if not lock_adquirido:
			frappe.db.set_value("IA Sincronizacion", log_name, {
				"estado": "Fallida",
				"inicio": inicio,
				"fin": now_datetime(),
				"error": "Ya hay otra sincronización en curso.",
			}, update_modified=False)
			frappe.db.commit()
			return
		# Una solicitud puede haber marcado como obsoleto el job mientras esperaba cola.
		if frappe.db.get_value("IA Sincronizacion", log_name, "estado") != "En cola":
			return

		frappe.db.set_value(
			"IA Sincronizacion",
			log_name,
			{"estado": "En curso", "inicio": inicio},
			update_modified=False,
		)
		frappe.db.commit()
		settings = frappe.get_single("Molduplastic IA Settings")
		database = (settings.sync_source_database or "").strip()
		batch_size = min(max(int(settings.sync_batch_size or 500), 100), TAMANO_LOTE_MAXIMO)
		pause_config = settings.sync_pause_ms
		pause_ms = min(max(int(100 if pause_config in (None, "") else pause_config), 0), PAUSA_MAXIMA_MS)
		checkpoints = _json_o(settings.sync_checkpoints_json, {})
		if not isinstance(checkpoints, dict):
			checkpoints = {}
		conn = _conectar_origen(settings)
		tables, views = _tablas_de_vistas(conn, database)
		_validar_tablas_origen(conn, database, tables)
		_validar_tablas_destino(tables)
		for table in tables:
			_identificador(table)
			detalle.append({
				"tabla": table,
				"vistas": [view for view in VISTAS_ORIGEN if table in views.get(view, set())],
				"estado": "Pendiente",
				"procesados": 0,
				"insertados": 0,
				"actualizados": 0,
				"lotes": 0,
			})
		_guardar_progreso(log_name, detalle, 0)
		frappe.db.commit()

		for table in tables:
			checkpoint = checkpoints.get(table, {})
			if not isinstance(checkpoint, dict):
				checkpoint = {}
			_sincronizar_tabla(
				conn,
				database,
				table,
				checkpoint,
				checkpoints,
				batch_size,
				pause_ms,
				log_name,
				detalle,
				total,
			)

		fin = now_datetime()
		frappe.db.set_value("IA Sincronizacion", log_name, {
			"estado": "Completada",
			"fin": fin,
			"duracion_segundos": max(0, int((fin - inicio).total_seconds())),
			"registros_sincronizados": total[0],
			"detalle_json": _serializar(detalle),
			"error": None,
		}, update_modified=False)
		frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		fin = now_datetime()
		message = f"{type(exc).__name__}: {exc}"[:2000]
		try:
			frappe.db.set_value("IA Sincronizacion", log_name, {
				"estado": "Fallida",
				"inicio": inicio,
				"fin": fin,
				"duracion_segundos": max(0, int((fin - inicio).total_seconds())),
				"registros_sincronizados": total[0],
				"detalle_json": _serializar(detalle),
				"error": message,
			}, update_modified=False)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
		frappe.log_error(title="Falló sincronización de producción", message=message)
	finally:
		if conn:
			try:
				conn.close()
			except Exception:
				pass
		if lock_adquirido:
			try:
				frappe.db.sql("SELECT RELEASE_LOCK(%s)", _nombre_lock())
				frappe.db.commit()
			except Exception:
				frappe.db.rollback()


@frappe.whitelist()
def iniciar_sincronizacion():
	_exigir_admin()
	activos = frappe.get_all(
		"IA Sincronizacion",
		filters={"estado": ["in", ["En cola", "En curso"]]},
		fields=["name", "estado", "fecha_solicitud"],
		order_by="fecha_solicitud asc",
	)
	if activos:
		ahora = now_datetime()
		lock_ocupado = False
		if any(row.estado == "En curso" for row in activos):
			lock = frappe.db.sql("SELECT GET_LOCK(%s, 0)", _nombre_lock())
			lock_ocupado = bool(lock and lock[0][0] != 1)
			if not lock_ocupado:
				frappe.db.sql("SELECT RELEASE_LOCK(%s)", _nombre_lock())
				frappe.db.commit()
		for row in activos:
			edad = (ahora - get_datetime(row.fecha_solicitud)).total_seconds() if row.fecha_solicitud else 0
			stale_worker = row.estado == "En curso" and not lock_ocupado
			stale_queue = row.estado == "En cola" and edad > 1800
			if stale_worker or stale_queue:
				frappe.db.set_value(
					"IA Sincronizacion",
					row.name,
					{
						"estado": "Fallida",
						"fin": ahora,
						"error": "El worker o la cola no completó la ejecución; puedes iniciar otra sincronización.",
					},
					update_modified=False,
				)
		frappe.db.commit()
		activos = [
			row
			for row in frappe.get_all(
				"IA Sincronizacion",
				filters={"estado": ["in", ["En cola", "En curso"]]},
				fields=["name"],
				limit=1,
			)
		]
	if activos:
		return {"ok": False, "mensaje": "Ya hay una sincronización en cola o en curso.", "name": activos[0].name}

	settings = frappe.get_single("Molduplastic IA Settings")
	if not settings.sync_source_host or not settings.sync_source_database or not settings.sync_source_user:
		frappe.throw("Configura los datos de origen en Molduplastic IA Settings antes de iniciar.")
	if not _password_origen(settings):
		frappe.throw("Configura la contraseña de origen en Molduplastic IA Settings antes de iniciar.")

	log = frappe.get_doc({
		"doctype": "IA Sincronizacion",
		"fecha_solicitud": now_datetime(),
		"estado": "En cola",
		"usuario": frappe.session.user,
		"registros_sincronizados": 0,
		"detalle_json": "[]",
	})
	log.insert(ignore_permissions=True)
	frappe.enqueue(
		"fc_molduplastic_frappe_ia.molduplastic_ia.sincronizacion.ejecutar_sincronizacion",
		queue="long",
		timeout=28800,
		job_name=f"ia-sync-{log.name}",
		enqueue_after_commit=True,
		log_name=log.name,
	)
	return {"ok": True, "name": log.name, "mensaje": "Sincronización puesta en cola."}


@frappe.whitelist()
def listar_sincronizaciones(limite=20):
	_exigir_admin()
	try:
		limite = min(max(int(limite), 1), 100)
	except (TypeError, ValueError):
		limite = 20
	rows = frappe.get_all(
		"IA Sincronizacion",
		fields=[
			"name", "fecha_solicitud", "estado", "usuario", "inicio", "fin",
			"duracion_segundos", "registros_sincronizados", "detalle_json", "error",
		],
		order_by="fecha_solicitud desc",
		limit=limite,
	)
	for row in rows:
		row["detalle"] = _json_o(row.pop("detalle_json"), [])
	return {"sincronizaciones": rows}
