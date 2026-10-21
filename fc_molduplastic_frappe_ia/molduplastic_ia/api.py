"""API whitelisteada del asistente IA: ask, historial, reportes."""

import json

import frappe

from fc_molduplastic_frappe_ia.molduplastic_ia.llm_factory import (
	MENSAJE_FUERA_DE_ALCANCE,
	LLMConfigError,
	LLMProviderError,
	explain,
	generate_spec,
)
from fc_molduplastic_frappe_ia.molduplastic_ia.permissions import (
	_alcance_efectivo,
	get_user_aseguradoras,
	lista_aseguradoras,
)
from fc_molduplastic_frappe_ia.molduplastic_ia.query_executor import ejecutar
from fc_molduplastic_frappe_ia.molduplastic_ia.validate_spec import SpecInvalida

FUERA_DE_ALCANCE = "Fuera de alcance"


def _ahora():
	return frappe.utils.now_datetime()


def _rate_limit_ok(user):
	s = frappe.get_cached_doc("Molduplastic IA Settings")
	maximo = int(s.max_llamadas_por_minuto or 20)
	ventana = frappe.utils.now_datetime().strftime("%Y%m%d%H%M")
	key = f"molduplastic_ia:ratelimit:{user}:{ventana}"
	usos = frappe.cache().get_value(key) or 0
	if usos >= maximo:
		return False
	frappe.cache().set_value(key, usos + 1, expires_in_sec=70)
	return True


