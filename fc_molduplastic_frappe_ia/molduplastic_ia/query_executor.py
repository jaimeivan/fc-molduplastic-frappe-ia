"""Ejecutor del Query Spec validado.

NOTA DE SEGURIDAD: el LLM nunca genera SQL. Este módulo construye el SQL
exclusivamente a partir de un spec ya validado: los identificadores
(tabla/columnas/funciones) salen de la whitelist y los valores viajan siempre
como parámetros. El filtro de tenant se inyecta aquí mismo, además de las
permission query conditions del Desk.

Se usa SQL directo (en lugar del ORM) porque tabPieza tiene 3M+ filas y las
agregaciones GROUP BY no son viables en Python.
"""

import frappe

from fc_molduplastic_frappe_ia.molduplastic_ia.permissions import _alcance_efectivo, get_tenant_scope
from fc_molduplastic_frappe_ia.molduplastic_ia.validate_spec import validar_spec

TABLAS = {"Pieza": "tabPieza", "Recoleccion": "tabRecoleccion", "Taller": "tabTaller"}

AGGS_SQL = {
	"count": "COUNT",
	"count_distinct": "COUNT",
	"sum": "SUM",
	"avg": "AVG",
	"min": "MIN",
	"max": "MAX",
}


ALIAS_REZAGADAS = "rezagadas_recolectadas"


def ejecutar(spec, user=None, alcance=None):
	"""Ejecuta un spec validado. Retorna {columnas, filas, cobertura}."""
	spec = validar_spec(spec)
	user = user or frappe.session.user
	if _un_solo_tenant(user, alcance):
		# Con un solo tenant el filtro por aseguradora no puede ampliar nada: o
		# es redundante (la propia) o da vacío (otra). Se descarta para que una
		# pregunta por otra aseguradora no devuelva "no hay datos" ni sugiera
		# que el filtro tuvo efecto; el aviso de alcance lo pone ask().
		if _tiene_filtro_aseguradora(spec):
			frappe.logger("molduplastic_ia").info(
				f"Filtro de aseguradora descartado por alcance único ({user}): {spec.get('filters')}"
			)
		spec = _sin_filtro_aseguradora(spec)
	spec = _spec_con_aseguradora(spec)
	if _lleva_dos_bases(spec):
		return _ejecutar_dos_bases(spec, user, alcance)
	return _ejecutar_spec(spec, user, alcance)


def _un_solo_tenant(user, alcance):
	"""True si el alcance efectivo es exactamente una aseguradora."""
	aseguradoras = _alcance_efectivo(user, alcance)
	return aseguradoras is not None and len(aseguradoras) == 1


def _tiene_filtro_aseguradora(spec):
	if "aseguradora" in (spec.get("filters") or {}):
		return True
	return any("aseguradora" in (agg.get("filtros") or {}) for agg in spec.get("aggregations") or [])


def _sin_filtro_aseguradora(spec):
	"""Quita `aseguradora` de los filtros (global y de agregaciones)."""
	quitar = lambda filtros: {c: v for c, v in (filtros or {}).items() if c != "aseguradora"}
	nuevo = {**spec, "filters": quitar(spec.get("filters"))}
	nuevo["aggregations"] = [
		{**agg, "filtros": quitar(agg.get("filtros"))} for agg in spec.get("aggregations") or []
	]
	return nuevo


