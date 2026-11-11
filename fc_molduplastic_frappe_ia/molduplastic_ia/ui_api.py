"""API de la interfaz web (ui_*): bootstrap, chat y tablero de indicadores.

`ui_ask` no reimplementa el asistente: compone los mismos helpers privados de
`api.py` (conversación, mensajes, historial, log, rate limit) y solo agrega la
presentación estructurada que necesita la web.

Los indicadores son del `usuario`, no del `owner` de Frappe: un indicador puede
venir de una plantilla creada por Administrator, así que la pertenencia se
valida a mano (`_exigir_dueno`) antes de tocar un documento ajeno.
"""

import json
from decimal import Decimal

import frappe

from fc_molduplastic_frappe_ia.molduplastic_ia.api import (
	FUERA_DE_ALCANCE,
	PREGUNTA_UNIVERSO,
	_get_conversacion,
	_guardar_mensajes,
	_historial_para_llm,
	_json_seguro,
	_log_llamada,
	_nombre_aseguradora,
	_rate_limit_ok,
)
from fc_molduplastic_frappe_ia.molduplastic_ia.llm_factory import (
	MENSAJE_FUERA_DE_ALCANCE,
	LLMConfigError,
	LLMProviderError,
	explain,
	generate_spec,
)
from fc_molduplastic_frappe_ia.molduplastic_ia.permissions import (
	_alcance_efectivo,
	es_admin,
	get_user_aseguradoras,
	lista_aseguradoras,
)
from fc_molduplastic_frappe_ia.molduplastic_ia.query_executor import ejecutar
from fc_molduplastic_frappe_ia.molduplastic_ia.validate_spec import SpecInvalida, validar_spec

CAMPOS_INDICADOR = [
	"name",
	"indicador",
	"descripcion",
	"tipo",
	"fuente",
	"reporte",
	"valor_num",
	"valor_texto",
	"unidad",
	"formato",
	"color",
	"icono",
	"spec_json",
	"columnas_json",
	"filas_json",
	"grafico_json",
	"cobertura_json",
	"universo",
	"orden",
	"visible",
	"plantilla",
	"usuario",
	"aseguradora",
	"estado",
	"actualizado",
]

CAMPOS_JSON = ("spec_json", "columnas_json", "filas_json", "grafico_json", "cobertura_json")
CAMPOS_COPIA = (
	"indicador",
	"descripcion",
	"tipo",
	"fuente",
	"reporte",
	"valor_num",
	"valor_texto",
	"unidad",
	"formato",
	"color",
	"icono",
	"spec_json",
	"columnas_json",
	"filas_json",
	"grafico_json",
	"cobertura_json",
	"universo",
	"orden",
	"visible",
)
MAX_FILAS_INDICADOR = 200


def _ahora():
	return frappe.utils.now_datetime()


def _exigir_sesion():
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Debes iniciar sesión.", frappe.PermissionError)
	return user


def _exigir_dueno(doc, user, etiqueta):
	"""Dueño lógico del documento (campo `usuario`), no el `owner` de Frappe."""
	if doc.usuario != user and "System Manager" not in frappe.get_roles(user):
		frappe.throw(f"No tienes acceso a este {etiqueta}.", frappe.PermissionError)


def _como_dict(valor):
	if isinstance(valor, dict):
		return valor
	if isinstance(valor, str) and valor.strip():
		try:
			datos = json.loads(valor)
		except ValueError:
			frappe.throw("El indicador debe ser un objeto JSON válido.")
		if isinstance(datos, dict):
			return datos
	frappe.throw("El indicador debe ser un objeto JSON.")


def _json_texto(valor):
	"""Normaliza un campo Code(JSON): dict/list → texto, texto → texto validado."""
	if valor is None:
		return ""
	if isinstance(valor, str):
		texto = valor.strip()
		if not texto:
			return ""
		try:
			json.loads(texto)
		except ValueError:
			frappe.throw(f"El campo debe contener JSON válido: {texto[:40]}")
		return texto
	return json.dumps(valor, ensure_ascii=False, default=str)


