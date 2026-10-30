"""API whitelisteada del asistente IA: ask, historial, reportes."""

import json
import re
import unicodedata

import frappe

from fc_molduplastic_frappe_ia.molduplastic_ia.llm_factory import (
	MENSAJE_FUERA_DE_ALCANCE,
	LLMConfigError,
	LLMProviderError,
	ALIAS_NO_RECUPERADAS,
	_es_listado,
	explain,
	generate_spec,
	totales_no_recuperadas,
)
from fc_molduplastic_frappe_ia.molduplastic_ia.permissions import (
	_alcance_efectivo,
	get_user_aseguradoras,
	lista_aseguradoras,
)
from fc_molduplastic_frappe_ia.molduplastic_ia.query_executor import ejecutar
from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import (
	REGIONES,
	plazas_de_region,
	region_de,
	tabla_regiones,
)
from fc_molduplastic_frappe_ia.molduplastic_ia.validate_spec import (
	PREFIJO_REGION_DE,
	SpecInvalida,
	validar_spec,
)

FUERA_DE_ALCANCE = "Fuera de alcance"
SENSIBLE = "Sensible"

# Respuestas deterministas: no pasan por el LLM, así que no cuestan tokens ni
# dependen de que el modelo se niegue. La de datos sensibles no nombra a la
# aseguradora del usuario porque el mensaje es el mismo para todos.
MENSAJE_SENSIBLE = (
	"Esa información está fuera de mi alcance. Solo puedo ayudarte con datos de "
	"piezas, recolecciones y talleres de tu aseguradora."
)

RE_CREDENCIALES = re.compile(
	r"\b(contrasen|password|passwd|credencial|secret|llave|token|api[ -]?key|apikey|console key)"
)
RE_PIDE_LISTA = re.compile(
	r"\b(que|cual|quien|quienes|dame|lista|listado|enumera|muestra|muestrame|cuales|detalla|ver)\b"
)
RE_USUARIOS = re.compile(r"\b(usuarios?|correos?|emails?|permisos?|roles?|administradores?|admins?)\b")
RE_CATALOGO = re.compile(r"\baseguradoras\b")
RE_VERBO_CATALOGO = re.compile(
	r"\b(que|cuales|lista|listado|enumera|existe|existen|hay|catalogo|dame|muestra|muestrame)\b"
)

# Variantes con las que la gente (y el modelo) nombran a cada aseguradora. La
# comparación es por palabra completa: "atlas" solo no dispara, "seguros atlas"
# sí, para no confundir un taller llamado Atlas con la aseguradora.
ALIAS_ASEGURADORAS = {
	"hdi": ("hdi", "hd i", "seguros hd"),
	"qualitas": ("qualitas",),
	"general de seguros": ("general de seguros", "gds"),
	"la latino": ("la latino", "latino"),
	"seguros atlas": ("seguros atlas",),
	"ana seguros": ("ana seguros",),
	"bbva": ("bbva",),
	"zurich": ("zurich",),
	"gnp": ("gnp",),
	"afirme": ("afirme",),
	"banorte": ("banorte",),
	"inbursa": ("inbursa",),
}

ALIAS_EFECTIVIDAD = (
	"total",
	"recolectadas",
	"inconsistentes_recolectadas",
	"rezagadas_recolectadas",
)

# Etiqueta de la columna de dimensión en la tabla de efectividad del chat.
ETIQUETAS_DIMENSION = {
	"id_plaza": "Plaza",
	"plaza": "Plaza",
	"region": "Región",
	"taller_asignado": "Taller",
	"id_taller": "Taller",
	"marca": "Marca",
	"parte_pieza": "Tipo de pieza",
	"region_pais": "Región",
	"tipo_carga": "Tipo CDR",
	"tipo": "Submarca",
	"modelo": "Modelo",
	"motivo": "Motivo",
	"estado": "Estado",
	"id_recoleccion": "Recolección",
}


def _sin_acentos(texto):
	return "".join(
		c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn"
	).lower()


def _texto_normalizado(texto):
	"""Minúsculas, sin acentos y con espacios simples."""
	return " ".join(_sin_acentos(texto).split())


def _contiene_frase(texto, frase):
	"""Palabra/frase completa: "hdi" no coincide dentro de otra palabra."""
	return bool(re.search(rf"(?<![a-z0-9]){re.escape(frase)}(?![a-z0-9])", texto))