def _base_de_agg(agg, spec=None):
	"""Base de fecha de una agregación: su propio "between" manda.

	Las medidas del período por ingreso llevan `fechaingreso` en sus filtros; las
	que se miden por recolección (rezagadas, recolectadas del período) llevan
	`fecha_hora_recoleccion`. Las que no traen ninguna usan el filtro global y
	corren en la consulta principal.
	"""
	filtros = agg.get("filtros") or {}

	def es_between(f):
		return isinstance(f, list) and len(f) == 3 and str(f[0]).lower() == "between"

	# La base es el campo cuyo BETWEEN define el período. Una rezagada trae
	# fecha_hora_recoleccion between (su período) y fechaingreso "<" (el corte);
	# la unión de recolectadas trae los dos between y va por ingreso.
	if es_between(filtros.get("fecha_hora_recoleccion")) and not es_between(
		filtros.get("fechaingreso")
	):
		return "recoleccion"
	if filtros.get("fechaingreso") is not None:
		return "ingreso"
	# Un filtro de recolección que no sea un between no define el período (p.
	# ej. "fecha_hora_recoleccion is null" de pendientes): va con el global.
	# Sin fecha propia: manda el filtro global del spec.
	glob = (spec or {}).get("filters") or {}
	if glob.get("fechaingreso") is not None:
		return "ingreso"
	if glob.get("fecha_hora_recoleccion") is not None:
		return "recoleccion"
	return None


def _lleva_dos_bases(spec):
	"""True si el spec mezcla medidas por ingreso y por recolección."""
	bases = {_base_de_agg(a, spec) for a in spec.get("aggregations") or []}
	return "ingreso" in bases and "recoleccion" in bases


def _periodo_comun(aggs, campo):
	"""El primer `between` del campo en los filtros de las agregaciones."""
	for agg in aggs:
		f = (agg.get("filtros") or {}).get(campo)
		if isinstance(f, list) and len(f) == 3 and str(f[0]).lower() == "between":
			return f
	return None


def _ejecutar_spec(spec, user, alcance, con_cobertura=True):
	"""Una consulta: WHERE + agregaciones + (opcional) cobertura."""
	doctype = spec["doctype"]
	tabla = TABLAS[doctype]
	where, params = _construir_where(tabla, spec["filters"], doctype, user, alcance)
	if where == "1 = 0":
		filas, columnas = [], _columnas_salida(spec)
	else:
		filas, columnas = _consultar(tabla, spec, where, params)
	if not con_cobertura:
		return {"columnas": columnas, "filas": filas, "cobertura": {}}
	universo = 0 if where == "1 = 0" else _contar(tabla, where, params)
	cobertura = _cobertura(tabla, spec, where, params, universo)
	return {"columnas": columnas, "filas": filas, "cobertura": cobertura}


def _ejecutar_dos_bases(spec, user, alcance):
	"""Consulta por separado lo que se mide por ingreso y lo que se mide por
	recolección, y lo une por la dimensión del group_by.

	En un solo SQL el WHERE no puede acotar las dos fechas a la vez: con
	`filters` vacío MySQL escanea el tenant completo (GNP en el año tardaba 11
	minutos; separadas son ~14 s + ~28 s, cada una por su índice). Sin cobertura
	por el mismo motivo que la ruta de efectividad: los campos medidos son las
	condiciones de la fórmula, no datos faltantes.
	"""
	aggs = spec["aggregations"]
	por_ingreso = [a for a in aggs if _base_de_agg(a, spec) != "recoleccion"]
	por_recoleccion = [a for a in aggs if _base_de_agg(a, spec) == "recoleccion"]
	principal = {**spec, "aggregations": por_ingreso}
	periodo = _periodo_comun(por_ingreso, "fechaingreso")
	if periodo:
		principal["filters"] = {**(spec.get("filters") or {}), "fechaingreso": periodo}
	aparte = {
		**spec,
		"filters": dict(spec.get("filters") or {}),
		"aggregations": por_recoleccion,
		"order_by": [],
		"limit": _max_limit(),
	}
	periodo_r = _periodo_comun(por_recoleccion, "fecha_hora_recoleccion")
	if periodo_r:
		aparte["filters"]["fecha_hora_recoleccion"] = periodo_r
	# La base de cada consulta la manda su propio período: el filtro global de
	# la otra base no aplica (una rezagada ingresó antes del período).
	aparte["filters"].pop("fechaingreso", None)
	principal["filters"].pop("fecha_hora_recoleccion", None)
	base = _ejecutar_spec(principal, user, alcance, con_cobertura=False)
	otros = _ejecutar_spec(aparte, user, alcance, con_cobertura=False)
	filas, columnas = _unir_medidas(base, otros, spec.get("group_by") or [])
	# Se devuelven en el orden del spec (group_by + alias) para que el resto del
	# código (porcentajes, tablas) encuentre las columnas donde espera.
	orden = _columnas_salida(spec)
	if set(orden) <= set(columnas):
		filas = [[fila[columnas.index(c)] for c in orden] for fila in filas]
		columnas = orden
	return {"columnas": columnas, "filas": filas, "cobertura": {}}