def _aseguradora_para(user, valor=""):
	"""Valida el tenant y devuelve la aseguradora a guardar ('' = todas).

	Un no-admin nunca sale de sus User Permissions: si no manda aseguradora, se
	usa la suya; si manda una ajena, se rechaza. Un admin puede dejarla vacía o
	elegir una existente.
	"""
	propias = get_user_aseguradoras(user)
	if propias is None:
		if valor:
			_alcance_efectivo(user, valor)
		return valor or ""
	if not propias:
		frappe.throw("Tu usuario no tiene una aseguradora asignada.", frappe.PermissionError)
	if valor:
		if str(valor) not in [str(a) for a in propias]:
			frappe.throw("No tienes acceso a esa aseguradora.", frappe.PermissionError)
		return valor
	return propias[0]


def _reporte_legible(nombre, user):
	if not nombre or not frappe.db.exists("IA Reporte", nombre):
		frappe.throw("El reporte indicado no existe.", frappe.DoesNotExistError)
	reporte = frappe.get_doc("IA Reporte", nombre)
	if reporte.estado == "Eliminado":
		frappe.throw("El reporte indicado fue eliminado.", frappe.PermissionError)
	# El DocPerm de IA Reporte es if_owner y el `usuario` es la fuente de verdad.
	if reporte.usuario != user and "System Manager" not in frappe.get_roles(user):
		frappe.throw("No tienes acceso a ese reporte.", frappe.PermissionError)
	return reporte


def _spec_del_reporte(reporte):
	spec = _json_seguro(reporte.spec_json, None)
	if not spec:
		frappe.throw(f"El reporte {reporte.name} no tiene un Query Spec válido.")
	return validar_spec(spec)


def _num(valor):
	if isinstance(valor, bool) or not isinstance(valor, (int, float, Decimal)):
		return 0.0
	return float(valor)


def _valor_numerico(spec, resultado):
	"""Valor del KPI: el agregado único, o la primera celda numérica de la tabla."""
	filas = resultado.get("filas") or []
	aggs = spec.get("aggregations") or []
	if not filas:
		return 0.0
	if len(aggs) == 1:
		# columnas = group_by + alias, así que el agregado es la última columna.
		return _num(filas[0][-1] if filas[0] else None)
	for fila in filas:
		for celda in fila or []:
			if isinstance(celda, bool):
				continue
			if isinstance(celda, (int, float, Decimal)):
				return float(celda)
	return 0.0


@frappe.whitelist()
def ui_bootstrap():
	"""Contexto de arranque de la web: usuario, tenant, límites y módulos."""
	user = _exigir_sesion()
	admin = es_admin(user)
	propias = get_user_aseguradoras(user) or []
	aseguradoras = lista_aseguradoras()
	if not admin:
		# Un usuario de tenant solo ve sus aseguradoras; el admin ve el catálogo.
		vistas = {str(a) for a in propias}
		aseguradoras = [a for a in aseguradoras if str(a["name"]) in vistas]
	settings = frappe.get_cached_doc("Molduplastic IA Settings")
	return {
		"usuario": user,
		"nombre_completo": frappe.utils.get_fullname(user),
		"email": frappe.db.get_value("User", user, "email") or "",
		"roles": frappe.get_roles(user),
		"es_admin": admin,
		"aseguradoras": aseguradoras,
		"universo_por_defecto": "TODAS" if admin else (propias[0] if propias else ""),
		"limites": {
			"max_llamadas_por_minuto": int(settings.max_llamadas_por_minuto or 20),
			"max_limit": int(settings.max_limit or 500),
		},
		"modulos": {"chat": True, "reportes": True, "dashboard": True, "plantillas": admin},
	}