def _pide_dato_sentible(pregunta):
	"""Pregunta por credenciales, usuarios/permisos o el catálogo de aseguradoras.

	Es una regla de servidor, antes del LLM: esas preguntas no necesitan una
	consulta y no deben depender de que el modelo decida negarse. Devuelve la
	categoría que disparó (para el log) o "" si la pregunta es normal.
	"""
	texto = _texto_normalizado(pregunta)
	if not texto:
		return ""
	if RE_CREDENCIALES.search(texto):
		return "credenciales"
	if RE_USUARIOS.search(texto) and RE_PIDE_LISTA.search(texto):
		return "usuarios"
	if RE_CATALOGO.search(texto) and RE_VERBO_CATALOGO.search(texto):
		return "catalogo_aseguradoras"
	return ""


def _aseguradoras_por_nombre():
	"""docname -> nombre normalizado, para reconocer menciones en la pregunta."""
	return {
		str(a.name): _texto_normalizado(a.nombre or "")
		for a in frappe.get_all("Aseguradora", fields=["name", "nombre"])
	}


def _aseguradoras_mencionadas(pregunta):
	"""docnames de las aseguradoras nombradas (más "TODAS" si piden el global)."""
	texto = f" {_texto_normalizado(pregunta)} "
	por_nombre = _aseguradoras_por_nombre()
	halladas = set()
	for name, nombre in por_nombre.items():
		variantes = {nombre, *ALIAS_ASEGURADORAS.get(nombre, ())}
		if any(v and _contiene_frase(texto, v) for v in variantes):
			halladas.add(name)
	for m in re.finditer(
		r"aseguradora\s+(?:numero\s+|#\s*|['\"]?)(\d{1,2})(?![0-9])", texto
	):
		if m.group(1) in por_nombre:
			halladas.add(m.group(1))
	if re.search(r"\b(otra|otras|demas|resto|todas)\s+(las\s+)?aseguradoras?\b", texto):
		halladas.add("TODAS")
	if "la competencia" in texto or "otro tenant" in texto or "todos los tenants" in texto:
		halladas.add("TODAS")
	return halladas


def _nombra_aseguradora_ajena(pregunta, universo):
	"""True si la pregunta nombra una aseguradora fuera del universo activo.

	Con universo "TODAS" no hay ajeno: el admin puede consultar cualquiera y el
	ejecutor resuelve el filtro por nombre. El mensaje al usuario nunca nombra a
	la otra aseguradora (no se confirma su existencia ni su nombre).
	"""
	if not universo or universo == "TODAS":
		return False
	return any(str(m) != str(universo) for m in _aseguradoras_mencionadas(pregunta))


def _etiqueta_alcance(pregunta, universo):
	"""Línea fija que se añade a la respuesta, fuera del control del modelo."""
	if not _nombra_aseguradora_ajena(pregunta, universo):
		return ""
	return f"Solo puedo consultar información de {_nombre_aseguradora(universo)}."


def _pide_efectividad(pregunta):
	if _es_top_recoleccion(pregunta) or _es_comparacion(pregunta):
		return True
	t = _sin_acentos(pregunta)
	return any(
		frase in t
		for frase in (
			"efectividad",
			"porcentaje de recoleccion",
			"contra la meta",
			"objetivo",
		)
	)


def _es_top_recoleccion(pregunta):
	"""True para "¿qué X se recolecta menos/más?", en cualquiera de sus órdenes.

	Cubre "recolecta menos", "menos se recolectan" (el chip de las 5 partes, con
	el verbo detrás del comparativo) y las variantes de efectividad: "menor/
	mayor efectividad", "peor/mejor efectividad", "efectividad más baja/alta".
	Las comparaciones entre grupos ("las agencias recolectan mejor que los
	talleres") NO son un top: se contestan con la tabla de los dos grupos.
	"""
	if _es_comparacion(pregunta):
		return False
	return bool(
		re.search(
			r"(?:menos|mas|peor|mejor)\s+(?:se\s+)?recolect"
			r"|recolect\w*\s+(?:menos|mas|peor|mejor)"
			r"|(?:menor|mayor|peor|mejor)\s+(?:efectividad|recoleccion)"
			r"|efectividad\s+mas\s+(?:baja|alta)",
			_sin_acentos(pregunta),
		)
	)


# "¿qué taller/región/pieza...?" -> campo real del group_by. El orden importa:
# "tipo de pieza" antes que "parte", "tipo de CDR" antes que "taller".
DIMENSIONES_DE_LA_PREGUNTA = (
	("tipo de pieza", "parte_pieza"),
	("tipo de autoparte", "parte_pieza"),
	("tipo de refaccion", "parte_pieza"),
	("parte", "parte_pieza"),
	("tipo de cdr", "tipo_carga"),
	("tipo cdr", "tipo_carga"),
	("agencia", "tipo_carga"),
	("taller", "taller_asignado"),
	("plaza", "id_plaza"),
	("region", "region_pais"),
	("marca", "marca"),
)