def _log_llamada(info, operacion, estado, consulta_id, user, aseguradora, error=""):
	doc = frappe.get_doc(
		{
			"doctype": "IA API Log",
			"fecha_hora": _ahora(),
			"estado": estado,
			"consulta_id": consulta_id,
			"usuario": user,
			"aseguradora": aseguradora,
			"proveedor": info.get("proveedor") or "",
			"modelo": info.get("modelo") or "",
			"operacion": operacion,
			"es_fallback": 1 if info.get("es_fallback") else 0,
			"motivo_fallback": info.get("motivo_fallback") or "",
			"prompt": info.get("prompt") or "",
			"respuesta": info.get("respuesta") or "",
			"tokens_entrada": info.get("tokens_entrada") or 0,
			"tokens_salida": info.get("tokens_salida") or 0,
			"latencia_ms": info.get("latencia_ms") or 0,
			"costo_estimado_usd": info.get("costo_estimado_usd") or 0,
			"error": error,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


PREGUNTA_UNIVERSO = (
	"Eres administrador: antes de consultar, elige el universo de esta conversación. "
	"¿Una aseguradora en particular o todas?"
)


def _nombre_aseguradora(name):
	if not name or name == "TODAS":
		return "Todas las aseguradoras"
	return frappe.db.get_value("Aseguradora", name, "nombre") or name


def _get_conversacion(nombre, user, aseguradora="", universo=""):
	if nombre:
		doc = frappe.get_doc("IA Conversacion", nombre)
		if doc.usuario != user and "System Manager" not in frappe.get_roles():
			frappe.throw("No tienes acceso a esta conversación.", frappe.PermissionError)
		if doc.estado == "Eliminada":
			frappe.throw("Esta conversación fue eliminada. Inicia una nueva.", frappe.PermissionError)
		if universo:
			doc.universo = universo
			if universo != "TODAS":
				doc.aseguradora = universo
		return doc, False
	doc = frappe.get_doc(
		{
			"doctype": "IA Conversacion",
			"titulo": "Nueva conversación",
			"usuario": user,
			"aseguradora": aseguradora,
			"universo": universo,
			"estado": "Abierta",
			"ultima_actividad": _ahora(),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc, True


MAX_TURNOS_CONTEXTO = 3
MAX_CHARS_CONTEXTO = 400


def _historial_para_llm(conversacion, user):
	"""Últimos turnos de la conversación, comprimidos para el prompt.

	No se mandan specs ni datos: cada pregunta se compila de nuevo y las cifras
	salen siempre de la consulta nueva. El contexto sirve solo para resolver
	referencias ("esos mismos", "y por taller?", "y del mes pasado?").
	"""
	if not conversacion or not frappe.db.exists("IA Conversacion", conversacion):
		return []
	doc = frappe.get_doc("IA Conversacion", conversacion)
	if doc.usuario != user and "System Manager" not in frappe.get_roles(user):
		return []
	if doc.estado == "Eliminada":
		return []
	turnos, pregunta = [], None
	for m in doc.mensajes:
		texto = (m.contenido or "").strip()
		if not texto:
			continue
		if m.rol == "usuario":
			pregunta = texto[:MAX_CHARS_CONTEXTO]
		elif pregunta:
			turnos.append({"pregunta": pregunta, "respuesta": texto[:MAX_CHARS_CONTEXTO]})
			pregunta = None
	return turnos[-MAX_TURNOS_CONTEXTO:]


@frappe.whitelist()
def ask(pregunta, conversacion=None, universo=None):
	"""Flujo completo: universo -> spec -> validación -> ejecución -> explicación."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Debes iniciar sesión.", frappe.PermissionError)
	if not pregunta or not pregunta.strip():
		return {"ok": False, "mensaje": "Escribe una pregunta primero."}
	if not _rate_limit_ok(user):
		return {"ok": False, "mensaje": "Límite de preguntas por minuto alcanzado. Intenta en un momento."}

	propias = get_user_aseguradoras(user)
	if propias is None:
		# Administrador: el universo se elige por conversación (o por llamada).
		if not universo and conversacion and frappe.db.exists("IA Conversacion", conversacion):
			try:
				universo = frappe.get_doc("IA Conversacion", conversacion).universo or ""
			except frappe.PermissionError:
				frappe.throw("No tienes acceso a esta conversación.", frappe.PermissionError)
		if not universo:
			return {
				"ok": True,
				"pregunta_universo": True,
				"mensaje": PREGUNTA_UNIVERSO,
				"aseguradoras": lista_aseguradoras(),
				"pregunta_pendiente": pregunta.strip(),
				"conversacion": conversacion,
			}
		try:
			_alcance_efectivo(user, universo)
		except frappe.PermissionError as e:
			return {"ok": False, "mensaje": str(e)}
		alcance = universo
		aseguradora_log = universo if universo != "TODAS" else ""
	else:
		if not propias:
			return {
				"ok": False,
				"mensaje": "Tu usuario no tiene una aseguradora asignada. Contacta al administrador.",
			}
		alcance = None  # sus propias aseguradoras (se ignora `universo`)
		universo = propias[0]
		aseguradora_log = propias[0]
	consulta_id = frappe.generate_hash(length=10)
	historial = _historial_para_llm(conversacion, user)

	try:
		spec, info = generate_spec(pregunta.strip(), historial=historial)
	except (LLMConfigError, LLMProviderError) as e:
		_log_llamada({}, "generate_spec", "Fallo", consulta_id, user, aseguradora_log, str(e))
		return {"ok": False, "mensaje": f"El servicio de IA no está disponible: {e}"}
	_log_llamada(info, "generate_spec", "Éxito", consulta_id, user, aseguradora_log)

	if spec.get("out_of_scope"):
		_log_llamada(info, "generate_spec", FUERA_DE_ALCANCE, consulta_id, user, aseguradora_log)
		doc, _ = _get_conversacion(conversacion, user, aseguradora_log, universo)
		_guardar_mensajes(doc, pregunta, MENSAJE_FUERA_DE_ALCANCE, None, None, None, primera=not conversacion)
		return {
			"ok": True,
			"fuera_de_alcance": True,
			"mensaje": MENSAJE_FUERA_DE_ALCANCE,
			"conversacion": doc.name,
			"universo": universo,
			"universo_nombre": _nombre_aseguradora(universo),
		}

	if spec.get("respuesta_directa"):
		# El prompt ya contesto sin tocar la BD (mapa region -> estados). No hay
		# resultado ni cobertura que guardar, y el ejecutor no entra.
		texto = spec["respuesta_directa"]
		if texto.strip().upper() in ("REGIONES", "TABLA_REGIONES"):
			# La tabla la arma el servidor desde regiones.ESTADOS. Si la escribiera
			# el modelo, podría omitir estados o cambiar una región de lugar.
			from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import tabla_regiones

			texto = tabla_regiones()
		_log_llamada(info, "generate_spec", "Respuesta directa", consulta_id, user, aseguradora_log)
		doc, _ = _get_conversacion(conversacion, user, aseguradora_log, universo)
		_guardar_mensajes(doc, pregunta, texto, spec, None, None, primera=not conversacion)
		return {
			"ok": True,
			"conversacion": doc.name,
			"universo": universo,
			"universo_nombre": _nombre_aseguradora(universo),
			"respuesta": texto,
			"sin_consulta": True,
		}

	try:
		resultado = ejecutar(spec, user, alcance=alcance)
	except SpecInvalida as e:
		_log_llamada(info, "generate_spec", "Spec rechazada", consulta_id, user, aseguradora_log, str(e))
		return {"ok": False, "mensaje": f"No pude construir una consulta válida: {e}"}
	except frappe.PermissionError as e:
		return {"ok": False, "mensaje": str(e)}

	try:
		explicacion, info_exp = explain(pregunta, spec, resultado, historial=historial)
		_log_llamada(info_exp, "explain", "Éxito", consulta_id, user, aseguradora_log)
	except (LLMConfigError, LLMProviderError) as e:
		_log_llamada({}, "explain", "Fallo", consulta_id, user, aseguradora_log, str(e))
		explicacion = "Obtuve los datos, pero no pude generar la explicación en este momento."

	doc, _ = _get_conversacion(conversacion, user, aseguradora_log, universo)
	_guardar_mensajes(
		doc, pregunta, explicacion, spec, resultado, resultado["cobertura"], primera=not conversacion
	)
	return {
		"ok": True,
		"conversacion": doc.name,
		"universo": universo,
		"universo_nombre": _nombre_aseguradora(universo),
		"respuesta": explicacion,
		"columnas": resultado["columnas"],
		"filas": resultado["filas"],
		"cobertura": resultado["cobertura"],
		"render_chart": spec.get("render_chart"),
		"spec": spec,
	}


def _guardar_mensajes(doc, pregunta, respuesta, spec, resultado, cobertura, primera=False):
	ahora = _ahora()
	doc.append("mensajes", {"rol": "usuario", "fecha_hora": ahora, "contenido": pregunta})
	doc.append(
		"mensajes",
		{
			"rol": "asistente",
			"fecha_hora": ahora,
			"contenido": respuesta,
			"spec_json": json.dumps(spec, ensure_ascii=False) if spec else "",
			"datos_json": json.dumps(
				{"columnas": resultado["columnas"], "filas": resultado["filas"]},
				ensure_ascii=False,
				default=str,
			)
			if resultado
			else "",
			"cobertura_json": json.dumps(cobertura, ensure_ascii=False, default=str) if cobertura else "",
		},
	)
	if primera:
		doc.titulo = (pregunta or "")[:60]
	doc.ultima_actividad = ahora
	doc.save(ignore_permissions=True)


@frappe.whitelist()
def universos():
	"""Lista de aseguradoras para elegir universo (solo admin llega aquí con sentido)."""
	if get_user_aseguradoras(frappe.session.user) is not None:
		frappe.throw("Solo un administrador elige universo.", frappe.PermissionError)
	return lista_aseguradoras()


@frappe.whitelist()
def sugerencias():
	"""Preguntas sugeridas del chat (Settings): activas y en orden.

	Se leen del Single con `get_cached_doc`, sin pedir permiso al usuario: el
	Settings es solo de System Manager y aquí únicamente salen los textos de
	las sugerencias, que no son datos sensibles.
	"""
	s = frappe.get_cached_doc("Molduplastic IA Settings")
	filas = [x for x in (s.get("sugerencias") or []) if x.activo and (x.sugerencia or "").strip()]
	filas.sort(key=lambda x: (x.orden or 0, x.sugerencia))
	return [x.sugerencia.strip() for x in filas]


@frappe.whitelist()
def historial():
	user = frappe.session.user
	return frappe.get_all(
		"IA Conversacion",
		filters={"usuario": user, "estado": ["!=", "Eliminada"]},
		fields=["name", "titulo", "estado", "aseguradora", "ultima_actividad"],
		order_by="ultima_actividad desc",
		limit=50,
	)


@frappe.whitelist()
def obtener_conversacion(nombre):
	doc = frappe.get_doc("IA Conversacion", nombre)
	if doc.usuario != frappe.session.user and "System Manager" not in frappe.get_roles():
		frappe.throw("No tienes acceso a esta conversación.", frappe.PermissionError)
	if doc.estado == "Eliminada":
		frappe.throw("Esta conversación fue eliminada.", frappe.PermissionError)
	return doc.as_dict()


@frappe.whitelist()
def cerrar_conversacion(nombre):
	doc = frappe.get_doc("IA Conversacion", nombre)
	if doc.usuario != frappe.session.user:
		frappe.throw("No tienes acceso a esta conversación.", frappe.PermissionError)
	if doc.estado == "Eliminada":
		frappe.throw("Esta conversación fue eliminada.", frappe.PermissionError)
	doc.estado = "Cerrada"
	doc.save(ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist()
def eliminar_conversacion(nombre):
	"""Borrado lógico: marca la conversación como Eliminada (no se borra de BD)."""
	doc = frappe.get_doc("IA Conversacion", nombre)
	if doc.usuario != frappe.session.user and "System Manager" not in frappe.get_roles():
		frappe.throw("No tienes acceso a esta conversación.", frappe.PermissionError)
	doc.estado = "Eliminada"
	doc.save(ignore_permissions=True)
	return {"ok": True}


def _json_seguro(valor, defecto):
	"""json.loads que no truena: ante texto no-JSON devuelve `defecto`."""
	if not valor:
		return defecto
	if not isinstance(valor, str):
		return valor
	try:
		return json.loads(valor)
	except ValueError:
		return defecto


def _normalizar_cobertura(cobertura):
	"""Garantiza que cobertura_json siempre guarde JSON válido.

	Acepta dict (estructurado del executor), string JSON, o texto plano
	(lo envuelve en {"texto": ...} en vez de guardarlo crudo).
	"""
	if cobertura is None:
		return None
	if isinstance(cobertura, str):
		cobertura = cobertura.strip()
		if not cobertura:
			return None
		try:
			json.loads(cobertura)
			return cobertura
		except ValueError:
			return json.dumps({"texto": cobertura}, ensure_ascii=False)
	return json.dumps(cobertura, ensure_ascii=False, default=str)


@frappe.whitelist()
def guardar_reporte(titulo, spec, snapshot, grafico=None, cobertura=None, universo=0, alcance="", conversacion=None, tipo="Tabla"):
	user = frappe.session.user
	propias = get_user_aseguradoras(user)
	if propias is None:
		alcance = alcance or "TODAS"
		try:
			_alcance_efectivo(user, alcance)
		except frappe.PermissionError as e:
			frappe.throw(str(e))
		aseguradora = alcance if alcance != "TODAS" else ""
	else:
		if not propias:
			frappe.throw("Tu usuario no tiene una aseguradora asignada.", frappe.PermissionError)
		alcance = propias[0]
		aseguradora = propias[0]
	if isinstance(spec, str):
		spec = json.loads(spec)
	from fc_molduplastic_frappe_ia.molduplastic_ia.validate_spec import validar_spec

	spec = validar_spec(spec)  # revalida antes de guardar
	doc = frappe.get_doc(
		{
			"doctype": "IA Reporte",
			"titulo": titulo[:140],
			"tipo": tipo if tipo in ("Tabla", "Gráfico") else "Tabla",
			"estado": "Activo",
			"fecha": _ahora(),
			"usuario": user,
			"aseguradora": aseguradora,
			"alcance": alcance,
			"conversacion": conversacion,
			"spec_json": json.dumps(spec, ensure_ascii=False),
			"snapshot_json": snapshot if isinstance(snapshot, str) else json.dumps(snapshot, ensure_ascii=False, default=str),
			"grafico_json": grafico if isinstance(grafico, str) or grafico is None else json.dumps(grafico, ensure_ascii=False),
			"cobertura_json": _normalizar_cobertura(cobertura),
			"universo": int(universo or 0),
		}
	)
	doc.insert(ignore_permissions=True)
	return {"ok": True, "reporte": doc.name}


@frappe.whitelist()
def listar_reportes():
	user = frappe.session.user
	return frappe.get_all(
		"IA Reporte",
		filters={"usuario": user, "estado": ["!=", "Eliminado"]},
		fields=["name", "titulo", "tipo", "fecha", "aseguradora", "universo"],
		order_by="fecha desc",
		limit=50,
	)


@frappe.whitelist()
def eliminar_reporte(nombre):
	"""Borrado lógico: marca el reporte como Eliminado (no se borra de BD)."""
	doc = frappe.get_doc("IA Reporte", nombre)
	if doc.usuario != frappe.session.user and "System Manager" not in frappe.get_roles():
		frappe.throw("No tienes acceso a este reporte.", frappe.PermissionError)
	doc.estado = "Eliminado"
	doc.save(ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist()
def reabrir_reporte(nombre, reejecutar=0):
	doc = frappe.get_doc("IA Reporte", nombre)
	if doc.usuario != frappe.session.user and "System Manager" not in frappe.get_roles():
		frappe.throw("No tienes acceso a este reporte.", frappe.PermissionError)
	if doc.estado == "Eliminado":
		frappe.throw("Este reporte fue eliminado.", frappe.PermissionError)
	cobertura = _json_seguro(doc.cobertura_json, None)
	if cobertura is None and doc.cobertura_json:
		cobertura = doc.cobertura_json  # texto plano de reportes viejos; el JS lo muestra tal cual
	datos = {"reporte": doc.name, "titulo": doc.titulo, "cobertura": cobertura or {}}
	if int(reejecutar or 0):
		spec = _json_seguro(doc.spec_json, None)
		if not spec:
			frappe.throw("El reporte guardado está corrupto y no se puede re-ejecutar.")
		resultado = ejecutar(spec, frappe.session.user, alcance=doc.alcance or None)
		datos.update(
			{"columnas": resultado["columnas"], "filas": resultado["filas"], "cobertura": resultado["cobertura"], "en_vivo": True}
		)
	else:
		snap = _json_seguro(doc.snapshot_json, {})
		datos.update({"columnas": snap.get("columnas", []), "filas": snap.get("filas", []), "en_vivo": False})
	datos["grafico"] = _json_seguro(doc.grafico_json, None)
	return datos