def _unir_medidas(base, otros, dimensiones):
	"""Une a la consulta principal las medidas de la consulta de recolección.

	La clave es la dimensión del group_by. Las medidas que solo existen en
	"otros" se agregan con 0 en las filas de la principal, y los grupos que solo
	aparecen por recolección se agregan con las medidas de ingreso en 0 (los
	descarta después `_porcentajes` porque no tienen ingresadas que medir).
	"""
	col_b = list(base["columnas"])
	col_o = list(otros["columnas"])
	medidas = [c for c in col_o if c not in dimensiones]
	if not medidas:
		return base["filas"], col_b
	if not dimensiones:
		fila = list(base["filas"][0]) if base["filas"] else [0] * len(col_b)
		fila += [0] * len(medidas)
		if otros["filas"]:
			for j, c in enumerate(medidas):
				fila[len(col_b) + j] = otros["filas"][0][col_o.index(c)] or 0
		return [fila], col_b + medidas
	if any(d not in col_b or d not in col_o for d in dimensiones):
		return base["filas"], col_b
	idx_b = [col_b.index(d) for d in dimensiones]
	idx_o = [col_o.index(d) for d in dimensiones]
	filas, pos = [], {}
	for fila in base["filas"]:
		pos[tuple(fila[i] for i in idx_b)] = len(filas)
		filas.append(list(fila) + [0] * len(medidas))
	for fila in otros["filas"]:
		clave = tuple(fila[i] for i in idx_o)
		valores = [fila[col_o.index(c)] or 0 for c in medidas]
		if clave in pos:
			i = pos[clave]
			for j, v in enumerate(valores):
				filas[i][len(col_b) + j] = v
			continue
		nueva = [0] * len(col_b)
		for i_b, i_o in zip(idx_b, idx_o):
			nueva[i_b] = fila[i_o]
		nueva += valores
		pos[clave] = len(filas)
		filas.append(nueva)
	return filas, col_b + medidas


def _docname_aseguradora(valor, mapa):
	"""Nombre de aseguradora -> docname. Cualquier otro valor pasa intacto."""
	if isinstance(valor, (str, int)):
		return mapa.get(str(valor).strip().lower(), valor)
	return valor


def _valores_con_docname(cond, mapa):
	"""Recorre una condición ya validada cambiando nombres por docnames."""
	if isinstance(cond, dict):
		return cond
	if isinstance(cond, list) and cond and isinstance(cond[0], str):
		op = cond[0].lower()
		if op in ("is", "mes_distinto", "mes_igual"):
			return cond
		if op == "between":
			return [cond[0], _docname_aseguradora(cond[1], mapa), _docname_aseguradora(cond[2], mapa)]
		if op == "in":
			return [cond[0], [_docname_aseguradora(v, mapa) for v in cond[1]]]
		if op == "like":
			# "GNP" con comodines es una igualdad disfrazada.
			nombre = mapa.get(str(cond[1]).strip().strip("%").strip().lower())
			return ["=", nombre] if nombre else cond
		return [cond[0], _valores_con_docname(cond[1], mapa)]
	if isinstance(cond, list) and all(isinstance(c, list) for c in cond):
		return [_valores_con_docname(c, mapa) for c in cond]
	return _docname_aseguradora(cond, mapa)