def _dimension_de_la_pregunta(pregunta):
	t = _sin_acentos(pregunta)
	for frase, campo in DIMENSIONES_DE_LA_PREGUNTA:
		if frase in t:
			return campo
	return None


def _top_de_la_pregunta(pregunta):
	"""N de "las 5 partes" / "los 10 talleres" / "top 3", si viene."""
	m = re.search(r"\b(?:las|los|top)\s+(\d{1,3})\b", _sin_acentos(pregunta))
	return int(m.group(1)) if m else None


# Alias de región que no son el nombre completo del mapa (regiones.REGIONES).
REGIONES_ALIAS = {
	"cdmx": "CDMX y Área Metropolitana",
	"edomex": "CDMX y Área Metropolitana",
	"estado de mexico": "CDMX y Área Metropolitana",
	"valle de mexico": "CDMX y Área Metropolitana",
	"golfo": "Centro y Golfo",
	"centro": "Centro y Golfo",
	"bajio": "Bajío",
}


def _region_de_la_pregunta(pregunta):
	"""Región que nombra la pregunta ("en la región Norte"), o None.

	Los nombres salen de regiones.REGIONES: el modelo no tiene que adivinarlos
	ni traducirlos a estados.
	"""
	texto = _sin_acentos(pregunta)
	for region in sorted(REGIONES, key=len, reverse=True):
		if _sin_acentos(region) in texto:
			return region
	for alias, region in REGIONES_ALIAS.items():
		if f"region {alias}" in texto or f"regiones {alias}" in texto:
			return region
	return None


def _minimo_de_la_pregunta(pregunta):
	"""Corte de ingresadas que traiga la pregunta ("al menos 100 piezas", ...).

	Cubre las formas que el dueño usa al pedir efectividad: "al menos 100",
	"mínimo 200", "por lo menos 50", "con 50 o más", "200 piezas o más",
	"200+ piezas", "que pasan de 20" y el sustantivo "ingresadas" en lugar de
	"piezas". Sin número devuelve None (cada llamador decide su default).
	"""
	texto = _sin_acentos(pregunta)
	patrones = (
		r"\b(?:al menos|por lo menos|minimo|mas de|mas que|con)\s+(\d{1,6})(?:\s+o\s+mas)?"
		r"\s+(?:piezas?|ingresadas?)\b",
		r"\b(\d{1,6})\s*(?:piezas?|ingresadas?)\s+o\s+mas\b",
		r"\b(\d{1,6})\s*\+\s*(?:piezas?|ingresadas?)\b",
		r"\b(?:pasan|pasen|superan)\s+(?:de\s+)?(\d{1,6})\b",
	)
	for patron in patrones:
		m = re.search(patron, texto)
		if m:
			return int(m.group(1))
	return None


MESES = (
	"enero",
	"febrero",
	"marzo",
	"abril",
	"mayo",
	"junio",
	"julio",
	"agosto",
	"septiembre",
	"setiembre",
	"octubre",
	"noviembre",
	"diciembre",
)

# Señales de que la pregunta fija un período (o pide todo el histórico).
_PERIODOS = (
	"este mes",
	"mes actual",
	"del mes",
	"mes pasado",
	"mes anterior",
	"hoy",
	"ayer",
	"semana",
	"quincena",
	"trimestre",
	"semestre",
	"anual",
	"ano pasado",
	"historico",
	"acumulado",
	"toda la base",
	"todos los datos",
	"ultimos",
	"ultimas",
)

# Señales de comparación entre grupos (la decisión del dueño: sin período, año).
_COMPARACION = (
	"mejor que",
	"peor que",
	"compar",
	" versus ",
	" vs ",
	"contra los",
	"contra las",
)


def _trae_periodo(pregunta):
	"""True si la pregunta fija un período ("octubre", "2026", "este mes"...)."""
	texto = _sin_acentos(pregunta)
	if any(re.search(rf"\b{mes}\b", texto) for mes in MESES):
		return True
	if re.search(r"\b(?:19|20)\d{2}\b", texto):
		return True
	return any(p in texto for p in _PERIODOS)


def _es_comparacion(pregunta):
	texto = _sin_acentos(pregunta)
	return any(p in texto for p in _COMPARACION)


