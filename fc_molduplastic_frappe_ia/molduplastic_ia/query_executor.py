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

from fc_molduplastic_frappe_ia.molduplastic_ia.permissions import get_tenant_scope
from fc_molduplastic_frappe_ia.molduplastic_ia.validate_spec import validar_spec

TABLAS = {"Pieza": "tabPieza", "Recoleccion": "tabRecoleccion", "Taller": "tabTaller"}

AGGS_SQL = {"count": "COUNT", "sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX"}


def ejecutar(spec, user=None, alcance=None):
	"""Ejecuta un spec validado. Retorna {columnas, filas, cobertura}."""
	spec = validar_spec(spec)
	doctype = spec["doctype"]
	tabla = TABLAS[doctype]
	user = user or frappe.session.user

	where, params = _construir_where(tabla, spec["filters"], doctype, user, alcance)
	if where == "1 = 0":
		universo = 0
		filas, columnas = [], _columnas_salida(spec)
	else:
		universo = _contar(tabla, where, params)
		filas, columnas = _consultar(tabla, spec, where, params)
	cobertura = _cobertura(tabla, spec, where, params, universo)
	return {"columnas": columnas, "filas": filas, "cobertura": cobertura}


def _col(tabla, campo):
	return f"`{tabla}`.`{campo}`"


def _construir_where(tabla, filters, doctype, user, alcance=None):
	"""Retorna (where_sql, params). Todo valor va como parámetro."""
	condiciones = []
	params = []
	for campo, cond in filters.items():
		col = _col(tabla, campo)
		if not isinstance(cond, list):
			cond = ["=", cond]
		op = cond[0]
		if op == "is":
			condiciones.append(f"{col} IS {'NULL' if cond[1] == 'null' else 'NOT NULL'}")
		elif op == "between":
			condiciones.append(f"{col} BETWEEN %s AND %s")
			params.extend([cond[1], cond[2]])
		elif op == "in":
			ph = ", ".join(["%s"] * len(cond[1]))
			condiciones.append(f"{col} IN ({ph})")
			params.extend(cond[1])
		elif op == "like":
			condiciones.append(f"{col} LIKE %s")
			params.append(_sanear_like(cond[1]))
		else:
			condiciones.append(f"{col} {op} %s")
			params.append(cond[1])
	tenant_where, tenant_params = get_tenant_scope(doctype, user, alcance)
	if tenant_where == "1 = 0":
		return "1 = 0", []
	if tenant_where:
		condiciones.append(f"({tenant_where})")
		params.extend(tenant_params)
	return (" AND ".join(condiciones) if condiciones else "1 = 1"), params


def _sanear_like(valor):
	"""Permite % solo como comodín inicial/final; escapa el resto."""
	valor = str(valor).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
	izq = valor.startswith("\\%")
	der = valor.endswith("\\%") and len(valor) > 2
	nucleo = valor[2:] if izq else valor
	nucleo = nucleo[:-2] if der else nucleo
	return f"{'%' if izq else ''}{nucleo}{'%' if der else ''}"


def _columnas_salida(spec):
	if spec["aggregations"]:
		return list(spec["group_by"]) + [a["alias"] for a in spec["aggregations"]]
	return list(spec["fields"])


def _consultar(tabla, spec, where, params):
	if spec["aggregations"]:
		select = [_col(tabla, g) for g in spec["group_by"]]
		for agg in spec["aggregations"]:
			fn = AGGS_SQL[agg["func"]]
			arg = "*" if agg["field"] == "*" else _col(tabla, agg["field"])
			select.append(f"{fn}({arg}) AS `{agg['alias']}`")
		sql = f"SELECT {', '.join(select)} FROM `{tabla}` WHERE {where}"
		if spec["group_by"]:
			sql += f" GROUP BY {', '.join(_col(tabla, g) for g in spec['group_by'])}"
	else:
		select = [_col(tabla, f) for f in spec["fields"]]
		sql = f"SELECT {', '.join(select)} FROM `{tabla}` WHERE {where}"
	if spec["order_by"]:
		orden = []
		for o in spec["order_by"]:
			orden.append(f"`{o['field']}` {o['dir'].upper()}")
		sql += f" ORDER BY {', '.join(orden)}"
	sql += " LIMIT %s"
	rows = frappe.db.sql(sql, params + [spec["limit"]], as_dict=True)
	columnas = _columnas_salida(spec)
	return [[r[c] for c in columnas] for r in rows], columnas


def _contar(tabla, where, params):
	if where == "1 = 0":
		return 0
	return frappe.db.sql(f"SELECT COUNT(*) FROM `{tabla}` WHERE {where}", params)[0][0]


def _cobertura(tabla, spec, where, params, universo):
	"""Universo + % de registros con dato en cada campo consultado."""
	campos = list(spec["fields"]) + [a["field"] for a in spec["aggregations"] if a["field"] != "*"]
	campos = list(dict.fromkeys(campos))
	detalle = {}
	if universo and campos:
		select = [f"COUNT({_col(tabla, c)}) AS `{c}`" for c in campos]
		row = frappe.db.sql(
			f"SELECT {', '.join(select)} FROM `{tabla}` WHERE {where}", params, as_dict=True
		)[0]
		for c in campos:
			con_datos = int(row[c] or 0)
			detalle[c] = {"con_datos": con_datos, "pct": round(100 * con_datos / universo, 1)}
	return {"universo": universo, "campos": detalle}
