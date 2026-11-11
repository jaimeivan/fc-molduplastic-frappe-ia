"""API whitelisteada del asistente IA: ask, historial, reportes."""

import json
import re
import time
import unicodedata

import frappe

from fc_molduplastic_frappe_ia.molduplastic_ia.llm_factory import (
	MENSAJE_FUERA_DE_ALCANCE,
	LLMConfigError,
	LLMProviderError,
	LLMTiempoAgotado,
	ALIAS_NO_RECUPERADAS,
	_es_listado,
	_filas_legibles,
	_etiqueta_objetivo,
	objetivo_efectividad,
	_listado_sin_cifras,
	_porcentajes,
	_valor_legible,
	explain,
	generate_spec,
	totales_no_recuperadas,
)
from fc_molduplastic_frappe_ia.molduplastic_ia.permissions import (
	_alcance_efectivo,
	get_tenant_scope,
	get_user_aseguradoras,
	lista_aseguradoras,
)
from fc_molduplastic_frappe_ia.molduplastic_ia.intencion import clasificar
from fc_molduplastic_frappe_ia.molduplastic_ia.query_executor import ejecutar
from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import (
	ALIAS_ESTADO,
	ESTADOS,
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

# Tipos de gráfica que el chat puede pedir. Los cinco son de frappe.Chart: las
# barras apiladas son `bar` + barOptions.stacked, no un tipo aparte.
TIPOS_GRAFICA = ("bar", "bar_stacked", "line", "pie", "donut")

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
# "aseguradora 7", "aseguradora numero 7", "aseguradora = 7", "aseguradora":
# ["=", "7"] (formato SQL/JSON de una inyección). El texto llega normalizado
# (sin acentos, minúsculas), así que "número" aparece como "numero".
RE_ASEGURADORA_ID = re.compile(
	r"aseguradora[\s\"'\[\](){}:=#,.;-]*(?:(?:numero|id)[\s\"'\[\](){}:=#,.;-]*)?(\d{1,2})(?![0-9])"
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
	"recolectadas_inconsistentes",
	"recolectadas_periodo",
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
	"piezades": "Nombre pieza",
	"motivo": "Motivo",
	"id_estatus": "Estatus",
	"estado": "Estado",
	"id_recoleccion": "Recolección",
	"siniestro": "Siniestro",
	"pais_origen": "País de origen",
	"estadolocalidad": "Estado",
}


def _sin_acentos(texto):
	return "".join(
		c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn"
	).lower()


def _limpio(texto):
	"""Sin acentos y con el caso original (los códigos de plaza van en mayúsculas)."""
	return "".join(
		c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn"
	)


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
	for m in re.finditer(RE_ASEGURADORA_ID, texto):
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


AVISO_ANIO = "No había datos en el período seleccionado; se muestra el año en curso."


def _pide_efectividad(pregunta):
	"""True para cualquier redacción de efectividad (tasa, nivel, cumplimiento...).

	El dueño pidió el 2026-10-31 que toda pregunta de efectividad se conteste con
	las cuatro cifras (Recolectadas sobre Ingresadas) y no con un conteo suelto ni
	con el resumen: si la redacción no entraba aquí, el spec del modelo se usaba
	tal cual y la respuesta salía sin porcentaje.
	"""
	if _es_top_recoleccion(pregunta) or _es_comparacion(pregunta):
		return True
	t = _sin_acentos(pregunta)
	if any(
		frase in t
		for frase in (
			"efectividad",
			"porcentaje de recoleccion",
			"porcentaje recolectado",
			"porcentaje de recolectadas",
			"porcentaje de piezas recolectadas",
			"contra la meta",
			"a la meta",
			"meta de recoleccion",
			"objetivo",
			"nivel de recoleccion",
			"tasa de recoleccion",
			"indice de recoleccion",
			"cumplimiento de recoleccion",
			"cumplimiento del objetivo",
			"cumplimiento de la meta",
			"avance de la recoleccion",
			"avance de recoleccion",
		)
	):
		return True
	# "¿cuánto se recolectó de lo que ingresó?" / "recolectadas contra ingresadas".
	return bool(
		re.search(
			r"cuanto\s+se\s+recolect"
			r"|recolect\w*\s+(?:sobre|respecto\s+a|vs\.?|contra)\s+(?:lo\s+)?(?:ingresad|ingreso)"
			r"|(?:ingresad|ingreso)\w*\s+(?:vs\.?|contra)\s+recolect",
			t,
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


def _pide_recolectan_mas(pregunta):
	""""más se recolectan" pide la tabla de efectividad (dueño, 2026-11-05).

	Es la forma plural de "¿qué X recolecta más?": se contesta con Ingresadas,
	Recolectadas y Efectividad, no con una sola línea. No aplica a las
	comparaciones ("X recolecta más que Y"), que cierran con el veredicto.
	"""
	if _es_comparacion(pregunta):
		return False
	return bool(
		re.search(
			r"\bmas\s+(?:se\s+)?recolect|recolect\w*\s+mas\b(?!\s+de\s+\d)"
			# "región con mayor recolección de piezas" es la misma pregunta
			# (dueño, 2026-11-07): pide las cuatro columnas, no una cifra suelta.
			r"|(?:mayor|mas)\s+(?:volumen\s+de\s+)?recoleccion",
			_sin_acentos(pregunta or ""),
		)
	)


# "¿qué taller/región/pieza...?" -> campo real del group_by. El orden importa:
# "tipo de pieza" antes que "parte", "tipo de CDR" antes que "taller".
DIMENSIONES_DE_LA_PREGUNTA = (
	("tipo de pieza", "parte_pieza"),
	("tipo de autoparte", "parte_pieza"),
	("tipo de refaccion", "parte_pieza"),
	("misma pieza", "parte_pieza"),
	("tipo de refacciones", "parte_pieza"),
	("refacciones", "parte_pieza"),
	("refaccion", "parte_pieza"),
	("parte", "parte_pieza"),
	("tipo de cdr", "tipo_carga"),
	("tipo cdr", "tipo_carga"),
	("agencia", "tipo_carga"),
	("taller", "taller_asignado"),
	("plaza", "id_plaza"),
	("estado", "estadolocalidad"),
	("region", "region_pais"),
	("pais de origen", "pais_origen"),
	("pais", "pais_origen"),
	("marca", "marca"),
	("submarca", "tipo"),
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


def _numero_por_singular_o_plural(pregunta):
	"""“¿cuál es …?” = una fila; “¿cuáles son …?” = la lista (dueño, 2026-11-05).

	Devuelve 1, 10 o None. Con None cada detección conserva su default (1 en los
	tops de efectividad, 10 en los rankings), así que la distinción solo cambia
	la respuesta cuando la frase es explícita.
	"""
	texto = _sin_acentos(pregunta or "")
	if re.search(r"\bcuales\s+son\b", texto):
		return 10
	if re.search(r"\bcual\s+es\b", texto):
		return 1
	return None


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
	"cuatrimestre",
	"semestre",
	"anual",
	"ano pasado",
	"ano anterior",
	"historico",
	"acumulado",
	"toda la base",
	"todos los datos",
	"ultimos",
	"ultimas",
)

# "Lo que va del año" es un rango del servidor (1 de enero → hoy), no el período
# del selector. Se reconocen las redacciones habituales; un año explícito
# ("en el año 2025") o un año pasado/anterior no entran aquí y los resuelve el
# modelo como siempre.
_ANIO_EN_CURSO = (
	"este ano",
	"en el ano",
	"del ano",
	"ano en curso",
	"ano actual",
	"ano corriente",
	"presente ano",
	"lo que va de ano",
	"lo que llevamos de ano",
)


def _pide_anio_en_curso(pregunta):
	"""True para "en lo que va del año", "este año", "del año en curso"."""
	texto = _sin_acentos(pregunta or "").lower()
	if re.search(r"\b(?:19|20)\d{2}\b", texto):
		return False
	if any(p in texto for p in ("ano pasado", "ano anterior", "ultimo ano", "ultimos anos")):
		return False
	return any(p in texto for p in _ANIO_EN_CURSO)


# Señales de comparación entre grupos (la decisión del dueño: sin período, año).
_COMPARACION = (
	"mejor que",
	"peor que",
	# "recolectan más que los talleres" es una comparación, no un top.
	" mas que",
	" menos que",
	"compar",
	" versus ",
	" vs ",
	"contra los",
	"contra las",
)


def _trae_periodo(pregunta):
	"""True si la pregunta fija un período ("octubre", "2026", "este mes"...)."""
	texto = _sin_acentos(pregunta)
	if _pide_anio_en_curso(pregunta):
		return True
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


# Períodos pre-seleccionados del selector que vive encima de la caja de texto
# del chat. La clave viaja en cada `ask`; el servidor la traduce a fechas.
PERIODOS_CONSULTA = (
	("esta_semana", "Esta semana (comenzando por el lunes)"),
	("ultimos_7", "Últimos 7 días"),
	("semana_pasada", "La semana pasada"),
	("ultimos_30", "Últimos 30 días"),
	("este_mes", "Este mes"),
	("mes_pasado", "El mes pasado"),
	("ultimos_90", "Últimos 90 días"),
	("este_anio", "Este año"),
	("ultimos_365", "Últimos 365 días"),
)
PERIODO_DEFAULT = "este_mes"
# El usuario elige las fechas: la clave viaja con `desde`/`hasta` (YYYY-MM-DD).
PERIODO_PERSONALIZADO = "personalizado"


def _dia(valor):
	return frappe.utils.getdate(valor).strftime("%Y-%m-%d")


def _rango_personalizado(desde, hasta):
	"""Rango elegido a mano, o None si las fechas no sirven.

	`getdate` normaliza cualquier entrada (nunca se interpola texto en SQL) y
	descarta basura; el fin se cierra a las 23:59:59 del día elegido.
	"""
	if not desde or not hasta:
		return None
	try:
		d1 = frappe.utils.getdate(desde)
		d2 = frappe.utils.getdate(hasta)
	except Exception:
		return None
	if not d1 or not d2 or d2 < d1:
		return None
	return f"{_dia(d1)} 00:00:00", f"{_dia(d2)} 23:59:59"


def _rango_de_periodo(clave, desde=None, hasta=None):
	"""Desde/hasta del período pedido, o None si la clave no existe.

	Los relativos ("últimos N días") cuentan hoy incluido; las semanas empiezan
	en lunes. Los rangos que llegan al presente terminan hoy a las 23:59:59. El
	período personalizado usa las fechas que manda el usuario.
	"""
	if clave == PERIODO_PERSONALIZADO:
		return _rango_personalizado(desde, hasta)
	claves = {k for k, _ in PERIODOS_CONSULTA}
	if clave not in claves:
		return None
	hoy = frappe.utils.getdate(frappe.utils.nowdate())
	fin = f"{_dia(hoy)} 23:59:59"
	if clave == "esta_semana":
		return f"{_dia(frappe.utils.add_days(hoy, -hoy.weekday()))} 00:00:00", fin
	if clave == "semana_pasada":
		lunes = frappe.utils.add_days(hoy, -(hoy.weekday() + 7))
		return f"{_dia(lunes)} 00:00:00", f"{_dia(frappe.utils.add_days(lunes, 6))} 23:59:59"
	if clave.startswith("ultimos_"):
		dias = int(clave.split("_")[1])
		return f"{_dia(frappe.utils.add_days(hoy, -(dias - 1)))} 00:00:00", fin
	if clave == "este_mes":
		return f"{_dia(frappe.utils.get_first_day(hoy))} 00:00:00", fin
	if clave == "mes_pasado":
		primero = frappe.utils.add_months(hoy, -1)
		return (
			f"{_dia(frappe.utils.get_first_day(primero))} 00:00:00",
			f"{_dia(frappe.utils.get_last_day(primero))} 23:59:59",
		)
	if clave == "este_anio":
		return f"{hoy.year}-01-01 00:00:00", fin
	return None


def _condiciones(valor):
	"""Condiciones de un filtro: una sola o una lista de condiciones (AND)."""
	if isinstance(valor, list) and valor and isinstance(valor[0], list):
		return valor
	return [valor]


def _es_between(cond):
	return (
		isinstance(cond, list)
		and len(cond) == 3
		and str(cond[0]).lower() == "between"
	)


# El año en trimestres (4 de 3 meses) y cuatrimestres (3 de 4 meses). El dueño
# los pidió el 2026-11-07: "divide el año en trimestres y detecta el trimestre
# actual, también en cuatrimestres y detecta el cuatrimestre actual".
MESES_POR_TRIMESTRE = 3
MESES_POR_CUATRIMESTRE = 4
ORDINALES_BLOQUE = {
	"primer": 1,
	"primero": 1,
	"primera": 1,
	"1er": 1,
	"1ro": 1,
	"1": 1,
	"segundo": 2,
	"segunda": 2,
	"2do": 2,
	"2": 2,
	"tercer": 3,
	"tercero": 3,
	"tercera": 3,
	"3er": 3,
	"3ro": 3,
	"3": 3,
	"cuarto": 4,
	"cuarta": 4,
	"4to": 4,
	"4": 4,
}


def _rango_de_bloque(anio, indice, meses):
	"""Rango del bloque `indice` (1..12/meses) del año, con `meses` de duración."""
	primero = frappe.utils.getdate(f"{anio}-{(indice - 1) * meses + 1:02d}-01")
	ultimo = frappe.utils.get_last_day(frappe.utils.add_months(primero, meses - 1))
	return f"{_dia(primero)} 00:00:00", f"{_dia(ultimo)} 23:59:59"


def _bloque_de_la_pregunta(pregunta):
	"""Rango del trimestre/cuatrimestre que nombra la pregunta, o None.

	Trimestres: 4 de 3 meses (ene-mar, abr-jun, jul-sep, oct-dic).
	Cuatrimestres: 3 de 4 meses (ene-abr, may-ago, sep-dic).
	Sin ordinal ("del trimestre", "del trimestre actual", "este cuatrimestre")
	toma el bloque de hoy; con ordinal ("primer", "2do", "3er", "cuarto") el
	indicado del año que diga la pregunta (o el año en curso). "Último"/"pasado"
	toma el bloque anterior.
	"""
	texto = _sin_acentos(pregunta or "")
	m = re.search(r"\b(trimestres?|cuatrimestres?)\b", texto)
	if not m:
		return None
	es_cuatrimestre = m.group(1).startswith("cuatri")
	meses = MESES_POR_CUATRIMESTRE if es_cuatrimestre else MESES_POR_TRIMESTRE
	total = 12 // meses
	hoy = frappe.utils.getdate(frappe.utils.nowdate())
	anio_match = re.search(r"\b(20\d{2})\b", texto)
	anio = int(anio_match.group(1)) if anio_match else hoy.year
	actual = (hoy.month - 1) // meses + 1
	previo = texto[: m.start()]
	if (
		m.group(1).endswith("s")
		and not re.search(
			r"\b(actual|corriente|este|esta|en\s+curso|ultimo|ultima|pasado|pasada|anterior)\b",
			texto,
		)
		and not re.search(
			r"\b(primer|primero|primera|segundo|segunda|tercer|tercero|tercera|cuarto|cuarta"
			r"|1er|1ro|2do|3er|3ro|4to|4ta|[1-4])\b",
			previo,
		)
	):
		# "crecimiento entre trimestres" / "variación por cuatrimestres": la
		# palabra indica la granularidad, no un bloque concreto.
		return None
	ordinales = re.findall(
		r"\b(primer|primero|primera|segundo|segunda|tercer|tercero|tercera|cuarto|cuarta"
		r"|1er|1ro|2do|3er|3ro|4to|4ta|[1-4])\b",
		previo,
	)
	if ordinales:
		indice = ORDINALES_BLOQUE.get(ordinales[-1], actual)
	elif re.search(r"\b(ultimo|ultima|pasado|pasada|anterior)\b", previo):
		indice = actual - 1
		if indice < 1:
			indice, anio = total, anio - 1
	else:
		indice = actual
	if not 1 <= indice <= total:
		return None
	desde, hasta = _rango_de_bloque(anio, indice, meses)
	if indice == actual and anio == hoy.year and not ordinales:
		# "Trimestre actual"/"cuatrimestre actual": hasta hoy, como "este mes"
		# (hay piezas con fechaingreso adelantada que no deben entrar).
		hasta = f"{_dia(hoy)} 23:59:59"
	return desde, hasta


def _aplicar_periodo(spec, clave, pregunta, desde=None, hasta=None):
	"""Fija el período del selector en todas las fechas del spec.

	El selector es el período de la conversación: se manda en cada `ask` y
	aplica a las preguntas que no traen un período escrito. Si la pregunta lo
	nombra ("en septiembre", "el mes pasado"), manda el texto: el selector es el
	default, no una camisa de fuerza; la respuesta devuelve el rango consultado
	para que el chat acomode el selector a esa fecha. "En lo que va del año"
	("este año", "del año en curso") también manda sobre el selector, pero el
	rango lo fija el servidor (1 de enero → hoy), no el modelo.

	Re-ancla cada `between` de un campo de fecha (global y de cada agregación).
	Las comparaciones contra un extremo del rango viejo (`fechaingreso < desde`
	de las rezagadas) se mueven con él. Si una medida no trae ninguna fecha, se
	agrega el filtro por defecto del DocType (`fechaingreso` en Pieza,
	`fecha_inicio` en Recoleccion); en Taller no hay fecha que aplicar. Los
	listados de campos sin fechas no se acotan (perderían filas del catálogo).
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if _pide_ficha_taller(pregunta):
		return spec
	if _pide_porcentaje_por_dimension(pregunta) and not (
		_trae_periodo(pregunta) or _trae_rango_de_fechas(pregunta)
	):
		# El ranking de porcentajes por dimensión usa el año cuando la pregunta
		# no trae período: el selector no lo debe dejar en el mes.
		return spec
	if _pide_mezcla_volumen_efectividad(pregunta):
		# "mucho volumen y baja efectividad": su ventana ya está fijada (el año
		# cuando la pregunta no trae período).
		return spec
	if _es_crecimiento_siniestros(spec) or (
		spec.get("group_by") == ["mes"] and len(spec.get("aggregations") or []) == 1
		and any(
			str(a.get("alias") or "").lower() in MEDIDAS_DE_CRECIMIENTO
			for a in spec.get("aggregations") or []
		)
	) or (
		len(spec.get("group_by") or []) == 2
		and str((spec.get("group_by") or [""])[-1]) == "mes"
		and any(
			str(a.get("alias") or "").lower() in MEDIDAS_DE_CRECIMIENTO
			for a in spec.get("aggregations") or []
		)
	):
		# La serie mensual trae su propia ventana (lo que va del año o el rango
		# pedido): el período del selector la dejaría en un solo mes.
		return spec
	bloque = _bloque_de_la_pregunta(pregunta)
	if bloque:
		# "Primer trimestre", "cuatrimestre actual": el rango lo fija el
		# servidor (no el modelo ni el selector), igual que "lo que va del año".
		clave, desde, hasta = PERIODO_PERSONALIZADO, bloque[0], bloque[1]
	elif _pide_anio_en_curso(pregunta):
		# "En lo que va del año" lo resuelve el servidor: 1 de enero → hoy. Antes
		# caía al período del selector y devolvía su rango (p. ej. "últimos 90
		# días") con la cifra del turno anterior.
		clave, desde, hasta = "este_anio", None, None
	elif _trae_periodo(pregunta) or _trae_rango_de_fechas(pregunta):
		return spec
	rango = _rango_de_periodo(clave, desde, hasta)
	if not rango:
		return spec
	desde, hasta = rango

	def fuentes():
		yield spec.get("filters") if isinstance(spec.get("filters"), dict) else {}
		for agg in spec.get("aggregations") or []:
			if isinstance(agg, dict) and isinstance(agg.get("filtros"), dict):
				yield agg["filtros"]

	# Extremos viejos: sirven para mover las comparaciones que los referencian
	# (la rezagada trae `fechaingreso < desde`).
	inicios, fines = set(), set()
	for filtros in fuentes():
		for campo in CAMPOS_DE_FECHA:
			for cond in _condiciones(filtros.get(campo)):
				if _es_between(cond):
					inicios.add(str(cond[1]))
					fines.add(str(cond[2]))

	encontrado = False

	def reescribir(filtros):
		nonlocal encontrado
		for campo in CAMPOS_DE_FECHA:
			if campo not in filtros:
				continue
			valor = filtros[campo]
			es_lista = isinstance(valor, list) and valor and isinstance(valor[0], list)
			nuevas = []
			for cond in _condiciones(valor):
				if _es_between(cond):
					cond = ["between", desde, hasta]
					encontrado = True
				elif (
					isinstance(cond, list)
					and len(cond) == 2
					and str(cond[0]).lower() in (">", ">=", "<", "<=")
				):
					referencia = str(cond[1])
					if referencia in inicios:
						cond = [cond[0], desde]
					elif referencia in fines:
						cond = [cond[0], hasta]
				nuevas.append(cond)
			if es_lista:
				filtros[campo] = nuevas
			elif nuevas:
				filtros[campo] = nuevas[0]

	for filtros in fuentes():
		reescribir(filtros)

	if not encontrado and spec.get("aggregations"):
		# Solo las medidas se acotan por defecto: un listado de campos (catálogo
		# de plazas, talleres...) no debe perder filas por no tener piezas en el
		# período, y además el prompt ya pide un período en toda consulta.
		campo = {"Pieza": "fechaingreso", "Recoleccion": "fecha_inicio"}.get(
			spec.get("doctype") or "Pieza"
		)
		if campo:
			filtros = dict(spec.get("filters") or {})
			if campo not in filtros:
				filtros[campo] = ["between", desde, hasta]
				spec["filters"] = filtros
	return spec


MESES_ES = (
	"enero",
	"febrero",
	"marzo",
	"abril",
	"mayo",
	"junio",
	"julio",
	"agosto",
	"septiembre",
	"octubre",
	"noviembre",
	"diciembre",
)


def _entre_mes_anterior_y_actual(pregunta):
	"""True para "entre el mes anterior y el mes actual" (un solo rango).

	El dueño lo pidió el 2026-10-31: esa frase no son dos resúmenes, es un rango
	con fecha inicio en el primer día del mes anterior y fecha fin en el último
	día del mes en curso.
	"""
	texto = _sin_acentos(pregunta or "")
	return bool(
		re.search(
			r"entre\s+el\s+mes\s+(?:anterior|pasado)\s+y\s+(?:el\s+)?(?:mes\s+)?(?:actual|este)",
			texto,
		)
		or re.search(
			r"del\s+mes\s+(?:anterior|pasado)\s+al\s+(?:mes\s+)?(?:actual|presente)", texto
		)
	)


def _periodo_dos_meses():
	"""Fechas del rango mes anterior + mes actual (inicio y fin inclusive)."""
	hoy = frappe.utils.getdate(frappe.utils.nowdate())
	desde = frappe.utils.get_first_day(frappe.utils.add_months(hoy, -1))
	hasta = frappe.utils.get_last_day(hoy)
	return f"{desde} 00:00:00", f"{hasta} 23:59:59"


def _texto_periodo_dos_meses():
	"""Etiqueta con los extremos del rango, pedida por el dueño (2026-10-31)."""
	desde, hasta = _periodo_dos_meses()
	d1 = frappe.utils.getdate(desde)
	d2 = frappe.utils.getdate(hasta)
	return (
		f"Del {d1.day} de {MESES_ES[d1.month - 1]} de {d1.year}"
		f" al {d2.day} de {MESES_ES[d2.month - 1]} de {d2.year}"
	)


CAMPOS_DE_FECHA = (
	"fechaingreso",
	"fecha_hora_recoleccion",
	"fecha_inicio",
	"fecha_completado",
	"fechaentrega",
	"fecha_hora_carga",
)


def _trae_rango_de_fechas(pregunta):
	"""True cuando la pregunta fija un rango explícito ("del 1 al 15 de septiembre").

	El dueño lo pidió el 2026-10-31: toda pregunta con rango de fechas muestra en
	el chat el mensaje con esas fechas.
	"""
	if _entre_mes_anterior_y_actual(pregunta):
		return True
	texto = _sin_acentos(pregunta or "").lower()
	if len(re.findall(r"\d{4}-\d{2}-\d{2}", texto)) >= 2:
		return True
	ancla = rf"(?:\d|{'|'.join(MESES_ES)}|mes|ano|semana|trimestre|hoy|ayer)"
	return bool(
		re.search(rf"\bdel?\s+{ancla}.*\bal\s+{ancla}", texto)
		or re.search(rf"\bentre\s+(?:el\s+)?{ancla}.*\by\s+(?:el\s+)?{ancla}", texto)
		or re.search(rf"\bdesde\s+(?:el\s+)?{ancla}.*\bhasta\s+(?:el\s+)?{ancla}", texto)
	)


def _rango_del_spec(spec):
	"""Primer `between` de un campo de fecha del spec (global o de una agregación)."""
	fuentes = [spec.get("filters")]
	fuentes += [a.get("filtros") for a in spec.get("aggregations") or [] if isinstance(a, dict)]
	for filtros in fuentes:
		if not isinstance(filtros, dict):
			continue
		for campo in CAMPOS_DE_FECHA:
			valor = filtros.get(campo)
			if (
				isinstance(valor, list)
				and len(valor) == 3
				and str(valor[0]).lower() == "between"
			):
				return valor[1], valor[2]
	return None


def _fecha_legible(valor):
	""""1 de septiembre de 2026" (con hora solo si el límite no es de día completo)."""
	try:
		d = frappe.utils.getdate(valor)
	except Exception:
		return str(valor)
	texto = f"{d.day} de {MESES_ES[d.month - 1]} de {d.year}"
	hora = str(valor)[11:19]
	hoy = frappe.utils.getdate(frappe.utils.nowdate())
	# La hora solo cuando el límite es una hora concreta: en "este mes" el fin es
	# "hoy a esta hora" y pintar "18:27" en el encabezado era ruido.
	if hora and hora not in ("00:00:00", "23:59:59") and d != hoy:
		texto += f" {hora[:5]}"
	return texto


def _texto_periodo(pregunta, spec):
	"""Etiqueta "Fecha inicio: … · Fecha fin: …" o None si no aplica.

	Sale con un rango explícito y también cuando el período es un mes o un año
	("septiembre", "2026", "este mes", "hoy"): el dueño lo pidió el 2026-10-31.
	Sin período en la pregunta no se muestra (el default es el mes en curso).
	"""
	if _entre_mes_anterior_y_actual(pregunta):
		return _texto_periodo_dos_meses()
	rango = _rango_del_spec(spec or {})
	if not rango:
		return None
	return f"Del {_fecha_legible(rango[0])} al {_fecha_legible(rango[1])}"


def _titulo_respuesta(spec):
	"""Título de lo que se muestra, con "(Top N)" cuando el resultado es un top."""
	base = _titulo_respuesta_base(spec)
	limite = int(spec.get("limit") or 0)
	if "top" in base.lower():
		return base
	# Con una sola fila ("lo que menos se recolecta") no se anuncia Top 1: la
	# respuesta ya lo dice en la frase.
	if (spec.get("group_by") or spec.get("fields")) and 1 < limite <= 20:
		return f"{base} (Top {limite})"
	return base


def _titulo_respuesta_base(spec):
	"""Título de lo que se muestra (primera línea de la respuesta).

	El dueño lo pidió el 2026-10-31: la primera línea dice qué se está
	mostrando y la segunda el período. Se arma del spec (medida + dimensión)
	para que no dependa del criterio del modelo.
	"""
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	alias = [str(a.get("alias") or "").lower() for a in aggs]
	dims = [str(g).lower() for g in spec.get("group_by") or []]
	etiquetas = [ETIQUETAS_DIMENSION.get(d, d.replace("_", " ").title()) for d in dims]
	if _es_ranking_partes(spec):
		return "Partes más ingresadas"
	if _es_preferencia_region(spec):
		etiqueta = "Submarca" if dims[-1] == "tipo" else "Marca"
		return f"{etiqueta} con más recolectadas por Región"
	if alias == ["siniestros"] and dims == ["modelo"]:
		return "Años modelo con más siniestros"
	if alias == ["siniestros"] and dims and dims[-1] == "mes":
		if len(dims) == 1:
			etiqueta = _etiqueta_del_filtro(spec)
			return f"Crecimiento de siniestros por mes {etiqueta}".strip()
		dim = ETIQUETAS_DIMENSION.get(dims[0], dims[0].replace("_", " ").title())
		return f"Crecimiento de siniestros por {dim}"
	if alias == ["siniestros"] and dims == ["pais_origen"]:
		return "Países de origen con más siniestros"
	if alias == ["siniestros"] and dims in (["marca"], ["tipo"]):
		return (
			"Submarcas con más siniestros"
			if dims == ["tipo"]
			else "Marcas con más siniestros"
		)
	if _es_promedio_ratio(spec):
		return "Promedio"
	if alias == ["piezas", "siniestros"]:
		etiqueta = _etiqueta_del_filtro(spec)
		return f"Promedio de piezas por siniestro {etiqueta}".strip()
	if alias == ["ingresadas"] and dims == ["tipo"]:
		return "Piezas ingresadas por Submarca"
	if _es_listado_mas_caras(spec):
		orden = (spec.get("order_by") or [{}])[0]
		asc = str(orden.get("dir") or "desc").lower() == "asc"
		singular = int(spec.get("limit") or 0) == 1
		if asc:
			return "Pieza más barata" if singular else "Piezas más baratas"
		return "Pieza más cara" if singular else "Piezas más caras"
	if spec.get("fields") and "preciovale" in [str(f).lower() for f in spec["fields"]]:
		return "Piezas pendientes por prioridad"
	if alias == ["pendientes"] and dims == ["siniestro"]:
		return "Siniestros con piezas pendientes"
	if {"total", "recolectadas"} <= set(alias) and len(aggs) <= 4:
		# La efectividad trae cuatro cifras; el resumen también tiene "total" y
		# "recolectadas", pero con las diez categorías.
		base = "Efectividad"
	elif len(aggs) > 3:
		base = "Valor de las piezas" if _son_sumas(aggs) else "Resumen de piezas"
	elif {"no_recolectadas", "inconsistentes_no_recolectadas", "rezagadas_no_recolectadas"} <= set(alias):
		base = "No recolectadas"
	elif {"recolectadas", "recolectadas_inconsistentes", "recolectadas_periodo"} <= set(alias):
		base = "Recolectadas"
	elif len(aggs) == 1:
		clave = alias[0]
		# Un count(*) genérico se titula "Piezas", no "Total".
		base = (
			"Piezas"
			if clave in ("total", "piezas", "ingresadas")
			else (ETIQUETAS_COLUMNA.get(clave) or clave.replace("_", " ").capitalize())
		)
	elif alias:
		base = " y ".join(
			(ETIQUETAS_COLUMNA.get(a) or a.replace("_", " ").capitalize()) for a in alias[:2]
		)
	else:
		base = "Piezas"
	if etiquetas:
		base += " por " + " y ".join(etiquetas)
	return base


def _son_sumas(aggs):
	return all(str(a.get("func") or "").lower() == "sum" for a in aggs)


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
		# El modelo a veces pone el período sobre fecha_hora_recoleccion ("más se
		# recolectan en septiembre"): es el mismo período de la pregunta.
		rango = _rango_del_spec(spec)
		if rango:
			return rango
		ahora = frappe.utils.now_datetime()
		inicio = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
		return inicio.strftime("%Y-%m-%d %H:%M:%S"), ahora.strftime("%Y-%m-%d %H:%M:%S")
	if isinstance(f, list) and len(f) == 3 and str(f[0]).lower() == "between":
		return f[1], f[2]
	return None


def _aggs_recolectadas(desde, hasta):
	"""A + B de "recolectada" (definición del dueño 2026-10-31).

	A1 "recolectadas en periodo" = ingresó en el período, estatus 100, recolectada
	en el mismo mes y no antes del ingreso. A2 inconsistentes recolectadas =
	ingresó en el período, estatus 100, recolectada antes del ingreso.
	B rezagadas recolectadas = recolectada en el período, ingresó antes.
	Las tres son disjuntas: Recolectadas = en periodo + inconsistentes + rezagadas.
	"""
	return [
		{
			"func": "count",
			"field": "*",
			"alias": "recolectadas",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"id_estatus": ["=", 100],
				"recolectable": ["=", 1],
				"fecha_hora_recoleccion": [
					["mes_igual", {"campo": "fechaingreso"}],
					[">=", {"campo": "fechaingreso"}],
				],
			},
		},
		{
			"func": "count",
			"field": "*",
			"alias": "recolectadas_inconsistentes",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"id_estatus": ["=", 100],
				"recolectable": ["=", 1],
				"fecha_hora_recoleccion": ["<", {"campo": "fechaingreso"}],
			},
		},
		{
			"func": "count",
			"field": "*",
			"alias": "recolectadas_periodo",
			"filtros": {
				"fecha_hora_recoleccion": ["between", desde, hasta],
				"fechaingreso": ["<", desde],
				"id_estatus": ["=", 100],
				"recolectable": ["=", 1],
			},
		},
	]


def _aggs_efectividad(desde, hasta):
	"""Ingresadas + la unión de recolectadas; la efectividad la calcula el código."""
	return [
		{
			"func": "count",
			"field": "*",
			"alias": "total",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"recolectable": ["=", 1],
			},
		},
		*_aggs_recolectadas(desde, hasta),
	]


def _pide_mapa_efectividad(pregunta):
	"""“Muestra el mapa de efectividad”: tabla de efectividad con semáforo."""
	return bool(re.search(r"\bmapa\s+(?:de\s+)?(?:la\s+)?efectividad\b", _sin_acentos(pregunta or "")))


def _circulo_de_efectividad(pct):
	"""Semáforo del mapa: verde ≥ 75%, amarillo 50-74.9%, rojo < 50%."""
	if pct >= objetivo_efectividad():
		return "🟢"
	if pct >= 50:
		return "🟡"
	return "🔴"


def _completar_mapa_efectividad(spec, pregunta):
	"""El “mapa de efectividad” es la efectividad por dimensión con semáforo.

	El dueño lo pidió el 2026-11-09: "cuando diga muestra mapa de efectividad
	agregarás una columna más, pondrás un círculo verde para la efectividad
	mayor o igual al 75%, amarillo menor que 75% pero mayor o igual que 50%, y
	un círculo rojo para efectividad menor al 50%". Si la pregunta no nombra la
	dimensión, el mapa es por región; se muestra completa (sin corte).
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_mapa_efectividad(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	spec.pop("min_ingresadas", None)
	spec.pop("order_by", None)
	spec.pop("render_chart", None)
	if not spec.get("group_by"):
		spec["group_by"] = [(_dimension_por_frase(pregunta) or "region_pais")]
	else:
		dimension = _dimension_por_frase(pregunta)
		if dimension:
			spec["group_by"] = [dimension]
	spec["limit"] = 50
	return spec


def _tabla_mapa_efectividad(spec, resultado):
	"""Tabla de efectividad + la columna del semáforo (🟢/🟡/🔴)."""
	porcentajes = [
		p for p in _porcentajes(spec, resultado) if (p.get("total") or 0) > 0
	]
	tabla = _tabla_efectividad(spec, porcentajes)
	if not tabla:
		return None
	columnas, filas = tabla
	# De mayor a menor efectividad: los verdes arriba y los rojos al final.
	pares = sorted(zip(filas, porcentajes), key=lambda par: par[1].get("pct") or 0, reverse=True)
	filas = [
		list(fila) + [_circulo_de_efectividad(p.get("pct") or 0)] for fila, p in pares
	]
	return columnas + ["Mapa"], filas


PALABRAS_VOLUMEN_ALTO = (
	"mucho volumen",
	"alto volumen",
	"volumen alto",
	"gran volumen",
	"volumen importante",
	"mucho movimiento",
	"muchos ingresos",
	"mas volumen",
	"mayor volumen",
	"volumenes altos",
	"mucho flujo",
)
PALABRAS_VOLUMEN_BAJO = (
	"bajo volumen",
	"volumen bajo",
	"poco volumen",
	"pocos ingresos",
	"bajo movimiento",
	"menos volumen",
	"menor volumen",
	"poco flujo",
)
PALABRAS_EFECTIVIDAD_BAJA = (
	"baja efectividad",
	"efectividad baja",
	"baja recoleccion",
	"recoleccion baja",
	"poca efectividad",
	"efectividad pobre",
	"pobre efectividad",
	"mal porcentaje",
	"porcentaje bajo",
	"bajo porcentaje",
	"menor efectividad",
	"efectividad menor",
	"baja tasa",
	"tasa baja",
	"recolectan mal",
	"recolecta poco",
	"recolectan poco",
	"poco se recolecta",
)
PALABRAS_EFECTIVIDAD_ALTA = (
	"alta efectividad",
	"efectividad alta",
	"buena efectividad",
	"efectividad buena",
	"alta recoleccion",
	"recoleccion alta",
	"mejor efectividad",
	"efectividad mayor",
	"alto porcentaje",
	"porcentaje alto",
	"alta tasa",
	"tasa alta",
	"recolectan bien",
	"mucho se recolecta",
)


def _pide_mezcla_volumen_efectividad(pregunta):
	""""Mucho volumen pero baja efectividad" (y variantes).

	Devuelve (volumen_alto, efectividad_alta) o None. El dueño lo pidió el
	2026-11-09: "¿Qué estados tienen mucho volumen pero baja efectividad?" y sus
	variantes (mucho/poco volumen × alta/baja efectividad).
	"""
	texto = _sin_acentos(pregunta or "")
	volumen_alto = any(p in texto for p in PALABRAS_VOLUMEN_ALTO)
	volumen_bajo = any(p in texto for p in PALABRAS_VOLUMEN_BAJO)
	efectividad_baja = any(p in texto for p in PALABRAS_EFECTIVIDAD_BAJA)
	efectividad_alta = any(p in texto for p in PALABRAS_EFECTIVIDAD_ALTA)
	if (volumen_alto or volumen_bajo) and (efectividad_baja or efectividad_alta):
		return (volumen_alto, efectividad_alta)
	return None


def _completar_mezcla_volumen_efectividad(spec, pregunta):
	"""Efectividad por dimensión con la ventana del año si no hay período.

	"Mucho volumen" pide una población grande: sin período en la pregunta se
	usa el año en curso (no el mes), que es lo que hace visible la mezcla.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	mezcla = _pide_mezcla_volumen_efectividad(pregunta)
	if not mezcla:
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	dimension = _dimension_de_la_pregunta(pregunta) or _dimension_por_frase(pregunta)
	if not dimension or dimension == "siniestro":
		return spec
	spec["fields"] = []
	spec["group_by"] = [dimension]
	spec["limit"] = 500
	spec.pop("min_ingresadas", None)
	spec.pop("order_by", None)
	spec.pop("render_chart", None)
	if not (_trae_periodo(pregunta) or _trae_rango_de_fechas(pregunta)):
		desde, hasta = _periodo_anio_actual()
		spec["aggregations"] = _aggs_efectividad(desde, hasta)
		filtros = dict(spec.get("filters") or {})
		for campo in ("fechaingreso", "fecha_hora_recoleccion", "recolectable", "id_estatus"):
			filtros.pop(campo, None)
		spec["filters"] = filtros
	return spec


def _percentil(valores, porcentaje):
	"""Percentil por rango: el valor que deja `porcentaje`% de los casos abajo."""
	ordenados = sorted(valores)
	if not ordenados:
		return 0
	posicion = min(int(len(ordenados) * porcentaje / 100), len(ordenados) - 1)
	return ordenados[posicion]


def _tabla_mezcla_volumen_efectividad(spec, resultado, mezcla):
	"""Tabla de efectividad con los grupos de mucho/poco volumen y alta/baja.

	"Mucho volumen" = ingresadas por encima de la **mediana** de los grupos con
	datos; "baja efectividad" = por debajo del objetivo (75%). Devuelve
	(columnas, filas, umbral) o None.
	"""
	volumen_alto, efectividad_alta = mezcla
	porcentajes = [
		p for p in _porcentajes(spec, resultado) if (p.get("total") or 0) > 0
	]
	if not porcentajes:
		return None
	# "Mucho volumen" = cuartil superior; "bajo volumen" = cuartil inferior.
	umbral = _percentil(
		[p.get("total") or 0 for p in porcentajes], 75 if volumen_alto else 25
	)
	seleccion = [
		p
		for p in porcentajes
		if (
			((p.get("total") or 0) >= umbral)
			if volumen_alto
			else ((p.get("total") or 0) <= umbral)
		)
		and (
			((p.get("pct") or 0) >= objetivo_efectividad())
			if efectividad_alta
			else ((p.get("pct") or 0) < objetivo_efectividad())
		)
	]
	seleccion.sort(key=lambda p: p.get("total") or 0, reverse=True)
	tabla = _tabla_efectividad(spec, seleccion)
	if not tabla:
		return [], [], umbral
	return tabla[0], tabla[1], umbral


def _titulo_mezcla_volumen_efectividad(pregunta, spec, mezcla, umbral):
	volumen_alto, efectividad_alta = mezcla
	dimension = str((spec.get("group_by") or ["region_pais"])[0])
	plural = ETIQUETAS_PLURAL.get(
		dimension, ETIQUETAS_DIMENSION.get(dimension, dimension.replace("_", " ").title())
	)
	limite = "desde" if volumen_alto else "hasta"
	return (
		f"{plural} con {'mucho' if volumen_alto else 'bajo'} volumen"
		f" y {'alta' if efectividad_alta else 'baja'} efectividad"
		f" ({limite} {int(umbral)} piezas)"
	)


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
	if _pide_crecimiento_periodos(pregunta) or _pide_crecimiento_dimension(pregunta):
		# "compara el crecimiento de piezas entre trimestres" / "… por marca" es
		# una serie con variación, no la efectividad (que "compara" también
		# dispara).
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
	# El período y el recolectable van en cada agregación: la unión de
	# recolectadas mezcla las bases de fecha y un filtro global de fecha
	# acotaría la consulta de recolección.
	filtros = dict(spec.get("filters") or {})
	filtros.pop("fechaingreso", None)
	# El modelo mete el período de "recolectadas" y el estatus 100 en el WHERE
	# global cuando la frase habla de recolección; la fórmula ya los lleva dentro
	# de cada agregación y en el universo solo dejan cifras vacías.
	filtros.pop("fecha_hora_recoleccion", None)
	filtros.pop("recolectable", None)
	if filtros.get("id_estatus") == ["=", 100]:
		filtros.pop("id_estatus")
	spec["filters"] = filtros
	spec["fields"] = []
	spec["aggregations"] = _aggs_efectividad(desde, hasta)
	# El `render_chart` del modelo apunta a los alias viejos ("piezas"), que ya
	# no existen: `ejecutar` lo rechazaba con "render_chart.y debe ser una
	# columna del resultado" y la pregunta moría sin datos.
	spec.pop("render_chart", None)
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
			if _pide_recolectan_mas(pregunta):
				# "más se recolectan" pide la tabla (Ingresadas / Recolectadas /
				# Efectividad), no el primer renglón: misma regla de siempre para
				# el N (el de la pregunta, 10 por defecto).
				spec["limit"] = top or _numero_por_singular_o_plural(pregunta) or 10
			else:
				spec["limit"] = top or _numero_por_singular_o_plural(pregunta) or 1
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


def _pide_recolectadas(pregunta):
	texto = _sin_acentos(pregunta)
	if _pide_promedio(pregunta):
		return False
	if _pide_piezas_con_precio(pregunta) or _pide_solo_precios(pregunta):
		return False
	# "pendientes por recolectar" es "pendientes" (dueño, 2026-11-07): no es la
	# unión de recolectadas, aunque la frase traiga el verbo recolectar.
	if "pendiente" in texto:
		return False
	if "no recolectada" in texto or "no recuperada" in texto or "no entrega" in texto:
		return False
	# Una efectividad ya se contesta con las cuatro cifras: sus tres numeradores
	# no se reemplazan por el conteo suelto.
	if _pide_efectividad(pregunta):
		return False
	if "porcentaje" in texto or "resumen" in texto:
		return False
	# "¿Hay preferencia…?" se interpreta como piezas recolectadas (dueño,
	# 2026-11-04): el dueño usa la frase para preguntar por la preferencia de
	# recolección y la medida es la unión de recolectadas.
	if "hay preferencia" in texto:
		return True
	# Cubre "recolectadas", "se recolectaron", "se recolectó" y "se recolectan";
	# deja fuera "recolector" y "recolectable", que no miden piezas.
	palabras = re.findall(r"[a-z]+", texto)
	return any(
		p.startswith("recolect") and not p.startswith(("recolector", "recolectable"))
		for p in palabras
	)


def _completar_recolectadas(spec, pregunta):
	"""Conteo de piezas recolectadas: la unión se arma con tres agregaciones."""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_recolectadas(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_de_recoleccion(spec) or _periodo_mes_actual()
	# Los filtros propios de la pregunta (taller, plaza, región, marca...) se
	# conservan como globales: antes se borraban y "recolectadas del taller X"
	# devolvía el total del universo. Las fechas, el estatus y el recolectable
	# los pone cada categoría de la unión.
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "recolectable", "id_estatus"):
		filtros.pop(campo, None)
	spec["filters"] = filtros
	spec["fields"] = []
	spec["aggregations"] = _aggs_recolectadas(desde, hasta)
	# El modelo a veces ordena por un alias suyo ("piezas") que ya no existe: el
	# ranking lo arma explain, así que ese order_by se descarta.
	alias = {"recolectadas", "recolectadas_inconsistentes", "recolectadas_periodo"}
	orden = spec.get("order_by")
	if orden:
		campos = {
			str(o.get("field") or "").lower() for o in orden if isinstance(o, dict)
		}
		if not campos <= alias:
			spec.pop("order_by", None)
	# El gráfico del modelo apunta a alias que ya no existen: se descarta.
	spec.pop("render_chart", None)
	# "¿Hay preferencia … por taller/región/…?": la dimensión la fija el
	# vocabulario de la pregunta, no las dimensiones extra que agregue el modelo.
	if "hay preferencia" in _sin_acentos(pregunta):
		dimension = _dimension_de_la_pregunta(pregunta)
		if dimension:
			spec["group_by"] = [dimension]
	spec.setdefault("limit", 50)
	return spec


def _pide_no_recuperadas(pregunta):
	"""El total "No recolectadas" = periodo + inconsistentes no + rezagadas no.

	"No recolectadas en periodo" es la categoría suelta y no entra por acá.
	"No recuperadas" y "no entregadas" se siguen aceptando como sinónimos.
	"""
	texto = _sin_acentos(pregunta)
	if "no recuperada" in texto or "no entrega" in texto:
		return True
	if "no recolectada" in texto and "no recolectadas en periodo" not in texto:
		return True
	# "Motivos de no entrega" = el mismo universo de las tres categorías.
	return "motivo" in texto and "no recolect" in texto


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


def _periodo_de_recoleccion(spec):
	"""Período del spec cuando la medida es de recolección.

	"¿cuántas piezas se recolectaron en septiembre?" pone el `between` sobre
	`fecha_hora_recoleccion` (es el campo que mide la recolección), y
	`_periodo_del_spec` solo mira `fechaingreso`: sin esto el conteo caía al mes
	en curso y devolvía las piezas de octubre.
	"""
	fuentes = []
	if isinstance(spec.get("filters"), dict):
		fuentes.append(spec["filters"])
	for agg in spec.get("aggregations") or []:
		if isinstance(agg, dict) and isinstance(agg.get("filtros"), dict):
			fuentes.append(agg["filtros"])
	for filtros in fuentes:
		f = filtros.get("fecha_hora_recoleccion")
		if isinstance(f, list) and len(f) == 3 and str(f[0]).lower() == "between":
			return f[1], f[2]
	return _periodo_del_spec(spec)


def _pide_resumen(pregunta):
	"""El resumen de piezas y "¿cómo va la recolección?" son la misma pregunta."""
	texto = _sin_acentos(pregunta)
	return (
		"resumen de piezas" in texto
		or "como va la recoleccion" in texto
		or "como vamos con las recolecciones" in texto
		or "estado de la recoleccion" in texto
	)


def _aggs_resumen(desde, hasta):
	"""Las 10 agregaciones del resumen (8 categorías + las 3 de la unión)."""
	estatus_no = [200, 201, 202, 203, 204, 205, 206]
	return [
		{
			"func": "count",
			"field": "*",
			"alias": "total",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"recolectable": ["=", 1],
			},
		},
		*_aggs_recolectadas(desde, hasta),
		{
			"func": "count",
			"field": "*",
			"alias": "no_recolectadas",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"recolectable": ["=", 1],
				"fecha_hora_recoleccion": [">=", {"campo": "fechaingreso"}],
				"id_estatus": ["in", list(estatus_no)],
			},
		},
		{
			"func": "count",
			"field": "*",
			"alias": "pendientes",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"id_estatus": ["is", "null"],
				"fecha_hora_recoleccion": ["is", "null"],
				"recolectable": ["=", 1],
			},
		},
		{
			"func": "count",
			"field": "*",
			"alias": "inconsistentes_recolectadas",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"fecha_hora_recoleccion": ["<", {"campo": "fechaingreso"}],
				"id_estatus": ["=", 100],
			},
		},
		{
			"func": "count",
			"field": "*",
			"alias": "inconsistentes_no_recolectadas",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"fecha_hora_recoleccion": ["<", {"campo": "fechaingreso"}],
				"id_estatus": ["in", list(estatus_no)],
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
		{
			"func": "count",
			"field": "*",
			"alias": "rezagadas_no_recolectadas",
			"filtros": {
				"fecha_hora_recoleccion": ["between", desde, hasta],
				"fechaingreso": ["<", desde],
				"id_estatus": ["in", list(estatus_no)],
			},
		},
	]


def _completar_resumen(spec, pregunta):
	"""El resumen de piezas se arma en código: 10 agregaciones, no las del modelo."""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_resumen(pregunta):
		return spec
	# Una pregunta de efectividad no se contesta con el resumen (2026-10-31): si
	# la redacción pide tasa/nivel/cumplimiento, mandan las cuatro cifras.
	if _pide_efectividad(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	if _entre_mes_anterior_y_actual(pregunta):
		desde, hasta = _periodo_dos_meses()
	else:
		desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	spec["filters"] = {}
	spec["fields"] = []
	spec["aggregations"] = _aggs_resumen(desde, hasta)
	spec.setdefault("limit", 10)
	return spec


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


def _porcentaje_categorias_pedido(pregunta):
	"""(dimensión | None, categorías) del porcentaje pedido, o (None, None).

	El dueño lo pidió el 2026-11-09: "porcentaje de 'piezas no entregadas' con
	respecto 'ingresadas', y porcentaje de 'pendientes' con respecto
	'ingresadas'". El 2026-11-10 agregó el desglose: "¿Cuál es el CDR con mayor
	% de piezas no entregadas?" → una fila por CDR con su porcentaje.
	"""
	texto = _sin_acentos(pregunta or "")
	if not any(
		p in texto
		for p in ("porcentaje", "por ciento", "porciento", "%", "proporcion", "tasa de")
	):
		return None, None
	if " contra " in texto or _comparacion_clasificada(pregunta):
		return None, None
	categorias = []
	if re.search(r"no entregad|no recolectad|no recuperad", texto):
		categorias.append("no_entregadas")
	if "pendiente" in texto:
		categorias.append("pendientes")
	if not categorias:
		return None, None
	dimension = _dimension_por_frase(pregunta) or _dimension_de_la_pregunta(pregunta)
	if (
		not dimension
		and re.search(r"\bcdr\b", texto)
		and not re.search(r"tipo\s+(?:de\s+)?cdr", texto)
	):
		# "CDR" a secas es el taller; "tipo CDR" es `tipo_carga`.
		dimension = "taller_asignado"
	return dimension, categorias


def _pide_porcentaje_de_categorias(pregunta):
	"""Porcentaje **total** (sin dimensión) de no entregadas/pendientes."""
	dimension, categorias = _porcentaje_categorias_pedido(pregunta)
	return categorias if not dimension else None


def _pide_porcentaje_por_dimension(pregunta):
	"""(dimensión, categorías) del porcentaje por grupo, o None."""
	dimension, categorias = _porcentaje_categorias_pedido(pregunta)
	return (dimension, categorias) if dimension else None


def _completar_porcentaje_de_categorias(spec, pregunta):
	"""Ingresadas + las categorías pedidas, para sacar el porcentaje en código."""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	dimension, categorias = _porcentaje_categorias_pedido(pregunta)
	if not categorias:
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	if dimension and not (_trae_periodo(pregunta) or _trae_rango_de_fechas(pregunta)):
		# Un ranking de porcentajes necesita población: sin período en la
		# pregunta se usa el año en curso (el mes puede estar vacío).
		desde, hasta = _periodo_anio_actual()
	aggs = [
		{
			"func": "count",
			"field": "*",
			"alias": "ingresadas",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"recolectable": ["=", 1],
			},
		}
	]
	if "no_entregadas" in categorias:
		# Para un porcentaje el numerador tiene que ser un SUBCONJUNTO de las
		# ingresadas del período: las que traen un estatus de no entrega del
		# catálogo (200-206). La unión de tres categorías del conteo incluye
		# rezagadas (ingresaron antes), así que podía pasar del 100%.
		aggs.append(
			{
				"func": "count",
				"field": "*",
				"alias": "no_entregadas",
				"filtros": {
					"fechaingreso": ["between", desde, hasta],
					"recolectable": ["=", 1],
					"id_estatus": ["in", [200, 201, 202, 203, 204, 205, 206]],
				},
			}
		)
	if "pendientes" in categorias:
		aggs.append(
			{
				"func": "count",
				"field": "*",
				"alias": "pendientes",
				"filtros": {
					"fechaingreso": ["between", desde, hasta],
					"id_estatus": ["is", "empty"],
					"fecha_hora_recoleccion": ["is", "empty"],
					"recolectable": ["=", 1],
				},
			}
		)
	spec["filters"] = {}
	spec["fields"] = []
	spec["group_by"] = [dimension] if dimension else []
	spec["limit"] = 500 if dimension else 1
	spec["aggregations"] = aggs
	spec.pop("order_by", None)
	spec.pop("render_chart", None)
	spec.pop("min_ingresadas", None)
	return spec


def _tabla_porcentaje_de_categorias(spec, resultado, categorias):
	"""Filas [Categoría, Piezas, % de ingresadas] o None si no hay ingresadas."""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	filas_datos = resultado.get("filas") or []
	if not filas_datos:
		return None

	def valor(alias):
		return (filas_datos[0][columnas.index(alias)] or 0) if alias in columnas else 0

	ingresadas = valor("ingresadas")
	if not ingresadas:
		return None
	filas = [["Ingresadas", ingresadas, "100.0%"]]
	if "no_entregadas" in categorias:
		no_entregadas = valor("no_entregadas")
		filas.append(
			["No entregadas", no_entregadas, f"{no_entregadas / ingresadas * 100:.1f}%"]
		)
	if "pendientes" in categorias:
		pendientes = valor("pendientes")
		filas.append(
			["Pendientes", pendientes, f"{pendientes / ingresadas * 100:.1f}%"]
		)
	return ["Categoría", "Piezas", "% de ingresadas"], filas


ETIQUETAS_CATEGORIA_PORCENTAJE = {
	"no_entregadas": "No entregadas",
	"pendientes": "Pendientes",
}


def _tabla_porcentaje_por_dimension(spec, resultado, dimension, categorias, pregunta):
	"""Una fila por grupo con sus porcentajes, ordenada de mayor a menor."""
	columnas_datos = [str(c).lower() for c in (resultado.get("columnas") or [])]
	if str(dimension).lower() not in columnas_datos:
		return None
	i_dim = columnas_datos.index(str(dimension).lower())

	def valor(fila, alias):
		return (fila[columnas_datos.index(alias)] or 0) if alias in columnas_datos else 0

	minimo = _minimo_de_la_pregunta(pregunta) or 50
	texto = _sin_acentos(pregunta or "")
	ascendente = any(p in texto for p in ("menor", "menos", "peor", "baja"))
	filas = []
	for fila in resultado.get("filas") or []:
		if fila[i_dim] in (None, ""):
			# "Sin taller"/"Sin plaza" no es un taller: no puede ganar el ranking.
			continue
		ingresadas = valor(fila, "ingresadas")
		if not ingresadas or ingresadas < minimo:
			continue
		nueva = [str(fila[i_dim]), ingresadas]
		con_dato = False
		for categoria in categorias:
			cantidad = valor(fila, categoria)
			if cantidad:
				con_dato = True
			nueva += [cantidad, f"{cantidad / ingresadas * 100:.1f}%"]
		if not con_dato and not ascendente:
			# En "el que más" un grupo sin ninguna no aporta (misma regla que
			# los rankings: no se listan grupos en 0).
			continue
		filas.append(nueva)
	# El porcentaje de la primera categoría manda el orden.
	filas.sort(
		key=lambda f: float(str(f[3]).replace("%", "") or 0),
		reverse=not ascendente,
	)
	tope = _top_de_la_pregunta(pregunta) or _numero_por_singular_o_plural(pregunta) or 10
	columnas = [
		ETIQUETAS_DIMENSION.get(str(dimension), str(dimension).replace("_", " ").title()),
		"Ingresadas",
	]
	for categoria in categorias:
		etiqueta = ETIQUETAS_CATEGORIA_PORCENTAJE.get(categoria, categoria)
		columnas += [etiqueta, f"% {etiqueta.lower()}"]
	return columnas, filas[:tope], minimo


def _titulo_porcentaje_por_dimension(pregunta, dimension, categorias, minimo):
	texto = _sin_acentos(pregunta or "")
	ascendente = any(p in texto for p in ("menor", "menos", "peor", "baja"))
	plural = ETIQUETAS_PLURAL.get(
		str(dimension), ETIQUETAS_DIMENSION.get(str(dimension), str(dimension).title())
	)
	nombres = [ETIQUETAS_CATEGORIA_PORCENTAJE.get(c, c).lower() for c in categorias]
	cuantos = _numero_por_singular_o_plural(pregunta)
	base = (
		(f"{plural} con {'menor' if ascendente else 'mayor'} % de "
		+ " y ".join(nombres))
		if cuantos != 1
		else (
			f"{ETIQUETAS_DIMENSION.get(str(dimension), str(dimension).title())} con "
			f"{'menor' if ascendente else 'mayor'} % de "
			+ " y ".join(nombres)
		)
	)
	return f"{base} (con {int(minimo)} o más ingresadas)"


def _titulo_porcentaje_de_categorias(categorias):
	nombres = {"no_entregadas": "no entregadas", "pendientes": "pendientes"}
	pedidas = [nombres.get(c, c) for c in categorias]
	if len(pedidas) == 1:
		return f"Porcentaje de {pedidas[0]} sobre ingresadas"
	return f"Porcentaje de {' y '.join(pedidas)} sobre ingresadas"


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
		# Pieza.motivo está vacío en toda la base (3,3M de filas), así que el
		# dueño pidió apoyarse en id_estatus: el catálogo Estatus es lo único que
		# distingue estas piezas.
		spec["group_by"] = ["id_estatus"]
	spec["filters"] = {}
	spec["fields"] = []
	spec["aggregations"] = _aggs_no_recuperadas(desde, hasta)
	spec.setdefault("limit", 50)
	return spec


ESTATUS_NO_LOCALIZADA = 206


def _pide_no_localizadas(pregunta):
	""""Piezas no localizadas" = id_estatus 206 del catálogo Estatus."""
	return "no localizad" in _sin_acentos(pregunta or "")


def _completar_no_localizadas(spec, pregunta):
	"""Conteo de piezas no localizadas por la dimensión que pida la pregunta.

	El dueño lo pidió el 2026-10-31 ("¿cuántas piezas no localizadas tengo por
	taller?"): el filtro es el estatus 206 del catálogo, no el texto del motivo,
	y la dimensión se resuelve del vocabulario de la pregunta.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_no_localizadas(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	dimension = _dimension_de_la_pregunta(pregunta)
	if dimension and dimension != "region_pais":
		spec["group_by"] = [dimension]
	elif not spec.get("group_by"):
		spec["group_by"] = []
	spec["filters"] = {"fechaingreso": ["between", desde, hasta]}
	spec["fields"] = []
	spec["aggregations"] = [
		{
			"func": "count",
			"field": "*",
			"alias": "no_localizadas",
			"filtros": {
				"id_estatus": ["=", ESTATUS_NO_LOCALIZADA],
				"recolectable": ["=", 1],
			},
		}
	]
	# El modelo a veces ordena por un alias suyo ("piezas_no_localizadas") que ya
	# no existe, o pide un gráfico con columnas que tampoco: el ranking y la
	# tabla los arma explain, así que se descartan. El limit del modelo puede
	# venir en 1 y recortar el desglose por taller a una fila.
	spec.pop("order_by", None)
	spec.pop("render_chart", None)
	spec["limit"] = max(int(spec.get("limit") or 0), 50)
	return spec


def _pide_siniestros_pendientes(pregunta):
	if _pide_promedio(pregunta):
		return False
	texto = _sin_acentos(pregunta or "")
	return "siniestro" in texto and "pendiente" in texto


def _completar_siniestros_pendientes(spec, pregunta):
	""""¿Qué siniestros tienen piezas pendientes?" → un renglón por siniestro.

	El dueño lo pidió el 2026-10-31. "Pendiente" es la definición de Pieza
	(id_estatus vacío, sin fecha de recolección y recolectable = 1). **Sin período
	en la pregunta se asume el mes en curso** (decisión del dueño 2026-11-01: toda
	pregunta sobre siniestros va al mes actual); si la frase trae período, manda el
	suyo.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_siniestros_pendientes(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	rango = (_rango_del_spec(spec) if _trae_periodo(pregunta) else None) or _periodo_mes_actual()
	spec["filters"] = {"fechaingreso": ["between", rango[0], rango[1]]}
	spec["fields"] = []
	spec["group_by"] = ["siniestro"]
	spec["aggregations"] = [
		{
			"func": "count",
			"field": "*",
			"alias": "pendientes",
			"filtros": {
				"id_estatus": ["is", "null"],
				"fecha_hora_recoleccion": ["is", "null"],
				"recolectable": ["=", 1],
			},
		}
	]
	spec["order_by"] = [{"field": "pendientes", "dir": "desc"}]
	spec["limit"] = 50
	spec.pop("render_chart", None)
	return spec


def _pide_siniestros_por_cdr(pregunta):
	texto = _sin_acentos(pregunta or "")
	return (
		"siniestro" in texto
		and ("cdr" in texto or "centro de reparacion" in texto)
		and any(palabra in texto for palabra in ("cuant", "manejar", "atender", "gestionar", "recibir"))
	)


def _completar_siniestros_por_cdr(spec, pregunta):
	"""Cuenta siniestros distintos registrados por taller (CDR), no capacidad teórica."""
	if not isinstance(spec, dict) or spec.get("respuesta_directa"):
		return spec
	if not _pide_siniestros_por_cdr(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec

	# Aunque el modelo clasifique la capacidad como fuera de alcance, los datos
	# permiten contar los siniestros asociados a cada taller en el período.
	rango = _rango_del_spec(spec) if _trae_periodo(pregunta) or _trae_rango_de_fechas(pregunta) else None
	if spec.get("out_of_scope") and (_trae_periodo(pregunta) or _trae_rango_de_fechas(pregunta)) and not rango:
		# No reemplazar una fecha explícita por el mes actual si el modelo no
		# construyó un spec del que podamos recuperar el rango.
		return spec
	rango = rango or _periodo_mes_actual()
	spec.pop("out_of_scope", None)
	spec["doctype"] = "Pieza"
	spec["filters"] = {"fechaingreso": ["between", rango[0], rango[1]]}
	spec["fields"] = []
	spec["group_by"] = ["taller_asignado"]
	spec["aggregations"] = [
		{"func": "count_distinct", "field": "siniestro", "alias": "siniestros"}
	]
	spec["order_by"] = [{"field": "siniestros", "dir": "desc"}]
	spec["limit"] = 50
	spec.pop("render_chart", None)
	return spec


COLUMNAS_PRIORIDAD = ["siniestro", "marca", "modelo", "tipo", "piezades", "preciovale"]


# Sinónimos de precio/costo (dueño, 2026-11-06).
PALABRAS_PRECIO = ("precio", "valor", "costo", "caro", "caros", "cara", "caras")


def _pide_prioridad_pendientes(pregunta):
	texto = _sin_acentos(pregunta or "")
	return "prioridad" in texto and "pendiente" in texto


def _pide_piezas_caras(pregunta):
	""""piezas más caras" = listado por precio (caro/cara son precio/costo).

	No toca los promedios, la prioridad de pendientes ni la efectividad, que
	tienen su propio camino.
	"""
	texto = _sin_acentos(pregunta or "")
	if _pide_promedio(pregunta) or _pide_efectividad(pregunta):
		return False
	if "prioridad" in texto or "pendiente" in texto:
		return False
	if re.search(r"\b(?:mas\s+)?(?:caro|caros|cara|caras)\b", texto):
		return True
	return bool(
		re.search(r"\b(?:precio|costo|valor)\s+(?:mas\s+)?(?:alto|alta|elevado|elevada)\b", texto)
	)


def _completar_piezas_caras(spec, pregunta):
	"""Listado de piezas por precio de mayor a menor (dueño, 2026-11-06).

	Mismos campos que la prioridad, pero **sin** la definición de pendiente:
	"las piezas más caras" son las de mayor `preciovale`, estén o no pendientes.
	El período y los filtros de la pregunta (marca, submarca, parte, taller…)
	se conservan.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_piezas_caras(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	rango = _periodo_del_spec(spec) or _periodo_mes_actual()
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "id_estatus", "recolectable"):
		filtros.pop(campo, None)
	filtros["fechaingreso"] = ["between", rango[0], rango[1]]
	filtros["recolectable"] = ["=", 1]
	spec["filters"] = filtros
	spec["fields"] = list(COLUMNAS_PRIORIDAD)
	spec["aggregations"] = []
	spec["group_by"] = []
	spec["order_by"] = [{"field": "preciovale", "dir": "desc"}]
	tope = _top_de_la_pregunta(pregunta) or _numero_por_singular_o_plural(pregunta)
	if tope is None:
		# "la pieza más cara" es una sola; "las piezas más caras" es la lista.
		texto = _sin_acentos(pregunta or "")
		tope = 1 if re.search(r"\b(?:la|una)\s+pieza\b", texto) else 10
	spec["limit"] = tope
	spec.pop("render_chart", None)
	return spec


def _objeto_del_cuantos(texto):
	"""Primer sustantivo después de cuántos/cantidad/número/total."""
	m = re.search(
		r"\b(?:cuantos|cuantas|cantidad|numero|total)\s+(?:de\s+)?(?:el|la|los|las)?\s*([a-z]+)",
		texto,
	)
	return m.group(1) if m else ""


def _objeto_del_verbo(texto):
	"""Primer sustantivo después del verbo ("dame las piezas…" → piezas)."""
	m = re.search(
		r"\b(?:dame|dime|lista|listar|muestra|muestrame|indica|enumera|cuales son)"
		r"\s+(?:las|los|la|el)?\s*([a-z]+)",
		texto,
	)
	return m.group(1) if m else ""


def _pide_piezas_con_precio(pregunta):
	""""Las piezas con su precio" = listado de piezas, una fila por pieza.

	Se distingue de "los precios" (agregado) por el objeto del verbo: si pide
	las piezas, se listan con su precio; si pide los precios, se suma.
	"""
	texto = _sin_acentos(pregunta or "")
	if any(
		p in texto
		for p in (
			"promedio",
			"caro",
			"cara",
			"prioridad",
			"efectividad",
			"resumen",
			"categoria",
			"cuanto valen",
			"valor total",
			"suma",
		)
	):
		return False
	pieza = "pieza" in texto or "refaccion" in texto
	precio = "precio" in texto or "costo" in texto or "valor" in texto
	if not precio:
		return False
	# "lista los precios…" sin dimensión: se listan las piezas con su precio.
	if _pide_lista_explicita(pregunta) and not _dimensiones_tras_por(pregunta):
		return True
	# "los precios de las facias…" (plural + parte, sin "por …"): también.
	if (
		not _dimensiones_tras_por(pregunta)
		and ("precios" in texto or "valores" in texto)
		and _parte_mencionada(pregunta)
	):
		return True
	if not pieza:
		return False
	if any(
		p in texto
		for p in ("cada pieza", "con su precio", "con sus precios", "y sus precios", "piezas precios")
	):
		return True
	primero = _objeto_del_verbo(texto)
	if primero:
		return primero.startswith(("pieza", "refaccion"))
	if re.search(r"\b(?:el|un)\s+(?:precio|costo|valor)\b", texto):
		return False
	return pieza


def _pide_solo_precios(pregunta):
	""""Los precios" (sin pedir las piezas) = suma de precios, por dimensión si hay."""
	texto = _sin_acentos(pregunta or "")
	if (
		_pide_piezas_con_precio(pregunta)
		or _pide_promedio(pregunta)
		or _pide_efectividad(pregunta)
		or _pide_piezas_caras(pregunta)
	):
		return False
	if any(
		p in texto
		for p in ("caro", "cara", "prioridad", "resumen", "categoria", "cuanto valen", "valor total", "suma")
	):
		return False
	if not ("precio" in texto or "costo" in texto or "valor" in texto):
		return False
	if _pide_lista_explicita(pregunta) and not _dimensiones_tras_por(pregunta):
		return False
	primero = _objeto_del_verbo(texto)
	if primero:
		return primero.startswith(("precio", "costo", "valor"))
	if re.search(r"\b(?:el|un)\s+(?:precio|costo|valor)\b", texto):
		return True
	return not ("pieza" in texto or "refaccion" in texto)


def _completar_piezas_con_precio(spec, pregunta):
	"""Listado de piezas con su precio (no es el agregado de precios)."""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_piezas_con_precio(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "id_estatus", "recolectable"):
		filtros.pop(campo, None)
	filtros["fechaingreso"] = ["between", desde, hasta]
	filtros["recolectable"] = ["=", 1]
	spec["filters"] = filtros
	spec["fields"] = list(COLUMNAS_PRIORIDAD)
	spec["aggregations"] = []
	spec["group_by"] = []
	spec["order_by"] = [{"field": "preciovale", "dir": "desc"}]
	spec["limit"] = _top_de_la_pregunta(pregunta) or 50
	spec.pop("render_chart", None)
	return spec


def _completar_solo_precios(spec, pregunta):
	"""Agregado de precios (“los precios”): suma, por dimensión si la nombran."""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_solo_precios(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "id_estatus", "recolectable"):
		filtros.pop(campo, None)
	filtros["fechaingreso"] = ["between", desde, hasta]
	filtros["recolectable"] = ["=", 1]
	spec["filters"] = filtros
	spec["fields"] = []
	spec["aggregations"] = [{"func": "sum", "field": "preciovale", "alias": "precios"}]
	dimensiones = _dimensiones_tras_por(pregunta)
	spec["group_by"] = dimensiones[:2]
	if dimensiones:
		spec["order_by"] = [{"field": "precios", "dir": "desc"}]
		spec["limit"] = _top_de_la_pregunta(pregunta) or 10
	else:
		spec["limit"] = 1
		spec.pop("order_by", None)
	spec.pop("render_chart", None)
	return spec


def _titulo_respuesta_para(spec, pregunta):
	"""Título que distingue “Piezas con su precio” de “Piezas más caras”."""
	if _es_listado_mas_caras(spec):
		if _pide_piezas_con_precio(pregunta):
			return "Piezas con su precio"
		if _pide_lista_explicita(pregunta):
			return "Piezas"
	return _titulo_respuesta(spec)


def _es_listado_prioridad(spec):
	"""""Prioridad de piezas pendientes": listado de campos con precio y siniestro."""
	campos = [str(f).lower() for f in spec.get("fields") or []]
	return not spec.get("aggregations") and "preciovale" in campos and "siniestro" in campos


def _es_listado_mas_caras(spec):
	"""Listado de piezas por precio sin la definición de pendiente."""
	campos = [str(f).lower() for f in spec.get("fields") or []]
	if spec.get("aggregations") or "preciovale" not in campos or "siniestro" not in campos:
		return False
	filtros = spec.get("filters") or {}
	return not ("id_estatus" in filtros and "fecha_hora_recoleccion" in filtros)


def _es_submarcas_ingresadas(spec):
	""""Submarcas con más piezas dañadas": ingresadas por tipo."""
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	return (
		spec.get("group_by") == ["tipo"]
		and len(aggs) == 1
		and str(aggs[0].get("alias") or "").lower() == "ingresadas"
	)


def _usa_tabla_servidor(spec):
	"""Respuestas cuya tabla pinta la app con encabezados fijos."""
	return (
		_es_listado_prioridad(spec)
		or _es_listado_mas_caras(spec)
		or _es_submarcas_ingresadas(spec)
		or _es_ranking_de_siniestros(spec)
		or _es_ranking_pais_origen(spec)
		or _es_crecimiento_siniestros(spec)
		or _es_ranking_anios(spec)
		or _es_preferencia_region(spec)
		or _es_ranking_partes(spec)
	)


def _completar_prioridad_pendientes(spec, pregunta):
	""""Prioridad de piezas pendientes" = top 10 por precio de la pieza.

	El dueño lo pidió el 2026-11-02: una fila por pieza pendiente con siniestro,
	marca, modelo, submarca, nombre de la pieza y precio, de mayor a menor precio.
	**Sin período en la pregunta se asume el mes en curso** (decisión del dueño
	2026-11-03); si la frase trae período, manda el suyo. Aquí también van la
	definición de pendiente y el orden.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_prioridad_pendientes(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	# El período hay que leerlo ANTES de reemplazar los filtros del modelo.
	rango = None
	if _trae_periodo(pregunta) or _trae_rango_de_fechas(pregunta):
		rango = _rango_del_spec(spec)
	spec["fields"] = list(COLUMNAS_PRIORIDAD)
	spec["aggregations"] = []
	spec["group_by"] = []
	spec["filters"] = {
		"id_estatus": ["is", "null"],
		"fecha_hora_recoleccion": ["is", "null"],
		"recolectable": ["=", 1],
	}
	# Sin período se usa el mes en curso (2026-11-03). Con período en la frase,
	# ese manda.
	rango = rango or _periodo_mes_actual()
	spec["filters"]["fechaingreso"] = ["between", rango[0], rango[1]]
	spec["order_by"] = [{"field": "preciovale", "dir": "desc"}]
	spec["limit"] = 10
	spec.pop("render_chart", None)
	return spec


def _pide_submarcas_danadas(pregunta):
	texto = _sin_acentos(pregunta or "")
	if "submarca" not in texto:
		return False
	habla_piezas = "danad" in texto or "pieza" in texto
	return habla_piezas and any(v in texto for v in VERBOS_TOP)


def _completar_submarcas_danadas(spec, pregunta):
	"""“¿Qué submarcas generan más piezas dañadas?” = piezas ingresadas por submarca.

	El dueño lo pidió el 2026-11-03: "dañadas" es la pieza que ingresó al taller,
	así que la medida es el conteo de ingresadas (fechaingreso en el período +
	recolectable = 1) agrupado por `tipo` (la submarca), de mayor a menor.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_submarcas_danadas(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	spec["filters"] = {"fechaingreso": ["between", desde, hasta]}
	spec["fields"] = []
	spec["group_by"] = ["tipo"]
	spec["aggregations"] = [
		{
			"func": "count",
			"field": "*",
			"alias": "ingresadas",
			"filtros": {"recolectable": ["=", 1]},
		}
	]
	spec["order_by"] = [{"field": "ingresadas", "dir": "desc"}]
	# El N de la pregunta manda ("las 5 más dañadas"); sin N, 10.
	spec["limit"] = _top_de_la_pregunta(pregunta) or _numero_por_singular_o_plural(pregunta) or 10
	spec.pop("render_chart", None)
	return spec


def _pide_promedio_siniestros_taller(pregunta):
	"""“¿Cuántos siniestros atiende en promedio cada taller al mes?” (2026-11-10)."""
	texto = _sin_acentos(pregunta or "")
	if "siniestro" not in texto:
		return False
	if "taller" not in texto and not re.search(r"\bcdr\b", texto):
		return False
	habla_promedio = any(p in texto for p in ("promedio", "media", "en promedio"))
	# "siniestros por taller al mes" es la misma pregunta sin la palabra.
	if not habla_promedio and not _pide_al_mes(pregunta):
		return False
	# "piezas por siniestro" es el otro promedio (no habla de talleres).
	return "pieza" not in texto


def _meses_del_rango(desde, hasta):
	"""Meses de calendario que toca el rango (1 si es un solo mes)."""
	d1 = frappe.utils.getdate(desde)
	d2 = frappe.utils.getdate(hasta)
	return (d2.year - d1.year) * 12 + d2.month - d1.month + 1


def _completar_promedio_siniestros_taller(spec, pregunta):
	"""Siniestros distintos por taller (y por mes si lo piden)."""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_promedio_siniestros_taller(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	filtros = {
		campo: valor
		for campo, valor in (spec.get("filters") or {}).items()
		if campo not in ("fechaingreso", "fecha_hora_recoleccion", "id_estatus", "recolectable")
	}
	filtros["fechaingreso"] = ["between", desde, hasta]
	filtros["recolectable"] = ["=", 1]
	spec["filters"] = filtros
	spec["fields"] = []
	# El dueño lo pidió el 2026-11-10: "se responde con los talleres y para cada
	# taller, su número de siniestros promedio". La agrupación es el taller y,
	# si la pregunta nombra otra dimensión ("por taller por región"), esa.
	dimension = _dimension_agrupada_promedio_taller(pregunta) or "taller_asignado"
	spec["group_by"] = [dimension]
	spec["aggregations"] = [
		{"func": "count_distinct", "field": "siniestro", "alias": "siniestros"},
		{"func": "count_distinct", "field": "taller_asignado", "alias": "talleres"},
	]
	spec["limit"] = 500
	spec.pop("order_by", None)
	spec.pop("render_chart", None)
	spec.pop("min_ingresadas", None)
	return spec


def _dimension_agrupada_promedio_taller(pregunta):
	""""Por taller" es el divisor; la agrupación es la segunda dimensión.

	"promedio de siniestros por taller por región" → tabla por región. Con una
	sola dimensión de taller no hay agrupación: es una cifra.
	"""
	dimensiones = _dimensiones_tras_por(pregunta)
	if not dimensiones:
		return None
	if dimensiones[0] == "taller_asignado":
		return dimensiones[1] if len(dimensiones) > 1 else None
	return dimensiones[0]


def _pide_al_mes(pregunta):
	texto = _sin_acentos(pregunta or "")
	return bool(re.search(r"\bal\s+mes\b|\bpor\s+mes\b|\bmensual", texto))


def _frase_promedio_siniestros_taller(resultado, meses, al_mes):
	"""Frase del promedio (o tabla si agrupa por dimensión)."""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	filas = resultado.get("filas") or []
	if not filas or "siniestros" not in columnas or "talleres" not in columnas:
		return None
	i_sin = columnas.index("siniestros")
	i_tal = columnas.index("talleres")
	fila = filas[0]
	siniestros = fila[i_sin] or 0
	talleres = fila[i_tal] or 0
	divisor = talleres * (meses if al_mes else 1)
	if not divisor:
		return "No hay talleres con siniestros en el período."
	promedio = siniestros / divisor
	sufijo = " al mes" if al_mes else ""
	detalle = (
		f"{siniestros:,.0f} siniestro{'s' if siniestros != 1 else ''} en "
		f"{talleres:,.0f} taller{'es' if talleres != 1 else ''}"
	)
	if al_mes and meses > 1:
		detalle += f", {meses} meses"
	return f"En promedio, cada taller atiende {promedio:.2f} siniestros{sufijo} ({detalle})."


def _tabla_promedio_siniestros_taller(spec, resultado, meses, al_mes, pregunta=None):
	"""Una fila por taller con su promedio (o por dimensión, si la nombra).

	Con el taller como agrupación: `Taller | Siniestros [| Promedio al mes]`,
	donde el promedio de cada taller es sus siniestros entre los meses del
	período. Con otra dimensión ("por taller por región") la tabla es
	`Dimensión | Siniestros | Talleres | Promedio`.
	"""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	dimension = str((spec.get("group_by") or [None])[0] or "").lower()
	if not dimension or dimension not in columnas or "siniestros" not in columnas:
		return None
	i_dim = columnas.index(dimension)
	i_sin = columnas.index("siniestros")
	divisor_meses = meses if al_mes else 1
	tope = _top_de_la_pregunta(pregunta) or 50
	if dimension == "taller_asignado":
		filas = []
		for fila in resultado.get("filas") or []:
			if fila[i_dim] in (None, ""):
				continue
			siniestros = fila[i_sin] or 0
			filas.append([str(fila[i_dim]), siniestros, f"{siniestros / divisor_meses:.2f}"])
		if not filas:
			return None
		filas.sort(key=lambda f: f[1], reverse=True)
		if al_mes and meses > 1:
			# Varios meses: el promedio mensual por taller.
			return ["Taller", "Siniestros", "Promedio al mes"], filas[:tope]
		# Un solo mes (el promedio mensual es el conteo) o sin "al mes".
		return ["Taller", "Siniestros al mes" if al_mes else "Siniestros"], [
			[f[0], f[1]] for f in filas[:tope]
		]
	i_tal = columnas.index("talleres") if "talleres" in columnas else None
	filas = []
	for fila in resultado.get("filas") or []:
		if fila[i_dim] in (None, ""):
			continue
		siniestros = fila[i_sin] or 0
		talleres = (fila[i_tal] or 0) if i_tal is not None else 1
		divisor = talleres * divisor_meses
		if not divisor:
			continue
		filas.append(
			[str(fila[i_dim]), siniestros, talleres, f"{siniestros / divisor:.2f}"]
		)
	if not filas:
		return None
	filas.sort(key=lambda f: float(f[3]), reverse=True)
	etiqueta = ETIQUETAS_DIMENSION.get(
		str(spec["group_by"][0]), str(spec["group_by"][0]).replace("_", " ").title()
	)
	return [etiqueta, "Siniestros", "Talleres", "Promedio"], filas[:tope]


def _es_promedio_siniestros_taller(spec):
	alias = [str(a.get("alias") or "").lower() for a in (spec.get("aggregations") or [])]
	return alias == ["siniestros", "talleres"]


def _pide_promedio_siniestro(pregunta):
	texto = _sin_acentos(pregunta or "")
	return "promedio" in texto and "siniestro" in texto and "pieza" in texto


def _es_promedio_siniestro(spec):
	alias = [
		str(a.get("alias") or "").lower()
		for a in (spec.get("aggregations") or [])
		if isinstance(a, dict)
	]
	return alias == ["piezas", "siniestros"]


def _dimensiones_tras_por(pregunta):
	"""Dimensiones nombradas después del primer "por", en orden (máx 3).

	"precio promedio por marca y región" → [marca, region_pais];
	"promedio de piezas por siniestro por taller" → [siniestro, taller_asignado].
	Cada cláusula se corta en "de la/del", "en", "con", "que"… para que los
	filtros ("por región de la marca Nissan") no se confundan con agrupaciones.
	"""
	clausulas = re.split(r"\bpor\b", _sin_acentos(pregunta or ""))[1:]
	dimensiones = []
	for clausula in clausulas:
		corte = re.split(
			r"\b(?:en|con|que|para|entre|durante)\b|\bde\s+(?:la|el)\s+|\bdel\s+",
			clausula,
		)[0]
		for dimension in _dimensiones_de_la_pregunta(corte):
			if dimension not in dimensiones:
				dimensiones.append(dimension)
	if not dimensiones and "misma pieza" in _sin_acentos(pregunta or ""):
		# "misma pieza" es "por parte" (dueño, 2026-11-07).
		dimensiones.append("parte_pieza")
	return dimensiones[:3]


def _completar_promedio_siniestro(spec, pregunta):
	"""“¿Cuántas piezas tiene en promedio un siniestro de X?” = piezas / siniestros.

	El dueño lo pidió el 2026-11-03, con el mes en curso por defecto. La división
	la hace el código: el SQL solo trae las dos cifras (piezas y siniestros
	distintos), así el modelo no calcula nada.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_promedio_siniestro(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	# Los filtros propios de la pregunta (marca, submarca, taller...) se
	# conservan; solo se reemplaza la fecha y se quitan los del DocType.
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "recolectable", "id_estatus"):
		filtros.pop(campo, None)
	filtros["fechaingreso"] = ["between", desde, hasta]
	# recolectable va GLOBAL: dentro de un count_distinct el filtro por agregación
	# se traduce a COUNT(CASE ... THEN DISTINCT ...), que MySQL no acepta.
	filtros["recolectable"] = ["=", 1]
	spec["filters"] = filtros
	spec["fields"] = []
	spec["group_by"] = []
	spec["aggregations"] = [
		{"func": "count", "field": "*", "alias": "piezas"},
		{"func": "count_distinct", "field": "siniestro", "alias": "siniestros"},
	]
	# "promedio de piezas por siniestro por taller": el "por siniestro" es el
	# divisor y las dimensiones siguientes agrupan el promedio (dueño, 2026-11-06).
	dimensiones = _dimensiones_tras_por(pregunta)
	agrupadas = [d for d in dimensiones if d != "siniestro"][:2]
	spec["group_by"] = agrupadas
	if agrupadas:
		spec["order_by"] = [{"field": "piezas", "dir": "desc"}]
		# Se traen todos los grupos y el recorte al top N se hace en Python,
		# porque el orden real es el promedio, no el conteo.
		spec["limit"] = 500
	else:
		spec["limit"] = 1
		spec.pop("order_by", None)
	spec.pop("render_chart", None)
	return spec


def _pide_promedio(pregunta):
	""""promedio" (o "media") en cualquier pregunta (dueño, 2026-11-05)."""
	texto = _sin_acentos(pregunta or "")
	return "promedio" in texto or "promedian" in texto or bool(re.search(r"\bmedia\b", texto))


def _es_promedio_precio(spec):
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	return len(aggs) == 1 and str(aggs[0].get("alias") or "").lower() == "precio_promedio"


def _es_promedio_ratio(spec):
	alias = [str(a.get("alias") or "").lower() for a in (spec.get("aggregations") or [])]
	return alias == ["unidades", "grupos"]


def _medida_promediada(pregunta):
	"""Etiqueta de lo que se promedia y su plural, para el título y la frase.

	La medida es el sustantivo que sigue a "promedio de": en "promedio de piezas
	por siniestro" se promedian piezas (el siniestro es el divisor).
	"""
	texto = _sin_acentos(pregunta or "")
	medidas = (
		("pieza", "piezas", "piezas"),
		("siniestro", "siniestros", "siniestros"),
		("recolectad", "piezas recolectadas", "piezas recolectadas"),
		("ingresad", "piezas ingresadas", "piezas ingresadas"),
		("recoleccion", "recolecciones", "recolecciones"),
		("pendiente", "piezas pendientes", "piezas pendientes"),
	)
	m = re.search(r"\b(?:promedio|media)\s+de\s+(?:las?\s+|los?\s+)?([a-z]+)", texto)
	if m:
		for clave, etiqueta, plural in medidas:
			if m.group(1).startswith(clave):
				return etiqueta, plural
	for clave, etiqueta, plural in medidas:
		if clave in texto:
			return etiqueta, plural
	return "piezas", "piezas"


def _plural_dimension(etiqueta):
	"""“Taller” → “talleres”, “Región” → “regiones”."""
	mapa = {
		"Taller": "talleres",
		"Marca": "marcas",
		"Región": "regiones",
		"Plaza": "plazas",
		"Tipo de pieza": "tipos de pieza",
		"Tipo CDR": "tipos CDR",
		"Estado": "estados",
		"Modelo": "modelos",
		"Siniestro": "siniestros",
		"País de origen": "países de origen",
		"Submarca": "submarcas",
	}
	if etiqueta in mapa:
		return mapa[etiqueta]
	base = etiqueta.lower()
	return base + ("es" if base.endswith(("l", "r", "n", "y")) else "s")


def _completar_promedios(spec, pregunta):
	"""Promedios por cualquier dimensión, con el valor específico que se mencione.

	El dueño lo pidió el 2026-11-05 y el 2026-11-06 lo extendió a cualquier
	dimensión agrupada. Dos formas:
	- "precio/valor promedio [por X y Y]" → AVG(preciovale); con una o dos
	  dimensiones, una fila por grupo (ordenado por el promedio, mayor primero).
	- "promedio de piezas/siniestros por X [por Y]" → dos conteos (unidades y
	  grupos) que el código divide; si además se nombra otra dimensión, una fila
	  por grupo con su promedio.

	Los filtros de la pregunta (marca, submarca, taller, plaza...) se conservan:
	`_completar_valor_especifico` los agrega después.

	"Cuántos siniestros atiende en promedio cada taller al mes" tiene su propia
	forma (`_completar_promedio_siniestros_taller`): no se toca aquí.
	
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if _pide_promedio_siniestros_taller(pregunta):
		return spec
	if not _pide_promedio(pregunta):
		return spec
	if _pide_promedio_siniestro(pregunta):
		# "piezas por siniestro" tiene su propio camino y su propio texto.
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	texto = _sin_acentos(pregunta or "")
	dimensiones = _dimensiones_tras_por(pregunta)
	es_precio = any(p in texto for p in (*PALABRAS_PRECIO, "valor", "monto"))
	medida = any(
		p in texto
		for p in ("pieza", "ingresad", "recolectad", "recoleccion", "siniestro", "pendiente")
	)
	if not es_precio and not medida:
		# "promedio por marca" sin medida = precio promedio (el campo numérico útil).
		es_precio = True
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "recolectable", "id_estatus"):
		filtros.pop(campo, None)
	filtros["fechaingreso"] = ["between", desde, hasta]
	filtros["recolectable"] = ["=", 1]
	spec["filters"] = filtros
	spec["fields"] = []
	spec.pop("order_by", None)
	spec.pop("render_chart", None)
	if es_precio:
		spec["aggregations"] = [
			{"func": "avg", "field": "preciovale", "alias": "precio_promedio"}
		]
		# Todas las dimensiones de las cláusulas "por …" agrupan el promedio
		# ("precio promedio por marca y región").
		spec["group_by"] = dimensiones[:2]
		if dimensiones:
			# Un desglose es tabla aunque la frase sea "¿cuál es…?": el singular
			# solo aplica cuando no hay dimensión.
			spec["order_by"] = [{"field": "precio_promedio", "dir": "desc"}]
			spec["limit"] = _top_de_la_pregunta(pregunta) or 10
		else:
			spec["limit"] = 1
		return spec
	if not dimensiones:
		# "promedio de piezas" a secas no dice entre qué dividir: lo arma el modelo.
		return spec
	divisor = dimensiones[0]
	agrupadas = [d for d in dimensiones[1:] if d != divisor][:2]
	es_siniestro = "siniestro" in texto
	spec["aggregations"] = [
		{
			"func": "count_distinct" if es_siniestro else "count",
			"field": "siniestro" if es_siniestro else "*",
			"alias": "unidades",
		},
		{"func": "count_distinct", "field": divisor, "alias": "grupos"},
	]
	spec["group_by"] = agrupadas
	if agrupadas:
		spec["order_by"] = [{"field": "unidades", "dir": "desc"}]
		spec["limit"] = 500
	else:
		spec["limit"] = 1
	return spec


def _promedio_ratio_de(resultado, pregunta):
	"""Frase del promedio contable ("cada taller tiene 47 piezas en promedio")."""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	filas = resultado.get("filas") or []
	if not filas or "unidades" not in columnas or "grupos" not in columnas:
		return None
	fila = filas[0]
	unidades = fila[columnas.index("unidades")] or 0
	grupos = fila[columnas.index("grupos")] or 0
	if not grupos:
		return "No hay grupos en el período."
	promedio = unidades / grupos
	_, plural = _medida_promediada(pregunta)
	dimension = _dimension_por_frase(pregunta) or "siniestro"
	etiqueta = ETIQUETAS_DIMENSION.get(dimension, dimension.replace("_", " ").title())
	return (
		f"En promedio, cada {etiqueta.lower()} tiene {promedio:.2f} {plural} "
		f"({unidades:,.0f} {plural} en {grupos:,.0f} {_plural_dimension(etiqueta)})."
	)


def _titulo_promedio(pregunta, spec):
	"""Título del promedio: "Promedio de piezas por Taller por Región (Top 10)"."""
	dimensiones = _dimensiones_tras_por(pregunta)
	etiquetas = [
		ETIQUETAS_DIMENSION.get(d, d.replace("_", " ").title()) for d in dimensiones
	]
	if _es_promedio_precio(spec):
		if not etiquetas:
			return "Precio promedio"
		limite = int(spec.get("limit") or 0)
		top = f" (Top {limite})" if 1 < limite <= 20 else ""
		return "Precio promedio por " + " y ".join(etiquetas) + top
	_, plural = _medida_promediada(pregunta)
	if not etiquetas:
		return f"Promedio de {plural}"
	base = f"Promedio de {plural} por {etiquetas[0]}"
	if len(etiquetas) > 1:
		base += " por " + " y ".join(etiquetas[1:])
	if spec.get("group_by"):
		tope = _top_de_la_pregunta(pregunta) or 10
		if tope > 1:
			base += f" (Top {tope})"
	return base


def _promedio_sin_datos(spec, resultado):
	"""True si las medidas del promedio están todas vacías o en cero."""
	filas = resultado.get("filas") or []
	if not filas:
		return True
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	alias = {
		str(a.get("alias") or "").lower() for a in (spec.get("aggregations") or [])
	}
	indices = [i for i, c in enumerate(columnas) if c in alias]
	if not indices:
		return False
	for fila in filas:
		for i in indices:
			valor = fila[i]
			if isinstance(valor, (int, float)) and valor != 0:
				return False
	return True


def _efectividad_sin_datos(spec, resultado):
	"""True si ninguna fila sobrevive al corte de `min_ingresadas`.

	`explain` aplica el HAVING sobre los grupos ya agregados (total > 0 y
	>= min_ingresadas), así que una efectividad puede traer filas del executor y
	aun así no mostrar nada: a principios de mes, "la región con mayor
	recolección" no llega a 50 ingresadas y la respuesta se quedaba sin
	ingresadas, recolectadas, efectividad ni objetivo (dueño, 2026-11-07).
	"""
	if not _es_spec_efectividad(spec):
		return False
	minimo = spec.get("min_ingresadas") or 0
	for p in _porcentajes(spec, resultado):
		total = p.get("total") or 0
		if total > 0 and total >= minimo:
			return False
	return True


def _formatear_precio_promedio(resultado):
	""""$1,234.56" en la columna del promedio (el chat no formatea decimales)."""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	if "precio_promedio" not in columnas:
		return resultado, None
	i = columnas.index("precio_promedio")
	filas = []
	unico = None
	for fila in resultado.get("filas") or []:
		nueva = list(fila)
		valor = nueva[i]
		if valor in (None, ""):
			nueva[i] = "Sin dato"
		elif not (isinstance(valor, str) and valor.startswith("$")):
			nueva[i] = f"${float(valor):,.2f}"
		filas.append(nueva)
	resultado["filas"] = filas
	if filas and len(columnas) == 1:
		unico = filas[0][i]
	return resultado, unico


def _promedios_por_grupo(spec, resultado):
	"""Filas dimensión → promedio (unidades/grupos o piezas/siniestros).

	Se calcula en Python porque el promedio ordena distinto que los conteos: se
	traen todos los grupos (limit 500) y aquí se divide, se ordena de mayor a
	menor y se recorta al top N.
	"""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	filas = resultado.get("filas") or []
	if not columnas or not filas:
		return []
	i_num = columnas.index("piezas") if "piezas" in columnas else (
		columnas.index("unidades") if "unidades" in columnas else None
	)
	i_den = columnas.index("siniestros") if "siniestros" in columnas else (
		columnas.index("grupos") if "grupos" in columnas else None
	)
	if i_num is None or i_den is None:
		return []
	indices = [
		(columnas.index(str(g).lower()), str(g).lower())
		for g in spec.get("group_by") or []
		if str(g).lower() in columnas
	]
	salida = []
	for fila in filas:
		denominador = fila[i_den] or 0
		if not denominador:
			continue
		promedio = (fila[i_num] or 0) / denominador
		salida.append(
			[_valor_legible(fila[i], dimension) for i, dimension in indices]
			+ [f"{promedio:.2f}"]
		)
	salida.sort(key=lambda f: float(f[-1]), reverse=True)
	return salida


def _promedio_de(resultado):
	"""Texto del promedio a partir de las columnas piezas/siniestros."""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	filas = resultado.get("filas") or []
	if not filas or "piezas" not in columnas or "siniestros" not in columnas:
		return None
	fila = filas[0]
	piezas = fila[columnas.index("piezas")] or 0
	siniestros = fila[columnas.index("siniestros")] or 0
	if not siniestros:
		return "No hay siniestros en el período."
	promedio = piezas / siniestros
	return (
		f"En promedio, cada siniestro tiene {promedio:.2f} piezas "
		f"({piezas:,.0f} piezas en {siniestros:,.0f} siniestros)."
	)


def _etiqueta_del_filtro(spec):
	"""“de la marca Toyota” / “de la submarca Versa…” para el encabezado."""
	filtros = spec.get("filters") or {}
	for campo, prefijo in (
		("marca", "de la marca"),
		("tipo", "de la submarca"),
		("pais_origen", "del país"),
		("taller_asignado", "del taller"),
	):
		valor = filtros.get(campo)
		if isinstance(valor, list) and len(valor) >= 2:
			valor = valor[1]
		if isinstance(valor, str) and valor:
			if campo == "pais_origen":
				valor = _pais_legible(valor)
			return f"{prefijo} {valor}"
	return ""


def _pide_marcas_accidentadas(pregunta):
	texto = _sin_acentos(pregunta or "")
	habla_siniestro = "accident" in texto or "siniestro" in texto
	habla_marca = "marca" in texto or "submarca" in texto
	return habla_siniestro and habla_marca and any(v in texto for v in VERBOS_TOP)


def _dimension_de_marca(pregunta):
	""""submarca" → tipo; "marca" (o marcas/submarcas) → marca."""
	texto = _sin_acentos(pregunta or "")
	solo_sub = re.search(r"\bsubmarcas?\b", texto) and not re.search(
		r"(?<!sub)\bmarcas?\b", texto
	)
	return "tipo" if solo_sub else "marca"


def _completar_marcas_accidentadas(spec, pregunta):
	"""“¿Qué marcas/submarcas se accidentan más por mes?” = top por siniestros.

	El dueño lo pidió el 2026-11-03: la medida es el número de **siniestros**
	distintos (no de piezas) por marca o submarca, de mayor a menor, con el mes en
	curso por defecto.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_marcas_accidentadas(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	dimension = _dimension_de_marca(pregunta)
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "recolectable", "id_estatus"):
		filtros.pop(campo, None)
	filtros["fechaingreso"] = ["between", desde, hasta]
	filtros["recolectable"] = ["=", 1]
	spec["filters"] = filtros
	spec["fields"] = []
	spec["group_by"] = [dimension]
	spec["aggregations"] = [
		{"func": "count_distinct", "field": "siniestro", "alias": "siniestros"}
	]
	spec["order_by"] = [{"field": "siniestros", "dir": "desc"}]
	spec["limit"] = _numero_por_singular_o_plural(pregunta) or 10
	spec.pop("render_chart", None)
	return spec


def _es_ranking_pais_origen(spec):
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	return (
		spec.get("group_by") == ["pais_origen"]
		and len(aggs) == 1
		and str(aggs[0].get("alias") or "").lower() == "siniestros"
	)


def _es_ranking_de_siniestros(spec):
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	return (
		spec.get("group_by") in (["marca"], ["tipo"])
		and len(aggs) == 1
		and str(aggs[0].get("alias") or "").lower() == "siniestros"
	)


PAISES_CATALOGO = (
	"ALEMANIA",
	"AUSTRIA",
	"CHINA",
	"COREA DEL SUR",
	"ESPAÑA",
	"ESTADOS UNIDOS",
	"FRANCIA",
	"INDIA",
	"INGLATERRA",
	"ITALIA",
	"JAPON",
	"MEXICO",
	"REINO UNIDO",
	"SUECIA",
)

ALIAS_PAIS = {
	"EEUU": "ESTADOS UNIDOS",
	"EUA": "ESTADOS UNIDOS",
	"USA": "ESTADOS UNIDOS",
	"EU": "ESTADOS UNIDOS",
	"UK": "REINO UNIDO",
	"GRAN BRETANA": "REINO UNIDO",
	"COREA": "COREA DEL SUR",
}


def _pais_canonico(valor):
	"""Nombre del catálogo para un país escrito de cualquier forma.

	"Japón", "japon" y "JAPON" son el mismo país: el filtro se normaliza al
	valor de Catalogo Datos (50-63) para que el WHERE no dependa de acentos ni
	de mayúsculas.
	"""
	texto = _sin_acentos(str(valor or "")).upper().strip()
	texto = ALIAS_PAIS.get(texto, texto)
	for pais in PAISES_CATALOGO:
		if _sin_acentos(pais).upper() == texto:
			return pais
	return valor


def _pais_de_la_pregunta(pregunta):
	"""País nombrado en la pregunta (nombre del catálogo), o None.

	Se buscan primero los nombres largos ("Corea del Sur" antes que "Corea") y
	los alias (EEUU, USA, UK…), sin acentos y con límites de palabra.
	"""
	texto = _sin_acentos(pregunta or "").upper()
	candidatos = sorted(
		[*PAISES_CATALOGO, *ALIAS_PAIS.keys()], key=len, reverse=True
	)
	for pais in candidatos:
		patron = r"(?<![A-Z])" + re.escape(_sin_acentos(pais).upper()) + r"(?![A-Z])"
		if re.search(patron, texto):
			return _pais_canonico(pais)
	return None


NOMBRES_PAIS = {"JAPON": "Japón", "MEXICO": "México", "ESPANA": "España"}


def _pais_legible(valor):
	"""“COREA DEL SUR” → “Corea del Sur”, “JAPON” → “Japón”."""
	texto = str(valor or "").upper()
	if texto in NOMBRES_PAIS:
		return NOMBRES_PAIS[texto]
	minusculas = {"de", "del", "la", "las", "los", "y", "e"}
	partes = texto.lower().split()
	return " ".join(p if p in minusculas else p.capitalize() for p in partes)


def _canonizar_pais_en_filtros(filtros):
	if not isinstance(filtros, dict):
		return filtros
	valor = filtros.get("pais_origen")
	if isinstance(valor, list) and len(valor) >= 2 and isinstance(valor[1], str):
		filtros["pais_origen"] = [valor[0], _pais_canonico(valor[1])]
	elif isinstance(valor, str):
		filtros["pais_origen"] = _pais_canonico(valor)
	return filtros


def _pide_pais_origen(pregunta):
	if _pide_promedio(pregunta):
		return False
	texto = _sin_acentos(pregunta or "")
	return ("pais" in texto and ("origen" in texto or "auto" in texto)) or "pais de origen" in texto


def _completar_pais_origen(spec, pregunta):
	"""“¿De qué país de origen son los autos con más siniestros?”.

	El país sale de la marca (`pais_origen` es virtual: marca → Marcas.cat_origen
	→ Catalogo Datos) y la medida son los siniestros distintos, de mayor a menor.
	Sin período, el mes en curso.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_pais_origen(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	# Las marcas que no están en el maestro no tienen país: un "sin origen" no
	# es un país de origen, así que no entra al ranking.
	spec["filters"] = {
		"fechaingreso": ["between", desde, hasta],
		"pais_origen": ["is", "not null"],
	}
	spec["fields"] = []
	spec["group_by"] = ["pais_origen"]
	spec["aggregations"] = [
		{"func": "count_distinct", "field": "siniestro", "alias": "siniestros"}
	]
	spec["order_by"] = [{"field": "siniestros", "dir": "desc"}]
	spec["limit"] = _numero_por_singular_o_plural(pregunta) or 10
	spec.pop("render_chart", None)
	return spec


def _pide_crecimiento_siniestros(pregunta):
	texto = _sin_acentos(pregunta or "")
	return any(p in texto for p in ("crecimiento", "crecido", "crece", "evolucion", "variacion")) and "siniestro" in texto


def _completar_crecimiento_siniestros(spec, pregunta):
	"""“¿Cómo ha crecido el número de siniestros?” = serie mensual + variación.

	El dueño lo pidió el 2026-11-03. El mes sale del campo virtual `mes`
	(YYYY-MM de fechaingreso) y la medida son los siniestros distintos; la
	variación la calcula el código (`_serie_con_variacion`). Sin período se usa
	lo que va del año, que es lo que hace visible una tendencia.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_crecimiento_siniestros(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	texto = _sin_acentos(pregunta or "")
	if any(p in texto for p in ("ultimo ano", "ultimos 12 meses", "ultimos doce meses")):
		rango = _periodo_doce_meses()
	else:
		rango = (_rango_del_spec(spec) if _trae_periodo(pregunta) else None) or _periodo_anio_actual()
	# Los filtros que nombra la pregunta (marca, país, taller…) se conservan.
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "recolectable", "id_estatus"):
		filtros.pop(campo, None)
	filtros["fechaingreso"] = ["between", rango[0], rango[1]]
	pais = _pais_de_la_pregunta(pregunta)
	if pais:
		# El país lo detecta el servidor: no depende de que el modelo lo filtre.
		filtros["pais_origen"] = ["=", pais]
	spec["filters"] = _canonizar_pais_en_filtros(filtros)
	spec["fields"] = []
	dimension = _dimension_de_la_pregunta(pregunta)
	por_dimension = bool(dimension) and bool(
		re.search(r"\bpor\s+(?:la\s+|el\s+)?(?:marca|submarca|pais|taller|plaza|region|tipo)", texto)
	)
	if por_dimension:
		# "crecimiento por marca": una fila por grupo comparando los DOS últimos
		# meses completos. Se acota la consulta a esos dos meses para que el
		# ranking no se trunque (marcas × meses se pasa del límite del SQL) y la
		# variación sale de la serie que devuelve la base.
		desde, hasta = _periodo_ultimos_dos_meses()
		filtros["fechaingreso"] = ["between", desde, hasta]
		spec["filters"] = filtros
		spec["group_by"] = [dimension, "mes"]
		spec["order_by"] = [{"field": "mes", "dir": "asc"}]
		spec["limit"] = 500
	elif dimension and not por_dimension:
		# "crecimiento de la marca X": la serie mensual de ese subconjunto.
		spec["group_by"] = ["mes"]
		spec["order_by"] = [{"field": "mes", "dir": "asc"}]
	else:
		spec["group_by"] = ["mes"]
		spec["order_by"] = [{"field": "mes", "dir": "asc"}]
	spec["aggregations"] = [
		{"func": "count_distinct", "field": "siniestro", "alias": "siniestros"}
	]
	spec.setdefault("limit", 36)
	spec.pop("render_chart", None)
	return spec


def _periodo_ultimos_dos_meses():
	"""Primer día del mes anterior al último completo → último día completo.

	Si el mes en curso ya cerró (hoy es el último día), el "último completo" es
	el mes en curso; si no, es el mes pasado.
	"""
	hoy = frappe.utils.getdate(frappe.utils.nowdate())
	ultimo = hoy if hoy.day == frappe.utils.get_last_day(hoy).day else frappe.utils.add_months(hoy, -1)
	previo = frappe.utils.add_months(ultimo, -1)
	desde = frappe.utils.get_first_day(previo)
	hasta = frappe.utils.get_last_day(ultimo)
	return f"{desde} 00:00:00", f"{hasta} 23:59:59"


def _periodo_doce_meses():
	"""Primer día del mes de hace 11 meses → hoy (12 meses contando el actual)."""
	hoy = frappe.utils.getdate(frappe.utils.nowdate())
	desde = frappe.utils.get_first_day(frappe.utils.add_months(hoy, -11))
	return f"{desde} 00:00:00", f"{hoy} 23:59:59"


def _periodo_anio_actual():
	ahora = frappe.utils.now_datetime()
	inicio = ahora.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
	return inicio.strftime("%Y-%m-%d %H:%M:%S"), ahora.strftime("%Y-%m-%d %H:%M:%S")


def _mes_legible(valor):
	"""“2026-09” → “sep 2026”."""
	texto = str(valor or "")
	if len(texto) >= 7 and texto[4] == "-":
		try:
			mes = int(texto[5:7])
			return f"{MESES_ES[mes - 1][:3]} {texto[:4]}"
		except (ValueError, IndexError):
			pass
	return texto


def _pide_crecimiento_dimension(pregunta):
	"""Crecimiento/decrecimiento **por dimensión** (dueño, 2026-11-07).

	"crecimiento de piezas por marca", "qué talleres crecieron más", "variación
	de siniestros por región": una fila por valor de la dimensión comparando los
	dos últimos bloques completos (meses, trimestres o cuatrimestres).
	"""
	texto = _sin_acentos(pregunta or "")
	if not any(
		p in texto
		for p in (
			"crecimiento",
			"creci",
			"crece",
			"decrec",
			"evolucion",
			"variacion",
			"aument",
			"disminuy",
			"compara",
		)
	):
		return False
	if " contra " in texto or _comparacion_clasificada(pregunta):
		return False
	if any(
		p in texto
		for p in ("efectividad", "porcentaje", "tasa de", "cumplimiento", "meta de", "objetivo")
	):
		return False
	if any(p in texto for p in ("recolect", "recuperad")):
		return False
	dimension = _dimension_por_frase(pregunta) or _dimension_de_la_pregunta(pregunta)
	# "crecimiento por siniestro" no es un ranking de crecimiento entre grupos.
	if not dimension or dimension == "siniestro":
		return False
	return True


def _bloques_completos(granularidad, cantidad=2):
	"""(año, índice) de los últimos bloques COMPLETOS, del más viejo al más nuevo."""
	meses = {"mes": 1, "trimestre": 3, "cuatrimestre": 4}[granularidad]
	hoy = frappe.utils.getdate(frappe.utils.nowdate())
	total = 12 // meses
	anio = hoy.year
	indice = (hoy.month - 1) // meses + 1
	if hoy.day != frappe.utils.get_last_day(hoy).day:
		# El bloque en curso va a medias: no entra en la comparación.
		indice -= 1
	if indice < 1:
		indice, anio = total, anio - 1
	bloques = []
	for paso in range(cantidad - 1, -1, -1):
		idx, a = indice - paso, anio
		while idx < 1:
			idx += total
			a -= 1
		bloques.append((a, idx))
	return bloques


def _etiqueta_de_bloque_por_granularidad(anio, indice, granularidad):
	meses = {"mes": 1, "trimestre": 3, "cuatrimestre": 4}[granularidad]
	if meses == 1:
		return _mes_legible(f"{anio}-{indice:02d}")
	return _etiqueta_de_bloque(anio, indice, meses)


def _completar_crecimiento_dimension(spec, pregunta):
	"""Serie por dimensión: dos últimos bloques completos + variación."""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_crecimiento_dimension(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	dimension = _dimension_por_frase(pregunta) or _dimension_de_la_pregunta(pregunta)
	if not dimension or dimension == "siniestro":
		return spec
	granularidad = _granularidad_de_crecimiento(pregunta)
	meses = {"mes": 1, "trimestre": 3, "cuatrimestre": 4}[granularidad]
	bloques = _bloques_completos(granularidad)
	desde = _rango_de_bloque(*bloques[0], meses)[0]
	hasta = _rango_de_bloque(*bloques[1], meses)[1]
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "recolectable", "id_estatus"):
		filtros.pop(campo, None)
	filtros["fechaingreso"] = ["between", desde, hasta]
	spec["filters"] = filtros
	spec["fields"] = []
	spec["group_by"] = [dimension, "mes"]
	spec["order_by"] = [{"field": "mes", "dir": "asc"}]
	spec["limit"] = 500
	spec.pop("render_chart", None)
	spec.pop("min_ingresadas", None)
	medida = _medida_de_crecimiento(pregunta)
	if medida == "siniestros":
		spec["aggregations"] = [
			{"func": "count_distinct", "field": "siniestro", "alias": "siniestros"}
		]
	elif medida == "pendientes":
		spec["aggregations"] = [
			{
				"func": "count",
				"field": "*",
				"alias": "pendientes",
				"filtros": {
					"fechaingreso": ["between", desde, hasta],
					"id_estatus": ["is", "empty"],
					"fecha_hora_recoleccion": ["is", "empty"],
					"recolectable": ["=", 1],
				},
			}
		]
	else:
		spec["aggregations"] = [{"func": "count", "field": "*", "alias": medida}]
	return spec


def _crecimiento_por_bloque_dimension(resultado, granularidad, descendente=True):
	"""Filas [dimensión, anterior, actual, variación] del crecimiento por grupo.

	`descendente` ordena de mayor crecimiento a mayor decrecimiento; para
	"qué X decrecieron más" va al revés.
	"""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	if len(columnas) < 3 or "mes" not in columnas:
		return [], [], ""
	i_dim, i_mes = 0, columnas.index("mes")
	medida = columnas[2]
	i_med = columnas.index(medida)
	meses = {"mes": 1, "trimestre": 3, "cuatrimestre": 4}[granularidad]
	bloques = _bloques_completos(granularidad)
	claves = [
		f"{a}-{i:02d}" if meses == 1 else f"{a}-{i}" for a, i in bloques
	]
	etiquetas = [
		_etiqueta_de_bloque_por_granularidad(a, i, granularidad) for a, i in bloques
	]
	por_dim = {}
	for fila in resultado.get("filas") or []:
		texto_mes = str(fila[i_mes] or "")[:7]
		try:
			anio, numero = int(texto_mes[:4]), int(texto_mes[5:7])
		except (ValueError, IndexError):
			continue
		clave = texto_mes if meses == 1 else f"{anio}-{(numero - 1) // meses + 1}"
		if clave not in claves:
			continue
		valores = por_dim.setdefault(fila[i_dim], {})
		valores[clave] = (valores.get(clave) or 0) + (fila[i_med] or 0)

	def _pct(texto):
		try:
			return float(str(texto).replace("%", "").replace("+", ""))
		except ValueError:
			return None

	filas = []
	for dimension, valores in por_dim.items():
		anterior = valores.get(claves[0]) or 0
		actual = valores.get(claves[1]) or 0
		if not anterior and not actual:
			continue
		variacion = f"{(actual - anterior) / anterior * 100:+.1f}%" if anterior else "—"
		etiqueta = dimension if dimension not in (None, "") else "Sin dato"
		filas.append([str(etiqueta), anterior, actual, variacion])
	# Los que más crecieron primero (o los que más cayeron); el volumen del
	# bloque actual desempata. Los grupos sin base anterior van al final.
	con_porcentaje = [f for f in filas if _pct(f[3]) is not None]
	sin_porcentaje = [f for f in filas if _pct(f[3]) is None]
	con_porcentaje.sort(key=lambda f: (_pct(f[3]), f[2]), reverse=descendente)
	sin_porcentaje.sort(key=lambda f: f[2], reverse=True)
	return con_porcentaje + sin_porcentaje, etiquetas, medida


def _es_crecimiento_dimension(spec, pregunta):
	dims = list(spec.get("group_by") or [])
	return (
		len(dims) == 2
		and dims[-1] == "mes"
		and len(spec.get("aggregations") or []) == 1
		and bool(_dimension_por_frase(pregunta) or _dimension_de_la_pregunta(pregunta))
		and _pide_crecimiento_dimension(pregunta)
	)


def _pide_crecimiento_periodos(pregunta):
	"""Crecimiento/decrecimiento entre meses, trimestres o cuatrimestres.

	El dueño lo pidió el 2026-11-07: "ten la capacidad de obtener
	crecimiento/decrecimiento entre meses, trimestres o cuatrimestres y
	comparalos". La serie de siniestros por mes ya tenía su camino; esto cubre
	piezas, siniestros y pendientes en las tres granularidades.
	"""
	texto = _sin_acentos(pregunta or "")
	if not any(
		p in texto
		for p in (
			"crecimiento",
			"creci",
			"crece",
			"decrec",
			"evolucion",
			"variacion",
			"aument",
			"disminuy",
			"compara",
		)
	):
		return False
	habla_bloques = bool(re.search(r"\btrimestres?\b|\bcuatrimestres?\b", texto))
	habla_meses = bool(
		re.search(r"mes\s+a\s+mes|mes\s+con\s+mes|entre\s+meses|por\s+mes(?:es)?\b", texto)
	)
	if not (habla_bloques or habla_meses):
		return False
	# "¿Cómo se compara el trimestre 1 contra el 2?" es la comparación de dos
	# bloques (otra rama), no una serie.
	if _comparacion_clasificada(pregunta) or " contra " in texto:
		return False
	# La efectividad explícita ("efectividad entre trimestres") no es una serie.
	if any(
		p in texto
		for p in ("efectividad", "porcentaje", "tasa de", "cumplimiento", "meta de", "objetivo")
	):
		return False
	# La serie de siniestros por mes (sin trimestres) ya tiene su camino.
	if "siniestro" in texto and not habla_bloques:
		return False
	# Las recolectadas se miden por su propia base de fecha: no entran acá.
	if any(p in texto for p in ("recolect", "recuperad")):
		return False
	# "por marca"/"por taller" tiene su propio camino (crecimiento por dimensión).
	dimension = _dimension_por_frase(pregunta) or _dimension_de_la_pregunta(pregunta)
	return not (dimension and dimension != "siniestro")


def _trae_periodo_especifico(pregunta):
	"""True si la pregunta fija fechas de verdad (mes, año o bloque con ordinal).

	"Por trimestre", "entre cuatrimestres" o "mes a mes" solo dicen la
	granularidad: la serie va del año en curso.
	"""
	texto = _sin_acentos(pregunta or "")
	if _pide_anio_en_curso(pregunta):
		return True
	if re.search(rf"\b(?:{'|'.join(MESES)})\b", texto) or re.search(r"\b20\d{2}\b", texto):
		return True
	if re.search(r"\bmes\s+(?:pasado|anterior)\b|\b(?:este|el)\s+mes\b", texto):
		return True
	bloque = _bloque_de_la_pregunta(pregunta)
	if not bloque:
		return False
	return bool(
		re.search(
			r"\b(actual|corriente|en\s+curso|primer|primero|segundo|tercer|tercero|cuarto"
			r"|1er|1ro|2do|3er|3ro|4to)\b",
			texto,
		)
	)


def _granularidad_de_crecimiento(pregunta):
	"""Bloque de la serie: mes, trimestre o cuatrimestre.

	Si la pregunta apunta a UN bloque concreto ("el primer trimestre",
	"el cuatrimestre actual"), la serie va mes a mes dentro de ese bloque: el
	crecimiento entre un solo bloque no dice nada.
	"""
	texto = _sin_acentos(pregunta or "")
	un_bloque = bool(
		re.search(
			r"\b(actual|corriente|en\s+curso|primer|primero|segundo|tercer|tercero|cuarto"
			r"|1er|1ro|2do|3er|3ro|4to)\b",
			texto,
		)
	) and bool(_bloque_de_la_pregunta(pregunta))
	if re.search(r"\bcuatrimestres?\b", texto):
		return "mes" if un_bloque else "cuatrimestre"
	if re.search(r"\btrimestres?\b", texto):
		return "mes" if un_bloque else "trimestre"
	return "mes"


def _medida_de_crecimiento(pregunta):
	texto = _sin_acentos(pregunta or "")
	if "siniestro" in texto:
		return "siniestros"
	if "pendiente" in texto:
		return "pendientes"
	return "piezas"


def _etiqueta_de_bloque(anio, indice, meses):
	nombre = "trimestre" if meses == 3 else "cuatrimestre"
	sufijo = {1: "er", 2: "do", 3: "er", 4: "to"}.get(indice, "")
	return f"{indice}{sufijo} {nombre} de {anio}"


def _completar_crecimiento_periodos(spec, pregunta):
	"""Serie por mes/trimestre/cuatrimestre con su variación (dueño, 2026-11-07).

	Se consulta la serie **mensual** (campo virtual `mes` de `fechaingreso`) y el
	código la agrupa en trimestres o cuatrimestres (`_serie_por_bloque`): una
	sola consulta sirve para las tres granularidades. Sin período se usa lo que
	va del año, que es lo que hace visible una tendencia.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_crecimiento_periodos(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	texto = _sin_acentos(pregunta or "")
	if any(p in texto for p in ("ultimo ano", "ultimos 12 meses", "ultimos doce meses")):
		rango = _periodo_doce_meses()
	else:
		# "por trimestre"/"mes a mes" indican la granularidad, no el período: la
		# serie es del año en curso salvo que la pregunta fije fechas.
		rango = (
			(_rango_del_spec(spec) if _trae_periodo_especifico(pregunta) else None)
			or _periodo_anio_actual()
		)
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "recolectable", "id_estatus"):
		filtros.pop(campo, None)
	filtros["fechaingreso"] = ["between", rango[0], rango[1]]
	spec["filters"] = filtros
	spec["fields"] = []
	spec["group_by"] = ["mes"]
	spec["order_by"] = [{"field": "mes", "dir": "asc"}]
	spec["limit"] = 36
	spec.pop("render_chart", None)
	spec.pop("min_ingresadas", None)
	medida = _medida_de_crecimiento(pregunta)
	if medida == "siniestros":
		spec["aggregations"] = [
			{"func": "count_distinct", "field": "siniestro", "alias": "siniestros"}
		]
	elif medida == "pendientes":
		# La definición de pendiente va dentro de la medida, con su período.
		spec["aggregations"] = [
			{
				"func": "count",
				"field": "*",
				"alias": "pendientes",
				"filtros": {
					"fechaingreso": ["between", rango[0], rango[1]],
					"id_estatus": ["is", "empty"],
					"fecha_hora_recoleccion": ["is", "empty"],
					"recolectable": ["=", 1],
				},
			}
		]
	else:
		spec["aggregations"] = [{"func": "count", "field": "*", "alias": medida}]
	return spec


def _serie_por_bloque(resultado, granularidad):
	"""Agrupa la serie mensual en meses/trimestres/cuatrimestres con variación."""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	if "mes" not in columnas or len(columnas) < 2:
		return [], ""
	medida = columnas[1]
	i_mes, i_med = columnas.index("mes"), columnas.index(medida)
	meses = MESES_POR_TRIMESTRE if granularidad == "trimestre" else MESES_POR_CUATRIMESTRE
	hoy = frappe.utils.getdate(frappe.utils.nowdate())
	acumulado, orden = {}, []
	for fila in resultado.get("filas") or []:
		texto_mes = str(fila[i_mes] or "")[:7]
		try:
			anio, numero = int(texto_mes[:4]), int(texto_mes[5:7])
		except (ValueError, IndexError):
			continue
		if granularidad == "mes":
			clave, etiqueta = texto_mes, _mes_legible(texto_mes)
		else:
			indice = (numero - 1) // meses + 1
			clave, etiqueta = f"{anio}-{indice}", _etiqueta_de_bloque(anio, indice, meses)
		if clave not in acumulado:
			acumulado[clave] = {"etiqueta": etiqueta, "valor": 0}
			orden.append(clave)
		acumulado[clave]["valor"] += fila[i_med] or 0
	# El bloque en curso va a medias: su caída no es crecimiento.
	indice_actual = (hoy.month - 1) // meses + 1
	clave_actual = hoy.strftime("%Y-%m") if granularidad == "mes" else f"{hoy.year}-{indice_actual}"
	_, hasta_actual = _rango_de_bloque(hoy.year, indice_actual, meses)
	completo = _dia(hoy) == hasta_actual[:10]
	filas, anterior = [], None
	for clave in orden:
		if clave == clave_actual and not completo:
			continue
		valor = acumulado[clave]["valor"]
		if anterior is None:
			variacion = "—"
		elif anterior:
			variacion = f"{(valor - anterior) / anterior * 100:+.1f}%"
		else:
			variacion = "—"
		filas.append([acumulado[clave]["etiqueta"], valor, variacion])
		anterior = valor
	return filas, medida


def _es_crecimiento_periodos(spec, pregunta):
	return (
		bool(spec.get("group_by"))
		and spec.get("group_by") == ["mes"]
		and len(spec.get("aggregations") or []) == 1
		and _pide_crecimiento_periodos(pregunta)
	)


MEDIDAS_DE_CRECIMIENTO = {
	"piezas": "Piezas",
	"pendientes": "Pendientes",
	"siniestros": "Siniestros",
}


def _titulo_crecimiento_periodos(pregunta, medida):
	granularidad = _granularidad_de_crecimiento(pregunta)
	por = {"mes": "Mes", "trimestre": "Trimestre", "cuatrimestre": "Cuatrimestre"}[granularidad]
	return f"Crecimiento de {MEDIDAS_DE_CRECIMIENTO.get(medida, 'Piezas').lower()} por {por}"


def _serie_con_variacion(resultado):
	"""Filas [mes, siniestros, variación %] a partir de la serie mensual."""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	if "mes" not in columnas or "siniestros" not in columnas:
		return []
	i_mes = columnas.index("mes")
	i_sin = columnas.index("siniestros")
	filas = []
	anterior = None
	# El mes en curso va a medias: su caída (-99.9% el día 2) no es crecimiento.
	hoy = frappe.utils.getdate(frappe.utils.nowdate())
	mes_actual = hoy.strftime("%Y-%m")
	mes_incompleto = hoy.day < frappe.utils.get_last_day(hoy).day
	for fila in resultado.get("filas") or []:
		if mes_incompleto and str(fila[i_mes])[:7] == mes_actual:
			break
		siniestros = fila[i_sin] or 0
		if anterior is None:
			variacion = "—"
		elif anterior:
			variacion = f"{(siniestros - anterior) / anterior * 100:+.1f}%"
		else:
			variacion = "—"
		filas.append([_mes_legible(fila[i_mes]), siniestros, variacion])
		anterior = siniestros
	return filas


def _crecimiento_por_dimension(resultado):
	"""Variación por grupo entre los dos últimos meses completos.

	Recibe la serie (dimensión, mes, siniestros) y devuelve una fila por grupo
	con el último mes, su variación y el mes anterior como referencia.
	"""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	if len(columnas) < 3 or "mes" not in columnas or "siniestros" not in columnas:
		return [], "", ""
	i_dim = 0
	i_mes = columnas.index("mes")
	i_sin = columnas.index("siniestros")
	hoy = frappe.utils.getdate(frappe.utils.nowdate())
	mes_actual = hoy.strftime("%Y-%m")
	incompleto = hoy.day < frappe.utils.get_last_day(hoy).day
	series = {}
	for fila in resultado.get("filas") or []:
		mes = str(fila[i_mes])[:7]
		if incompleto and mes == mes_actual:
			continue
		grupo = fila[i_dim] if fila[i_dim] not in (None, "") else "Sin dato"
		series.setdefault(grupo, {})[mes] = fila[i_sin] or 0
	filas = []
	for grupo, por_mes in series.items():
		meses = sorted(por_mes)
		if len(meses) < 2:
			continue
		previo, ultimo = meses[-2], meses[-1]
		antes, ahora = por_mes[previo] or 0, por_mes[ultimo] or 0
		if not antes:
			variacion = "—"
		else:
			variacion = f"{(ahora - antes) / antes * 100:+.1f}%"
		filas.append([grupo, ahora, variacion])
	filas.sort(key=lambda f: f[1], reverse=True)
	meses = sorted({m for por_mes in series.values() for m in por_mes})
	return filas[:10], (meses[-2] if len(meses) > 1 else ""), (meses[-1] if meses else "")


def _es_crecimiento_siniestros(spec):
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	dims = list(spec.get("group_by") or [])
	return (
		bool(dims)
		and dims[-1] == "mes"
		and len(aggs) == 1
		and str(aggs[0].get("alias") or "").lower() == "siniestros"
	)


def _pide_anios_modelo(pregunta):
	texto = _sin_acentos(pregunta or "")
	habla_anios = "ano modelo" in texto or "anos modelo" in texto
	habla_modelo_ano = "modelo" in texto and "ano" in texto
	if not (habla_anios or habla_modelo_ano):
		return False
	# Solo el ranking de años modelo: "muestra las piezas por año modelo" es un
	# desglose de piezas, no "qué años aparecen más en los siniestros".
	return any(v in texto for v in VERBOS_TOP)


def _completar_anios_modelo(spec, pregunta):
	"""“¿Qué años modelo aparecen más en los siniestros?” = top por siniestros.

	El año del vehículo es `modelo` (Int); la medida son los siniestros distintos.
	Sin período, el mes en curso (regla del dueño para siniestros).
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_anios_modelo(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "recolectable", "id_estatus"):
		filtros.pop(campo, None)
	filtros["fechaingreso"] = ["between", desde, hasta]
	filtros["modelo"] = ["is", "not null"]
	spec["filters"] = filtros
	spec["fields"] = []
	spec["group_by"] = ["modelo"]
	spec["aggregations"] = [
		{"func": "count_distinct", "field": "siniestro", "alias": "siniestros"}
	]
	spec["order_by"] = [{"field": "siniestros", "dir": "desc"}]
	spec["limit"] = _numero_por_singular_o_plural(pregunta) or 10
	spec.pop("render_chart", None)
	return spec


def _es_ranking_anios(spec):
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	return (
		spec.get("group_by") == ["modelo"]
		and len(aggs) == 1
		and str(aggs[0].get("alias") or "").lower() == "siniestros"
	)


def _pide_preferencia_region(pregunta):
	texto = _sin_acentos(pregunta or "")
	return "preferencia" in texto and "region" in texto


def _completar_preferencia_region(spec, pregunta):
	"""“Preferencia por región”: la marca/submarca con más recolectadas en cada una.

	El dueño lo pidió el 2026-11-04: cuando piden preferencia puede ser por
	región, y por cada región hay que listar la marca (o submarca) con más
	piezas recolectadas. El spec agrupa por región + marca/submarca y el top por
	región lo arma el código (`_top_por_region`).
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_preferencia_region(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	texto = _sin_acentos(pregunta)
	dimension = "tipo" if "submarca" in texto else "marca"
	spec["filters"] = {"fechaingreso": ["between", desde, hasta]}
	spec["fields"] = []
	spec["group_by"] = ["region_pais", dimension]
	spec["aggregations"] = _aggs_recolectadas(desde, hasta)
	spec.pop("order_by", None)
	spec.pop("render_chart", None)
	spec["limit"] = 500
	return spec


def _es_preferencia_region(spec):
	dims = list(spec.get("group_by") or [])
	alias = [
		str(a.get("alias") or "").lower()
		for a in (spec.get("aggregations") or [])
		if isinstance(a, dict)
	]
	return (
		dims in (["region_pais", "marca"], ["region_pais", "tipo"])
		and alias
		== ["recolectadas", "recolectadas_inconsistentes", "recolectadas_periodo"]
	)


def _top_por_region(resultado):
	"""Una fila por región: la marca/submarca con más recolectadas."""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	if len(columnas) < 4 or "region_pais" not in columnas:
		return []
	i_region = columnas.index("region_pais")
	i_dim = 1 if i_region == 0 else 0
	i_reco = [columnas.index(c) for c in (
		"recolectadas",
		"recolectadas_inconsistentes",
		"recolectadas_periodo",
	) if c in columnas]
	if not i_reco:
		return []
	mejores = {}
	for fila in resultado.get("filas") or []:
		region = fila[i_region] if fila[i_region] not in (None, "") else "Sin región"
		dimension = fila[i_dim] if fila[i_dim] not in (None, "") else "Sin dato"
		total = sum((fila[i] or 0) for i in i_reco)
		if region not in mejores or total > mejores[region][1]:
			mejores[region] = (dimension, total)
	return [[region, dim, valor] for region, (dim, valor) in sorted(mejores.items())]


def _habla_de_parte(pregunta):
	texto = _sin_acentos(pregunta or "")
	return any(
		frase in texto
		for frase in (
			"misma pieza",
			"tipos de piezas",
			"tipos de pieza",
			"tipo de piezas",
			"tipo de pieza",
			"tipo pieza",
			"tipos de autopartes",
			"tipos de autoparte",
			"tipo de autopartes",
			"tipo de autoparte",
			"tipos de refacciones",
			"tipos de refaccion",
			"tipo de refacciones",
			"tipo de refaccion",
			"autoparte",
			"refaccion",
			"parte",
		)
	)


CACHE_PARTE_PIEZA = "molduplastic_ia:parte_pieza:valores"


def _valores_parte_pieza():
	"""Catálogo de `parte_pieza` (253 valores), cacheado 24 h.

	Con el índice `idx_ia_parte` el DISTINCT es index-only (antes escaneaba 3.3M
	filas y tardaba ~1 min).
	"""
	valores = frappe.cache().get_value(CACHE_PARTE_PIEZA)
	if valores:
		return valores
	valores = [
		fila[0]
		for fila in frappe.db.sql(
			"SELECT DISTINCT parte_pieza FROM tabPieza"
			" WHERE parte_pieza IS NOT NULL AND parte_pieza <> ''"
		)
	]
	valores = sorted(valores, key=len, reverse=True)
	frappe.cache().set_value(CACHE_PARTE_PIEZA, valores, expires_in_sec=86400)
	return valores


# Palabras del catálogo de `parte_pieza` que también son del habla normal (o
# que se cruzan con la submarca): sin la palabra "parte"/"tipo pieza" de por
# medio no se convierten en filtro ("piezas por caja" no es CAJA).
PARTES_GENERICAS = {
	"ALMA",
	"ARBOL",
	"BORDA",
	"CABEZA",
	"CAJA",
	"CAJAS",
	"CAMPANA",
	"CUARTO",
	"CUERPO",
	"FECHA",
	"FAN",
	"FRENTE",
	"GATO",
	"LED",
	"LIENZO",
	"LUCES",
	"LUZ",
	"MANGO",
	"MARCO",
	"MASA",
	"METALES",
	"MICA",
	"OTROS",
	"PANEL",
	"PARED",
	"PISO",
	"PLANCHA",
	"POSTE",
	"PUENTE",
	"QUINTA",
	"RAMPA",
	"SISTEMA",
	"TAPA",
	"TAPAS",
	"TINA",
	"TUBO",
	"UNIDAD",
	"VARA",
	"VIGA",
	"YUGO",
	# También son submarca (dato sucio): "escape"/"chasis" como pieza solo con
	# la palabra "parte" de por medio, para no cruzar los dos filtros.
	"CHASIS",
	"ESCAPE",
	"PLATAFORMA",
	"REMOLQUE",
	"TUMBABURROS",
}


def _parte_base(valor):
	"""FAROS → FARO: el concepto de `piezades` usa el singular.

	El catálogo de `parte_pieza` tiene las dos formas (FARO/FAROS,
	LLANTA/LLANTAS), pero `piezades` escribe "FARO I.CPL.": si el valor
	encontrado es plural y su singular está en el catálogo, se usa el singular.
	"""
	planos = {_sin_acentos(str(v)).upper(): v for v in _valores_parte_pieza()}
	plano = _sin_acentos(str(valor)).upper()
	for sufijo in ("ES", "S"):
		if plano.endswith(sufijo) and len(plano) - len(sufijo) >= 3:
			base = plano[: -len(sufijo)]
			if base in planos:
				return planos[base]
	return valor


def _parte_mencionada(pregunta):
	"""Valor de `parte_pieza` mencionado, aunque se diga en plural.

	El dueño lo pidió el 2026-11-06: igual que la marca y la submarca, el valor
	se detecta por sí solo ("piezas FACIA", "piezas de faros", "facias
	delanteras"). Se comparan los 253 valores del catálogo con límites de palabra,
	se acepta el plural (FARO → FAROS, MOTOR → MOTORES, RIN → RINES) y se
	descartan los que también son del habla normal (`PARTES_GENERICAS`).
	"""
	texto = _sin_acentos(pregunta or "").upper()
	for valor in _valores_parte_pieza():
		plano = _sin_acentos(str(valor)).upper()
		if len(plano) < 3 or plano in PARTES_GENERICAS:
			continue
		patron = r"(?<![A-Z])" + re.escape(plano) + r"(?:ES|S)?(?![A-Z])"
		if re.search(patron, texto):
			return _parte_base(valor)
	return None


# Cómo aparece la posición/lado en `piezades`: "FACIA DEL.", "FARO I.CPL."…
ABREVIATURAS_POSICION = (
	("delantera", "DEL"),
	("delantero", "DEL"),
	("frontal", "DEL"),
	("del.", "DEL"),
	("trasera", "TRA"),
	("trasero", "TRA"),
	("posterior", "TRA"),
	("tras.", "TRA"),
	("izquierda", "I"),
	("izquierdo", "I"),
	("izq.", "I"),
	("derecha", "D"),
	("derecho", "D"),
	("der.", "D"),
	("inferior", "INF"),
	("inf.", "INF"),
	("superior", "SUP"),
	("sup.", "SUP"),
	("completa", "CPL"),
	("completo", "CPL"),
	("cpl", "CPL"),
)


def _patron_piezades(valor, pregunta):
	"""Patrón LIKE sobre `piezades` para una parte y su posición.

	El dueño lo pidió el 2026-11-06: cuando se pide una parte específica se
	busca en el **concepto** (`piezades`), no solo en la categoría `parte_pieza`.
	"facia delantera" → `FACIA%DEL%` (FACIA DEL., FACIA DEL.INF., …);
	"faro izquierdo" → `FARO%I%`; sin posición → `FACIA%`.
	"""
	texto = _sin_acentos(pregunta or "")
	abreviaturas = []
	for palabra, abreviatura in ABREVIATURAS_POSICION:
		# Se acepta el plural: "delanteras", "traseras", "izquierdos"…
		patron_palabra = (
			r"(?<![a-z0-9])" + re.escape(_sin_acentos(palabra)) + r"(?:es|s)?(?![a-z0-9])"
		)
		if re.search(patron_palabra, texto) and abreviatura not in abreviaturas:
			abreviaturas.append(abreviatura)
	patron = _sin_acentos(str(valor)).upper()
	if abreviaturas:
		patron += "%" + "%".join(abreviaturas)
	return f"{patron}%"


def _parte_de_la_pregunta(pregunta):
	"""Valor de `parte_pieza` nombrado en la pregunta ("facia", "faro", "espejo").

	Se compara contra el catálogo real (sin acentos y con límites de palabra),
	así "piezas de tipo faro" filtra FARO y no "pieza faro".
	"""
	if not _habla_de_parte(pregunta):
		return None
	texto = _sin_acentos(pregunta or "").upper()
	for valor in _valores_parte_pieza():
		plano = _sin_acentos(str(valor)).upper()
		patron = r"(?<![A-Z])" + re.escape(plano) + r"(?:ES|S)?(?![A-Z])"
		if re.search(patron, texto):
			return _parte_base(valor)
	return None


DIMENSIONES_POR_FRASE = {
	"tipos de refacciones": "parte_pieza",
	"tipos de refaccion": "parte_pieza",
	"tipos de autopartes": "parte_pieza",
	"tipos de autoparte": "parte_pieza",
	"tipos de piezas": "parte_pieza",
	"tipos de pieza": "parte_pieza",
	"misma pieza": "parte_pieza",
	"tipo de pieza": "parte_pieza",
	"tipo de autoparte": "parte_pieza",
	"tipo de refaccion": "parte_pieza",
	"tipo de refacciones": "parte_pieza",
	"refacciones": "parte_pieza",
	"refaccion": "parte_pieza",
	"tipo cdr": "tipo_carga",
	"tipo carga": "tipo_carga",
	"pais de origen": "pais_origen",
	"pais": "pais_origen",
	"submarcas": "tipo",
	"submarca": "tipo",
	"marcas": "marca",
	"marca": "marca",
	"talleres": "taller_asignado",
	"taller": "taller_asignado",
	"regiones": "region_pais",
	"region": "region_pais",
	"plazas": "id_plaza",
	"plaza": "id_plaza",
	"estados": "estadolocalidad",
	"estado": "estadolocalidad",
	"modelos": "modelo",
	"modelo": "modelo",
	"siniestros": "siniestro",
	"siniestro": "siniestro",
	"partes": "parte_pieza",
	"parte": "parte_pieza",
	"agencias": "tipo_carga",
	"agencia": "tipo_carga",
}

RE_DIMENSION_POR = re.compile(
	r"\bpor\s+(?:la\s+|el\s+)?("
	r"tipos de piezas|tipos de autopartes|tipos de refacciones|tipos de refaccion|"
	r"tipos de pieza|tipos de autoparte|tipo de piezas|tipo de autopartes|"
	r"misma pieza|"
	r"tipo de pieza|tipo de autoparte|tipo de refacciones|tipo de refaccion|"
	r"refacciones|refaccion|tipo cdr|tipo carga|pais de origen|pais|"
	r"submarcas|submarca|marcas|marca|talleres|taller|regiones|region|"
	r"plazas|plaza|estados|estado|modelos|modelo|siniestros|siniestro|"
	r"partes|parte|agencias|agencia"
	r")\b"
)


def _dimension_por_frase(pregunta):
	"""Dimensión que sigue a "por …" (evita que "parte FACIA por taller" gane parte)."""
	texto = _sin_acentos(pregunta or "")
	m = RE_DIMENSION_POR.search(texto)
	return DIMENSIONES_POR_FRASE.get(m.group(1)) if m else None


def _pide_volumen(pregunta):
	"""“Volumen” = piezas ingresadas (dueño, 2026-11-09)."""
	texto = _sin_acentos(pregunta or "")
	if not re.search(r"\bvolumen\b", texto):
		return False
	# Otras medidas tienen su camino propio.
	if any(
		p in texto
		for p in (
			"recolect",
			"recuperad",
			"pendiente",
			"no localizada",
			"siniestro",
			"efectividad",
			"porcentaje",
			"precio",
			"costo",
			"valor",
			"promedio",
		)
	):
		return False
	if " contra " in texto or _comparacion_clasificada(pregunta):
		return False
	return not (
		_pide_crecimiento_periodos(pregunta) or _pide_crecimiento_dimension(pregunta)
	)


def _completar_volumen(spec, pregunta):
	"""“Volumen” se contesta con el conteo de piezas ingresadas.

	El dueño lo fijó así el 2026-11-09: "volumen" = "piezas ingresadas". La
	dimensión sale del "por …" de la pregunta; el período y los filtros de valor
	los pone el resto de la cadena.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_volumen(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	spec["fields"] = []
	dimension = _dimension_por_frase(pregunta)
	if dimension:
		spec["group_by"] = [dimension]
	spec.pop("render_chart", None)
	spec["aggregations"] = [
		{
			"func": "count",
			"field": "*",
			"alias": "piezas",
			"filtros": {"recolectable": ["=", 1]},
		}
	]
	return spec


def _completar_parte(spec, pregunta):
	"""Agrupa o filtra por parte/tipo de pieza cuando la pregunta lo especifica.

	El dueño lo pidió el 2026-11-04: "permite agrupar por parte/tipo pieza donde
	se especifique alguna dimensión o valor específico". Si nombra un valor del
	catálogo ("piezas de facia"), filtra `parte_pieza`; si además pide una
	dimensión ("por taller", "por región"), agrupa por ella; si solo habla de
	tipo de pieza, agrupa por `parte_pieza`.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if _pide_promedio(pregunta) or _pide_efectividad(pregunta) or _es_spec_efectividad(spec):
		return spec
	if _pide_resumen(pregunta) or _pide_recolectadas(pregunta) or _pide_tabla_comparativa(pregunta):
		return spec
	# Los listados/agregados de precios ("lista los precios de las facias…") ya
	# tienen su forma: `_completar_parte` no debe convertirlos en un conteo.
	if _pide_piezas_con_precio(pregunta) or _pide_solo_precios(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	# Los listados de precio (prioridad, más caras) tienen su propio formato; los
	# demás campos que devuelva el modelo se reemplazan por el conteo.
	if spec.get("fields") and (
		_pide_piezas_caras(pregunta) or _pide_prioridad_pendientes(pregunta)
	):
		return spec
	valor = _parte_de_la_pregunta(pregunta) or _parte_mencionada(pregunta)
	if not valor and not _habla_de_parte(pregunta):
		return spec
	# Solo la dimensión que sigue a "por …": "piezas de parte FACIA" es un
	# conteo, no un desglose por categoría.
	dimension = _dimension_por_frase(pregunta)
	if not dimension and not valor:
		# "muestra las partes ingresadas de la marca Tesla": el objeto es la
		# parte, así que va el desglose por parte_pieza (tabla), no un total.
		dimensiones = _dimensiones_de_la_pregunta(pregunta)
		if dimensiones and dimensiones[0] == "parte_pieza":
			dimension = "parte_pieza"
	union = {
		"recolectadas",
		"recolectadas_inconsistentes",
		"recolectadas_periodo",
	}
	alias = {
		str(a.get("alias") or "").lower()
		for a in (spec.get("aggregations") or [])
		if isinstance(a, dict)
	}
	es_union = bool(alias) and alias <= union
	filtros = dict(spec.get("filters") or {})
	if valor:
		# La parte específica se busca en el concepto (`piezades`), que distingue
		# delantera/trasera/lado ("FACIA DEL.", "FARO I.CPL.").
		filtros.pop("parte_pieza", None)
		filtros["piezades"] = ["like", _patron_piezades(valor, pregunta)]
		filtros.pop("fechaingreso", None)
		periodo = _periodo_del_spec(spec) or _periodo_mes_actual()
		filtros["fechaingreso"] = ["between", periodo[0], periodo[1]]
	spec["filters"] = filtros
	if not es_union:
		spec["fields"] = []
		spec["aggregations"] = [
			{
				"func": "count",
				"field": "*",
				"alias": "piezas",
				"filtros": {"recolectable": ["=", 1]},
			}
		]
		if dimension:
			spec["group_by"] = [dimension]
			spec.setdefault("limit", 50)
		else:
			spec["group_by"] = []
			spec["limit"] = 1
			spec.pop("order_by", None)
	spec.pop("render_chart", None)
	if spec.get("group_by") and not spec.get("order_by"):
		spec["order_by"] = [{"field": "piezas", "dir": "desc"}]
	return spec


def _pide_piezas_mas_danadas(pregunta):
	"""“más dañadas” es igual a “más ingresadas” (dueño, 2026-11-05).

	Con "submarca" manda `_completar_submarcas_danadas` (agrupa por `tipo`).
	"""
	texto = _sin_acentos(pregunta or "")
	return "mas danad" in texto and "submarca" not in texto


CACHE_CATALOGOS = "molduplastic_ia:valores:filtros"
_CATALOGOS_MEMO = {}


def _catalogos_de_valores():
	"""Valores de los campos filtrables (Marcas, tipo, Taller, Plazas, estados)."""
	if _CATALOGOS_MEMO.get("valor"):
		return _CATALOGOS_MEMO["valor"]
	cache = frappe.cache().get_value(CACHE_CATALOGOS)
	if cache:
		_CATALOGOS_MEMO["valor"] = cache
		return cache
	from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import ESTADOS

	# `Pieza.tipo` (submarca) no coincide con Modelos.submarca: se lee de la
	# propia Pieza (con el índice idx_ia_tipo el DISTINCT es index-only).
	tipos = [
		fila[0]
		for fila in frappe.db.sql(
			"SELECT DISTINCT tipo FROM tabPieza WHERE tipo IS NOT NULL AND tipo <> ''"
		)
	]
	catalogos = {
		"tipo": tipos,
		"marca": [m.get("name") for m in frappe.get_all("Marcas", fields=["name"])],
		"taller_asignado": [t.get("name") for t in frappe.get_all("Taller", fields=["name"])],
		"id_plaza": [p.get("name") for p in frappe.get_all("Taller-Plazas", fields=["name"])],
		"estadolocalidad": list(ESTADOS.keys()),
	}
	catalogos = {
		campo: sorted({str(v) for v in valores if v}, key=len, reverse=True)
		for campo, valores in catalogos.items()
	}
	# tipo_carga va por nombre del catálogo (10=Taller, 11=Agencia…).
	etiquetas = {
		str(f.descripcion or ""): str(f.name)
		for f in frappe.get_all(
			"Catalogo Datos", filters={"name": ["in", ["10", "11", "12", "13"]]},
			fields=["name", "descripcion"],
		)
	}
	catalogos["tipo_carga_map"] = etiquetas
	frappe.cache().set_value(CACHE_CATALOGOS, catalogos, expires_in_sec=86400)
	_CATALOGOS_MEMO["valor"] = catalogos
	return catalogos


def _valor_mencionado(pregunta, valores):
	"""El valor de la lista que aparece en la pregunta (el más largo)."""
	texto = _sin_acentos(pregunta or "").upper()
	for valor in valores:
		plano = _sin_acentos(str(valor)).upper()
		if len(plano) < 3:
			continue
		if re.search(r"(?<![A-Z0-9])" + re.escape(plano) + r"(?![A-Z0-9])", texto):
			return valor
	return None


def _filtro_de_precio(pregunta):
	"""Rango de `preciovale` para "más de 50 mil", "menos de $20,000", "entre 10 y 20 mil"."""
	texto = _sin_acentos(pregunta or "").lower().replace(",", "")

	def _monto(cadena):
		m = re.search(r"(\d+(?:\.\d+)?)\s*(mil|millon(?:es)?)?", cadena)
		if not m:
			return None
		valor = float(m.group(1))
		unidad = m.group(2) or ""
		if unidad.startswith("mil"):
			valor *= 1000
		elif unidad.startswith("millon"):
			valor *= 1000000
		return valor

	m = re.search(r"entre\s+(?:\$\s*)?([\d.]+\s*(?:mil|millon(?:es)?)?)\s+y\s+(?:\$\s*)?([\d.]+\s*(?:mil|millon(?:es)?)?)", texto)
	if m:
		desde, hasta = _monto(m.group(1)), _monto(m.group(2))
		if desde is not None and hasta is not None:
			# "entre 10 y 20 mil": la unidad del segundo número aplica a los dos.
			if desde < 1000 <= hasta:
				desde *= 1000
			return ["between", desde, hasta]
	m = re.search(r"(?:mas de|mayor a|arriba de|superior a|que pasan de)\s+(?:\$\s*)?([\d.]+\s*(?:mil|millon(?:es)?)?)", texto)
	if m and (monto := _monto(m.group(1))) is not None:
		return [">=", monto]
	m = re.search(r"(?:menos de|menor a|por debajo de|hasta)\s+(?:\$\s*)?([\d.]+\s*(?:mil|millon(?:es)?)?)", texto)
	if m and (monto := _monto(m.group(1))) is not None:
		return ["<=", monto]
	return None


# Verbos de ranking: "¿qué X aparecen/hay/tienen/existen con más…?"
VERBOS_TOP = (
	"aparecen",
	"aparezcan",
	"hay",
	"tienen",
	"tiene",
	"existen",
	"existe",
	"mas",
	"frecuente",
	"comun",
)

# Verbos equivalentes a "filtrar" (dueño, 2026-11-04).
VERBOS_DE_FILTRO = ("filtr", "dame", "dime", "lista", "cuales", "muestra")


def _sinonimo_de_que(pregunta):
	"""“¿de qué …?” es sinónimo de “¿qué …?” (dueño, 2026-11-05).

	Se usa solo para las detecciones (marca, submarca, periodo, dimensiones…);
	el texto que se guarda en la conversación sigue siendo el original.
	"""
	return re.sub(r"^\s*¿?\s*de\s+qu[eé]\b", "qué", pregunta or "", flags=re.IGNORECASE)


def _cada_como_por(pregunta):
	"""“cada X” y “por cada X” son “por X” (dueño, 2026-11-10).

	Trabaja sobre el texto normalizado para encontrar la mención y reconstruye
	la pregunta original con “por X”: así no se pierden acentos ni mayúsculas del
	resto de la frase. Solo aplica cuando sigue una dimensión conocida
	(`NOMBRES_DIMENSION`), para no tocar “cada 1,000 piezas”.
	"""
	if not pregunta:
		return pregunta
	plano = _sin_acentos(pregunta)
	nombres = "|".join(
		sorted({_sin_acentos(nombre) for nombre in NOMBRES_DIMENSION}, key=len, reverse=True)
	)
	patron = re.compile(rf"\b(?:por\s+)?cada\s+({nombres})\b", re.IGNORECASE)
	trozos, ultimo = [], 0
	for m in patron.finditer(plano):
		trozos.append(pregunta[ultimo : m.start()])
		trozos.append("por " + pregunta[m.start(1) : m.end(1)])
		ultimo = m.end()
	trozos.append(pregunta[ultimo:])
	return "".join(trozos)


def _pide_filtro_danadas(pregunta):
	if _pide_promedio(pregunta):
		return False
	if _pide_piezas_con_precio(pregunta) or _pide_solo_precios(pregunta):
		return False
	texto = _sin_acentos(pregunta or "")
	if "mas danad" in texto:
		# "dame las piezas más dañadas" es el ranking de partes, no un filtro.
		return False
	return (
		any(v in texto for v in VERBOS_DE_FILTRO)
		and "danad" in texto
		and ("pieza" in texto or "refaccion" in texto)
	)


def _completar_filtro_danadas(spec, pregunta):
	"""En preguntas de "filtrar", "piezas dañadas" = "piezas ingresadas".

	El dueño lo pidió el 2026-11-04: al filtrar, "dañadas" no es un estatus, es la
	pieza que ingresó; la medida es count(*) con recolectable = 1 y se conservan
	los filtros y la agrupación que pida la pregunta.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_filtro_danadas(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	alias = {
		str(a.get("alias") or "").lower()
		for a in (spec.get("aggregations") or [])
		if isinstance(a, dict)
	}
	if alias and alias <= {
		"recolectadas",
		"recolectadas_inconsistentes",
		"recolectadas_periodo",
	}:
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	filtros = dict(spec.get("filters") or {})
	filtros.setdefault("fechaingreso", ["between", desde, hasta])
	spec["filters"] = filtros
	spec["fields"] = []
	spec["aggregations"] = [
		{
			"func": "count",
			"field": "*",
			"alias": "piezas",
			"filtros": {"recolectable": ["=", 1]},
		}
	]
	# La dimensión sale de "por …" (taller, región, tipo de pieza…), no del modelo.
	dimension = _dimension_por_frase(pregunta)
	if dimension:
		spec["group_by"] = [dimension]
	# El order_by del modelo apuntaba a su alias ("ingresadas"): se descarta.
	orden = spec.get("order_by")
	if orden and not all(
		o.get("field") == "piezas" for o in orden if isinstance(o, dict)
	):
		spec.pop("order_by", None)
	spec.setdefault("limit", 50)
	spec.pop("render_chart", None)
	return spec


def _sanear_order_by(spec):
	"""Quita los `order_by` que apunten a una columna que ya no existe.

	Los completers reemplazan las agregaciones del modelo (su alias "ingresadas"
	o "piezas" deja de existir) y el `order_by` viejo tumbaba la consulta con
	"order_by sobre columna inexistente". Se conserva solo lo que apunta a una
	columna real del spec (group_by, alias de agregaciones o `fields`).
	"""
	if not isinstance(spec, dict):
		return spec
	alias = {
		str(a.get("alias") or "").strip().lower()
		for a in spec.get("aggregations") or []
		if isinstance(a, dict) and a.get("alias")
	}
	validos = alias | {str(g).strip().lower() for g in spec.get("group_by") or []}
	validos |= {str(f).strip().lower() for f in spec.get("fields") or []}
	orden = spec.get("order_by")
	if not orden:
		return spec
	limpio = [
		o
		for o in orden
		if isinstance(o, dict) and str(o.get("field") or "").strip().lower() in validos
	]
	if limpio:
		spec["order_by"] = limpio
	else:
		spec.pop("order_by", None)
	return spec


def _completar_dimension(spec, pregunta):
	"""Agrupa por la dimensión que siga a "por …" (dueño, 2026-11-05).

	Cubre taller, región, plaza, estado, marca, submarca, tipo de pieza, tipo CDR,
	país de origen, modelo y siniestro. No toca el resumen de piezas, las series
	por mes ni los listados de campos (cada uno tiene su propio formato).
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		# Los campos de Recoleccion/Taller son otros (region, no region_pais).
		return spec
	dimension = _dimension_por_frase(pregunta)
	if not dimension:
		return spec
	if spec.get("fields"):
		return spec
	if "mes" in (spec.get("group_by") or []) or _es_crecimiento_siniestros(spec):
		# La serie mensual (y el crecimiento por dimensión) ya tiene su forma.
		return spec
	if _pide_resumen(pregunta):
		return spec
	if _es_preferencia_region(spec):
		return spec
	if (
		_es_promedio_ratio(spec)
		or _es_promedio_precio(spec)
		or _es_promedio_siniestro(spec)
		or _es_promedio_siniestros_taller(spec)
	):
		return spec
	if _pide_tabla_comparativa(pregunta):
		# El cruce necesita sus dos dimensiones: no se colapsan a la de "por …".
		return spec
	spec["group_by"] = [dimension]
	return spec


# Palabras del habla normal que también existen como `tipo` en la base (dato
# sucio): no deben convertirse en filtro ("los autos que más siniestros…").
PALABRAS_GENERICAS = {
	"AUTO",
	"AUTOS",
	"CARRO",
	"CARROS",
	"CAMION",
	"CAMIONES",
	"MOTO",
	"MOTOS",
	"VEHICULO",
	"VEHICULOS",
	"PIEZA",
	"PIEZAS",
	"UNIDAD",
	"UNIDADES",
	"FLOTA",
	"MARCA",
	"MARCAS",
	"SUBMARCA",
	"SUBMARCAS",
	"MODELO",
	"MODELOS",
}


NOMBRES_DIMENSION = {
	"tipos de refacciones": "parte_pieza",
	"tipos de refaccion": "parte_pieza",
	"tipos de autopartes": "parte_pieza",
	"tipos de autoparte": "parte_pieza",
	"tipos de piezas": "parte_pieza",
	"tipos de pieza": "parte_pieza",
	"misma pieza": "parte_pieza",
	"tipo de autopartes": "parte_pieza",
	"tipo de autoparte": "parte_pieza",
	"tipo de refacciones": "parte_pieza",
	"tipo de refaccion": "parte_pieza",
	"tipo de piezas": "parte_pieza",
	"tipo de pieza": "parte_pieza",
	"refacciones": "parte_pieza",
	"refaccion": "parte_pieza",
	"tipo cdr": "tipo_carga",
	"tipo carga": "tipo_carga",
	"pais de origen": "pais_origen",
	"paises de origen": "pais_origen",
	"submarcas": "tipo",
	"submarca": "tipo",
	"marcas": "marca",
	"marca": "marca",
	"talleres": "taller_asignado",
	"taller": "taller_asignado",
	"regiones": "region_pais",
	"region": "region_pais",
	"plazas": "id_plaza",
	"plaza": "id_plaza",
	"estados": "estadolocalidad",
	"estado": "estadolocalidad",
	"modelos": "modelo",
	"modelo": "modelo",
	"siniestros": "siniestro",
	"siniestro": "siniestro",
	"partes": "parte_pieza",
	"parte": "parte_pieza",
	"agencias": "tipo_carga",
	"agencia": "tipo_carga",
}


def _dimensiones_de_la_pregunta(pregunta):
	"""Dimensiones nombradas, en el orden en que aparecen (máximo 3)."""
	texto = _sin_acentos(pregunta or "")
	encontradas = []
	nombres = sorted(NOMBRES_DIMENSION, key=len, reverse=True)
	for nombre in nombres:
		for m in re.finditer(r"\b" + re.escape(nombre) + r"\b", texto):
			encontradas.append((m.start(), NOMBRES_DIMENSION[nombre]))
	encontradas.sort()
	vistas = []
	for _, campo in encontradas:
		if campo not in vistas:
			vistas.append(campo)
	return vistas[:3]


def _dimensiones_para_cruce(pregunta):
	"""Dimensiones del cruce: las del "por …" más "misma pieza" y "distintos X"."""
	dimensiones = list(_dimensiones_tras_por(pregunta))
	texto = _sin_acentos(pregunta or "")
	if "misma pieza" in texto and "parte_pieza" not in dimensiones:
		# "misma pieza" es "por parte": va primero si no hay un "por" que mande.
		dimensiones.insert(0, "parte_pieza")
	distintas = _pide_agrupar_por_distintas(pregunta)
	if distintas and distintas not in dimensiones:
		dimensiones.append(distintas)
	return dimensiones[:3]


def _pide_tabla_comparativa(pregunta):
	"""Tabla cruzada: la piden explícitamente o hay varias dimensiones.

	El dueño lo pidió el 2026-11-07: "detecta si hay varias dimensiones para que
	realices una tabla cruzada". Un desglose simple con dos dimensiones
	("piezas por marca y región") se cruza; las preguntas con formato propio
	(listados, promedios, efectividad, pendientes, precios, rankings…) no.
	"""
	texto = _sin_acentos(pregunta or "")
	if re.search(r"\bcontra\b", texto):
		# "A contra B" tiene su propio camino (`_completar_comparacion`): no es
		# un cruce de dimensiones.
		return False
	pide = any(
		frase in texto
		for frase in ("comparativ", "tabla cruzada", "doble entrada", "cruce de")
	)
	dimensiones = _dimensiones_para_cruce(pregunta)
	if pide:
		return len(dimensiones) >= 2
	if len(dimensiones) < 2:
		return False
	if _es_listado(pregunta):
		return False
	if _pide_promedio_siniestros_taller(pregunta):
		# "siniestros por taller por región" es un promedio agrupado, no un cruce.
		return False
	if "pendiente" in texto:
		# Las pendientes tienen su propia definición; no se cruzan como piezas.
		return False
	for detector in (
		_pide_efectividad,
		_pide_recolectadas,
		_pide_no_recuperadas,
		_pide_resumen,
		_pide_no_localizadas,
		_pide_siniestros_pendientes,
		_pide_siniestros_por_cdr,
		_pide_prioridad_pendientes,
		_pide_piezas_caras,
		_pide_piezas_con_precio,
		_pide_solo_precios,
		_pide_submarcas_danadas,
		_pide_promedio,
		_pide_marcas_accidentadas,
		_pide_pais_origen,
		_pide_crecimiento_siniestros,
		_pide_anios_modelo,
		_pide_preferencia_region,
		_pide_piezas_mas_danadas,
		_pide_filtro_danadas,
		_pide_recolectan_mas,
	):
		if detector(pregunta):
			return False
	return True


# ── Comparación “A contra B” (dueño, 2026-11-07) ─────────────────────────────
# "La palabra contra significa que compares dos cosas": la pregunta "X contra Y"
# se contesta con las dos cosas. Quedan fuera las frases donde "contra" ya tiene
# otro sentido: "contra la meta/el objetivo" (efectividad contra el 75%) y
# "recolectadas contra ingresadas" (la efectividad de siempre).
CONTRA_SIN_COMPARAR = (
	"contra la meta",
	"contra el objetivo",
	"contra lo ingresado",
	"contra ingresadas",
	"contra ingresos",
	"contra lo que ingreso",
)
GRUPOS_DE_TIPO_CARGA = (
	("equipo pesado", "13", "Equipo pesado"),
	("audavales", "12", "Audavales"),
	("audaval", "12", "Audavales"),
	("agencias", "11", "Agencias"),
	("agencia", "11", "Agencias"),
	("talleres", "10", "Talleres"),
	("taller", "10", "Talleres"),
)
# (palabra, medida interna, etiqueta). Medida None = no soportada: la pregunta
# se queda como estaba.
MEDIDAS_CONTRA = (
	("no localizada", "no_localizadas", "No localizadas"),
	("no recolectad", None, ""),
	("no entrega", None, ""),
	("no recuperad", None, ""),
	("recolectad", "recolectadas", "Recolectadas"),
	("pendiente", "pendientes", "Pendientes"),
	("siniestro", None, ""),
	("ingresad", "ingresadas", "Ingresadas"),
	("pieza", "ingresadas", "Piezas"),
	("totales", "ingresadas", "Ingresadas"),
	("total", "ingresadas", "Ingresadas"),
)
RE_RELLENO_LADO = re.compile(
	r"\b(?:las?|los|el|del|de|en|al|para|por|con|piezas?|refacciones?|recoleccion"
	r"|efectividad|porcentaje|tasa|nivel|indice)\b",
	re.IGNORECASE,
)
RE_PERIODO_LADO = re.compile(
	rf"\b(?:{'|'.join(MESES_ES)})(?:\s+(?:de|del)\s+(?:19|20)\d{{2}})?\b"
	r"|\b(?:este|el)\s+(?:mes|ano)\b|\bmes\s+(?:pasado|anterior)\b"
	r"|\b(?:19|20)\d{2}\b"
	r"|\b(?:primer|primero|primera|segundo|segunda|tercer|tercero|tercera|cuarto|cuarta"
	r"|1er|1ro|2do|3er|3ro|4to|4ta|actual|corriente|en\s+curso)?\s*"
	r"(?:trimestres?|cuatrimestres?)\b",
	re.IGNORECASE,
)


def _lados_contra(pregunta):
	"""(izquierda, derecha) de "A contra B", o None si no es una comparación."""
	if not isinstance(pregunta, str):
		return None
	texto = _limpio(pregunta)
	if not re.search(r"\bcontra\b", texto, re.IGNORECASE):
		return None
	if any(frase in _sin_acentos(texto) for frase in CONTRA_SIN_COMPARAR):
		return None
	izq, der = re.split(r"\s+contra\s+", texto, maxsplit=1, flags=re.IGNORECASE)
	izq = izq.strip(" ?¿.,;:")
	der = der.strip(" ?¿.,;:")
	if not izq or not der:
		return None
	return izq, der


def _rango_de_lado(texto):
	"""Rango de fechas del lado si es un período ("septiembre", "este mes", "2026")."""
	texto = _sin_acentos(texto or "").strip()
	bloque = _bloque_de_la_pregunta(texto)
	if bloque:
		return bloque
	hoy = frappe.utils.getdate(frappe.utils.nowdate())
	m = re.search(rf"\b({'|'.join(MESES_ES)})(?:\s+(?:de|del)\s+((?:19|20)\d{{2}}))?\b", texto)
	if m:
		mes = MESES_ES.index(m.group(1)) + 1
		anio = int(m.group(2)) if m.group(2) else hoy.year
		primero = frappe.utils.getdate(f"{anio}-{mes:02d}-01")
		return (
			f"{_dia(primero)} 00:00:00",
			f"{_dia(frappe.utils.get_last_day(primero))} 23:59:59",
		)
	if re.search(r"\b(?:este|el)\s+mes\b|\bmes\s+en\s+curso\b", texto):
		return _rango_de_periodo("este_mes")
	if re.search(r"\b(?:el\s+)?mes\s+(?:pasado|anterior)\b", texto):
		return _rango_de_periodo("mes_pasado")
	if re.search(r"\b(?:este|el)\s+ano\b|\bano\s+en\s+curso\b", texto):
		return _rango_de_periodo("este_anio")
	m = re.search(r"\b((?:19|20)\d{2})\b", texto)
	if m:
		anio = m.group(1)
		return f"{anio}-01-01 00:00:00", f"{anio}-12-31 23:59:59"
	return None


def _etiqueta_de_rango(rango):
	"""“Septiembre de 2026” / “2026” / “1 de septiembre de 2026” para el título."""
	desde, hasta = rango
	d1 = frappe.utils.getdate(desde)
	d2 = frappe.utils.getdate(hasta)
	meses = (d2.year - d1.year) * 12 + d2.month - d1.month + 1
	completo = d1.day == 1 and d2 == frappe.utils.getdate(
		frappe.utils.get_last_day(frappe.utils.add_months(d1, meses - 1))
	)
	if completo and d1.year == d2.year and meses in (3, 4) and (d1.month - 1) % meses == 0:
		# "1er trimestre de 2026", "3er cuatrimestre de 2026".
		indice = (d1.month - 1) // meses + 1
		nombre = "trimestre" if meses == 3 else "cuatrimestre"
		sufijo = {1: "er", 2: "do", 3: "er", 4: "to"}.get(indice, "")
		return f"{indice}{sufijo} {nombre} de {d1.year}"
	if completo:
		if d1.month == 1 and d2.month == 12:
			return str(d1.year)
		return f"{MESES_ES[d1.month - 1].capitalize()} de {d1.year}"
	return f"{_fecha_legible(desde)} al {_fecha_legible(hasta)}"


def _nucleo_de_lado(lado):
	texto = RE_PERIODO_LADO.sub(" ", _limpio(lado or ""))
	texto = RE_RELLENO_LADO.sub(" ", texto)
	return " ".join(texto.split())


# Estados y sus alias (CDMX, Edo. Méx., etc.) → nombre canónico. Se compara sin
# acentos y con límites de palabra, el nombre más largo primero ("Ciudad de
# México" antes que "México").
_CLAVES_ESTADO = {}
for _nombre in ESTADOS:
	_CLAVES_ESTADO.setdefault(_sin_acentos(_nombre).upper(), _nombre)
for _alias, _nombre in ALIAS_ESTADO.items():
	_CLAVES_ESTADO.setdefault(_sin_acentos(_alias).upper(), _nombre)


def _estado_mencionado(texto):
	"""("Nuevo León", "NUEVO LEON") del estado que nombra el texto, o (None, None)."""
	plano = _sin_acentos(texto or "").upper()
	for clave in sorted(_CLAVES_ESTADO, key=len, reverse=True):
		if re.search(r"(?<![A-Z0-9])" + re.escape(clave) + r"(?![A-Z0-9])", plano):
			return _CLAVES_ESTADO[clave], clave
	return None, None


def _plaza_mencionada(texto):
	"""Código de plaza escrito en mayúsculas ("MTY", "GDL", "CDMX2").

	En mayúsculas a propósito: "Leon" es también una submarca (Seat Leon) y en
	minúsculas/mixto se prefiere el catálogo del vehículo. Con la pista "plaza"
	la búsqueda es la de siempre (`_completar_valor_especifico`).
	"""
	original = str(texto or "")
	for plaza in sorted(_catalogos_de_valores().get("id_plaza") or [], key=len, reverse=True):
		codigo = str(plaza)
		if len(codigo) < 3:
			continue
		if re.search(r"(?<![A-Za-z0-9])" + re.escape(codigo) + r"(?![A-Za-z0-9])", original):
			return plaza, codigo
	return None, None


def _region_mencionada(texto):
	plano = _sin_acentos(texto or "")
	for region in sorted(REGIONES, key=len, reverse=True):
		if re.search(r"\b" + re.escape(_sin_acentos(region)) + r"\b", plano):
			return region, _sin_acentos(region)
	return None, None


def _ciudad_mencionada(texto):
	"""("Nuevo León", "monterrey") si el texto nombra una ciudad del catálogo."""
	from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import CIUDADES

	plano = _sin_acentos(texto or "")
	for ciudad in sorted(CIUDADES, key=len, reverse=True):
		if len(ciudad) < 4:
			continue
		if re.search(r"\b" + re.escape(_sin_acentos(ciudad)) + r"\b", plano):
			return CIUDADES[ciudad], _sin_acentos(ciudad)
	return None, None


# `tabPieza.estadolocalidad` guarda CDMX como "Distrito Federal" (y en otras
# bases como "Ciudad de México"/"CDMX"): con `=` la pregunta salía en 0.
VARIANTES_CDMX_PIEZA = (
	"Distrito Federal",
	"Ciudad de México",
	"CDMX",
	"CD DE MEXICO",
	"CIUDAD DE MEXICO",
)


def _condicion_de_estado(valor):
	"""Condición de `estadolocalidad` para un estado (CDMX con sus variantes)."""
	if _sin_acentos(str(valor)) in (
		"cdmx",
		"ciudad de mexico",
		"cd de mexico",
		"distrito federal",
	):
		return ["in", list(VARIANTES_CDMX_PIEZA)]
	return ["=", valor]


def _claves_del_estado(valor):
	"""Valores con los que puede aparecer un estado en el resultado."""
	if _condicion_de_estado(valor)[0] == "in":
		return {str(v).casefold() for v in VARIANTES_CDMX_PIEZA}
	return {str(valor).casefold()}


def _lugar_mencionado(texto, con_plazas=True):
	"""(campo, valor, mención) del lugar que nombra la pregunta.

	Orden: región con la palabra "región" (y sus alias), estado (32 nombres y
	sus abreviaturas), plaza en mayúsculas y, al final, el nombre suelto de una
	región ("Bajío", "Norte") o de una ciudad ("Monterrey" → Nuevo León). El
	dueño lo pidió el 2026-11-07: "detecta estados, plazas y regiones".
	`con_plazas=False` cuando la pregunta habla de una submarca: "Leon" es una
	submarca (Seat Leon) y también una plaza y una ciudad.
	"""
	region = _region_de_la_pregunta(texto)
	if region:
		return "region_pais", region, region
	estado, mencion = _estado_mencionado(texto)
	if estado:
		return "estadolocalidad", estado, mencion
	if con_plazas:
		plaza, mencion = _plaza_mencionada(texto)
		if plaza:
			return "id_plaza", plaza, mencion
	region, mencion = _region_mencionada(texto)
	if region:
		return "region_pais", region, mencion
	if con_plazas:
		# Ciudades del catálogo al final: "Monterrey" y "León" son de un estado,
		# no de una plaza (la plaza se escribe en mayúsculas: MTY, LEON).
		estado, mencion = _ciudad_mencionada(texto)
		if estado:
			return "estadolocalidad", estado, mencion
	return None, None, None


def _taller_mencionado(pregunta):
	"""Nombre exacto de taller que aparece en la pregunta (6,707 valores).

	El dueño lo pidió el 2026-11-09: "detecta talleres", con el mismo criterio
	que la marca y la submarca — sin necesidad de decir "taller".
	"""
	valor = _valor_mencionado(pregunta, _catalogos_de_valores().get("taller_asignado") or [])
	return (valor, _sin_acentos(str(valor))) if valor else (None, None)


def _valor_de_lado(nucleo):
	"""Campo y valor del catálogo que nombra un lado de la comparación."""
	catalogos = _catalogos_de_valores()
	for pista, campo in (
		("marca", "marca"),
		("submarca", "tipo"),
		("taller", "taller_asignado"),
		("plaza", "id_plaza"),
		("estado", "estadolocalidad"),
	):
		if re.search(rf"\b{pista}\b", _sin_acentos(nucleo)):
			valor = _valor_mencionado(nucleo, catalogos.get(campo) or [])
			if valor:
				return campo, valor
	# Estados, plazas y regiones antes de marca/submarca: "Nuevo León" es un
	# estado, no la submarca "Leon" (dueño, 2026-11-07).
	campo, valor, _ = _lugar_mencionado(nucleo)
	if campo:
		return campo, valor
	if not re.search(r"\b(?:marca|submarca|modelo)\b", _sin_acentos(nucleo)):
		taller = _valor_mencionado(nucleo, catalogos.get("taller_asignado") or [])
		if taller:
			return "taller_asignado", taller
	for campo in ("marca", "tipo", "taller_asignado"):
		valor = _valor_mencionado(nucleo, catalogos.get(campo) or [])
		if valor:
			return campo, valor
	return None, None


def _lado_comparado(lado):
	"""Clasifica un lado de la comparación.

	Devuelve ("periodo", rango, etiqueta), ("grupo", campo, valor, etiqueta),
	("medida", nombre, etiqueta) o None si no se reconoce.
	"""
	if not lado:
		return None
	rango = _rango_de_lado(lado)
	nucleo = _nucleo_de_lado(lado)
	if rango and not nucleo:
		return ("periodo", rango, _etiqueta_de_rango(rango))
	if not nucleo:
		return None
	nucleo_plano = _sin_acentos(nucleo)
	for palabra, medida, etiqueta in MEDIDAS_CONTRA:
		if re.search(rf"\b{palabra}", nucleo_plano):
			return ("medida", medida, etiqueta) if medida else None
	for palabra, valor, etiqueta in GRUPOS_DE_TIPO_CARGA:
		if nucleo_plano in (palabra, palabra.rstrip("s")):
			return ("grupo", "tipo_carga", valor, etiqueta)
	campo, valor = _valor_de_lado(nucleo)
	if campo:
		etiqueta = (
			str(valor)
			if campo in ("id_plaza", "taller_asignado")
			else _titulo_legible(valor)
		)
		return ("grupo", campo, valor, etiqueta)
	return None


def _etiqueta_del_lado(lado):
	return lado[3] if lado[0] == "grupo" else lado[2]


def _comparacion_clasificada(pregunta):
	"""Los dos lados ya clasificados, o None si "contra" no compara dos cosas."""
	lados = _lados_contra(pregunta)
	if not lados:
		return None
	izq = _lado_comparado(lados[0])
	der = _lado_comparado(lados[1])
	if not izq or not der:
		return None
	return izq, der


def _medida_de_la_pregunta(pregunta):
	""""Piezas" por defecto; recolectadas/pendientes/no localizadas si se nombran."""
	texto = _sin_acentos(pregunta or "")
	for palabra, medida in (
		("no localizada", "no_localizadas"),
		("recolectad", "recolectadas"),
		("pendiente", "pendientes"),
		("no recolectad", "ingresadas"),
	):
		if palabra in texto:
			return medida
	return "ingresadas"


def _aggs_de_medida(medida, desde, hasta):
	"""Agregaciones de una medida de la comparación, con su período adentro."""
	if medida == "recolectadas":
		return _aggs_recolectadas(desde, hasta)
	filtros = {"fechaingreso": ["between", desde, hasta], "recolectable": ["=", 1]}
	if medida == "pendientes":
		filtros.update({"id_estatus": ["is", "empty"], "fecha_hora_recoleccion": ["is", "empty"]})
	elif medida == "no_localizadas":
		filtros["id_estatus"] = ["=", 206]
	return [{"func": "count", "field": "*", "alias": medida, "filtros": filtros}]


def _completar_comparacion(spec, pregunta):
	"""“A contra B” se contesta con las dos cosas (dueño, 2026-11-07).

	Tres formas, según lo que se compare:
	- dos valores (o dos grupos) de la misma dimensión → `group_by` de esa
	  dimensión y filtro `in` con los dos valores: una fila por lado;
	- dos medidas ("recolectadas contra pendientes") → una columna por medida;
	- dos períodos ("septiembre contra octubre") → una columna por período.
	Las dos últimas van en una sola fila y las arma `_tabla_comparacion_contra`;
	la efectividad entre dos períodos se ejecuta en dos consultas (el período de
	un lado no cabe en el `WHERE` del otro).
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	lados = _comparacion_clasificada(pregunta)
	if not lados:
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	izq, der = lados
	periodo = _periodo_del_spec(spec) or _periodo_mes_actual()
	desde, hasta = periodo
	efectividad = _es_spec_efectividad(spec)
	spec["fields"] = []
	spec.pop("min_ingresadas", None)
	spec.pop("order_by", None)
	spec.pop("render_chart", None)
	if izq[0] == "grupo" and der[0] == "grupo" and izq[1] == der[1] and izq[2] != der[2]:
		# (a) Dos valores de la misma dimensión.
		campo = izq[1]
		filtros = dict(spec.get("filters") or {})
		for clave in ("fechaingreso", "fecha_hora_recoleccion", "recolectable", "id_estatus", campo):
			filtros.pop(clave, None)
		valores_lado = []
		for lado in (izq, der):
			if campo == "estadolocalidad":
				condicion = _condicion_de_estado(lado[2])
				valores_lado.extend(condicion[1] if condicion[0] == "in" else [condicion[1]])
			else:
				valores_lado.append(lado[2])
		filtros[campo] = ["in", valores_lado]
		spec["filters"] = filtros
		spec["group_by"] = [campo]
		spec["limit"] = 50
		if efectividad:
			# Se rearman con el período del spec: así el reintento al año en
			# curso (período vacío) mide la ventana correcta.
			spec["aggregations"] = _aggs_efectividad(desde, hasta)
		else:
			spec["aggregations"] = _aggs_de_medida(_medida_de_la_pregunta(pregunta), desde, hasta)
		return spec
	if izq[0] != der[0] or izq[0] not in ("medida", "periodo"):
		return spec
	if efectividad and izq[0] == "medida":
		return spec
	filtros = dict(spec.get("filters") or {})
	for clave in ("fechaingreso", "fecha_hora_recoleccion", "recolectable", "id_estatus"):
		filtros.pop(clave, None)
	spec["filters"] = filtros
	spec["group_by"] = []
	spec["limit"] = 1
	if efectividad:
		# (c-bis) Efectividad entre dos períodos: se consulta el primero y la
		# segunda ventana se ejecuta aparte en `ask()`.
		spec["aggregations"] = _aggs_efectividad(*izq[1])
		return spec
	# (b) Dos medidas y (c) dos períodos: una columna por lado.
	aggs = []
	for i, lado in enumerate((izq, der)):
		sufijo = "_a" if i == 0 else "_b"
		if lado[0] == "medida":
			base = _aggs_de_medida(lado[1], desde, hasta)
		else:
			base = _aggs_de_medida(_medida_de_la_pregunta(pregunta), *lado[1])
		aggs.extend({**a, "alias": f"{a['alias']}{sufijo}"} for a in base)
	spec["aggregations"] = aggs
	return spec


def _comparacion_vacia(tabla):
	"""True si la tabla de la comparación no tiene ninguna cifra."""
	if not tabla:
		return True
	_, filas = tabla
	for fila in filas:
		for i, valor in enumerate(fila):
			if i == 0 and isinstance(valor, str):
				continue
			if isinstance(valor, (int, float)) and valor:
				return False
	return True


def _rango_de_comparacion(pregunta):
	"""Período escrito en la pregunta (o el mes en curso) para la comparación."""
	bloque = _bloque_de_la_pregunta(pregunta)
	if bloque:
		return bloque
	if _pide_anio_en_curso(pregunta):
		return _periodo_anio_actual()
	return _rango_de_lado(pregunta) or _periodo_mes_actual()


def _spec_sin_modelo(pregunta):
	"""Spec base de las familias que el código arma completo (sin el modelo).

	Comparaciones "contra", porcentaje de no entregadas/pendientes sobre
	ingresadas, mapa de efectividad, mucho/bajo volumen con alta/baja
	efectividad y las series de crecimiento: todas tienen un completer que fija
	la medida, la dimensión y el período. Armarlas en código ahorra los 10-60 s
	del modelo y no dependen de que el proveedor responda.
	"""
	if not (
		_comparacion_clasificada(pregunta)
		or _pide_porcentaje_de_categorias(pregunta)
		or bool(_pide_porcentaje_por_dimension(pregunta))
		or _pide_ficha_taller(pregunta)
		or _pide_promedio_siniestros_taller(pregunta)
		or _pide_mapa_efectividad(pregunta)
		or _pide_mezcla_volumen_efectividad(pregunta)
		or _pide_ficha_taller(pregunta)
		or _pide_crecimiento_periodos(pregunta)
		or _pide_crecimiento_dimension(pregunta)
	):
		return None
	desde, hasta = _rango_de_comparacion(pregunta)
	return {
		"doctype": "Pieza",
		"fields": [],
		"filters": {"fechaingreso": ["between", desde, hasta]},
		"aggregations": [{"func": "count", "field": "*", "alias": "piezas"}],
		"group_by": [],
		"order_by": [],
	}


def _spec_de_comparacion(pregunta):
	"""Spec base de “A contra B”, armado sin llamar al modelo.

	El dueño preguntó por qué no se contestaba "¿Cómo se compara Nuevo León
	contra Jalisco en efectividad?": el spec salía bien, pero `generate_spec`
	tardaba 20-60 s (12k tokens de entrada) y el navegador cortaba la petición
	(HTTP 499 en nginx). La consulta tarda 0.1 s, así que la comparación se arma
	completa en código: el resto de la cadena (`_completar_efectividad`,
	`_completar_valor_especifico`, `_completar_region`, `_completar_comparacion`)
	la termina.
	"""
	if not _comparacion_clasificada(pregunta):
		return None
	desde, hasta = _rango_de_comparacion(pregunta)
	return {
		"doctype": "Pieza",
		"fields": [],
		"filters": {"fechaingreso": ["between", desde, hasta]},
		"aggregations": [{"func": "count", "field": "*", "alias": "piezas"}],
		"group_by": [],
		"order_by": [],
	}


def _valor_de_un_lado(fila, columnas_datos, lado, sufijo, pregunta):
	"""Valor de un lado en la fila del resultado (suma la unión de recolectadas)."""

	def suma(nombre):
		clave = f"{nombre}{sufijo}"
		if clave not in columnas_datos:
			return 0
		return fila[columnas_datos.index(clave)] or 0

	medida = lado[1] if lado[0] == "medida" else _medida_de_la_pregunta(pregunta)
	if medida == "recolectadas":
		return sum(
			suma(nombre)
			for nombre in (
				"recolectadas",
				"recolectadas_inconsistentes",
				"recolectadas_periodo",
			)
		)
	return suma(medida)


def _tabla_comparacion_contra(lados, spec, resultado, pregunta):
	"""Columnas y filas de “A contra B”, o None si hay que ejecutar la otra ventana."""
	if not lados:
		return None
	izq, der = lados
	columnas_datos = [str(c).lower() for c in (resultado.get("columnas") or [])]
	filas_datos = resultado.get("filas") or []
	if spec.get("group_by"):
		# (a) Una fila por lado, en el orden en que se mencionan.
		campo = str(spec["group_by"][0]).lower()
		if campo not in columnas_datos:
			return None
		i_dim = columnas_datos.index(campo)
		# Las claves se comparan sin distinguir mayúsculas: el catálogo guarda
		# "NISSAN" y el resultado trae "Nissan". Un estado puede aparecer con
		# cualquiera de sus variantes ("CDMX"/"Distrito Federal").
		por_valor = {str(f[i_dim]).casefold(): f for f in filas_datos}

		def _fila_del_lado(lado):
			if lado[0] == "grupo" and lado[1] == "estadolocalidad":
				for clave in _claves_del_estado(lado[2]):
					if clave in por_valor:
						return por_valor[clave]
				return None
			return por_valor.get(str(lado[2]).casefold())
		if _es_spec_efectividad(spec):
			por_grupo = {str(p.get("grupo")).casefold(): p for p in _porcentajes(spec, resultado)}
			filas = []
			for lado in (izq, der):
				p = {}
				for clave in _claves_del_estado(lado[2]) if lado[1] == "estadolocalidad" else {
					str(lado[2]).casefold()
				}:
					if clave in por_grupo:
						p = por_grupo[clave]
						break
				pct = p.get("pct") or 0
				filas.append(
					[
						lado[3],
						p.get("total") or 0,
						p.get("recuperadas") or 0,
						f"{pct:.1f}%",
						p.get("vs_objetivo") or _etiqueta_objetivo(pct),
					]
				)
			return (
				[
					ETIQUETAS_DIMENSION.get(campo, campo.replace("_", " ").title()),
					"Ingresadas",
					"Recolectadas",
					"Efectividad",
					"Objetivo",
				],
				filas,
			)
		union = {"recolectadas", "recolectadas_inconsistentes", "recolectadas_periodo"}
		if union <= set(columnas_datos):
			indices = [columnas_datos.index(a) for a in sorted(union)]
			filas = []
			for lado in (izq, der):
				f = _fila_del_lado(lado) or [None] * len(columnas_datos)
				filas.append([lado[3], sum((f[i] or 0) for i in indices)])
			etiqueta = "Recolectadas"
		else:
			i_med = next(
				(
					i
					for i, c in enumerate(columnas_datos)
					if c in ("piezas", "ingresadas", "pendientes", "no_localizadas", "total")
				),
				None,
			)
			if i_med is None:
				return None
			filas = []
			for lado in (izq, der):
				f = _fila_del_lado(lado)
				filas.append([lado[3], (f[i_med] if f else 0) or 0])
			etiqueta = ETIQUETAS_COLUMNA.get(
				columnas_datos[i_med], columnas_datos[i_med].replace("_", " ").title()
			)
		return (
			[ETIQUETAS_DIMENSION.get(campo, campo.replace("_", " ").title()), etiqueta],
			filas,
		)
	if not filas_datos:
		return None
	if "total_a" not in columnas_datos and _es_spec_efectividad(spec):
		# Efectividad entre dos períodos: la otra ventana se ejecuta en `ask()`.
		return None
	fila = filas_datos[0]
	return (
		[_etiqueta_del_lado(izq), _etiqueta_del_lado(der)],
		[
			[
				_valor_de_un_lado(fila, columnas_datos, izq, "_a", pregunta),
				_valor_de_un_lado(fila, columnas_datos, der, "_b", pregunta),
			]
		],
	)


def _tabla_efectividad_entre_periodos(lados, spec, resultado_a, resultado_b):
	"""Tabla de efectividad con una columna por período."""
	izq, der = lados
	pct_a = _porcentajes(spec, resultado_a) or [{}]
	pct_b = _porcentajes(spec, resultado_b) or [{}]
	a, b = pct_a[0], pct_b[0]
	columnas = ["Concepto", _etiqueta_del_lado(izq), _etiqueta_del_lado(der)]
	filas = [
		["Ingresadas", a.get("total") or 0, b.get("total") or 0],
		["Recolectadas", a.get("recuperadas") or 0, b.get("recuperadas") or 0],
		[
			"Efectividad",
			f"{a.get('pct') or 0:.1f}%",
			f"{b.get('pct') or 0:.1f}%",
		],
		["Objetivo", a.get("vs_objetivo") or "", b.get("vs_objetivo") or ""],
	]
	return columnas, filas


def _completar_tabla_comparativa(spec, pregunta):
	"""“Tabla comparativa … por X y Y”: tabla cruzada con las dos dimensiones.

	El dueño lo pidió el 2026-11-05. Se agrupa por las dos dimensiones que nombre
	la pregunta y el cruce lo arma el código (`_pivot`).
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_tabla_comparativa(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	dimensiones = _dimensiones_para_cruce(pregunta)[:2]
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "recolectable", "id_estatus"):
		filtros.pop(campo, None)
	filtros["fechaingreso"] = ["between", desde, hasta]
	spec["filters"] = filtros
	spec["fields"] = []
	spec["group_by"] = dimensiones
	# La medida sigue a la pregunta: precio (promedio o valor), siniestros únicos
	# o piezas.
	texto = _sin_acentos(pregunta or "")
	es_precio = any(p in texto for p in ("cuesta", "cuestan", "precio", "costo", "valor", "valen"))
	if es_precio and "siniestro" not in texto:
		spec["filters"]["recolectable"] = ["=", 1]
		if "valen" in texto or "valor" in texto:
			spec["aggregations"] = [
				{"func": "sum", "field": "preciovale", "alias": "valor"}
			]
		else:
			spec["aggregations"] = [
				{"func": "avg", "field": "preciovale", "alias": "precio_promedio"}
			]
	elif "siniestro" in texto:
		# `count_distinct` no admite filtro por agregación (MySQL rechaza
		# `COUNT(DISTINCT CASE …)`), así que `recolectable` va en el WHERE.
		spec["filters"]["recolectable"] = ["=", 1]
		spec["aggregations"] = [
			{"func": "count_distinct", "field": "siniestro", "alias": "siniestros"}
		]
	else:
		spec["aggregations"] = [
			{
				"func": "count",
				"field": "*",
				"alias": "piezas",
				"filtros": {"recolectable": ["=", 1]},
			}
		]
	spec["limit"] = 500
	spec.pop("order_by", None)
	spec.pop("render_chart", None)
	return spec


def _es_tabla_comparativa(spec, pregunta):
	"""Spec de tabla cruzada: dos dimensiones, un `count(*)` alias "piezas".

	La forma la produce `_completar_tabla_comparativa`, que solo corre cuando la
	pregunta pide una tabla comparativa, así que la bandera no viaja en el spec
	(las claves extra las rechaza `validar_spec` y el spec se guarda en la
	conversación para reabrir reportes).
	"""
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	return (
		bool(pregunta)
		and _pide_tabla_comparativa(pregunta)
		and len(spec.get("group_by") or []) == 2
		and len(aggs) == 1
		and str(aggs[0].get("alias") or "").lower()
		in ("piezas", "siniestros", "precio_promedio", "valor")
	)


def _titulo_comparativa(spec):
	"""Título de la tabla cruzada: "Comparativa: Marca por Región"."""
	dims = [
		ETIQUETAS_DIMENSION.get(str(g), str(g).replace("_", " ").title())
		for g in spec.get("group_by") or []
	]
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	prefijo = "Comparativa"
	if len(aggs) == 1 and str(aggs[0].get("field") or "").lower() == "preciovale":
		prefijo = "Valor" if str(aggs[0].get("func") or "").lower() == "sum" else "Precio promedio"
	return f"{prefijo}: " + " por ".join(dims)


def _pivot(dimensiones, resultado, tope_columnas=12, vacio=0):
	"""Cruza (dim1, dim2, medida) en una tabla dim1 × dim2.

	Devuelve `(columnas, filas, columnas_totales)`: si hay más valores en la
	segunda dimensión que `tope_columnas`, la respuesta avisa cuántos quedaron
	fuera. Las filas no se recortan (el chat ya muestra las primeras 50).
	"""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	if len(columnas) < 3:
		return [], [], 0
	i_medida = columnas.index("piezas") if "piezas" in columnas else len(columnas) - 1
	dim1, dim2 = dimensiones[0], dimensiones[1]
	tabla = {}
	totales_col = {}
	for fila in resultado.get("filas") or []:
		d1 = _valor_legible(fila[0], dim1)
		d2 = _valor_legible(fila[1], dim2)
		valor = fila[i_medida] or 0
		tabla.setdefault(d1, {})[d2] = tabla.get(d1, {}).get(d2, 0) + valor
		totales_col[d2] = totales_col.get(d2, 0) + valor
	# Columnas: las de mayor total, en orden descendente.
	cols = sorted(totales_col, key=lambda c: totales_col[c], reverse=True)[:tope_columnas]
	filas = []
	for d1, por_d2 in sorted(
		tabla.items(), key=lambda kv: sum(kv[1].values()), reverse=True
	):
		filas.append([d1] + [por_d2.get(c, vacio) for c in cols])
	etiqueta = ETIQUETAS_DIMENSION.get(str(dim1), str(dim1).replace("_", " ").title())
	return [etiqueta] + cols, filas, len(totales_col)


def _completar_valor_especifico(spec, pregunta):
	"""Filtra por el valor concreto que nombre la pregunta.

	El dueño lo pidió el 2026-11-04 ("filtrar por marca, submarca, parte, estado,
	región, taller, plaza, tipo carga, modelo, precio cuando mencionen un valor")
	y el 2026-11-05 pidió que **la marca se detecte siempre**, aunque la pregunta
	no diga "marca" ("piezas Toyota en septiembre"). El 2026-11-06 lo extendió a
	la **parte/tipo de pieza** ("piezas FACIA"), así que marca, submarca y parte
	se buscan siempre; el resto de catálogos solo se recorre cuando hay una pista
	de campo, para no pagar 25k comparaciones en cada consulta.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	catalogos = _catalogos_de_valores()
	filtros = dict(spec.get("filters") or {})
	puesto = False
	texto = _sin_acentos(pregunta or "").lower()
	# Estados, plazas y regiones se detectan siempre (dueño, 2026-11-07). La
	# mención se quita del texto de búsqueda: "Nuevo León" no es la submarca
	# "Leon" ni "Jalisco" una marca.
	habla_vehiculo = bool(re.search(r"\b(?:submarca|marca|modelo)\b", texto))
	campo_lugar, valor_lugar, mencion_lugar = _lugar_mencionado(
		pregunta, con_plazas=not habla_vehiculo
	)
	# El taller también se busca siempre (dueño, 2026-11-09): "piezas de
	# CELLINI en septiembre" filtra `taller_asignado` sin decir "taller". Con
	# "marca"/"submarca"/"modelo" en la pregunta no se busca (el nombre del
	# taller puede contener la marca).
	taller_detectado, mencion_taller = (None, None)
	if not habla_vehiculo:
		taller_detectado, mencion_taller = _taller_mencionado(pregunta)
	texto_busqueda = texto
	for mencion in (mencion_lugar, mencion_taller):
		if not mencion:
			continue
		texto_busqueda = re.sub(
			r"(?<!\w)" + re.escape(_sin_acentos(mencion)) + r"(?!\w)", " ", texto_busqueda
		)

	def _exacto(cond):
		return isinstance(cond, list) and len(cond) >= 2 and str(cond[0]) == "="

	# 1) Marca: se busca SIEMPRE (92 valores). No si piden submarca ni si la
	# pregunta es por taller/plaza (ahí la marca puede ser parte del nombre).
	# "del taller/plaza X": la marca puede ser parte del nombre, no se filtra.
	filtra_lugar = bool(re.search(r"\b(?:del|de la|de el)\s+(?:taller|plaza)\b", texto))
	# "de la marca X del taller Y": la marca es explícita y sí se filtra (la
	# mención del taller ya se quitó del texto de búsqueda).
	marca_explicita = bool(re.search(r"\b(?:marca|submarca)\b", texto))
	if (not filtra_lugar or marca_explicita) and not _exacto(filtros.get("marca")):
		valor = _valor_mencionado(texto_busqueda, catalogos.get("marca") or [])
		if valor:
			filtros["marca"] = ["=", valor]
			puesto = True

	# 2) Submarca: también se busca siempre (17,814 valores). Se excluyen los
	# valores que son nombre de marca (dato sucio) para no cruzar los filtros.
	marcas_planas = {_sin_acentos(str(m)).upper() for m in catalogos.get("marca") or []}
	if not _exacto(filtros.get("tipo")):
		valores_tipo = [
			v
			for v in catalogos.get("tipo") or []
			if _sin_acentos(str(v)).upper() not in marcas_planas
			and _sin_acentos(str(v)).upper() not in PALABRAS_GENERICAS
		]
		valor_tipo = _valor_mencionado(texto_busqueda, valores_tipo)
		if valor_tipo:
			# `like valor%`: "Versa" cubre "Versa/Almera/V-Drive [N17X]" y demás
			# variantes (con `=` el valor corto "Versa" devolvía 0 filas).
			filtros["tipo"] = ["like", f"{valor_tipo}%"]
			puesto = True

	# 3) Parte / tipo de pieza: se busca SIEMPRE (253 valores), como la marca y
	# la submarca ("piezas FACIA", "piezas de faro"). La palabra "parte" no es
	# necesaria; las palabras ambiguas quedan en PARTES_GENERICAS.
	valor_parte = _parte_mencionada(texto_busqueda)
	if valor_parte:
		# El concepto manda: `piezades` distingue la posición ("FACIA DEL.") y
		# reemplaza cualquier patrón que haya puesto el modelo ("%faro izquierdo").
		filtros.pop("parte_pieza", None)
		filtros["piezades"] = ["like", _patron_piezades(valor_parte, pregunta)]
		puesto = True

	# 4) Estados, plazas y regiones: el dueño los pidió siempre (2026-11-07),
	# como la marca y la submarca. Con "del taller/plaza X" manda el catálogo de
	# esa dimensión (el nombre puede contener una ciudad); la región de una
	# plaza la resuelve `_completar_region` más adelante.
	if taller_detectado and not _exacto(filtros.get("taller_asignado")):
		# El nombre del taller puede contener una submarca o una parte: si el
		# modelo las puso a partir del mismo nombre, se descartan.
		mencion_plana = _sin_acentos(str(taller_detectado)).upper()
		for campo_confundido in ("marca", "tipo", "parte_pieza", "piezades"):
			cond = filtros.get(campo_confundido)
			if isinstance(cond, str):
				cond = ["=", cond]
			if not (isinstance(cond, list) and len(cond) >= 2):
				continue
			patron = _sin_acentos(str(cond[1]).strip("% ")).upper()
			if patron and patron in mencion_plana:
				filtros.pop(campo_confundido, None)
		filtros["taller_asignado"] = ["=", taller_detectado]
		puesto = True

	if not filtra_lugar and campo_lugar:
		for otro in ("estadolocalidad", "id_plaza", "region_pais"):
			if otro != campo_lugar:
				filtros.pop(otro, None)
		# El modelo confunde el lugar con una submarca o un taller ("León" →
		# tipo LEON, "MTY" → taller %MTY%): si su patrón está dentro de la
		# mención del lugar, se descarta. El lugar detectado manda sobre un `=`
		# del modelo ("CDMX" se guarda como "Ciudad de México").
		mencion_plana = _sin_acentos(mencion_lugar).upper()
		for campo_confundido in ("tipo", "taller", "taller_asignado"):
			cond = filtros.get(campo_confundido)
			if not (isinstance(cond, list) and len(cond) >= 2):
				continue
			patron = _sin_acentos(str(cond[1]).strip("% ")).upper()
			if patron and patron in mencion_plana:
				filtros.pop(campo_confundido, None)
		filtros[campo_lugar] = (
			_condicion_de_estado(valor_lugar)
			if campo_lugar == "estadolocalidad"
			else ["=", valor_lugar]
		)
		puesto = True

	# 5) Las demás dimensiones, solo con una pista de campo.
	if not any(
		x in texto
		for x in (
			"marca",
			"submarca",
			"estado",
			"taller",
			"plaza",
			"modelo",
			"precio",
			"valor",
			"costo",
			"caro",
			"cara",
			"tipo cdr",
			"tipo carga",
			"agencia",
			"audavales",
			"equipo pesado",
			"$",
		)
	):
		filtros = _normalizar_filtros_de_texto(filtros, marcas_planas)
		if puesto or filtros:
			spec["filters"] = filtros
		return spec

	for campo in ("taller_asignado", "id_plaza", "estadolocalidad"):
		# Un `like` del modelo se reemplaza por el valor exacto del catálogo
		# ("%CELLINI" también traería "CELLINI 2"); un `=` se respeta.
		if _exacto(filtros.get(campo)):
			continue
		if campo == "id_plaza" and habla_vehiculo and "plaza" not in texto:
			# "de la submarca Leon" no es la plaza LEON.
			continue
		if campo == "taller_asignado" and habla_vehiculo and "taller" not in texto:
			# "de la marca CELLINI" no es el taller CELLINI.
			continue
		valor = _valor_mencionado(pregunta, catalogos.get(campo) or [])
		if valor:
			filtros[campo] = (
				_condicion_de_estado(valor) if campo == "estadolocalidad" else ["=", valor]
			)
			puesto = True

	texto_plano = _sin_acentos(pregunta or "")
	if "tipo_carga" not in filtros and re.search(r"tipo\s*(?:cdr|carga)", texto_plano):
		# Solo con la frase "tipo CDR/carga": "por taller" no es tipo_carga.
		etiquetas = catalogos.get("tipo_carga_map") or {}
		for nombre, ident in etiquetas.items():
			if re.search(r"\b" + re.escape(_sin_acentos(nombre)) + r"\b", texto_plano):
				filtros["tipo_carga"] = ["=", ident]
				puesto = True
				break

	if "modelo" not in filtros and re.search(r"\bmodelo\b|\ba[nñ]o modelo\b", _sin_acentos(pregunta or "")):
		anio = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", pregunta or "")
		if anio:
			filtros["modelo"] = ["=", int(anio.group(1))]
			puesto = True

	if "preciovale" not in filtros:
		rango = _filtro_de_precio(pregunta)
		if rango:
			filtros["preciovale"] = rango
			puesto = True

	filtros = _normalizar_filtros_de_texto(filtros, marcas_planas)
	if puesto or filtros:
		spec["filters"] = filtros
	return spec


def _tipo_base(resto, tipos):
	"""Prefijo de `tipo` para el resto de un valor combinado.

	"VERSA" → "VERSA"; "VERSA/ALMERA" → "VERSA/ALMERA" (hay tipos que empiezan
	así); "ALTIMA L33" → "ALTIMA" (el tipo real es "Altima (L33)", con paréntesis).
	"""
	planos = [(_sin_acentos(str(t)).upper(), str(t)) for t in tipos]
	if any(plano.startswith(resto) for plano, _ in planos):
		return resto
	mejor = ""
	for plano, original in planos:
		if len(plano) >= 4 and resto.startswith(plano) and len(plano) > len(mejor):
			mejor = original
	return mejor or None


def _separar_marca_submarca(valor, solo_con_catalogo=False):
	"""“Nissan Versa” / “Nissan/Versa” → (NISSAN, VERSA).

	Se usa cuando el modelo manda el valor combinado en `marca` o en `tipo`
	(pregunta/prompt): se separa en los dos filtros. Con `solo_con_catalogo` solo
	se parte un `tipo` si la parte de la submarca existe en el catálogo (así no
	se rompen los valores sucios que ya empiezan con la marca, como
	“NISSAN 300 Y 350”).
	"""
	texto = _sin_acentos(str(valor or "")).upper().strip()
	if not texto:
		return None
	catalogos = _catalogos_de_valores()
	marcas = {
		_sin_acentos(str(m)).upper(): str(m) for m in catalogos.get("marca") or []
	}
	tipos = catalogos.get("tipo") or []
	for plano in sorted(marcas, key=len, reverse=True):
		if len(plano) < 3 or not texto.startswith(plano):
			continue
		resto = texto[len(plano):]
		if not resto or resto[0] not in " /-_,.":
			continue
		resto = resto.lstrip(" /-_,.")
		if not resto:
			continue
		tipo = _tipo_base(resto, tipos)
		if not tipo:
			return None
		return marcas[plano], tipo
	return None


def _normalizar_filtros_de_texto(filtros, marcas_planas):
	"""Ajusta los filtros que puso el modelo antes de ejecutar.

	- `tipo` exacto → `like valor%`: "Versa" debe cubrir "Versa/Almera/V-Drive…".
	- `marca` exacta que no existe en el catálogo → fuera (el modelo a veces la
	  inventa con un nombre de modelo: "marca Versa").
	"""
	cond = filtros.get("tipo")
	if isinstance(cond, list) and len(cond) >= 2:
		valor_tipo = str(cond[1]).strip().strip("%")
		partes = _separar_marca_submarca(valor_tipo, solo_con_catalogo=True)
		if partes:
			# "Nissan Versa" en `tipo` → marca NISSAN + tipo like "VERSA%".
			marca, tipo = partes
			marca_actual = filtros.get("marca")
			valida = (
				isinstance(marca_actual, list)
				and len(marca_actual) >= 2
				and _sin_acentos(str(marca_actual[1])).upper() in marcas_planas
			)
			if not valida:
				filtros["marca"] = ["=", marca]
			filtros["tipo"] = ["like", f"{tipo}%"]
		elif str(cond[0]) == "=":
			filtros["tipo"] = ["like", f"{cond[1]}%"]
	marca = filtros.get("marca")
	if isinstance(marca, list) and len(marca) >= 2 and str(marca[0]) == "=":
		if _sin_acentos(str(marca[1])).upper() not in marcas_planas:
			valor = str(marca[1])
			partes = _separar_marca_submarca(valor)
			if partes:
				# "Chevrolet Aveo" / "Nissan Versa" en `marca` → marca + tipo.
				filtros["marca"] = ["=", partes[0]]
				if "tipo" not in filtros:
					filtros["tipo"] = ["like", f"{partes[1]}%"]
			else:
				# Un valor inválido como marca puede ser una submarca ("Versa");
				# se usa el valor normalizado del catálogo y no se pisa el `tipo`
				# que ya haya puesto el detector de la pregunta.
				base = _tipo_base(
					_sin_acentos(valor).upper(),
					_catalogos_de_valores().get("tipo") or [],
				)
				if "tipo" not in filtros:
					filtros["tipo"] = ["like", f"{base or valor}%"]
				filtros.pop("marca")
	return filtros


def _completar_piezas_mas_danadas(spec, pregunta):
	"""“Piezas más dañadas” = las PARTES más ingresadas (dueño, 2026-11-04).

	La medida es el conteo de piezas ingresadas (`recolectable = 1`) agrupado por
	`parte_pieza`, de mayor a menor.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_piezas_mas_danadas(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	spec["filters"] = {"fechaingreso": ["between", desde, hasta]}
	spec["fields"] = []
	spec["group_by"] = ["parte_pieza"]
	spec["aggregations"] = [
		{
			"func": "count",
			"field": "*",
			"alias": "ingresadas",
			"filtros": {"recolectable": ["=", 1]},
		}
	]
	spec["order_by"] = [{"field": "ingresadas", "dir": "desc"}]
	# El N de la pregunta manda ("las 5 más dañadas"); sin N, 10.
	spec["limit"] = _top_de_la_pregunta(pregunta) or _numero_por_singular_o_plural(pregunta) or 10
	spec.pop("render_chart", None)
	return spec


def _es_ranking_partes(spec):
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	return (
		spec.get("group_by") == ["parte_pieza"]
		and len(aggs) == 1
		and str(aggs[0].get("alias") or "").lower() == "ingresadas"
	)


def _pide_ficha_taller(pregunta):
	"""“Ficha del taller” = “resumen de taller” (dueño, 2026-11-10).

	Necesita el taller: sin él no hay ficha que armar, y la pregunta sigue su
	camino normal.
	"""
	texto = _sin_acentos(pregunta or "")
	if "taller" not in texto and not re.search(r"\bcdr\b", texto):
		return False
	if not re.search(r"\b(?:ficha|resumen)\b", texto):
		return False
	taller, _ = _taller_mencionado(pregunta)
	return bool(taller)


def _periodo_anterior(rango):
	"""Ventana anterior comparable: el mes (o bloque) previo, o el mismo lapso."""
	desde = frappe.utils.getdate(rango[0])
	hasta = frappe.utils.getdate(rango[1])
	meses = (hasta.year - desde.year) * 12 + hasta.month - desde.month + 1
	if (
		desde.day == 1
		and hasta == frappe.utils.getdate(frappe.utils.get_last_day(hasta))
		and hasta
		== frappe.utils.getdate(frappe.utils.get_last_day(frappe.utils.add_months(desde, meses - 1)))
	):
		# Mes/trimestre/cuatrimestre completo: se retrocede el mismo número de
		# meses de calendario.
		inicio = frappe.utils.add_months(desde, -meses)
		fin = frappe.utils.add_months(inicio, meses - 1)
		return (
			f"{_dia(frappe.utils.get_first_day(inicio))} 00:00:00",
			f"{_dia(frappe.utils.get_last_day(fin))} 23:59:59",
		)
	dias = (hasta - desde).days + 1
	fin = frappe.utils.add_days(desde, -1)
	inicio = frappe.utils.add_days(fin, -(dias - 1))
	return f"{_dia(inicio)} 00:00:00", f"{_dia(fin)} 23:59:59"


def _completar_ficha_taller(spec, pregunta):
	"""Ficha del taller: una tabla vertical con las siete cifras (2026-11-10).

	Ingresadas, recolectadas, no recolectadas, pendientes, efectividad, marcas y
	crecimiento contra el mes anterior, todo para el taller que nombre la
	pregunta y el período vigente. Las diez agregaciones caben en el máximo del
	validador (`MAX_AGGS`): las tres de recolectadas, las tres de no
	recolectadas, pendientes, marcas e ingresadas del período anterior.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_ficha_taller(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	taller, _ = _taller_mencionado(pregunta)
	if not taller:
		filtros = spec.get("filters") or {}
		cond = filtros.get("taller_asignado")
		if isinstance(cond, str):
			taller = cond
		elif isinstance(cond, list) and len(cond) >= 2:
			taller = cond[1]
	if not taller:
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	previo_desde, previo_hasta = _periodo_anterior((desde, hasta))
	filtros = {
		campo: valor
		for campo, valor in (spec.get("filters") or {}).items()
		if campo in ("marca", "tipo", "parte_pieza", "piezades")
	}
	filtros["taller_asignado"] = ["=", taller]
	# El período y `recolectable` van globales: `count_distinct` no admite
	# filtro por agregación (MySQL rechaza `COUNT(DISTINCT CASE …)`).
	filtros["fechaingreso"] = ["between", desde, hasta]
	filtros["recolectable"] = ["=", 1]
	aggs = [
		{
			"func": "count",
			"field": "*",
			"alias": "ingresadas",
			"filtros": {"fechaingreso": ["between", desde, hasta], "recolectable": ["=", 1]},
		},
		*_aggs_recolectadas(desde, hasta),
		{
			# Subconjunto de las ingresadas del período (estatus de no entrega
			# del catálogo): con la unión de tres categorías el número podía
			# superar a las ingresadas y la ficha quedaba incoherente.
			"func": "count",
			"field": "*",
			"alias": "no_recolectadas",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"recolectable": ["=", 1],
				"id_estatus": ["in", [200, 201, 202, 203, 204, 205, 206]],
			},
		},
		{
			"func": "count",
			"field": "*",
			"alias": "pendientes",
			"filtros": {
				"fechaingreso": ["between", desde, hasta],
				"id_estatus": ["is", "empty"],
				"fecha_hora_recoleccion": ["is", "empty"],
				"recolectable": ["=", 1],
			},
		},
		{"func": "count_distinct", "field": "marca", "alias": "marcas"},
	]
	spec["filters"] = filtros
	spec["fields"] = []
	spec["group_by"] = []
	spec["limit"] = 1
	spec["aggregations"] = aggs
	spec.pop("order_by", None)
	spec.pop("render_chart", None)
	spec.pop("min_ingresadas", None)
	return spec


def _tabla_ficha_taller(spec, resultado, efectividad_previa):
	"""Tabla vertical Concepto | Valor con las siete cifras de la ficha."""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	filas_datos = resultado.get("filas") or []
	if not filas_datos:
		return None

	def valor(alias):
		return (filas_datos[0][columnas.index(alias)] or 0) if alias in columnas else 0

	ingresadas = valor("ingresadas")
	recolectadas = sum(
		valor(alias)
		for alias in (
			"recolectadas",
			"recolectadas_inconsistentes",
			"recolectadas_periodo",
		)
	)
	no_recolectadas = valor("no_recolectadas")
	efectividad = (recolectadas / ingresadas * 100) if ingresadas else 0.0
	if efectividad_previa is None:
		variacion = "—"
	else:
		# Variación en puntos porcentuales contra el período anterior.
		variacion = f"{efectividad - efectividad_previa:+.1f} pts"
	filas = [
		["Ingresadas", ingresadas],
		["Recolectadas", recolectadas],
		["No recolectadas", no_recolectadas],
		["Pendientes", valor("pendientes")],
		["Efectividad", f"{efectividad:.1f}%"],
		["Marcas", valor("marcas")],
		["Variación de efectividad vs período anterior", variacion],
	]
	return ["Concepto", "Valor"], filas


def _completar_sin_lista_de_precios(spec, pregunta):
	"""Quita una lista de piezas con precio que la pregunta no pidió.

	El modelo a veces devuelve `fields` con `preciovale` para preguntas que no
	hablan de precios ("piezas de la marca Nissan del taller CELLINI en
	septiembre" salía como "Piezas más caras"): si no hay intención de precio,
	se vuelve al conteo.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	campos = [str(f).lower() for f in spec.get("fields") or []]
	if "preciovale" not in campos:
		return spec
	if (
		_pide_piezas_caras(pregunta)
		or _pide_piezas_con_precio(pregunta)
		or _pide_solo_precios(pregunta)
		or _pide_prioridad_pendientes(pregunta)
	):
		return spec
	spec["fields"] = []
	spec.pop("order_by", None)
	spec["aggregations"] = [{"func": "count", "field": "*", "alias": "piezas"}]
	return spec


def _completar_singular_plural(spec, pregunta):
	"""“¿cuál es …?” pide una fila; “¿cuáles son …?” pide la lista
	(dueño, 2026-11-05).

	Los completers de ranking ya lo aplican; esto cubre las preguntas que arma el
	modelo. Solo actúa con la frase explícita y una intención de ranking ("más",
	"menos", "mejor", "peor", "mayor", "menor", "top"), para no convertir
	"¿cuál es la efectividad por taller?" (que pide la tabla completa) en una
	sola fila.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	numero = _numero_por_singular_o_plural(pregunta)
	if numero is None:
		return spec
	if spec.get("fields") or not spec.get("group_by") or not spec.get("aggregations"):
		return spec
	texto = _sin_acentos(pregunta or "")
	if not re.search(r"\b(?:mas|menos|mejor|peor|mayor|menor|top)\b", texto):
		return spec
	actual = int(spec.get("limit") or 0)
	if numero == 1:
		spec["limit"] = 1
	else:
		# La lista se queda en el top 10 del ranking, igual que los demás
		# rankings (un listado de presentación de 50 no se recorta).
		spec["limit"] = min(actual or 10, 10)
	return spec


def _completar_listado(spec, pregunta):
	"""Listado de los/las [dimensión], con filtro de otra dimensión.

	"lista los talleres de la marca Nissan" → group_by taller_asignado y el
	filtro `marca = Nissan` lo agrega `_completar_valor_especifico`; "lista las
	marcas del taller CELLINI" → group_by marca + filtro del taller. Si además
	hay "por [dimensión]" se conservan las dos columnas ("lista las plazas por
	región" → región + plaza). `explain()` quita la medida y deja solo nombres.
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _es_listado(pregunta):
		return spec
	dimensiones = _dimensiones_de_la_pregunta(pregunta)
	if spec.get("doctype") not in (None, "Pieza"):
		# "lista las plazas del estado Jalisco": el modelo a veces arma la
		# consulta sobre Recoleccion o Taller y el período de esas tablas deja la
		# lista vacía. Los estados, plazas y regiones del universo de piezas
		# salen de Pieza, así que se reconstruye sobre esa base (dueño 2026-11-07).
		if not dimensiones or dimensiones[0] not in (
			"taller_asignado",
			"id_plaza",
			"estadolocalidad",
			"region_pais",
		):
			return spec
		spec = {
			"doctype": "Pieza",
			"fields": [],
			"filters": {},
			"aggregations": [],
			"group_by": [],
		}
		spec = _completar_valor_especifico(spec, pregunta)
		spec = _completar_region(spec, pregunta)
	if not dimensiones:
		return spec
	dimension = dimensiones[0]
	agrupador = _dimension_por_frase(pregunta)
	if agrupador and agrupador != dimension:
		spec["group_by"] = [agrupador, dimension]
	else:
		spec["group_by"] = [dimension]
	spec["fields"] = []
	spec["aggregations"] = [{"func": "count", "field": "*", "alias": "piezas"}]
	filtros = dict(spec.get("filters") or {})
	filtros.setdefault(dimension, ["is", "not null"])
	spec["filters"] = filtros
	orden = [agrupador, dimension] if agrupador and agrupador != dimension else [dimension]
	spec["order_by"] = [{"field": campo, "dir": "asc"} for campo in orden]
	# La lista completa; el chat corta a 50 filas al pintar.
	spec["limit"] = 500
	spec.pop("render_chart", None)
	return spec


ETIQUETAS_PLURAL = {
	"taller_asignado": "Talleres",
	"marca": "Marcas",
	"tipo": "Submarcas",
	"id_plaza": "Plazas",
	"region_pais": "Regiones",
	"estadolocalidad": "Estados",
	"modelo": "Modelos",
	"siniestro": "Siniestros",
	"parte_pieza": "Tipos de pieza",
	"tipo_carga": "Tipos CDR",
	"pais_origen": "Países de origen",
}


def _titulo_legible(valor):
	""""NISSAN" → "Nissan"; las siglas cortas ("BMW") se quedan igual."""
	texto = str(valor or "").strip()
	if texto.isupper() and len(texto) > 3:
		return texto.title()
	return texto


def _etiqueta_filtro_listado(spec):
	"""“de la marca Nissan” / “del taller CELLINI” para el título del listado."""
	filtros = spec.get("filters") or {}
	for campo, prefijo in (
		("marca", "de la marca"),
		("tipo", "de la submarca"),
		("parte_pieza", "de la parte"),
		("taller_asignado", "del taller"),
		("id_plaza", "de la plaza"),
		("estadolocalidad", "del estado"),
		("region_pais", "de la región"),
		("pais_origen", "del país"),
		("modelo", "del modelo"),
	):
		cond = filtros.get(campo)
		if not (isinstance(cond, list) and len(cond) >= 2):
			continue
		if str(cond[0]).lower() == "is":
			# "is not null" es una condición técnica, no un valor que mostrar.
			continue
		valor = cond[1]
		if isinstance(valor, str) and valor:
			if campo in ("marca", "tipo", "pais_origen"):
				valor = _titulo_legible(valor)
			return f"{prefijo} {valor}"
	cond = filtros.get("piezades")
	if isinstance(cond, list) and len(cond) >= 2:
		valor = str(cond[1]).replace("%", " ").strip()
		if valor:
			return f"de {valor.lower()}"
	return ""


def _titulo_listado(pregunta, spec):
	"""Título del listado: “Talleres de la marca Nissan” / “Plazas por Región”."""
	dimensiones = _dimensiones_de_la_pregunta(pregunta)
	if not dimensiones:
		return ""
	dimension = dimensiones[0]
	titulo = ETIQUETAS_PLURAL.get(
		dimension, dimension.replace("_", " ").capitalize()
	)
	agrupador = _dimension_por_frase(pregunta)
	if agrupador and agrupador != dimension:
		etiqueta = ETIQUETAS_DIMENSION.get(agrupador, agrupador.replace("_", " ").title())
		titulo += f" por {etiqueta}"
	filtro = _etiqueta_filtro_listado(spec)
	if filtro:
		titulo += f" {filtro}"
	return titulo


def _pide_lista_explicita(pregunta):
	""""lista"/"listar"/"listado"/"enumera": el dueño espera una tabla."""
	return bool(
		re.search(r"\b(?:lista|listar|listado|enumera)\b", _sin_acentos(pregunta or ""))
	)


def _completar_lista_explicita(spec, pregunta):
	"""Con "lista" el resultado va en tabla, aunque pida una medida.

	Los listados de dimensiones ya los maneja `_completar_listado`; aquí se
	cubre "lista las piezas" (tabla de piezas), "lista las piezas por tipo CDR"
	(tabla con la medida por grupo) y "lista los siniestros" (tabla por siniestro).
	"""
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if not _pide_lista_explicita(pregunta) or _es_listado(pregunta):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	if _pide_promedio(pregunta) or _pide_efectividad(pregunta) or _pide_resumen(pregunta):
		return spec
	if _pide_solo_precios(pregunta) or _pide_piezas_con_precio(pregunta):
		return spec
	if _pide_recolectadas(pregunta) or spec.get("fields") or spec.get("group_by"):
		return spec
	texto = _sin_acentos(pregunta or "")
	dimension = _dimension_por_frase(pregunta)
	if dimension:
		spec["fields"] = []
		spec["group_by"] = [dimension]
		spec["aggregations"] = [{"func": "count", "field": "*", "alias": "piezas"}]
		spec.setdefault("limit", 50)
		spec.pop("render_chart", None)
		return spec
	desde, hasta = _periodo_del_spec(spec) or _periodo_mes_actual()
	filtros = dict(spec.get("filters") or {})
	for campo in ("fechaingreso", "fecha_hora_recoleccion", "id_estatus", "recolectable"):
		filtros.pop(campo, None)
	filtros["fechaingreso"] = ["between", desde, hasta]
	filtros["recolectable"] = ["=", 1]
	spec["filters"] = filtros
	if "siniestro" in texto:
		spec["fields"] = []
		spec["group_by"] = ["siniestro"]
		spec["aggregations"] = [{"func": "count", "field": "*", "alias": "piezas"}]
		spec["order_by"] = [{"field": "piezas", "dir": "desc"}]
		spec.setdefault("limit", 50)
	elif "pieza" in texto or "refaccion" in texto:
		spec["fields"] = list(COLUMNAS_PRIORIDAD)
		spec["aggregations"] = []
		spec["group_by"] = []
		spec["order_by"] = [{"field": "preciovale", "dir": "desc"}]
		spec["limit"] = _top_de_la_pregunta(pregunta) or 50
	else:
		return spec
	spec.pop("render_chart", None)
	return spec


def _pide_agrupar_por_distintas(pregunta):
	""""Piezas de marcas distintas" = "piezas por marca" (dueño, 2026-11-07).

	Devuelve la dimensión cuando la pregunta pide una medida sobre valores
	distintos de otra dimensión ("piezas de marcas distintas", "distintos
	talleres"); no aplica cuando la dimensión ES el objeto de "cuántos"
	("¿cuántas marcas distintas?" → count distinct) ni cuando el "distinto" es
	la propia medida ("siniestros distintos" → count distinct de siniestros).
	"""
	texto = _sin_acentos(pregunta or "")
	if "distint" not in texto:
		return None
	dimension = None
	nombre_dim = ""
	for nombre, campo in sorted(NOMBRES_DIMENSION.items(), key=lambda kv: len(kv[0]), reverse=True):
		patron = r"\b" + re.escape(nombre) + r"\s+distint[oa]s\b"
		inverso = r"\bdistint[oa]s\s+" + re.escape(nombre) + r"\b"
		if re.search(patron, texto) or re.search(inverso, texto):
			dimension = campo
			nombre_dim = nombre
			break
	if not dimension or dimension == "siniestro":
		return None
	# "¿cuántas marcas distintas?" / "¿cuántos talleres distintos?" = count distinct.
	primer_cuantos = _objeto_del_cuantos(texto)
	if primer_cuantos and primer_cuantos.startswith(nombre_dim.rstrip("s")):
		return None
	# Hace falta una medida sobre la que agrupar; si solo pide la dimensión, ya
	# lo resuelve el listado (`_es_listado`).
	medida = any(
		p in texto
		for p in ("pieza", "refaccion", "ingresad", "recolect", "recoleccion", "pendiente")
	)
	return dimension if medida else None


def _completar_distintas_agrupadas(spec, pregunta):
	"""Agrupa la medida por la dimensión de "X distintos/distintas"."""
	dimension = _pide_agrupar_por_distintas(pregunta)
	if not dimension or _pide_tabla_comparativa(pregunta):
		return spec
	if not isinstance(spec, dict) or spec.get("out_of_scope") or spec.get("respuesta_directa"):
		return spec
	if spec.get("doctype") not in (None, "Pieza"):
		return spec
	if spec.get("fields"):
		return spec
	texto = _sin_acentos(pregunta or "")
	spec["fields"] = []
	spec["group_by"] = [dimension]
	if "siniestro" in texto:
		spec["aggregations"] = [
			{"func": "count_distinct", "field": "siniestro", "alias": "siniestros"}
		]
		alias = "siniestros"
	else:
		spec["aggregations"] = [{"func": "count", "field": "*", "alias": "piezas"}]
		alias = "piezas"
	spec["order_by"] = [{"field": alias, "dir": "desc"}]
	spec["limit"] = _top_de_la_pregunta(pregunta) or 50
	spec.pop("render_chart", None)
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
	# En tests el límite por minuto no aplica: la suite hace decenas de ask() en
	# segundos con el mismo usuario y tumbaba tests ajenos al límite.
	if getattr(frappe.flags, "in_test", False):
		return True
	s = frappe.get_cached_doc("Molduplastic IA Settings")
	maximo = int(s.max_llamadas_por_minuto or 20)
	ventana = frappe.utils.now_datetime().strftime("%Y%m%d%H%M")
	key = f"molduplastic_ia:ratelimit:{user}:{ventana}"
	usos = frappe.cache().get_value(key) or 0
	if usos >= maximo:
		return False
	frappe.cache().set_value(key, usos + 1, expires_in_sec=70)
	return True


def _log_llamada(
	info, operacion, estado, consulta_id, user, aseguradora, error="", historial=None,
	modo=None, intencion=None, analisis_omitido=False,
):
	"""Una fila por llamada al LLM, con el contexto conversacional que viajó.

	`turnos_previos`/`chars_previos` son lo efectivamente enviado (0 cuando la
	rama corta antes de armar historial); los dos `max_*` son la configuración
	vigente, para poder comparar el uso real contra el límite en el análisis.
	"""
	turnos_max, chars_max = _limites_contexto()
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
			"max_turnos_contexto": turnos_max,
			"max_chars_contexto": chars_max,
			"turnos_previos": len(historial or []),
			"chars_previos": sum(
				len(t.get("pregunta") or "") + len(t.get("respuesta") or "")
				for t in (historial or [])
			),
			"prompt": info.get("prompt") or "",
			"respuesta": info.get("respuesta") or "",
			"tokens_entrada": info.get("tokens_entrada") or 0,
			"tokens_salida": info.get("tokens_salida") or 0,
			"latencia_ms": info.get("latencia_ms") or 0,
			"costo_estimado_usd": info.get("costo_estimado_usd") or 0,
			"error": error,
			# Modo efectivo: el del explain cuando lo hubo; "Código" cuando la
			# respuesta la armó el servidor. La intención viene del clasificador.
			"modo_respuesta": modo or info.get("modo") or "",
			"intencion": intencion or info.get("intencion") or "",
			"analisis_omitido": 1 if analisis_omitido else 0,
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


def _limites_contexto():
	"""Turnos y caracteres del historial, configurados en el Settings.

	Los dos campos son obligatorios: no hay valores de respaldo en el código.
	Si faltan, se avisa en vez de mandar un historial a medias.
	"""
	s = frappe.get_cached_doc("Molduplastic IA Settings")
	if not s.max_turnos_contexto or not s.max_chars_contexto:
		frappe.throw(
			"Configura los límites del contexto conversacional en Molduplastic IA Settings."
		)
	return int(s.max_turnos_contexto), int(s.max_chars_contexto)


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
	turnos_max, chars_max = _limites_contexto()
	turnos, pregunta = [], None
	for m in doc.mensajes:
		texto = (m.contenido or "").strip()
		if not texto:
			continue
		if m.rol == "usuario":
			pregunta = texto[:chars_max]
		elif pregunta:
			turnos.append({"pregunta": pregunta, "respuesta": texto[:chars_max]})
			pregunta = None
	return turnos[-turnos_max:]


def _reglas_de_intencion():
	"""Orden de precedencia de las intenciones (fase 0 del análisis).

	Las más específicas van primero. Solo se usan para nombrar la intención:
	el flujo de `ask()` no depende de este orden.
	"""
	return (
		("sensible", lambda p: bool(_pide_dato_sentible(p))),
		("comparacion", lambda p: bool(_lados_contra(p))),
		("porcentaje_categorias", lambda p: bool(_pide_porcentaje_de_categorias(p))),
		("porcentaje_dimension", lambda p: bool(_pide_porcentaje_por_dimension(p))),
		("mapa_efectividad", _pide_mapa_efectividad),
		("mezcla_volumen_efectividad", lambda p: bool(_pide_mezcla_volumen_efectividad(p))),
		("ficha_taller", _pide_ficha_taller),
		("efectividad", _pide_efectividad),
		("no_recolectadas", _pide_no_recuperadas),
		("no_localizadas", _pide_no_localizadas),
		("siniestros_pendientes", _pide_siniestros_pendientes),
		("prioridad_pendientes", _pide_prioridad_pendientes),
		("piezas_caras", _pide_piezas_caras),
		("precios", lambda p: bool(_pide_piezas_con_precio(p) or _pide_solo_precios(p))),
		("promedio_siniestros_taller", _pide_promedio_siniestros_taller),
		("promedio", _pide_promedio),
		("crecimiento", lambda p: bool(
			_pide_crecimiento_siniestros(p)
			or _pide_crecimiento_dimension(p)
			or _pide_crecimiento_periodos(p)
		)),
		("resumen", _pide_resumen),
		("recolectadas", _pide_recolectadas),
		("siniestros_por_cdr", _pide_siniestros_por_cdr),
		("preferencia_region", _pide_preferencia_region),
		("marcas_accidentadas", _pide_marcas_accidentadas),
		("pais_origen", _pide_pais_origen),
		("anios_modelo", _pide_anios_modelo),
		("submarcas_danadas", _pide_submarcas_danadas),
		("piezas_mas_danadas", _pide_piezas_mas_danadas),
		("filtro_danadas", _pide_filtro_danadas),
		("tabla_comparativa", _pide_tabla_comparativa),
		("distintas", _pide_agrupar_por_distintas),
		("volumen", _pide_volumen),
		("parte", _habla_de_parte),
		("lista_explicita", _pide_lista_explicita),
		("listado", _es_listado),
	)


def intencion_de_pregunta(pregunta):
	"""Nombre de la intención de una pregunta (sin llamar a la BD ni al LLM)."""
	return clasificar(pregunta, _reglas_de_intencion())


def intenciones_conocidas():
	"""Nombres válidos para las excepciones y el glosario (la validación de Settings los usa)."""
	return {nombre for nombre, _ in _reglas_de_intencion()} | {"general"}


MODOS_RESPUESTA = ("Breve", "Analítico")
NOTA_TIEMPO_ANALISIS = (
	"El análisis completo tardó demasiado para este período. Para verlo, reduce el "
	"período (por ejemplo, a un mes) o acota una dimensión."
)


def _modo_de_respuesta(intencion):
	"""Modo efectivo de la respuesta: lo decide solo la configuración del servidor.

	Excepción por intención si existe; si no, el modo global; si falta o es
	inválido, Breve. El cliente no participa: `ask()` no lee ningún modo.
	"""
	try:
		s = frappe.get_cached_doc("Molduplastic IA Settings")
	except frappe.DoesNotExistError:
		return "Breve"
	for fila in s.get("modo_excepciones") or []:
		if fila.intencion == intencion and fila.modo in MODOS_RESPUESTA:
			return fila.modo
	return s.modo_respuesta if s.modo_respuesta in MODOS_RESPUESTA else "Breve"


def _presupuesto_analisis_seg():
	"""Segundos para la respuesta analítica (acotado por la validación de Settings)."""
	s = frappe.get_cached_doc("Molduplastic IA Settings")
	return min(max(int(s.presupuesto_analisis_seg or 60), 5), 60)


def _explicacion_en_codigo(spec, pregunta):
	"""True si la respuesta la arma el código y no hace falta llamar al modelo.

	Promedios, rankings, listados de precio, comparativas y crecimiento ya
	tienen título, período y tabla deterministas: el texto del modelo se
	descartaba, así que la llamada solo agregaba latencia (20-80 s) y costo.
	"""
	return bool(
		_es_promedio_precio(spec)
		or _es_promedio_ratio(spec)
		or _es_promedio_siniestro(spec)
		or _es_promedio_siniestros_taller(spec)
		or _es_crecimiento_siniestros(spec)
		or _es_crecimiento_periodos(spec, pregunta)
		or _es_crecimiento_dimension(spec, pregunta)
		or _pide_mapa_efectividad(pregunta)
		or bool(_pide_mezcla_volumen_efectividad(pregunta))
		or bool(_pide_porcentaje_de_categorias(pregunta))
		or bool(_pide_porcentaje_por_dimension(pregunta))
		or _es_tabla_comparativa(spec, pregunta)
		or _usa_tabla_servidor(spec)
		or _es_listado(pregunta)
	)


@frappe.whitelist()
def ask(pregunta, conversacion=None, universo=None, periodo=None, desde=None, hasta=None, **_ignorados):
	"""Flujo completo: universo -> spec -> validación -> ejecución -> explicación.

	El modo de respuesta no es un parámetro del cliente: cualquier clave extra
	(p. ej. "modo") se ignora y el modo sale de la configuración en el servidor.
	"""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Debes iniciar sesión.", frappe.PermissionError)
	# Clave del selector de período: cualquier valor desconocido (o un
	# personalizado sin fechas válidas) cae al default.
	valido = (
		_rango_de_periodo(periodo, desde, hasta) is not None
		if periodo == PERIODO_PERSONALIZADO
		else periodo in {k for k, _ in PERIODOS_CONSULTA}
	)
	periodo = periodo if valido else PERIODO_DEFAULT
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
		# Si la pregunta nombra una aseguradora, manda esa: "te dije Afirme, no
		# busques en otros universos" (dueño, 2026-11-07). Antes el admin seguía
		# consultando el universo que tenía seleccionado y respondía con cifras
		# de otra aseguradora (con el aviso de alcance, pero cifras ajenas).
		mencionadas = _aseguradoras_mencionadas(pregunta)
		if len(mencionadas) == 1:
			elegida = next(iter(mencionadas))
			if elegida == "TODAS" or frappe.db.exists("Aseguradora", elegida):
				universo = elegida
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
	# "¿de qué …?" = "¿qué …?" para las detecciones.
	pregunta_det = _cada_como_por(_sinonimo_de_que(pregunta))
	intencion = intencion_de_pregunta(pregunta_det)
	modo_pedido = _modo_de_respuesta(intencion)

	# "A contra B" se arma en código: el modelo tardaba 20-60 s y el navegador
	# cortaba la petición (499) aunque la consulta tarda 0.1 s.
	spec = _spec_sin_modelo(pregunta_det)
	if spec is not None:
		_log_llamada(
			{}, "generate_spec", "Éxito", consulta_id, user, aseguradora_log,
			"spec en código (sin modelo)", historial=historial,
		)
	else:
		try:
			spec, info = generate_spec(pregunta.strip(), historial=historial)
		except (LLMConfigError, LLMProviderError) as e:
			_log_llamada(
				{}, "generate_spec", "Fallo", consulta_id, user, aseguradora_log, str(e),
				historial=historial,
			)
			mensaje = str(e)
			# Sin saldo en el proveedor no es "servicio caído": el aviso ya dice
			# qué hacer, así que no se le antepone nada.
			if "no tiene saldo" in mensaje:
				return {"ok": False, "mensaje": mensaje}
			return {"ok": False, "mensaje": f"El servicio de IA no está disponible: {e}"}
		_log_llamada(
			info, "generate_spec", "Éxito", consulta_id, user, aseguradora_log,
			historial=historial,
		)
	spec = _completar_efectividad(spec, pregunta_det)
	spec = _completar_mapa_efectividad(spec, pregunta_det)
	spec = _completar_mezcla_volumen_efectividad(spec, pregunta_det)
	spec = _completar_resumen(spec, pregunta_det)
	spec = _completar_no_recuperadas(spec, pregunta_det)
	spec = _completar_porcentaje_de_categorias(spec, pregunta_det)
	spec = _completar_no_localizadas(spec, pregunta_det)
	spec = _completar_siniestros_pendientes(spec, pregunta_det)
	spec = _completar_siniestros_por_cdr(spec, pregunta_det)
	spec = _completar_prioridad_pendientes(spec, pregunta_det)
	spec = _completar_piezas_caras(spec, pregunta_det)
	spec = _completar_piezas_con_precio(spec, pregunta_det)
	spec = _completar_solo_precios(spec, pregunta_det)
	spec = _completar_submarcas_danadas(spec, pregunta_det)
	spec = _completar_promedio_siniestros_taller(spec, pregunta_det)
	spec = _completar_promedio_siniestro(spec, pregunta_det)
	spec = _completar_promedios(spec, pregunta_det)
	spec = _completar_marcas_accidentadas(spec, pregunta_det)
	spec = _completar_pais_origen(spec, pregunta_det)
	spec = _completar_crecimiento_siniestros(spec, pregunta_det)
	spec = _completar_crecimiento_periodos(spec, pregunta_det)
	spec = _completar_crecimiento_dimension(spec, pregunta_det)
	spec = _completar_anios_modelo(spec, pregunta_det)
	spec = _completar_recolectadas(spec, pregunta_det)
	spec = _completar_preferencia_region(spec, pregunta_det)
	spec = _completar_parte(spec, pregunta_det)
	spec = _completar_piezas_mas_danadas(spec, pregunta_det)
	spec = _completar_filtro_danadas(spec, pregunta_det)
	spec = _completar_tabla_comparativa(spec, pregunta_det)
	spec = _completar_dimension(spec, pregunta_det)
	spec = _completar_volumen(spec, pregunta_det)
	spec = _completar_distintas_agrupadas(spec, pregunta_det)
	spec = _completar_valor_especifico(spec, pregunta_det)
	spec = _completar_listado(spec, pregunta_det)
	spec = _completar_lista_explicita(spec, pregunta_det)
	spec = _completar_region(spec, pregunta_det)
	spec = _completar_ficha_taller(spec, pregunta_det)
	spec = _completar_sin_lista_de_precios(spec, pregunta_det)
	spec = _completar_singular_plural(spec, pregunta_det)
	spec = _completar_comparacion(spec, pregunta_det)
	spec = _sanear_order_by(spec)
	spec = _aplicar_periodo(spec, periodo, pregunta, desde, hasta)
	if _pide_anio_en_curso(pregunta):
		# La respuesta devuelve el período que de verdad se consultó (el selector
		# se queda en "Este año" en el chat aunque estuviera en otro preset).
		periodo = "este_anio"

	if spec.get("out_of_scope"):
		_log_llamada(
			info, "generate_spec", FUERA_DE_ALCANCE, consulta_id, user, aseguradora_log,
			historial=historial,
		)
		# Si la pregunta nombraba otra aseguradora, el aviso de alcance va también
		# aquí: "fuera de alcance" a secas no le dice al usuario qué universo sí
		# se consulta, y deja la puerta abierta a reintentar con otra redacción.
		mensaje = MENSAJE_FUERA_DE_ALCANCE
		etiqueta = _etiqueta_alcance(pregunta, universo)
		if etiqueta:
			mensaje = f"{mensaje}\n\n{etiqueta}"
		doc, _ = _get_conversacion(conversacion, user, aseguradora_log, universo)
		_guardar_mensajes(
			doc, pregunta, mensaje, None, None, None,
			primera=not conversacion, universo=universo,
		)
		return {
			"ok": True,
			"fuera_de_alcance": True,
			"mensaje": mensaje,
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
				info, "generate_spec", "Spec rechazada", consulta_id, user, aseguradora_log, str(e),
				historial=historial,
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
		_log_llamada(
			info, "generate_spec", "Respuesta directa", consulta_id, user, aseguradora_log,
			historial=historial,
		)
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
		_log_llamada(
			info, "generate_spec", "Spec rechazada", consulta_id, user, aseguradora_log, str(e),
			historial=historial,
		)
		return {"ok": False, "mensaje": f"No pude construir una consulta válida: {e}"}
	except frappe.PermissionError as e:
		return {"ok": False, "mensaje": str(e)}

	# Sin período explícito que cae en un mes sin datos (los primeros días del
	# mes): se amplía al año en curso para poder contestar. El encabezado muestra
	# el rango real y el chat sincroniza el selector. Aplica a los promedios,
	# listados y precios, y desde el 2026-11-07 también a la efectividad: el
	# dueño preguntó "¿cuál es la región con mayor recolección de piezas?" a
	# principios de mes y la respuesta no traía ingresadas, recolectadas,
	# efectividad ni objetivo porque el mes no tenía grupos con 50 piezas.
	es_efectividad = _es_spec_efectividad(spec) and _pide_efectividad(pregunta_det)
	lados_contra = _comparacion_clasificada(pregunta_det)
	tabla_contra = (
		_tabla_comparacion_contra(lados_contra, spec, resultado, pregunta_det)
		if lados_contra
		else None
	)
	ajuste_promedio = False
	if (
		(
			_es_promedio_precio(spec)
			or _es_promedio_ratio(spec)
			or _es_promedio_siniestro(spec)
			or _es_listado(pregunta_det)
			or _pide_piezas_con_precio(pregunta_det)
			or _pide_solo_precios(pregunta_det)
			or _pide_lista_explicita(pregunta_det)
			or _es_tabla_comparativa(spec, pregunta_det)
			or bool(lados_contra and _comparacion_vacia(tabla_contra))
			or es_efectividad
		)
		and (
			_efectividad_sin_datos(spec, resultado)
			if es_efectividad
			else _promedio_sin_datos(spec, resultado)
		)
		and not (_trae_periodo(pregunta) or _trae_rango_de_fechas(pregunta))
	):
		desde_anio, hasta_anio = _periodo_anio_actual()
		if lados_contra:
			# La comparación se rearma con el año: las medidas llevan su período
			# dentro de cada agregación y hay que recalcularlo.
			filtros = dict(spec.get("filters") or {})
			filtros["fechaingreso"] = ["between", desde_anio, hasta_anio]
			spec = _completar_comparacion({**spec, "filters": filtros}, pregunta_det)
		elif _es_spec_efectividad(spec):
			# La efectividad lleva el período dentro de cada agregación (la unión
			# mezcla las dos bases de fecha): un `fechaingreso` global acotaría
			# solo la mitad de la fórmula.
			spec["aggregations"] = _aggs_efectividad(desde_anio, hasta_anio)
		else:
			filtros = dict(spec.get("filters") or {})
			filtros["fechaingreso"] = ["between", desde_anio, hasta_anio]
			spec["filters"] = filtros
		try:
			resultado = ejecutar(spec, user, alcance=alcance)
			ajuste_promedio = True
			if lados_contra:
				tabla_contra = _tabla_comparacion_contra(
					lados_contra, spec, resultado, pregunta_det
				)
		except (SpecInvalida, frappe.PermissionError):
			ajuste_promedio = False

	# Si explain falla (proveedor caído, JSON inválido), el except deja la
	# respuesta de respaldo y `info_exp` tiene que existir: sin esto, cualquier
	# hipo del LLM tronaba el endpoint con UnboundLocalError después de haber
	# consultado los datos.
	# Las respuestas que el código arma (título + período + tabla: promedios,
	# rankings, listados de precio, comparativas, crecimiento…) no gastan una
	# llamada al modelo: el proveedor puede tardar 20-80 s y el proxy corta a
	# los 120 s ("Error de conexión"). El texto del modelo se descartaba igual.
	explicacion_en_codigo = _explicacion_en_codigo(spec, pregunta_det) or bool(lados_contra)
	info_exp = {}
	explicacion = ""
	analisis_omitido = False
	if explicacion_en_codigo:
		_log_llamada(
			{"modo": "Código", "intencion": intencion}, "explain", "Éxito", consulta_id,
			user, aseguradora_log, "explicación en código (sin modelo)",
		)
	else:
		argumentos = dict(
			historial=historial,
			universo_nombre=_nombre_aseguradora(universo),
			periodo_texto=_texto_periodo(pregunta, spec),
			titulo_texto=_titulo_respuesta_para(spec, pregunta_det),
		)
		explicado = False
		if modo_pedido == "Analítico":
			# Presupuesto de tiempo: si el análisis no termina, no se espera más.
			# Se entrega la respuesta breve con una nota (decisión del dueño).
			try:
				explicacion, info_exp = explain(
					pregunta_det, spec, resultado, **argumentos,
					modo="Analítico", intencion=intencion,
					deadline=time.time() + _presupuesto_analisis_seg(),
				)
				_log_llamada(
					info_exp, "explain", "Éxito", consulta_id, user, aseguradora_log,
					historial=historial,
				)
				explicado = True
			except LLMTiempoAgotado as e:
				analisis_omitido = True
				_log_llamada(
					{}, "explain", "Fallo", consulta_id, user, aseguradora_log, str(e),
					historial=historial, modo="Analítico", intencion=intencion,
					analisis_omitido=True,
				)
			except (LLMConfigError, LLMProviderError) as e:
				_log_llamada(
					{}, "explain", "Fallo", consulta_id, user, aseguradora_log, str(e),
					historial=historial, modo="Analítico", intencion=intencion,
				)
		if not explicado:
			try:
				explicacion, info_exp = explain(
					pregunta_det, spec, resultado, **argumentos, intencion=intencion,
				)
				_log_llamada(
					info_exp, "explain", "Éxito", consulta_id, user, aseguradora_log,
					historial=historial,
				)
			except (LLMConfigError, LLMProviderError) as e:
				_log_llamada(
					{}, "explain", "Fallo", consulta_id, user, aseguradora_log, str(e),
					historial=historial, intencion=intencion,
				)
				explicacion = "Obtuve los datos, pero no pude generar la explicación en este momento."

	# El listado de prioridad lo pinta la app con sus encabezados exactos
	# (Submarca / Nombre pieza / Precio): el modelo los cambiaba por "Tipo /
	# Pieza / Valor" y agregaba prosa, así que su texto se descarta y queda solo
	# el encabezado de título (y período si lo hay).
	if _es_preferencia_region(spec):
		filas_region = _top_por_region(resultado)
		if filas_region:
			dim = spec["group_by"][1]
			resultado["columnas"] = ["region_pais", dim, "recolectadas"]
			resultado["filas"] = filas_region
			# explain dejó armada la tabla de la unión (una fila por región+marca):
			# aquí manda el top por región que calculamos.
			if isinstance(info_exp.get("datos"), dict):
				info_exp["datos"]["recolectadas_por_fila"] = None
		partes = [_titulo_respuesta(spec)]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		explicacion = "\n\n".join(partes)

	if (
		_es_crecimiento_siniestros(spec)
		and not _es_crecimiento_periodos(spec, pregunta_det)
		and not _es_crecimiento_dimension(spec, pregunta_det)
		and len(spec.get("group_by") or []) > 1
	):
		dimension = spec["group_by"][0]
		filas_dim, mes_previo, mes_ultimo = _crecimiento_por_dimension(resultado)
		titulo_dim = _titulo_respuesta(spec)
		if filas_dim:
			resultado["columnas"] = [dimension, "siniestros", "variacion"]
			resultado["filas"] = filas_dim
			if mes_previo and mes_ultimo:
				titulo_dim += (
					f" ({_mes_legible(mes_ultimo)} vs {_mes_legible(mes_previo)})"
				)
		partes = [titulo_dim]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		explicacion = "\n\n".join(partes)

	if (
		_es_crecimiento_siniestros(spec)
		and not _es_crecimiento_periodos(spec, pregunta_det)
		and len(spec.get("group_by") or []) == 1
	):
		filas_serie = _serie_con_variacion(resultado)
		if filas_serie:
			resultado["columnas"] = ["mes", "siniestros", "variacion"]
			resultado["filas"] = filas_serie
		partes = [_titulo_respuesta(spec)]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		explicacion = "\n\n".join(partes)

	if _es_crecimiento_dimension(spec, pregunta_det):
		# Crecimiento por dimensión: dos últimos bloques completos y variación.
		granularidad = _granularidad_de_crecimiento(pregunta_det)
		texto_dim = _sin_acentos(pregunta_det)
		descendente = not any(
			p in texto_dim for p in ("decrec", "disminuy", "baj", "menor", "peor", "caid")
		)
		filas_dim, etiquetas_dim, medida_dim = _crecimiento_por_bloque_dimension(
			resultado, granularidad, descendente=descendente
		)
		dimension = str(spec["group_by"][0])
		etiqueta_dim = ETIQUETAS_DIMENSION.get(
			dimension, dimension.replace("_", " ").title()
		)
		tope = _top_de_la_pregunta(pregunta_det) or 10
		filas_dim = filas_dim[:tope]
		if filas_dim:
			resultado["columnas"] = [dimension, "anterior", "actual", "variacion"]
			resultado["filas"] = filas_dim
		else:
			resultado["filas"] = []
		titulo_dim = (
			f"Crecimiento de {MEDIDAS_DE_CRECIMIENTO.get(medida_dim, 'Piezas').lower()}"
			f" por {etiqueta_dim}"
		)
		if len(etiquetas_dim) == 2:
			titulo_dim += f" ({etiquetas_dim[1]} vs {etiquetas_dim[0]})"
		partes = [titulo_dim]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if not filas_dim:
			partes.append("No hay datos en el período.")
		explicacion = "\n\n".join(partes)

	if _es_crecimiento_periodos(spec, pregunta_det):
		# Serie por mes/trimestre/cuatrimestre con la variación contra el bloque
		# anterior: la tabla y el texto los arma el código (dueño, 2026-11-07).
		granularidad = _granularidad_de_crecimiento(pregunta_det)
		filas_bloque, medida = _serie_por_bloque(resultado, granularidad)
		columna = {"mes": "mes", "trimestre": "trimestre", "cuatrimestre": "cuatrimestre"}[
			granularidad
		]
		if filas_bloque:
			resultado["columnas"] = [columna, medida, "variacion"]
			resultado["filas"] = filas_bloque
		else:
			resultado["filas"] = []
		partes = [_titulo_crecimiento_periodos(pregunta_det, medida)]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if not filas_bloque:
			partes.append("No hay datos en el período.")
		explicacion = "\n\n".join(partes)

	if _es_promedio_siniestros_taller(spec):
		# "¿Cuántos siniestros atiende en promedio cada taller al mes?"
		desde_t, hasta_t = _periodo_del_spec(spec) or _periodo_mes_actual()
		meses_t = _meses_del_rango(desde_t, hasta_t)
		al_mes_t = _pide_al_mes(pregunta_det)
		tabla_t = _tabla_promedio_siniestros_taller(
			spec, resultado, meses_t, al_mes_t, pregunta_det
		)
		if tabla_t:
			# Se deja en el resultado: `tabla_comparativa` se reinicia más abajo.
			resultado["columnas"] = tabla_t[0]
			resultado["filas"] = tabla_t[1]
		sufijo_t = " al mes" if al_mes_t else ""
		partes = [f"Siniestros por taller{sufijo_t}"]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if not tabla_t:
			if spec.get("group_by"):
				partes.append("No hay talleres con siniestros en el período.")
			else:
				frase = _frase_promedio_siniestros_taller(resultado, meses_t, al_mes_t)
				partes.append(frase or "No hay talleres con siniestros en el período.")
		explicacion = "\n\n".join(partes)

	if _es_promedio_siniestro(spec):
		if spec.get("group_by"):
			# "piezas por siniestro por taller": una fila por grupo con su
			# promedio, ordenado de mayor a menor.
			tope = _top_de_la_pregunta(pregunta_det) or 10
			filas_prom = _promedios_por_grupo(spec, resultado)
			partes = [_titulo_promedio(pregunta_det, spec)]
			periodo_txt = _texto_periodo(pregunta, spec)
			if periodo_txt:
				partes.append(periodo_txt)
			if ajuste_promedio:
				partes.append(
					"No había datos en el período seleccionado; se muestra el año en curso."
				)
			if filas_prom:
				resultado["columnas"] = list(spec["group_by"]) + ["promedio"]
				resultado["filas"] = filas_prom[:tope]
			else:
				resultado["filas"] = []
				partes.append("No hay siniestros en el período.")
			explicacion = "\n\n".join(partes)
		else:
			promedio = _promedio_de(resultado)
			partes = [_titulo_respuesta(spec)]
			periodo_txt = _texto_periodo(pregunta, spec)
			if periodo_txt:
				partes.append(periodo_txt)
			if ajuste_promedio:
				partes.append(
					"No había datos en el período seleccionado; se muestra el año en curso."
				)
			if promedio:
				partes.append(promedio)
			explicacion = "\n\n".join(partes)
			# La respuesta es la cifra: sin tabla de piezas/siniestros debajo.
			resultado["filas"] = []

	if _es_promedio_precio(spec) and not _es_tabla_comparativa(spec, pregunta_det):
		# El promedio se formatea en el servidor y el chat pinta la tabla (marca
		# + Precio promedio); el texto del modelo no se usa. En una tabla cruzada
		# de precios las celdas las formatea `_formatear_pivote_dinero`.
		resultado, valor_unico = _formatear_precio_promedio(resultado)
		partes = [_titulo_promedio(pregunta_det, spec)]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if ajuste_promedio:
			partes.append(
				"No había datos en el período seleccionado; se muestra el año en curso."
			)
		if not (resultado.get("filas") or []):
			partes.append("No hay piezas en el período.")
		elif valor_unico is not None:
			partes.append(f"Precio promedio: {valor_unico}")
		explicacion = "\n\n".join(partes)

	if _es_promedio_ratio(spec):
		partes = [_titulo_promedio(pregunta_det, spec)]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if ajuste_promedio:
			partes.append(
				"No había datos en el período seleccionado; se muestra el año en curso."
			)
		if spec.get("group_by"):
			# "promedio de piezas por taller por región": una fila por grupo.
			tope = _top_de_la_pregunta(pregunta_det) or 10
			filas_prom = _promedios_por_grupo(spec, resultado)
			if filas_prom:
				resultado["columnas"] = list(spec["group_by"]) + ["promedio"]
				resultado["filas"] = filas_prom[:tope]
			else:
				resultado["filas"] = []
				partes.append("No hay grupos en el período.")
		else:
			texto_prom = _promedio_ratio_de(resultado, pregunta_det)
			if texto_prom:
				partes.append(texto_prom)
			# Las dos cifras ya están en la frase: sin tabla unidades/grupos.
			resultado["filas"] = []
		explicacion = "\n\n".join(partes)


	if _es_listado(pregunta_det):
		# Listado de nombres: se quita la medida y el título sale de la pregunta
		# ("Talleres de la marca Nissan", "Plazas por Región").
		filas_lista, columnas_lista = _listado_sin_cifras(
			spec,
			resultado,
			_filas_legibles(spec, resultado, resultado.get("filas") or []),
		)
		resultado["columnas"] = columnas_lista
		resultado["filas"] = filas_lista
		partes = [_titulo_listado(pregunta_det, spec) or _titulo_respuesta(spec)]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if ajuste_promedio:
			partes.append(
				"No había datos en el período seleccionado; se muestra el año en curso."
			)
		if not filas_lista:
			partes.append("No hay resultados en el período.")
		explicacion = "\n\n".join(partes)

	# Tabla comparativa: el cruce lo arma el código, no el modelo (`_pivot`).
	tabla_comparativa = None
	if _es_tabla_comparativa(spec, pregunta_det):
		dinero = bool(_columnas_de_dinero(spec))
		columnas_piv, filas_piv, total_columnas = _pivot(
			spec["group_by"], resultado, vacio="—" if dinero else 0
		)
		if dinero:
			filas_piv = _formatear_pivote_dinero(filas_piv)
		tabla_comparativa = {"columnas": columnas_piv, "filas": filas_piv}
		if filas_piv:
			resultado["columnas"] = columnas_piv
			resultado["filas"] = filas_piv
		partes = [_titulo_comparativa(spec)]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if ajuste_promedio:
			partes.append(
				"No había datos en el período seleccionado; se muestra el año en curso."
			)
		if not filas_piv:
			partes.append("No hay piezas en el período.")
		elif total_columnas > len(columnas_piv) - 1:
			etiqueta = ETIQUETAS_DIMENSION.get(
				str(spec["group_by"][1]), str(spec["group_by"][1]).replace("_", " ").title()
			)
			partes.append(
				f"Se muestran las {len(columnas_piv) - 1} columnas de {etiqueta} con más volumen."
			)
		explicacion = "\n\n".join(partes)

	if _pide_recolectan_mas(pregunta_det) and _es_spec_efectividad(spec):
		# "más se recolectan" se contesta con la tabla de Ingresadas, Recolectadas
		# y Efectividad: el modelo la resumía en una línea ("Cellini es el que más
		# recolecta…") y el dueño pidió las tres cifras.
		partes = [_titulo_respuesta(spec)]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if ajuste_promedio:
			partes.append(AVISO_ANIO)
		if not ((info_exp.get("datos") or {}).get("porcentajes") or []):
			partes.append("No hay piezas con recolección en el período.")
		explicacion = "\n\n".join(partes)
	elif ajuste_promedio and _es_spec_efectividad(spec):
		explicacion = f"{explicacion}\n\n{AVISO_ANIO}"

	# El crecimiento ya armó su encabezado (con los meses comparados): este
	# bloque no debe pisarlo.
	if _usa_tabla_servidor(spec) and not _es_crecimiento_siniestros(spec):
		partes = [_titulo_respuesta_para(spec, pregunta_det)]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if ajuste_promedio:
			partes.append(
				"No había datos en el período seleccionado; se muestra el año en curso."
			)
		if not (resultado.get("filas") or []):
			# Sin filas el encabezado solo dejaría la respuesta en blanco.
			if _es_listado_prioridad(spec):
				partes.append("No hay piezas pendientes en el período.")
			elif _es_listado_mas_caras(spec):
				partes.append("No hay piezas en el período.")
			else:
				partes.append("No hay piezas ingresadas en el período.")
		explicacion = "\n\n".join(partes)

	if _pide_ficha_taller(pregunta_det):
		taller_ficha, _ = _taller_mencionado(pregunta_det)
		if not taller_ficha:
			cond = (spec.get("filters") or {}).get("taller_asignado")
			taller_ficha = cond[1] if isinstance(cond, list) and len(cond) >= 2 else cond
		# El crecimiento sale de una segunda consulta: el período anterior no
		# cabe en el WHERE del actual.
		previas_ficha = None
		desde_ficha, hasta_ficha = _periodo_del_spec(spec) or _periodo_mes_actual()
		previo_desde, previo_hasta = _periodo_anterior((desde_ficha, hasta_ficha))
		spec_previo = {
			"doctype": "Pieza",
			"fields": [],
			"filters": {"taller_asignado": ["=", taller_ficha]},
			"aggregations": _aggs_efectividad(previo_desde, previo_hasta),
			"group_by": [],
			"order_by": [],
			"limit": 1,
		}
		try:
			resultado_previo = ejecutar(spec_previo, user, alcance=alcance)
			porcentajes_previos = _porcentajes(spec_previo, resultado_previo)
			if porcentajes_previos and (porcentajes_previos[0].get("total") or 0):
				previas_ficha = porcentajes_previos[0].get("pct") or 0
		except (SpecInvalida, frappe.PermissionError):
			previas_ficha = None
		tabla_ficha = _tabla_ficha_taller(spec, resultado, previas_ficha)
		if tabla_ficha:
			tabla_comparativa = {"columnas": tabla_ficha[0], "filas": tabla_ficha[1]}
		partes = [f"Ficha del taller {taller_ficha}"]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if not tabla_ficha:
			partes.append("No hay datos del taller en el período.")
		explicacion = "\n\n".join(partes)

	porcentaje_dimension = _pide_porcentaje_por_dimension(pregunta_det)
	if porcentaje_dimension:
		dimension_pct, categorias_pct = porcentaje_dimension
		tabla_pct = _tabla_porcentaje_por_dimension(
			spec, resultado, dimension_pct, categorias_pct, pregunta_det
		)
		if tabla_pct:
			tabla_comparativa = {"columnas": tabla_pct[0], "filas": tabla_pct[1]}
		partes = [
			_titulo_porcentaje_por_dimension(
				pregunta_det, dimension_pct, categorias_pct, tabla_pct[2] if tabla_pct else 50
			)
		]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if not tabla_pct:
			partes.append(
				"No hay grupos con 50 o más piezas ingresadas en el período."
			)
		explicacion = "\n\n".join(partes)

	categorias_porcentaje = _pide_porcentaje_de_categorias(pregunta_det)
	if categorias_porcentaje:
		tabla_pct = _tabla_porcentaje_de_categorias(
			spec, resultado, categorias_porcentaje
		)
		if tabla_pct:
			tabla_comparativa = {"columnas": tabla_pct[0], "filas": tabla_pct[1]}
		partes = [_titulo_porcentaje_de_categorias(categorias_porcentaje)]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if not tabla_pct:
			partes.append("No hay piezas ingresadas en el período.")
		explicacion = "\n\n".join(partes)

	mezcla_volumen = _pide_mezcla_volumen_efectividad(pregunta_det)
	if mezcla_volumen and _es_spec_efectividad(spec):
		# "¿Qué estados tienen mucho volumen pero baja efectividad?"
		resultado_mezcla = _tabla_mezcla_volumen_efectividad(
			spec, resultado, mezcla_volumen
		)
		columnas_mezcla, filas_mezcla, umbral = resultado_mezcla or ([], [], 0)
		tope_mezcla = _top_de_la_pregunta(pregunta_det) or 50
		if filas_mezcla:
			tabla_comparativa = {
				"columnas": columnas_mezcla,
				"filas": filas_mezcla[:tope_mezcla],
			}
		partes = [
			_titulo_mezcla_volumen_efectividad(pregunta_det, spec, mezcla_volumen, umbral)
		]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if not filas_mezcla:
			volumen_alto, efectividad_alta = mezcla_volumen
			partes.append(
				"No hay grupos con "
				f"{'mucho' if volumen_alto else 'bajo'} volumen y "
				f"{'alta' if efectividad_alta else 'baja'} efectividad en el período."
			)
		explicacion = "\n\n".join(partes)

	if _pide_mapa_efectividad(pregunta_det) and _es_spec_efectividad(spec):
		# Mapa de efectividad: la tabla de siempre más la columna del semáforo.
		mapa = _tabla_mapa_efectividad(spec, resultado)
		if mapa:
			tabla_comparativa = {"columnas": mapa[0], "filas": mapa[1]}
		dimensiones = [
			ETIQUETAS_DIMENSION.get(str(g), str(g).replace("_", " ").title())
			for g in (spec.get("group_by") or [])
		]
		partes = ["Mapa de efectividad" + (f" por {' y '.join(dimensiones)}" if dimensiones else "")]
		periodo_txt = _texto_periodo(pregunta, spec)
		if periodo_txt:
			partes.append(periodo_txt)
		if not mapa:
			partes.append("No hay datos en el período.")
		explicacion = "\n\n".join(partes)

	# "A contra B" (dueño, 2026-11-07): la respuesta son las dos cosas. Las dos
	# dimensiones son columnas o filas según lo que se compare y el texto lo
	# arma el código (el modelo tendía a quedarse con un solo lado).
	if lados_contra:
		if (
			tabla_contra is None
			and _es_spec_efectividad(spec)
			and all(lado[0] == "periodo" for lado in lados_contra)
		):
			# Efectividad de dos períodos: cada ventana tiene su propia base de
			# fecha, así que se consultan por separado.
			spec_b = {**spec, "aggregations": _aggs_efectividad(*lados_contra[1][1])}
			try:
				resultado_b = ejecutar(spec_b, user, alcance=alcance)
			except (SpecInvalida, frappe.PermissionError):
				resultado_b = None
			if resultado_b:
				tabla_contra = _tabla_efectividad_entre_periodos(
					lados_contra, spec, resultado, resultado_b
				)
		if tabla_contra:
			tabla_comparativa = {"columnas": tabla_contra[0], "filas": tabla_contra[1]}
		partes = [
			f"Comparación: {_etiqueta_del_lado(lados_contra[0])} contra "
			f"{_etiqueta_del_lado(lados_contra[1])}"
		]
		if all(lado[0] == "periodo" for lado in lados_contra):
			desde_a, hasta_a = lados_contra[0][1]
			desde_b, hasta_b = lados_contra[1][1]
			desde_min = min(desde_a, desde_b)
			hasta_max = max(hasta_a, hasta_b)
			partes.append(f"Del {_fecha_legible(desde_min)} al {_fecha_legible(hasta_max)}")
		else:
			periodo_txt = _texto_periodo(pregunta, spec)
			if periodo_txt:
				partes.append(periodo_txt)
		if ajuste_promedio:
			partes.append(AVISO_ANIO)
		if not tabla_contra:
			partes.append("No hay piezas en el período.")
		explicacion = "\n\n".join(partes)

	# Etiqueta de alcance impuesta por el servidor: si la pregunta nombra otra
	# aseguradora, la respuesta lo dice aunque el modelo haya redactado otra
	# cosa. No se nombra a la otra aseguradora (no se confirma su existencia).
	etiqueta = _etiqueta_alcance(pregunta, universo)
	if etiqueta and _texto_normalizado(etiqueta) not in _texto_normalizado(explicacion):
		explicacion = f"{explicacion}\n\n{etiqueta}"
	if analisis_omitido:
		explicacion = f"{explicacion}\n\n{NOTA_TIEMPO_ANALISIS}"

	# La efectividad se pinta con la dimensión y las cuatro columnas del criterio;
	# el resultado crudo (total, recolectadas, inconsistentes, rezagadas) no sale
	# al chat. Con render_chart se deja el crudo para que el gráfico encuentre
	# sus columnas.


	porcentajes = (info_exp.get("datos") or {}).get("porcentajes") or []
	mostrar = _tabla_para_chat(spec, resultado, porcentajes, info_exp.get("datos"))
	if tabla_comparativa is not None:
		mostrar = tabla_comparativa
	rango_consultado = _rango_del_spec(spec)
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
		"periodo": "este_anio" if ajuste_promedio else periodo,
		"periodo_del_texto": bool(
			_trae_periodo(pregunta)
			or _trae_rango_de_fechas(pregunta)
			or ajuste_promedio
		),
		"rango": (
			{"desde": rango_consultado[0], "hasta": rango_consultado[1]}
			if rango_consultado
			else None
		),
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
	con_dimension = bool(dimensiones)
	columnas = (
		([" / ".join(dimensiones)] if con_dimension else [])
		+ ["Ingresadas", "Recolectadas", "Efectividad", "Objetivo"]
	)
	filas = [
		(
			([p.get("grupo") or ""] if con_dimension else [])
			+ [
				p.get("total") or 0,
				p.get("recuperadas") or 0,
				f"{p.get('pct') or 0:.1f}%",
				p.get("vs_objetivo") or "",
			]
		)
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
	"marca": "Marca",
	"piezades": "Nombre pieza",
	"siniestros": "Siniestros",
	"mes": "Mes",
	"trimestre": "Trimestre",
	"cuatrimestre": "Cuatrimestre",
	"variacion": "Variación",
	"anterior": "Bloque anterior",
	"actual": "Bloque actual",
	"estado": "Estado",
	"no_recolectadas": "No recolectadas en periodo",
	"id_estatus": "Estatus",
	"no_localizadas": "No localizadas",
	"siniestro": "Siniestro",
	"pais_origen": "País de origen",
	"estadolocalidad": "Estado",
	"recolectadas_en_periodo": "Recolectadas en periodo",
	"fechaingreso": "Fecha de ingreso",
	"fecha_hora_recoleccion": "Fecha de recolección",
	"fecha_inicio": "Fecha de inicio",
	"fecha_completado": "Fecha de completado",
	"preciovale": "Precio",
	"precio_promedio": "Precio promedio",
	"precios": "Precios",
	"promedio": "Promedio",
	"unidades": "Unidades",
	"grupos": "Grupos",
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
			if isinstance(valor, (int, float)):
				if valor != 0:
					return False
			elif valor not in (None, "", "0", "0.0", "$0.00"):
				# Celdas ya formateadas ("$3,444.78") también son dato.
				return False
	return True


def _es_spec_efectividad(spec):
	alias = {str(a.get("alias") or "").strip().lower() for a in spec.get("aggregations") or []}
	return "total" in alias and "recolectadas" in alias


def _tabla_no_recuperadas(spec, datos, totales, etiqueta="No recolectadas"):
	"""Dimensión (si hay) + la suma de las tres categorías no recuperadas."""
	columnas = [str(c).lower() for c in datos.get("columnas") or []]
	dims = [str(g).lower() for g in spec.get("group_by") or []]
	indices = [columnas.index(d) for d in dims if d in columnas]
	titulo = " / ".join(
		ETIQUETAS_DIMENSION.get(d, d.replace("_", " ").title()) for d in dims
	)
	encabezados = ([titulo] if titulo else []) + [etiqueta]
	filas = [
		[fila[i] for i in indices] + [total]
		for fila, total in zip(datos.get("filas") or [], totales)
	]
	return encabezados, filas


def _columnas_de_dinero(spec):
	"""Columnas que son precios: `preciovale` y las sumas/promedios de él."""
	columnas = set()
	campos = {str(f).lower() for f in spec.get("fields") or []}
	if "preciovale" in campos:
		columnas.add("preciovale")
	for agg in spec.get("aggregations") or []:
		if isinstance(agg, dict) and str(agg.get("field") or "").lower() == "preciovale":
			columnas.add(str(agg.get("alias") or "").lower())
	return columnas


def _formatear_dinero(spec, resultado, filas):
	""""$1,234.56": separador de miles en la columna de precio del chat."""
	columnas = [str(c).lower() for c in resultado.get("columnas") or []]
	dinero = _columnas_de_dinero(spec)
	indices = [i for i, c in enumerate(columnas) if c in dinero]
	if not indices or not filas:
		return filas
	salida = []
	for fila in filas:
		nueva = list(fila)
		for i in indices:
			if i >= len(nueva):
				continue
			valor = nueva[i]
			if valor in (None, ""):
				nueva[i] = "Sin dato"
				continue
			if isinstance(valor, str) and valor.startswith("$"):
				continue
			try:
				numero = float(valor)
			except (TypeError, ValueError):
				continue
			nueva[i] = f"${numero:,.2f}"
		salida.append(nueva)
	return salida


def _formatear_pivote_dinero(filas):
	"""Formatea las celdas de un cruce de precios ("$1,234.56")."""
	salida = []
	for fila in filas:
		nueva = []
		for i, valor in enumerate(fila):
			if i == 0:
				nueva.append(valor)
			elif isinstance(valor, (int, float)):
				nueva.append(f"${valor:,.2f}")
			else:
				nueva.append(valor)
		salida.append(nueva)
	return salida


def _resultado_de_un_solo_valor(spec, resultado):
	"""Una fila, una medida y sin desglose: es una cifra, no una tabla."""
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	return (
		len(resultado.get("filas") or []) == 1
		and len(resultado.get("columnas") or []) == 1
		and not spec.get("group_by")
		and not spec.get("fields")
		and len(aggs) == 1
	)


def _tabla_para_chat(spec, resultado, porcentajes, datos=None):
	"""Tabla que se pinta en el chat (no se toca ninguna fila)."""
	return _tabla_para_chat_base(spec, resultado, porcentajes, datos)


def _tabla_para_chat_base(spec, resultado, porcentajes, datos=None):
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
			"filas": _formatear_dinero(
				spec,
				{"columnas": datos.get("columnas")},
				datos["filas"],
			),
		}
	if _todo_en_cero(spec, resultado):
		return {"columnas": None, "filas": None, "columnas_crudas": None}
	totales = datos.get("no_recuperadas_por_fila")
	if totales is not None:
		columnas, filas = _tabla_no_recuperadas(spec, datos, totales)
		return {"columnas": columnas, "filas": filas}
	recolectadas = datos.get("recolectadas_por_fila")
	if recolectadas is not None:
		columnas, filas = _tabla_no_recuperadas(
			spec, datos, recolectadas, etiqueta="Recolectadas"
		)
		return {"columnas": columnas, "filas": filas}
	tabla = None if spec.get("render_chart") else _tabla_efectividad(spec, porcentajes)
	if tabla:
		return {"columnas": tabla[0], "filas": tabla[1]}
	if spec.get("render_chart"):
		return resultado
	if _es_spec_efectividad(spec):
		# Sin `explain` (proveedor caído) no venían los porcentajes; se calculan
		# aquí para no perder la tabla cuando hay más de una fila.
		tabla = _tabla_efectividad(spec, _porcentajes(spec, resultado))
		if tabla and len(tabla[1]) > 1:
			return {"columnas": tabla[0], "filas": tabla[1]}
		return {"columnas": None, "filas": None}
	if not resultado.get("filas"):
		return {"columnas": None, "filas": None}
	if _resultado_de_un_solo_valor(spec, resultado):
		# Un solo valor se contesta en la frase (el modelo o el bloque de código
		# ya lo escriben); no se pinta la tabla de una casilla.
		return {"columnas": None, "filas": None}
	return {
		"columnas": [_titulo_columna(c) for c in resultado.get("columnas") or []],
		"filas": _formatear_dinero(spec, resultado, resultado.get("filas")),
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


# El rango se mueve con las cargas de piezas (cada día, como mucho), así que se
# cachea: el chat lo pide al abrirse y en cada cambio de universo.
CACHE_PERIODO_DATOS = 600


def _fecha_de_rango(valor):
	"""Fecha legible sin hora: el rango es de días, no de segundos."""
	if not valor:
		return ""
	try:
		return _fecha_legible(frappe.utils.getdate(valor))
	except Exception:
		return str(valor)


def _cache_periodo():
	"""Cache del rango de piezas (en una función para poder sustituirlo en tests)."""
	return frappe.cache()


@frappe.whitelist()
def periodo_datos(universo=None):
	"""Rango de información disponible en Pieza para el universo activo.

	Lo pide la barra superior del chat cuando el usuario pertenece a una
	aseguradora o el admin eligió un universo: la fecha de creación más antigua
	y la de última actualización de las piezas del alcance, para que se sepa
	hasta cuándo llegan los datos antes de preguntar. El alcance lo impone el
	servidor (get_tenant_scope), nunca el parámetro del navegador.
	"""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Debes iniciar sesión.", frappe.PermissionError)
	propias = get_user_aseguradoras(user)
	if propias is None:
		# Administrador: solo hay rango cuando ya eligió un universo. Antes de
		# elegirlo la barra se queda oculta (no se anuncia el global).
		if not universo:
			return {"mostrar": False}
		try:
			_alcance_efectivo(user, universo)
		except frappe.PermissionError as e:
			return {"mostrar": False, "mensaje": str(e)}
		alcance = universo
	elif not propias:
		return {"mostrar": False, "mensaje": "Tu usuario no tiene una aseguradora asignada."}
	else:
		alcance = None
	nombre = _nombre_aseguradora(alcance or (propias[0] if propias else ""))
	if propias is not None and len(propias) > 1:
		nombre = "Tus aseguradoras"

	where, params = get_tenant_scope("Pieza", user, alcance)
	if where == "1 = 0":
		return {"mostrar": False, "universo": nombre}

	llave = "molduplastic_ia:periodo_pieza:" + (",".join(sorted(str(p) for p in params)) or "TODAS")
	cache = _cache_periodo()
	datos = cache.get_value(llave)
	if datos is None:
		sql = "SELECT MIN(creation) AS desde, MAX(modified) AS hasta FROM `tabPieza`"
		if where:
			sql += f" WHERE {where}"
		try:
			filas = frappe.db.sql(sql, params, as_dict=True)
		except Exception:
			# Sin los índices del rango la consulta puede exceder MAX_JOIN_SIZE.
			# La barra es informativa: si falla, el chat sigue funcionando.
			frappe.log_error(title="IA: periodo de datos", message=frappe.get_traceback())
			filas = []
		fila = filas[0] if filas else {}
		datos = {
			"desde": str(fila.get("desde") or ""),
			"hasta": str(fila.get("hasta") or ""),
		}
		cache.set_value(llave, datos, expires_in_sec=CACHE_PERIODO_DATOS)
	if not datos.get("desde") and not datos.get("hasta"):
		return {"mostrar": False, "universo": nombre}
	return {
		"mostrar": True,
		"universo": nombre,
		"desde": datos.get("desde") or "",
		"hasta": datos.get("hasta") or "",
		"desde_texto": _fecha_de_rango(datos.get("desde")),
		"hasta_texto": _fecha_de_rango(datos.get("hasta")),
	}


@frappe.whitelist()
def periodos():
	"""Períodos del selector del chat, con su rango ya resuelto.

	La lista vive en `PERIODOS_CONSULTA` (una sola copia, servidor y cliente):
	el JS solo pinta lo que recibe. `rango_texto` es la etiqueta que se ve al
	lado del selector, para que se sepa exactamente qué fechas se consultan.
	"""
	items = []
	for clave, etiqueta in PERIODOS_CONSULTA:
		desde, hasta = _rango_de_periodo(clave)
		items.append(
			{
				"clave": clave,
				"etiqueta": etiqueta,
				"desde": desde,
				"hasta": hasta,
				"rango_texto": f"Del {_fecha_legible(desde)} al {_fecha_legible(hasta)}",
			}
		)
	# El personalizado no trae rango: lo eligen los dos campos de fecha del chat.
	items.append(
		{"clave": PERIODO_PERSONALIZADO, "etiqueta": "Personalizado", "desde": "", "hasta": "", "rango_texto": ""}
	)
	return {"defecto": PERIODO_DEFAULT, "periodos": items}


def _alcance_tokens(user, aseguradora=None):
	"""Alcance del consumo: tenant propio o elección independiente de admin."""
	propias = get_user_aseguradoras(user)
	if propias is not None:
		if not propias:
			frappe.throw("Tu usuario no tiene una aseguradora asignada.", frappe.PermissionError)
		return propias, False, "Tus aseguradoras" if len(propias) > 1 else _nombre_aseguradora(propias[0])

	if not aseguradora or aseguradora == "TODAS":
		return [], True, "Todas las aseguradoras"
	if not frappe.db.exists("Aseguradora", aseguradora):
		frappe.throw("La aseguradora seleccionada no existe.", frappe.PermissionError)
	return [str(aseguradora)], True, _nombre_aseguradora(aseguradora)


def _resolver_periodo_tokens(periodo=None, desde=None, hasta=None):
	"""Rango de tokens con las mismas reglas de presets del selector del chat."""
	valido = (
		_rango_de_periodo(periodo, desde, hasta) is not None
		if periodo == PERIODO_PERSONALIZADO
		else periodo in {k for k, _ in PERIODOS_CONSULTA}
	)
	clave = periodo if valido else PERIODO_DEFAULT
	rango = _rango_de_periodo(clave, desde, hasta)
	return clave, rango


@frappe.whitelist()
def tokens_uso(aseguradora=None, periodo=None, desde=None, hasta=None):
	"""Tokens consumidos por día; el servidor impone el alcance del usuario."""
	user = frappe.session.user
	if user == "Guest":
		frappe.throw("Debes iniciar sesión.", frappe.PermissionError)

	aseguradoras, admin, universo = _alcance_tokens(user, aseguradora)
	clave, rango = _resolver_periodo_tokens(periodo, desde, hasta)
	desde_sql, hasta_sql = rango
	where = ["fecha_hora BETWEEN %s AND %s"]
	params = [desde_sql, hasta_sql]
	if aseguradoras:
		where.append("aseguradora IN (" + ", ".join(["%s"] * len(aseguradoras)) + ")")
		params.extend(aseguradoras)

	filas = frappe.db.sql(
		"SELECT DATE(fecha_hora) AS dia, SUM(tokens_entrada) AS entrada, "
		"SUM(tokens_salida) AS salida FROM `tabIA API Log` WHERE "
		+ " AND ".join(where)
		+ " GROUP BY DATE(fecha_hora) ORDER BY dia",
		params,
		as_dict=True,
	)
	por_dia = {}
	for fila in filas:
		dia = frappe.utils.getdate(fila.get("dia"))
		por_dia[dia.strftime("%Y-%m-%d")] = {
			"entrada": int(fila.get("entrada") or 0),
			"salida": int(fila.get("salida") or 0),
		}

	dia_desde = frappe.utils.getdate(desde_sql)
	dia_hasta = frappe.utils.getdate(hasta_sql)
	dias = []
	for desplazamiento in range(frappe.utils.date_diff(dia_hasta, dia_desde) + 1):
		dia = frappe.utils.add_days(dia_desde, desplazamiento)
		fecha = dia.strftime("%Y-%m-%d")
		uso = por_dia.get(fecha, {"entrada": 0, "salida": 0})
		total_dia = uso["entrada"] + uso["salida"]
		dias.append(
			{
				"fecha": fecha,
				"etiqueta": f"{dia.day} {MESES_ES[dia.month - 1][:3]}",
				"entrada": uso["entrada"],
				"salida": uso["salida"],
				"total": total_dia,
			}
		)

	entrada_total = sum(d["entrada"] for d in dias)
	salida_total = sum(d["salida"] for d in dias)
	resultado = {
		"ok": True,
		"es_admin": admin,
		"aseguradora": aseguradoras[0] if len(aseguradoras) == 1 else ("TODAS" if admin else ""),
		"universo": universo,
		"periodo": clave,
		"desde": desde_sql,
		"hasta": hasta_sql,
		"rango_texto": f"Del {_fecha_legible(desde_sql)} al {_fecha_legible(hasta_sql)}",
		"entrada": entrada_total,
		"salida": salida_total,
		"total": entrada_total + salida_total,
		"dias": dias,
	}
	if admin:
		resultado["aseguradoras"] = lista_aseguradoras()
	return resultado


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


def _normalizar_grafico(grafico):
	"""Config de gráfica en una sola forma: {tipo, x, y, titulo}.

	Acepta el formato que manda el chat y el viejo de `spec.render_chart`
	(`{"type": "pie", "x": ..., "y": ...}`), y conserva las series ya calculadas
	de los reportes anteriores (`labels`/`values`) para que sigan abriendo.
	Un tipo desconocido cae a barras: la gráfica se pinta igual en vez de
	romper el reporte.
	"""
	if not grafico:
		return None
	if isinstance(grafico, str):
		grafico = _json_seguro(grafico, None)
	if not isinstance(grafico, dict):
		return None
	tipo = str(grafico.get("tipo") or grafico.get("type") or "").strip().lower()
	if tipo not in TIPOS_GRAFICA:
		tipo = "bar"
	titulo = str(grafico.get("titulo") or grafico.get("title") or "")[:140]
	y = grafico.get("y")
	if isinstance(y, str):
		y = [y]
	y = [str(c)[:140] for c in (y or []) if c]
	x = str(grafico.get("x") or "")[:140]
	if not y:
		# Reportes viejos: el JS guardaba la serie ya calculada, sin ejes.
		labels, values = grafico.get("labels"), grafico.get("values")
		if isinstance(labels, list) and isinstance(values, list):
			return {"tipo": tipo, "x": "", "y": [], "titulo": titulo, "labels": labels, "values": values}
		return None
	return {"tipo": tipo, "x": x, "y": y, "titulo": titulo}


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
	grafico = _normalizar_grafico(grafico)
	# El tipo lo decide el contenido: con config de gráfico es "Gráfico"; sin
	# ella, "Tabla". No se confía en el texto que mande el navegador.
	tipo = "Gráfico" if grafico else "Tabla"
	doc = frappe.get_doc(
		{
			"doctype": "IA Reporte",
			"titulo": titulo[:140],
			"tipo": tipo,
			"estado": "Activo",
			"fecha": _ahora(),
			"usuario": user,
			"aseguradora": aseguradora,
			"alcance": alcance,
			"conversacion": conversacion,
			"spec_json": json.dumps(spec, ensure_ascii=False),
			"snapshot_json": snapshot if isinstance(snapshot, str) else json.dumps(snapshot, ensure_ascii=False, default=str),
			"grafico_json": json.dumps(grafico, ensure_ascii=False) if grafico else None,
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
	spec = _json_seguro(doc.spec_json, None)
	if int(reejecutar or 0):
		if not spec:
			frappe.throw("El reporte guardado está corrupto y no se puede re-ejecutar.")
		resultado = ejecutar(spec, frappe.session.user, alcance=doc.alcance or None)
		datos.update(
			{"columnas": resultado["columnas"], "filas": resultado["filas"], "cobertura": resultado["cobertura"], "en_vivo": True}
		)
	else:
		snap = _json_seguro(doc.snapshot_json, {})
		datos.update({"columnas": snap.get("columnas", []), "filas": snap.get("filas", []), "en_vivo": False})
	# El spec y la gráfica viajan de vuelta para que el chat pueda volver a
	# graficar o guardar el reporte sin perder lo que ya tenía.
	datos["spec"] = spec
	datos["grafico"] = _normalizar_grafico(_json_seguro(doc.grafico_json, None))
	return datos