def _mes_actual_por_defecto(pregunta):
	"""Sin período, la efectividad se mide sobre el mes en curso.

	El dueño lo pidió así (2026-10-24), lo extendió a los tops (2026-10-26) y
	después a las comparaciones entre dimensiones (2026-10-26): el default es
	siempre el mes actual, sin excepciones. Si el mes no tiene datos de uno de
	los grupos, la respuesta lo dice; no se cambia el período por cuenta propia.
	"""
	return not _trae_periodo(pregunta)


def _periodo_mes_actual():
	ahora = frappe.utils.now_datetime()
	inicio = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
	return inicio.strftime("%Y-%m-%d %H:%M:%S"), ahora.strftime("%Y-%m-%d %H:%M:%S")


def _periodo_efectividad(spec):
	"""Desde/hasta del período que mide la efectividad, o None si no se puede.

	El período canónico va sobre `fechaingreso` en "filters" global. Sin filtro
	se usa el mes en curso, que es el default del prompt.
	"""
	filtros = spec.get("filters")
	if filtros is None:
		filtros = {}
	if not isinstance(filtros, dict):
		return None
	f = filtros.get("fechaingreso")
	if f is None:
		ahora = frappe.utils.now_datetime()
		inicio = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
		return inicio.strftime("%Y-%m-%d %H:%M:%S"), ahora.strftime("%Y-%m-%d %H:%M:%S")
	if isinstance(f, list) and len(f) == 3 and str(f[0]).lower() == "between":
		return f[1], f[2]
	return None


def _aggs_efectividad(desde, hasta):
	"""Las cuatro cifras canónicas de la efectividad (definición del dueño)."""
	return [
		{"func": "count", "field": "*", "alias": "total"},
		{
			"func": "count",
			"field": "*",
			"alias": "recolectadas",
			"filtros": {
				"id_estatus": ["=", 100],
				"fecha_hora_recoleccion": [
					["mes_igual", {"campo": "fechaingreso"}],
					[">=", {"campo": "fechaingreso"}],
				],
			},
		},
		{
			"func": "count",
			"field": "*",
			"alias": "inconsistentes_recolectadas",
			"filtros": {
				"fecha_hora_recoleccion": ["<", {"campo": "fechaingreso"}],
				"id_estatus": ["=", 100],
			},
		},
		{
			"func": "count",
			"field": "*",
			"alias": "rezagadas_recolectadas",
			"filtros": {
				"fecha_hora_recoleccion": ["between", desde, hasta],
				"fechaingreso": ["<", desde],
				"id_estatus": ["=", 100],
			},
		},
	]