def _spec_con_aseguradora(spec):
	"""Traduce los filtros por aseguradora de nombre a docname.

	`tabPieza.aseguradora` guarda el docname ("6"), pero el modelo escribe el
	nombre que aparece en la pregunta ("GNP"): el filtro no coincidía con nada y
	la consulta salía vacía sin error ("no hay datos"). El mapa se arma solo si
	algún filtro menciona aseguradora.
	"""
	mapa = None

	def normalizar(filtros):
		nonlocal mapa
		if not filtros or "aseguradora" not in filtros:
			return filtros
		if mapa is None:
			# filas como diccionarios: `for name, nombre in ...` desempacaba las
			# claves ("nombre"/"name") y el mapa quedaba inservible.
			mapa = {
				str(fila["nombre"]).strip().lower(): str(fila["name"])
				for fila in frappe.get_all("Aseguradora", fields=["name", "nombre"])
			}
		return {**filtros, "aseguradora": _valores_con_docname(filtros["aseguradora"], mapa)}

	nuevo = dict(spec)
	nuevo["filters"] = normalizar(spec.get("filters") or {})
	nuevo["aggregations"] = [
		{**agg, "filtros": normalizar(agg.get("filtros") or {})} for agg in spec.get("aggregations") or []
	]
	return nuevo


def _col(tabla, campo):
	"""Columna calificada. Las columnas virtuales devuelven su expresión SQL.

	`region_pais` no es una columna de tabPieza: es un CASE sobre
	estadolocalidad que arma regiones.py. El mapa vive en el código, así que
	el LLM no puede escribir la expresión, solo pedirla por nombre.
	`Recoleccion.estado` tampoco existe: es el estado del taller asignado y se
	resuelve con una subconsulta sobre Taller.
	"""
	if tabla == "tabPieza" and campo == "region_pais":
		from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import expresion_region

		return expresion_region()
	if tabla == "tabPieza" and campo == "mes":
		# YYYY-MM del ingreso. El %% es obligatorio: frappe hace `query % args` y
		# un %Y pelado rompe con ValueError.
		return f"DATE_FORMAT(`{tabla}`.`fechaingreso`, '%%Y-%%m')"
	if tabla == "tabPieza" and campo == "pais_origen":
		# País de origen de la marca (Marcas.cat_origen es Link a Catalogo Datos).
		# Pieza.marca guarda "Nissan" y el maestro "NISSAN": se compara en mayúsculas.
		return (
			"(SELECT `tabCatalogo Datos`.`descripcion` FROM `tabMarcas`"
			" JOIN `tabCatalogo Datos` ON `tabCatalogo Datos`.`name` = `tabMarcas`.`cat_origen`"
			f" WHERE `tabMarcas`.`name` = UPPER(`{tabla}`.`marca`) LIMIT 1)"
		)
	if tabla == "tabTaller" and campo == "estado":
		from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import expresion_estado

		return expresion_estado(f"`{tabla}`.`{campo}`")
	if tabla == "tabRecoleccion" and campo == "estado":
		from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import expresion_estado

		sub = expresion_estado("`tabTaller`.`estado`")
		return (
			f"(SELECT {sub} FROM `tabTaller`"
			f" WHERE `tabTaller`.`name` = `{tabla}`.`id_taller`)"
		)
	if tabla == "tabRecoleccion" and campo == "region":
		from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import expresion_region_de_plaza

		return expresion_region_de_plaza(_col(tabla, "plaza"))
	if tabla == "tabRecoleccion" and campo == "plaza":
		# Recolector-Talleres guarda la plaza del taller (id_taller_plaza). El
		# par taller+recolector es el exacto; si no está, se cae al taller.
		rt = "`tabRecolector-Talleres`"
		return (
			"(SELECT COALESCE("
			f"(SELECT {rt}.`id_taller_plaza` FROM {rt}"
			f" WHERE {rt}.`id_taller` = `{tabla}`.`id_taller`"
			f" AND {rt}.`id_recolector` = `{tabla}`.`id_recolector` LIMIT 1),"
			f"(SELECT {rt}.`id_taller_plaza` FROM {rt}"
			f" WHERE {rt}.`id_taller` = `{tabla}`.`id_taller` LIMIT 1)))"
		)
	return f"`{tabla}`.`{campo}`"


