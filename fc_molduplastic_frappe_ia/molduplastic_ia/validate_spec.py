"""Validación dura del Query Spec devuelto por el LLM.

Todo lo que no esté expresamente permitido se rechaza. Esta es la única
puerta entre el LLM y los datos (prevención de prompt injection).
"""

import re

import frappe

from fc_molduplastic_frappe_ia.molduplastic_ia.whitelist import (
	FIELDTYPES_NUMERICOS,
	get_whitelist,
)

OPERADORES_PERMITIDOS = {"=", "!=", ">", "<", ">=", "<=", "like", "in", "between", "is"}
FUNCIONES_AGREGACION = {"count", "sum", "avg", "min", "max"}
TIPOS_GRAFICO = {"bar", "line", "pie"}
ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,30}$")
MAX_FIELDS = 20
MAX_AGGS = 3
MAX_GROUP_BY = 3
MAX_VALORES_IN = 100


class SpecInvalida(frappe.ValidationError):
	pass


def _get_settings():
	return frappe.get_cached_doc("Molduplastic IA Settings")


def validar_spec(spec):
	"""Valida y normaliza el spec. Retorna el spec normalizado o lanza SpecInvalida."""
	if not isinstance(spec, dict):
		raise SpecInvalida("El spec debe ser un objeto JSON.")
	if spec.get("out_of_scope"):
		raise SpecInvalida("Pregunta fuera de alcance.")
	if set(spec) - {
		"doctype",
		"fields",
		"filters",
		"aggregations",
		"group_by",
		"order_by",
		"limit",
		"render_chart",
		"out_of_scope",
	}:
		raise SpecInvalida("El spec contiene claves no permitidas.")

	whitelist = get_whitelist()
	doctype = spec.get("doctype")
	if doctype not in whitelist:
		raise SpecInvalida(f"DocType no permitido: {doctype}")
	info = whitelist[doctype]
	campos_ok = set(info["fields"])

	fields = spec.get("fields") or []
	if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
		raise SpecInvalida("fields debe ser una lista de máximo 20 campos.")
	for f in fields:
		if f not in campos_ok:
			raise SpecInvalida(f"Campo no permitido: {f}")

	filters = spec.get("filters") or {}
	if not isinstance(filters, dict):
		raise SpecInvalida("filters debe ser un objeto.")
	filters_norm = {}
	for campo, cond in filters.items():
		if campo not in campos_ok:
			raise SpecInvalida(f"Filtro sobre campo no permitido: {campo}")
		filters_norm[campo] = _validar_condicion(campo, cond)

	aggs = spec.get("aggregations") or []
	if not isinstance(aggs, list) or len(aggs) > MAX_AGGS:
		raise SpecInvalida("aggregations debe ser una lista de máximo 3.")
	aliases = set()
	for agg in aggs:
		if not isinstance(agg, dict) or set(agg) - {"func", "field", "alias"}:
			raise SpecInvalida("Cada agregación necesita func, field y alias.")
		func, campo, alias = agg.get("func"), agg.get("field"), agg.get("alias")
		if func not in FUNCIONES_AGREGACION:
			raise SpecInvalida(f"Función no permitida: {func}")
		if campo != "*" and campo not in campos_ok:
			raise SpecInvalida(f"Agregación sobre campo no permitido: {campo}")
		if campo == "*" and func != "count":
			raise SpecInvalida("Solo count admite '*'.")
		if func != "count":
			ft = info["fields"][campo]["fieldtype"]
			if ft not in FIELDTYPES_NUMERICOS:
				raise SpecInvalida(f"{func} requiere campo numérico, no {campo} ({ft}).")
		if not alias or not ALIAS_RE.match(alias):
			raise SpecInvalida(f"Alias inválido: {alias}")
		if alias in aliases or alias in campos_ok:
			raise SpecInvalida(f"Alias duplicado o en conflicto: {alias}")
		aliases.add(alias)

	group_by = spec.get("group_by") or []
	if not isinstance(group_by, list) or len(group_by) > MAX_GROUP_BY:
		raise SpecInvalida("group_by debe ser una lista de máximo 3 campos.")
	for g in group_by:
		if g not in campos_ok:
			raise SpecInvalida(f"group_by sobre campo no permitido: {g}")

	if aggs and not group_by and fields:
		raise SpecInvalida("Agregación global no admite fields; usa group_by o quítalos.")
	if aggs and group_by and set(fields) - set(group_by):
		raise SpecInvalida("Con agregación, fields solo puede contener columnas de group_by.")
	if not aggs and group_by:
		raise SpecInvalida("group_by sin aggregations no está permitido.")
	if not aggs and not fields:
		raise SpecInvalida("Sin agregación debes pedir al menos un campo.")

	order_by = spec.get("order_by") or []
	if not isinstance(order_by, list) or len(order_by) > MAX_GROUP_BY:
		raise SpecInvalida("order_by debe ser una lista de máximo 3.")
	columnas_salida = set(fields) | set(group_by) | aliases
	for o in order_by:
		if not isinstance(o, dict) or set(o) - {"field", "dir"}:
			raise SpecInvalida("Cada order_by necesita field y dir.")
		if o.get("field") not in columnas_salida:
			raise SpecInvalida(f"order_by sobre columna inexistente: {o.get('field')}")
		if str(o.get("dir", "")).lower() not in ("asc", "desc"):
			raise SpecInvalida("order_by dir debe ser asc o desc.")

	max_limit = int(_get_settings().get("max_limit") or 500)
	limit = spec.get("limit") or max_limit
	if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= max_limit:
		raise SpecInvalida(f"limit debe ser entero entre 1 y {max_limit}.")

	chart = spec.get("render_chart")
	if chart is not None:
		if not isinstance(chart, dict) or set(chart) - {"type", "x", "y", "title"}:
			raise SpecInvalida("render_chart necesita type, x, y (title opcional).")
		if chart.get("type") not in TIPOS_GRAFICO:
			raise SpecInvalida(f"Tipo de gráfico no permitido: {chart.get('type')}")
		for eje in ("x", "y"):
			if chart.get(eje) not in columnas_salida:
				raise SpecInvalida(f"render_chart.{eje} debe ser una columna del resultado.")

	return {
		"doctype": doctype,
		"fields": fields,
		"filters": filters_norm,
		"aggregations": aggs,
		"group_by": group_by,
		"order_by": [{"field": o["field"], "dir": o["dir"].lower()} for o in order_by],
		"limit": limit,
		"render_chart": chart,
		"out_of_scope": False,
	}