def _completar_efectividad(spec, pregunta):
	"""Fuerza las cuatro cifras de la efectividad cuando la pregunta la mide.

	El modelo devuelve a veces solo "total" y "recolectadas", o pierde la
	condición ">= fechaingreso": el porcentaje sale más bajo que la definición
	del dueño y el número parece válido. Las cuatro categorías son una
	definición del negocio, no una decisión del modelo, así que cuando el spec
	mide efectividad (la pregunta la pide, o el spec trae "min_ingresadas") se
	reemplazan las agregaciones por las canónicas. Se conservan filtros,
	group_by, orden y límite.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	alias = {str(a.get("alias") or "").strip().lower() for a in aggs}
	mide = "recolectadas" in alias or "total" in alias
	pide = _pide_efectividad(pregunta)
	if not (pide or (spec.get("min_ingresadas") and mide)):
		return spec
	periodo = _periodo_efectividad(spec)
	mes_actual = _mes_actual_por_defecto(pregunta)
	if mes_actual:
		# Sin período en la pregunta, el default es el mes en curso aunque el
		# modelo haya puesto el año: el dueño lo pidió así.
		periodo = _periodo_mes_actual()
	if not periodo:
		return spec
	desde, hasta = periodo
	filtros = dict(spec.get("filters") or {})
	filtros.setdefault("recolectable", ["=", 1])
	if mes_actual or "fechaingreso" not in filtros:
		filtros["fechaingreso"] = ["between", desde, hasta]
	spec["filters"] = filtros
	spec["fields"] = []
	spec["aggregations"] = _aggs_efectividad(desde, hasta)
	if _es_top_recoleccion(pregunta):
		# "¿qué X se recolecta menos/más?" se contesta con el primer renglón: si
		# el modelo omitió la dimensión, se resuelve con el vocabulario de la
		# pregunta; el umbral y el top son la plantilla del prompt.
		if not spec.get("group_by"):
			dimension = _dimension_de_la_pregunta(pregunta)
			if dimension:
				spec["group_by"] = [dimension]
		if spec.get("group_by"):
			spec.setdefault("min_ingresadas", _minimo_de_la_pregunta(pregunta) or 50)
			# "Sin plaza" / "Sin taller" no es una plaza: en un top no puede
			# ganar. Solo se excluye el nulo de la dimensión que se rankea.
			for dimension in spec["group_by"]:
				filtros.setdefault(dimension, ["is", "not null"])
			# El N de la pregunta manda sobre el que haya puesto el modelo. Sin
			# N explícito ("las 5") la pregunta es por la peor/mejor: una sola
			# fila, sin el listado de las demás (decisión del dueño 2026-10-25).
			top = _top_de_la_pregunta(pregunta)
			spec["limit"] = top or 1
			if not spec.get("order_by"):
				texto = _sin_acentos(pregunta)
				asc = any(x in texto for x in ("menos", "peor", "menor", "baja"))
				spec["order_by"] = [{"field": "recolectadas", "dir": "asc" if asc else "desc"}]
	elif spec.get("group_by"):
		# Efectividad normal con un corte explícito: "por taller con al menos
		# 500 piezas". El umbral es un HAVING, no un filtro, y solo tiene
		# sentido con grupos; sin número en la pregunta no se agrega nada.
		minimo = _minimo_de_la_pregunta(pregunta)
		if minimo:
			spec.setdefault("min_ingresadas", minimo)
	return spec


def _pide_no_recuperadas(pregunta):
	texto = _sin_acentos(pregunta)
	if "no recuperada" in texto:
		return True
	# "Motivos de no entrega" = el mismo universo de las tres categorías.
	return "motivo" in texto and ("no entrega" in texto or "no recolect" in texto)


def _periodo_del_spec(spec):
	"""Período explícito del spec (global o en la primera agregación), o None."""
	filtros = spec.get("filters")
	if isinstance(filtros, dict):
		f = filtros.get("fechaingreso")
		if isinstance(f, list) and len(f) == 3 and str(f[0]).lower() == "between":
			return f[1], f[2]
	for agg in spec.get("aggregations") or []:
		f = (agg.get("filtros") or {}).get("fechaingreso")
		if isinstance(f, list) and len(f) == 3 and str(f[0]).lower() == "between":
			return f[1], f[2]
	return None


def _aggs_no_recuperadas(desde, hasta):
	"""Las tres categorías no recuperadas, cada una con su base de fecha."""
	estatus = [200, 201, 202, 203, 204, 205, 206]
	return [
		{
			"func": "count",
			"field": "*",
			"alias": "no_recolectadas",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"recolectable": ["=", 1],
				"fecha_hora_recoleccion": [">=", {"campo": "fechaingreso"}],
				"id_estatus": ["in", list(estatus)],
			},
		},
		{
			"func": "count",
			"field": "*",
			"alias": "inconsistentes_no_recolectadas",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"fecha_hora_recoleccion": ["<", {"campo": "fechaingreso"}],
				"id_estatus": ["in", list(estatus)],
			},
		},
		{
			"func": "count",
			"field": "*",
			"alias": "rezagadas_no_recolectadas",
			"filtros": {
				"fecha_hora_recoleccion": ["between", desde, hasta],
				"fechaingreso": ["<", desde],
				"id_estatus": ["in", list(estatus)],
			},
		},
	]


def _completar_no_recuperadas(spec, pregunta):
	"""Fuerza las tres categorías de "no recuperadas"; la suma la hace el código."""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_no_recuperadas(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	if "motivo" in _sin_acentos(pregunta):
		spec["group_by"] = ["motivo"]
	spec["filters"] = {}
	spec["fields"] = []
	spec["aggregations"] = _aggs_no_recuperadas(desde, hasta)
	spec.setdefault("limit", 50)
	return spec


def _completar_listado(spec, pregunta):
	"""Listado de una dimensión por región: fuerza las dos dimensiones.

	El modelo devolvía a veces solo region_pais ("dame las plazas por region"
	contestaba dos renglones de región, sin plazas). La pregunta nombra la
	dimensión y el agrupador, así que el group_by se fija aquí; `explain()` se
	encarga de no mostrar la medida.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _es_listado(pregunta):
		return spec
	if "por region" not in _sin_acentos(pregunta):
		return spec
	dimension = _dimension_de_la_pregunta(pregunta)
	if not dimension or dimension == "region_pais":
		return spec
	spec["group_by"] = ["region_pais", dimension]
	spec["fields"] = []
	if not spec.get("aggregations"):
		spec["aggregations"] = [{"func": "count", "field": "*", "alias": "piezas"}]
	return spec