def _es_virtual(tabla, campo):
	if tabla == "tabPieza" and campo in ("region_pais", "pais_origen", "mes"):
		return True
	if campo == "estado" and tabla in ("tabRecoleccion", "tabTaller"):
		return True
	if campo == "plaza" and tabla == "tabRecoleccion":
		return True
	if campo == "region" and tabla == "tabRecoleccion":
		return True
	return False


def _select(tabla, campo):
	"""`_col` con alias cuando la columna es virtual (un CASE no trae nombre)."""
	expr = _col(tabla, campo)
	if _es_virtual(tabla, campo):
		return f"{expr} AS `{campo}`"
	return expr


def _construir_where(tabla, filters, doctype, user, alcance=None):
	"""Retorna (where_sql, params). Todo valor va como parámetro."""
	condiciones = []
	params = []
	for campo, cond in filters.items():
		condiciones.append(_condicion(tabla, campo, cond))
		params.extend(_params_de(cond))
	tenant_where, tenant_params = get_tenant_scope(doctype, user, alcance)
	if tenant_where == "1 = 0":
		return "1 = 0", []
	if tenant_where:
		condiciones.append(f"({tenant_where})")
		params.extend(tenant_params)
	return (" AND ".join(condiciones) if condiciones else "1 = 1"), params


def _condicion(tabla, campo, cond):
	"""Un campo + una condición ya validada → fragmento SQL.

	Varias condiciones sobre el mismo campo se unen con AND (entre paréntesis
	para no romper el CASE WHEN de las agregaciones).
	"""
	if isinstance(cond, list) and cond and isinstance(cond[0], list):
		partes = " AND ".join(_condicion_simple(tabla, campo, c) for c in cond)
		return f"({partes})"
	return _condicion_simple(tabla, campo, cond)


def _condicion_simple(tabla, campo, cond):
	"""Una condición simple (no lista de condiciones) → fragmento SQL.

	El valor puede ser una referencia a otro campo ({"campo": ...}, solo de
	comparación, mes_distinto o mes_igual): se inserta como identificador de la
	whitelist, nunca como parámetro.
	"""
	col = _col(tabla, campo)
	if not isinstance(cond, list):
		cond = ["=", cond]
	op = cond[0]
	if op == "is":
		if cond[1] == "empty":
			# "Vacío" en el negocio es null o cadena vacía; la lista de
			# condiciones del spec se une con AND, así que el OR va aquí.
			return f"({col} IS NULL OR {col} = '')"
		return f"{col} IS {'NULL' if cond[1] == 'null' else 'NOT NULL'}"
	if op in ("mes_distinto", "mes_igual"):
		otro = _col(tabla, cond[1]["campo"])
		comparador = "!=" if op == "mes_distinto" else "="
		return f"DATE_FORMAT({col}, '%%Y-%%m') {comparador} DATE_FORMAT({otro}, '%%Y-%%m')"
	if isinstance(cond[1], dict):
		return f"{col} {op} {_col(tabla, cond[1]['campo'])}"
	if op == "between":
		return f"{col} BETWEEN %s AND %s"
	if op == "in":
		return f"{col} IN ({', '.join(['%s'] * len(cond[1]))})"
	if op == "like":
		return f"{col} LIKE %s"
	return f"{col} {op} %s"