def _validar_condicion(campo, cond):
	"""Valida una condición y devuelve su forma canónica.

	Formas canónicas: valor escalar (=), [op, valor], [op, v1, v2] para between
	y [op, [v...]] para in. También se acepta ["between", [v1, v2]] (límites
	anidados), que es como los modelos escriben el rango con frecuencia; se
	aplana acá para que el executor y el spec guardado solo vean la canónica.
	"""
	if isinstance(cond, (str, int, float, bool)) or cond is None:
		return cond
	if not isinstance(cond, list) or len(cond) not in (2, 3):
		raise SpecInvalida(f"Condición inválida en {campo}.")
	op = cond[0]
	if op not in OPERADORES_PERMITIDOS:
		raise SpecInvalida(f"Operador no permitido en {campo}: {op}")
	if op == "between":
		if len(cond) == 2 and isinstance(cond[1], list):
			cond = [op, *cond[1]]
		if len(cond) != 3:
			raise SpecInvalida(f"between necesita dos valores en {campo}.")
		for v in cond[1:]:
			_validar_valor(campo, v)
		return list(cond)
	if op == "in":
		valores = cond[1] if len(cond) == 2 else None
		if not isinstance(valores, list) or not valores:
			raise SpecInvalida(f"in necesita una lista no vacía en {campo}.")
		if len(valores) > MAX_VALORES_IN:
			raise SpecInvalida(f"in admite máximo {MAX_VALORES_IN} valores en {campo}.")
		for v in valores:
			_validar_valor(campo, v)
		return [op, list(valores)]
	if op == "is":
		if len(cond) != 2 or cond[1] not in ("null", "not null"):
			raise SpecInvalida(f"is solo admite null/not null en {campo}.")
		return [op, cond[1]]
	for v in cond[1:]:
		_validar_valor(campo, v)
	return list(cond)


def _validar_valor(campo, v):
	"""Los valores viajan siempre como parámetro, pero no pueden anidar estructuras."""
	if isinstance(v, (dict, list)):
		raise SpecInvalida(f"Valor inválido en {campo}.")