@frappe.whitelist()
def ui_ask(pregunta, conversacion=None, universo=None):
	"""Mismo flujo que `api.ask`, más la presentación ya calculada.

	La presentación (`presentacion`) es el dict que `explain` le pasó al modelo:
	filas ordenadas y recortadas, columnas, porcentajes, objetivo y avisos de
	cobertura. La web no debe reordenar ni recalcular nada de ahí.
	"""
	user = _exigir_sesion()
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
		_log_llamada(
			{}, "generate_spec", "Fallo", consulta_id, user, aseguradora_log, str(e), historial=historial
		)
		return {"ok": False, "mensaje": f"El servicio de IA no está disponible: {e}"}
	_log_llamada(info, "generate_spec", "Éxito", consulta_id, user, aseguradora_log, historial=historial)

	if spec.get("out_of_scope"):
		_log_llamada(
			info, "generate_spec", FUERA_DE_ALCANCE, consulta_id, user, aseguradora_log,
			historial=historial,
		)
		doc, _ = _get_conversacion(conversacion, user, aseguradora_log, universo)
		_guardar_mensajes(
			doc, pregunta, MENSAJE_FUERA_DE_ALCANCE, None, None, None, primera=not conversacion
		)
		return {
			"ok": True,
			"fuera_de_alcance": True,
			"mensaje": MENSAJE_FUERA_DE_ALCANCE,
			"conversacion": doc.name,
			"universo": universo,
			"universo_nombre": _nombre_aseguradora(universo),
		}

	if spec.get("respuesta_directa"):
		texto = spec["respuesta_directa"]
		if texto.strip().upper() in ("REGIONES", "TABLA_REGIONES"):
			# La tabla la arma el servidor desde regiones.ESTADOS, no el modelo.
			from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import tabla_regiones

			texto = tabla_regiones()
		_log_llamada(
			info, "generate_spec", "Respuesta directa", consulta_id, user, aseguradora_log,
			historial=historial,
		)
		doc, _ = _get_conversacion(conversacion, user, aseguradora_log, universo)
		_guardar_mensajes(doc, pregunta, texto, spec, None, None, primera=not conversacion)
		return {
			"ok": True,
			"conversacion": doc.name,
			"universo": universo,
			"universo_nombre": _nombre_aseguradora(universo),
			"respuesta": texto,
			"sin_consulta": True,
			"presentacion": None,
		}

	try:
		resultado = ejecutar(spec, user, alcance=alcance)
	except SpecInvalida as e:
		_log_llamada(
			info, "generate_spec", "Spec rechazada", consulta_id, user, aseguradora_log, str(e),
			historial=historial,
		)
		return {"ok": False, "mensaje": f"No pude construir una consulta válida: {e}"}
	except frappe.PermissionError as e:
		return {"ok": False, "mensaje": str(e)}

	presentacion = None
	try:
		explicacion, info_exp = explain(pregunta, spec, resultado, historial=historial)
		_log_llamada(info_exp, "explain", "Éxito", consulta_id, user, aseguradora_log, historial=historial)
		presentacion = info_exp.get("datos")
	except (LLMConfigError, LLMProviderError) as e:
		_log_llamada({}, "explain", "Fallo", consulta_id, user, aseguradora_log, str(e), historial=historial)
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
		"presentacion": presentacion,
		"cobertura": resultado["cobertura"],
		# Red de seguridad si `explain` falló: la tabla cruda sigue disponible.
		"columnas": resultado["columnas"],
		"filas": resultado["filas"],
		"spec": spec,
		"render_chart": spec.get("render_chart"),
	}


@frappe.whitelist()
def ui_csrf():
	"""Token CSRF para una sesión que nunca cargó el Desk."""
	_exigir_sesion()
	from frappe.sessions import get_csrf_token

	return {"csrf_token": get_csrf_token()}


@frappe.whitelist()
def ui_listar_indicadores(incluir_plantillas=0):
	"""Indicadores propios activos y, opcionalmente, las plantillas globales."""
	user = _exigir_sesion()
	propios = frappe.get_all(
		"IA Indicador",
		filters={"usuario": user, "estado": "Activo"},
		fields=CAMPOS_INDICADOR,
		order_by="orden asc, modified desc",
	)
	if not frappe.utils.cint(incluir_plantillas):
		return propios
	vistos = {i["name"] for i in propios}
	plantillas = frappe.get_all(
		"IA Indicador",
		filters={"plantilla": 1, "estado": "Activo"},
		fields=CAMPOS_INDICADOR,
		order_by="orden asc",
	)
	propios.extend(p for p in plantillas if p["name"] not in vistos)
	return propios