def _params_de(cond):
	"""Los valores de una condición, en el orden en que `_condicion` los usa.

	Las referencias a otro campo no aportan parámetros. Varias condiciones
	sobre el mismo campo aportan los suyos en orden.
	"""
	if isinstance(cond, list) and cond and isinstance(cond[0], list):
		params = []
		for c in cond:
			params.extend(_params_de(c))
		return params
	if not isinstance(cond, list):
		return [cond]
	op = cond[0]
	if op in ("mes_distinto", "mes_igual"):
		return []
	if isinstance(cond[1], dict):
		return []
	if op == "between":
		return [cond[1], cond[2]]
	if op == "in":
		return list(cond[1])
	if op == "is":
		return []
	if op == "like":
		return [_sanear_like(cond[1])]
	return [cond[1]]


def _sanear_like(valor):
	"""Los % son comodines en cualquier posición; escapa `_` y la barra invertida.

	El dueño pidió el 2026-11-06 filtrar por partes compuestas
	("piezades LIKE 'FACIA%DEL%'"), así que el % interno también es comodín.
	"""
	return str(valor).replace("\\", "\\\\").replace("_", "\\_")


def _columnas_salida(spec):
	if spec["aggregations"]:
		return list(spec["group_by"]) + [a["alias"] for a in spec["aggregations"]]
	return list(spec["fields"])


def _max_limit():
	return int(frappe.get_cached_doc("Molduplastic IA Settings").max_limit or 500)


def _limite_sql(spec):
	"""LIMIT del SQL, ampliado cuando la medida es la efectividad.

	El porcentaje no es una columna del SQL: el orden y el corte se hacen en
	Python (`explain`). Con el `limit` que elige el modelo (50) MySQL devolvía
	50 grupos al azar y los mejores del mes quedaban fuera antes de poder
	ordenarlos (septiembre: 210 talleres; DITTRICH, con 5.1%, no venía en el
	corte). Se traen todos los grupos hasta `max_limit` y la presentación
	respeta el `limit` pedido (un top 10 muestra 10).
	"""
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	if not aggs or not spec.get("group_by"):
		return spec["limit"]
	alias = {str(a.get("alias") or "").strip().lower() for a in aggs}
	if "recolectadas" in alias:
		return max(spec["limit"], _max_limit())
	# "No recuperadas" también se ordena en Python por la suma de las tres
	# categorías: con el limit del modelo se perdían grupos antes de sumar.
	if {"no_recolectadas", "inconsistentes_no_recolectadas", "rezagadas_no_recolectadas"} <= alias:
		return max(spec["limit"], _max_limit())
	# Ranking con filtro (una medida con "filtros" propia: "piezas no
	# localizadas por taller"): el COUNT da 0 en los grupos sin ninguna, MySQL
	# devuelve esos ceros mezclados y `explain` los descarta DESPUÉS del LIMIT,
	# así que el desglose salía con una sola fila. Se traen todos los grupos.
	if any(a.get("filtros") for a in aggs):
		return max(spec["limit"], _max_limit())
	return spec["limit"]


def _consultar(tabla, spec, where, params):
	if spec["aggregations"]:
		select = [_select(tabla, g) for g in spec["group_by"]]
		agg_params = []
		for agg in spec["aggregations"]:
			select.append(_agregacion(tabla, agg, agg_params))
		sql = f"SELECT {', '.join(select)} FROM `{tabla}` WHERE {where}"
		if spec["group_by"]:
			sql += f" GROUP BY {', '.join(_col(tabla, g) for g in spec['group_by'])}"
		# El SELECT va antes del WHERE en el SQL, así que sus parámetros van primero.
		params = agg_params + params
	else:
		select = [_select(tabla, f) for f in spec["fields"]]
		sql = f"SELECT {', '.join(select)} FROM `{tabla}` WHERE {where}"
	if spec["order_by"]:
		orden = []
		for o in spec["order_by"]:
			orden.append(f"`{o['field']}` {o['dir'].upper()}")
		sql += f" ORDER BY {', '.join(orden)}"
	sql += " LIMIT %s"
	rows = frappe.db.sql(sql, params + [_limite_sql(spec)], as_dict=True)
	columnas = _columnas_salida(spec)
	return [[r[c] for c in columnas] for r in rows], columnas