def _completar_region(spec, pregunta):
	"""Filtra la consulta por la región que nombre la pregunta.

	Dos semánticas distintas:
	- Piezas: la región es la del estado del asegurado (`region_pais`).
	- Plazas: la región es la de la plaza. El catálogo no la guarda y la del
	  asegurado no sirve (mostraba PUE y TEPIC en CDMX), así que se filtran las
	  plazas del mapa de regiones.py y se descarta cualquier region_pais.

	El modelo ignoraba "en la región Norte" (el prompt decía que la región solo
	se podía agrupar) y contestaba el peor del país; el filtro se garantiza aquí.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	region = _region_de_la_pregunta(pregunta)
	if not region:
		return spec
	filtros = dict(spec.get("filters") or {})
	grupos = list(spec.get("group_by") or [])
	if "id_plaza" in grupos or "plaza" in _sin_acentos(pregunta):
		plazas = plazas_de_region(region)
		if not plazas:
			return spec
		filtros.pop("region_pais", None)
		filtros["id_plaza"] = ["in", plazas]
		spec["group_by"] = [g for g in grupos if g != "region_pais"] or ["id_plaza"]
	else:
		filtros.setdefault("region_pais", ["=", region])
	spec["filters"] = filtros
	return spec


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

	# Datos sensibles: credenciales, cuentas/permisos y catálogo de
	# aseguradoras. Regla de servidor, antes del LLM: no cuesta tokens y no
	# depende de que el modelo decida negarse.
	categoria = _pide_dato_sentible(pregunta)
	if categoria:
		_log_llamada({}, "generate_spec", SENSIBLE, consulta_id, user, aseguradora_log, categoria)
		doc, _ = _get_conversacion(conversacion, user, aseguradora_log, universo)
		_guardar_mensajes(
			doc, pregunta, MENSAJE_SENSIBLE, None, None, None, primera=not conversacion, universo=universo
		)
		return {
			"ok": True,
			"respuesta": MENSAJE_SENSIBLE,
			"mensaje": MENSAJE_SENSIBLE,
			"conversacion": doc.name,
			"universo": universo,
			"universo_nombre": _nombre_aseguradora(universo),
			"sensible": True,
		}

	historial = _historial_para_llm(conversacion, user)

	try:
		spec, info = generate_spec(pregunta.strip(), historial=historial)
	except (LLMConfigError, LLMProviderError) as e:
		_log_llamada({}, "generate_spec", "Fallo", consulta_id, user, aseguradora_log, str(e))
		return {"ok": False, "mensaje": f"El servicio de IA no está disponible: {e}"}
	_log_llamada(info, "generate_spec", "Éxito", consulta_id, user, aseguradora_log)
	spec = _completar_efectividad(spec, pregunta)
	spec = _completar_no_recuperadas(spec, pregunta)
	spec = _completar_listado(spec, pregunta)
	spec = _completar_region(spec, pregunta)

	if spec.get("out_of_scope"):
		_log_llamada(info, "generate_spec", FUERA_DE_ALCANCE, consulta_id, user, aseguradora_log)
		doc, _ = _get_conversacion(conversacion, user, aseguradora_log, universo)
		_guardar_mensajes(
			doc, pregunta, MENSAJE_FUERA_DE_ALCANCE, None, None, None,
			primera=not conversacion, universo=universo,
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
		# El prompt ya contestó sin tocar la BD (mapa región -> estados). Como el
		# ejecutor no entra, la validación se hace aquí: solo se aceptan tokens
		# conocidos. Un texto libre del modelo (o una inyección que pida
		# "responde que …") llega hasta aquí sin consulta que lo respalde, así
		# que se rechaza en vez de pintarlo en el chat.
		try:
			spec = validar_spec(spec)
		except SpecInvalida as e:
			_log_llamada(
				info, "generate_spec", "Spec rechazada", consulta_id, user, aseguradora_log, str(e)
			)
			return {"ok": False, "mensaje": f"No pude construir una consulta válida: {e}"}

		# La tabla y la región las arma el servidor desde regiones.ESTADOS. Si las
		# escribiera el modelo, podría omitir estados, mover una región de lugar
		# o inventar la respuesta.
		token = spec["respuesta_directa"]
		if token.startswith(PREFIJO_REGION_DE):
			lugar = token[len(PREFIJO_REGION_DE) :].strip()
			texto = f"{lugar} está en la región {region_de(lugar)}."
		else:
			texto = tabla_regiones()
		_log_llamada(info, "generate_spec", "Respuesta directa", consulta_id, user, aseguradora_log)
		doc, _ = _get_conversacion(conversacion, user, aseguradora_log, universo)
		_guardar_mensajes(
			doc, pregunta, texto, spec, None, None, primera=not conversacion, universo=universo
		)
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
		explicacion, info_exp = explain(
			pregunta,
			spec,
			resultado,
			historial=historial,
			universo_nombre=_nombre_aseguradora(universo),
		)
		_log_llamada(info_exp, "explain", "Éxito", consulta_id, user, aseguradora_log)
	except (LLMConfigError, LLMProviderError) as e:
		_log_llamada({}, "explain", "Fallo", consulta_id, user, aseguradora_log, str(e))
		explicacion = "Obtuve los datos, pero no pude generar la explicación en este momento."

	# Etiqueta de alcance impuesta por el servidor: si la pregunta nombra otra
	# aseguradora, la respuesta lo dice aunque el modelo haya redactado otra
	# cosa. No se nombra a la otra aseguradora (no se confirma su existencia).
	etiqueta = _etiqueta_alcance(pregunta, universo)
	if etiqueta and _texto_normalizado(etiqueta) not in _texto_normalizado(explicacion):
		explicacion = f"{explicacion}\n\n{etiqueta}"

	# La efectividad se pinta con la dimensión y las cuatro columnas del criterio;
	# el resultado crudo (total, recolectadas, inconsistentes, rezagadas) no sale
	# al chat. Con render_chart se deja el crudo para que el gráfico encuentre
	# sus columnas.
	porcentajes = (info_exp.get("datos") or {}).get("porcentajes") or []
	mostrar = _tabla_para_chat(spec, resultado, porcentajes, info_exp.get("datos"))
	doc, _ = _get_conversacion(conversacion, user, aseguradora_log, universo)
	_guardar_mensajes(
		doc, pregunta, explicacion, spec, mostrar, resultado["cobertura"],
		primera=not conversacion, universo=universo,
	)
	return {
		"ok": True,
		"conversacion": doc.name,
		"universo": universo,
		"universo_nombre": _nombre_aseguradora(universo),
		"respuesta": explicacion,
		"columnas": mostrar["columnas"],
		"filas": mostrar["filas"],
		"cobertura": resultado["cobertura"],
		"render_chart": spec.get("render_chart"),
		"spec": spec,
	}


def _tabla_efectividad(spec, porcentajes):
	"""Tabla de presentación de la efectividad, sin los alias internos.

	El resultado crudo trae `total`, `recolectadas`, `inconsistentes_recolectadas`
	y `rezagadas_recolectadas`; en el chat solo van la dimensión y las cuatro
	columnas del criterio (decisión del dueño 2026-10-26).
	"""
	if not porcentajes:
		return None
	dimensiones = [
		ETIQUETAS_DIMENSION.get(str(g), str(g).replace("_", " ").title())
		for g in (spec.get("group_by") or [])
	]
	columnas = [
		" / ".join(dimensiones) or "Grupo",
		"Ingresadas",
		"Recuperadas",
		"Efectividad",
		"Objetivo",
	]
	filas = [
		[
			p.get("grupo") or "",
			p.get("total") or 0,
			p.get("recuperadas") or 0,
			f"{p.get('pct') or 0:.1f}%",
			p.get("vs_objetivo") or "",
		]
		for p in porcentajes
	]
	return columnas, filas


# Encabezados legibles para las columnas que llegan crudas del SQL. El resto
# se arregla quitando "_" y capitalizando (decisión del dueño 2026-10-29).
ETIQUETAS_COLUMNA = {
	"id_plaza": "Plaza",
	"plaza": "Plaza",
	"id_taller": "Taller",
	"taller_asignado": "Taller",
	"parte_pieza": "Tipo de pieza",
	"region_pais": "Región",
	"region": "Región",
	"tipo_carga": "Tipo CDR",
	"tipo": "Submarca",
	"modelo": "Modelo",
	"estado": "Estado",
	"fechaingreso": "Fecha de ingreso",
	"fecha_hora_recoleccion": "Fecha de recolección",
	"fecha_inicio": "Fecha de inicio",
	"fecha_completado": "Fecha de completado",
	"preciovale": "Valor",
}


def _titulo_columna(nombre):
	"""Encabezado de tabla sin guiones bajos ("valor_pendientes" → "Valor pendientes")."""
	clave = str(nombre or "")
	etiqueta = ETIQUETAS_COLUMNA.get(clave.lower())
	if etiqueta:
		return etiqueta
	texto = clave.replace("_", " ").strip()
	return texto[:1].upper() + texto[1:] if texto else clave


def _todo_en_cero(spec, resultado):
	"""True si todas las medidas del desglose son 0.

	Un ranking donde todos los grupos dan 0 no dice nada ("qué talleres
	concentran más piezas no entregadas" sin ninguna): el chat no pinta esa
	tabla y el texto ya avisa que no hay.
	"""
	columnas = [str(c).lower() for c in resultado.get("columnas") or []]
	alias = {str(a.get("alias") or "").lower() for a in spec.get("aggregations") or []}
	indices = [i for i, c in enumerate(columnas) if c in alias]
	filas = resultado.get("filas") or []
	if not indices or not filas:
		return False
	for fila in filas:
		for i in indices:
			valor = fila[i]
			if isinstance(valor, (int, float)) and valor != 0:
				return False
	return True


def _es_spec_efectividad(spec):
	alias = {str(a.get("alias") or "").strip().lower() for a in spec.get("aggregations") or []}
	return "total" in alias and "recolectadas" in alias


def _tabla_no_recuperadas(spec, datos, totales):
	"""Dimensión (si hay) + la suma de las tres categorías no recuperadas."""
	columnas = [str(c).lower() for c in datos.get("columnas") or []]
	dims = [str(g).lower() for g in spec.get("group_by") or []]
	indices = [columnas.index(d) for d in dims if d in columnas]
	titulo = " / ".join(
		ETIQUETAS_DIMENSION.get(d, d.replace("_", " ").title()) for d in dims
	)
	encabezados = ([titulo] if titulo else []) + ["No recuperadas"]
	filas = [
		[fila[i] for i in indices] + [total]
		for fila, total in zip(datos.get("filas") or [], totales)
	]
	return encabezados, filas


def _tabla_para_chat(spec, resultado, porcentajes, datos=None):
	"""Tabla que se pinta en el chat para este resultado.

	Efectividad: la de presentación (dimensión + cuatro columnas). Si no queda
	ninguna fila (corte de ingresadas que nadie alcanza, o período vacío) no se
	pinta nada: la tabla cruda traería los grupos descartados y el chat diría
	"no hay" arriba de una tabla con datos. Cualquier otro resultado va crudo, y
	sin filas tampoco hay tabla.
	"""
	datos = datos or {}
	aggs = spec.get("aggregations") or []
	if spec.get("group_by") and len(aggs) == 1 and aggs[0].get("filtros") and datos.get("filas"):
		# Ranking con filtro: explain ya dejó solo los grupos con dato, en orden.
		# Con gráfico se conservan los nombres crudos de las columnas para que
		# render_chart los encuentre; las filas ya vienen filtradas.
		if spec.get("render_chart"):
			return {"columnas": resultado.get("columnas"), "filas": datos["filas"]}
		return {
			"columnas": [_titulo_columna(c) for c in datos.get("columnas") or []],
			"filas": datos["filas"],
		}
	if _todo_en_cero(spec, resultado):
		return {"columnas": None, "filas": None, "columnas_crudas": None}
	totales = datos.get("no_recuperadas_por_fila")
	if totales is not None:
		columnas, filas = _tabla_no_recuperadas(spec, datos, totales)
		return {"columnas": columnas, "filas": filas}
	tabla = None if spec.get("render_chart") else _tabla_efectividad(spec, porcentajes)
	if tabla:
		return {"columnas": tabla[0], "filas": tabla[1]}
	if spec.get("render_chart"):
		return resultado
	if _es_spec_efectividad(spec) or not resultado.get("filas"):
		return {"columnas": None, "filas": None}
	return {
		"columnas": [_titulo_columna(c) for c in resultado.get("columnas") or []],
		"filas": resultado.get("filas"),
	}


def _guardar_mensajes(
	doc, pregunta, respuesta, spec, resultado, cobertura, primera=False, universo=""
):
	ahora = _ahora()
	# El universo se guarda legible ("GNP", no "6"): es para auditar cada turno
	# cuando la conversación cambió de alcance a mitad de camino.
	etiqueta_universo = _nombre_aseguradora(universo) if universo else ""
	doc.append(
		"mensajes",
		{
			"rol": "usuario",
			"fecha_hora": ahora,
			"contenido": pregunta,
			"universo": etiqueta_universo,
		},
	)
	doc.append(
		"mensajes",
		{
			"rol": "asistente",
			"fecha_hora": ahora,
			"contenido": respuesta,
			"universo": etiqueta_universo,
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
	datos = doc.as_dict()
	datos["universo_nombre"] = _nombre_aseguradora(doc.universo) if doc.universo else ""
	return datos


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