@frappe.whitelist()
def ui_guardar_indicador(indicador):
	"""Crea o actualiza un indicador propio (upsert por `name`)."""
	user = _exigir_sesion()
	datos = _como_dict(indicador)
	nombre = datos.get("name")
	if nombre:
		doc = frappe.get_doc("IA Indicador", nombre)
		_exigir_dueno(doc, user, "indicador")
		if doc.estado == "Eliminado":
			frappe.throw("Este indicador fue eliminado.", frappe.PermissionError)
	else:
		doc = frappe.get_doc({"doctype": "IA Indicador", "usuario": user, "estado": "Activo"})

	admin = es_admin(user)
	es_plantilla = 1 if (frappe.utils.cint(datos.get("plantilla")) and admin) else 0
	if frappe.utils.cint(datos.get("plantilla")) and not admin:
		frappe.throw("Solo un administrador puede crear plantillas.", frappe.PermissionError)
	doc.plantilla = es_plantilla

	# `usuario` y `aseguradora` no se confían al payload.
	doc.usuario = user
	doc.aseguradora = _aseguradora_para(user, datos.get("aseguradora") or "") or None

	doc.indicador = (datos.get("indicador") or doc.indicador or "")[:140]
	if not doc.indicador.strip():
		frappe.throw("El indicador necesita un nombre.")
	doc.descripcion = datos.get("descripcion", doc.descripcion) or ""
	doc.tipo = _opcion(datos.get("tipo"), ("KPI", "Tabla", "Gráfico"), "KPI")
	doc.fuente = _opcion(datos.get("fuente"), ("Manual", "Reporte"), "Manual")
	doc.formato = _opcion(
		datos.get("formato"), ("numero", "porcentaje", "moneda", "texto"), "numero"
	)
	doc.color = _opcion(
		datos.get("color"),
		("default", "primary", "success", "warning", "error", "info"),
		"primary",
	)
	doc.icono = datos.get("icono") or ""
	doc.unidad = datos.get("unidad") or ""
	doc.valor_num = frappe.utils.flt(datos.get("valor_num") or 0)
	doc.valor_texto = datos.get("valor_texto") or ""
	doc.orden = max(0, frappe.utils.cint(datos.get("orden")))
	doc.visible = 1 if frappe.utils.cint(datos.get("visible", 1)) else 0
	doc.universo = max(0, frappe.utils.cint(datos.get("universo")))
	doc.estado = _opcion(datos.get("estado"), ("Activo", "Eliminado"), doc.estado or "Activo")

	for campo in CAMPOS_JSON:
		if campo in datos:
			setattr(doc, campo, _json_texto(datos.get(campo)))

	if doc.fuente == "Reporte":
		# El spec del indicador es el del reporte, revalidado aquí: un reporte
		# guardado antes de un cambio de gramática no puede colarse sin pasar
		# por la puerta de validación.
		reporte = _reporte_legible(datos.get("reporte"), user)
		doc.reporte = reporte.name
		doc.spec_json = json.dumps(_spec_del_reporte(reporte), ensure_ascii=False)
	elif doc.spec_json:
		doc.spec_json = json.dumps(
			validar_spec(_json_seguro(doc.spec_json, {})), ensure_ascii=False
		)

	doc.actualizado = _ahora()
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	return {"ok": True, "indicador": doc.as_dict()}


def _opcion(valor, permitidos, defecto):
	"""Valor de un Select: si no viene o no es válido, se deja el actual/defecto."""
	if valor in permitidos:
		return valor
	return defecto


@frappe.whitelist()
def ui_eliminar_indicador(nombre):
	"""Borrado lógico: el indicador queda con estado Eliminado."""
	user = _exigir_sesion()
	doc = frappe.get_doc("IA Indicador", nombre)
	_exigir_dueno(doc, user, "indicador")
	doc.estado = "Eliminado"
	doc.actualizado = _ahora()
	doc.save(ignore_permissions=True)
	return {"ok": True}