def _agregacion(tabla, agg, params_salida):
	"""Una agregación → fragmento SELECT, FEZ su where, y sus parámetros.

	Sin filtro propio es `FN(arg)`. Con filtro se envuelve en CASE WHEN para que
	la medida caiga en cero en vez de NULL cuando no haya filas que contar:
	`SUM(CASE WHEN <filtro> THEN <arg> END)`.

	El CASE va DENTRO del agregado. Al revés —`CASE WHEN cond THEN SUM(x)`— el
	agregado se evalúa sobre todas las filas y el CASE solo descarta el
	resultado entero, así que la medida sale sin filtrar: es un bug silencioso
	que devuelve el total completo. Con el CASE dentro, las filas que no cumplen
	aportan NULL y el agregado las ignora, que es lo buscado. Sin coincidencia
	la celda queda NULL, no 0.
	"""
	func, campo, alias = agg["func"], agg["field"], agg["alias"]
	arg = "*" if campo == "*" else _col(tabla, campo)
	if func == "count_distinct":
		arg = f"DISTINCT {arg}"
	if agg.get("filtros"):
		piezas = " AND ".join(_condicion(tabla, c, cond) for c, cond in agg["filtros"].items())
		for cond in agg["filtros"].values():
			params_salida.extend(_params_de(cond))
		# COUNT(CASE ...) no cuenta nada: se cuenta un 1 por fila que cumple.
		if func == "count" and campo == "*":
			arg = "1"
		arg = f"CASE WHEN {piezas} THEN {arg} END"
	elif func == "count" and campo == "*":
		arg = "*"
	if func == "count":
		return f"COUNT({arg}) AS `{alias}`"
	return f"{AGGS_SQL[func]}({arg}) AS `{alias}`"


def _contar(tabla, where, params):
	if where == "1 = 0":
		return 0
	return frappe.db.sql(f"SELECT COUNT(*) FROM `{tabla}` WHERE {where}", params)[0][0]


def _refs_de(filtros):
	"""Campos referidos por {"campo": ...} dentro de un dict de filtros."""
	refs = []
	for cond in (filtros or {}).values():
		for c in _subcondiciones(cond):
			if isinstance(c, list) and len(c) == 2 and isinstance(c[1], dict):
				refs.append(c[1]["campo"])
	return refs


def _subcondiciones(cond):
	if isinstance(cond, list) and cond and isinstance(cond[0], list):
		return cond
	return [cond]


def _cobertura(tabla, spec, where, params, universo):
	"""Universo + % de registros con dato en cada campo consultado.

	El denominador de cada campo es la población de la agregación **sin las
	condiciones de ese mismo campo**. Filtrar por id_estatus = 100 (o IS NULL) y
	medir después la cobertura de id_estatus daría 0% por construcción: la
	cobertura mide si el dato viene vacío en la población de la que se extrajo la
	cifra, no dentro del subconjunto que ya lo exige.
	"""
	campos = list(spec["fields"]) + [a["field"] for a in spec["aggregations"] if a["field"] != "*"]
	# Los campos que solo aparecen en un filtro de agregación también se consultan,
	# así que su cobertura tiene que salir o el "monto pendiente" puede salir en
	# cero sin que nadie vea que el campo venía vacío.
	campos += [c for a in spec["aggregations"] for c in (a.get("filtros") or {})]
	# Los campos referidos por {"campo": ...} también se leen en el SQL.
	campos += _refs_de(spec["filters"])
	for a in spec["aggregations"]:
		campos += _refs_de(a.get("filtros"))
	campos = list(dict.fromkeys(campos))
	# Si una categoría se define por el campo VACÍO (id_estatus IS NULL en
	# "pendientes", fecha_hora_recoleccion IS NULL en lo mismo), esos nulos son
	# una categoría del resumen, no datos faltantes. Anunciarlos como cobertura
	# al 90% en cada respuesta del resumen hacía dudar de una cifra correcta.
	como_categoria = {
		campo_f
		for a in spec["aggregations"]
		for campo_f, cond in (a.get("filtros") or {}).items()
		if any(
			# El campo VACÍO es la categoría (pendientes) y el campo con un valor
			# fijo o una lista también define la población (id_estatus = 206 en
			# "piezas no localizadas", recolectable = 1). En los dos casos el
			# nulo no es un dato faltante y no se avisa como cobertura.
			isinstance(sub, list)
			and sub
			and (
				(sub[0] == "is" and len(sub) > 1 and sub[1] == "null")
				or sub[0] in ("=", "in")
			)
			for sub in _subcondiciones(cond)
		)
	}
	campos = [c for c in campos if c not in como_categoria]
	if not campos or not universo:
		return {"universo": universo, "campos": {}}

	global_where = where not in ("", "1 = 1")
	con_filtros = [a for a in spec["aggregations"] if a.get("filtros")]

	# Universo de cada agregación tal cual (sin quitarle condiciones).
	totales = []
	for agg in con_filtros:
		w, prm = _where_y_params(tabla, where, params, agg["filtros"], global_where)
		totales.append((agg, _contar(tabla, w, prm)))

	detalle = {}
	for c in campos:
		elegida = None
		for agg in con_filtros:
			partes, prm = [], []
			for campo_f, cond in agg["filtros"].items():
				if campo_f == c:
					# Del campo medido se descarta la SELECCIÓN (=, in, is null):
					# medir la cobertura de id_estatus dentro de "id_estatus = 100"
					# da 0% por construcción. El PERÍODO (between) sí se conserva:
					# es lo que define la población; quitarlo devuelve el tenant
					# entero (fechaingreso salía 92% en vez de 100%).
					for sub in _subcondiciones(cond):
						if isinstance(sub, list) and sub and sub[0] == "between":
							partes.append(_condicion_simple(tabla, campo_f, sub))
							prm.extend(_params_de(sub))
					continue
				partes.append(_condicion(tabla, campo_f, cond))
				prm.extend(_params_de(cond))
			w = " AND ".join(([f"({where})"] if global_where else []) + partes) or "1 = 1"
			prm = params + prm
			total = _contar(tabla, w, prm)
			if total and (elegida is None or total > elegida[2]):
				elegida = (w, prm, total)
			if total >= universo:
				break
		if elegida is None:
			# Sin población medible (todas las agregaciones condicionan el campo y
			# no dejan filas al quitarlo): mejor no inventar un porcentaje.
			continue
		w, prm, u = elegida
		row = frappe.db.sql(
			f"SELECT COUNT({_col(tabla, c)}) AS n FROM `{tabla}` WHERE {w}", prm, as_dict=True
		)[0]
		con_datos = int(row["n"] or 0)
		detalle[c] = {
			"con_datos": con_datos,
			"universo": u,
			"pct": round(100 * con_datos / u, 1),
		}

	# El universo que se informa es el de la agregación más general (la del
	# resumen: "ingresadas"), no el del tenant: si no, los porcentajes de arriba
	# no cuadrarían con el número que se anuncia al lado.
	if totales:
		general = min(totales, key=lambda par: len(par[0]["filtros"]))
		universo_informado = general[1]
		if not universo_informado:
			universo_informado = max([d["universo"] for d in detalle.values()] + [universo])
	else:
		universo_informado = universo
	return {"universo": universo_informado, "campos": detalle}


def _where_y_params(tabla, where, params, filtros, global_where):
	partes, prm = [], []
	for campo_f, cond in filtros.items():
		partes.append(_condicion(tabla, campo_f, cond))
		prm.extend(_params_de(cond))
	w = " AND ".join(([f"({where})"] if global_where else []) + partes) or "1 = 1"
	return w, params + prm