@frappe.whitelist()
def ui_actualizar_indicador(nombre, reejecutar=0):
	"""Refresca los datos de un indicador desde su spec (o el de su reporte)."""
	user = _exigir_sesion()
	doc = frappe.get_doc("IA Indicador", nombre)
	_exigir_dueno(doc, user, "indicador")
	if doc.estado == "Eliminado":
		frappe.throw("Este indicador fue eliminado.", frappe.PermissionError)
	if not frappe.utils.cint(reejecutar):
		return {"ok": True, "indicador": doc.as_dict()}
	if doc.plantilla and not es_admin(user):
		frappe.throw("Solo un administrador puede refrescar plantillas.", frappe.PermissionError)

	spec = _json_seguro(doc.spec_json, None)
	if not spec and doc.reporte:
		spec = _json_seguro(_reporte_legible(doc.reporte, user).spec_json, None)
	if not spec:
		return {"ok": False, "mensaje": "El indicador no tiene un Query Spec que ejecutar."}

	try:
		spec = validar_spec(spec)
		resultado = ejecutar(spec, user, alcance=doc.aseguradora or None)
	except SpecInvalida as e:
		return {"ok": False, "mensaje": f"No pude ejecutar el indicador: {e}"}
	except frappe.PermissionError as e:
		return {"ok": False, "mensaje": str(e)}

	doc.valor_num = _valor_numerico(spec, resultado)
	doc.columnas_json = json.dumps(resultado["columnas"], ensure_ascii=False, default=str)
	doc.filas_json = json.dumps(
		resultado["filas"][:MAX_FILAS_INDICADOR], ensure_ascii=False, default=str
	)
	doc.cobertura_json = json.dumps(resultado["cobertura"], ensure_ascii=False, default=str)
	doc.universo = int(resultado["cobertura"].get("universo") or 0)
	doc.actualizado = _ahora()
	doc.save(ignore_permissions=True)
	return {"ok": True, "indicador": doc.as_dict()}


@frappe.whitelist()
def ui_listar_plantillas():
	"""Plantillas globales: solo un administrador las ve."""
	user = _exigir_sesion()
	if not es_admin(user):
		return []
	return frappe.get_all(
		"IA Indicador",
		filters={"plantilla": 1, "estado": "Activo"},
		fields=CAMPOS_INDICADOR,
		order_by="orden asc",
	)


@frappe.whitelist()
def ui_aplicar_plantilla(nombre):
	"""Copia una plantilla al usuario actual (idempotente por spec)."""
	user = _exigir_sesion()
	plantilla = frappe.get_doc("IA Indicador", nombre)
	if not plantilla.plantilla or plantilla.estado != "Activo":
		frappe.throw("El indicador no es una plantilla activa.", frappe.PermissionError)

	for fila in frappe.get_all(
		"IA Indicador",
		filters={
			"usuario": user,
			"indicador": plantilla.indicador,
			"estado": "Activo",
			"plantilla": 0,
		},
		fields=["name", "spec_json"],
	):
		if _mismo_spec(fila.spec_json, plantilla.spec_json):
			return {
				"ok": True,
				"ya_existia": True,
				"indicador": frappe.get_doc("IA Indicador", fila.name).as_dict(),
			}

	datos = {campo: plantilla.get(campo) for campo in CAMPOS_COPIA}
	datos.update(
		{
			"doctype": "IA Indicador",
			"usuario": user,
			"aseguradora": _aseguradora_para(user) or None,
			"plantilla": 0,
			"estado": "Activo",
			"actualizado": _ahora(),
		}
	)
	# El reporte de la plantilla es del admin: si el usuario no puede leerlo, se
	# copia el spec pero no el vínculo (un link roto rompería el refresh).
	if datos.get("reporte"):
		try:
			_reporte_legible(datos["reporte"], user)
		except (frappe.PermissionError, frappe.DoesNotExistError):
			datos["reporte"] = None

	nuevo = frappe.get_doc(datos)
	nuevo.insert(ignore_permissions=True)
	return {"ok": True, "ya_existia": False, "indicador": nuevo.as_dict()}


def _mismo_spec(uno, otro):
	"""Igualdad de specs: compara estructuras; si no son JSON, compara texto."""
	primero, segundo = _json_seguro(uno, None), _json_seguro(otro, None)
	if primero is not None and segundo is not None:
		return primero == segundo
	return (uno or "").strip() == (otro or "").strip()
