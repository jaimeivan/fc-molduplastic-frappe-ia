"""Cálculos del análisis (fase 2). Funciones puras: sin BD, sin LLM.

Toda cifra que la respuesta analítica cita sale de aquí. Las consultas que
alimentan estas funciones las arma `api._calcular_analisis`, que usa el mismo
camino que la consulta principal (validar_spec, alcance y ejecutar).

Convenciones:
- Un porcentaje o una variación sin base de comparación es None, nunca 0.
- Una base cero no produce variación porcentual; sí produce variación absoluta.
- Los puntos porcentuales se restan directamente (no son aditivos entre
  periodos cuando las rezagadas cambian de mes).
"""

import math


class TiempoAgotado(Exception):
	"""El presupuesto de tiempo del análisis se agotó antes de terminar."""


def porcentaje(parte, total):
	if parte is None or not total:
		return None
	return round(parte / total * 100, 1)


def variacion(actual, anterior):
	"""(variación absoluta, variación porcentual) o (None, None) sin base."""
	if actual is None or anterior is None:
		return None, None
	absoluta = actual - anterior
	if not anterior:
		return absoluta, None
	return absoluta, round(absoluta / anterior * 100, 1)


def puntos_pp(pct_actual, pct_anterior):
	"""Diferencia en puntos porcentuales, o None si falta alguno de los dos."""
	if pct_actual is None or pct_anterior is None:
		return None
	return round(pct_actual - pct_anterior, 1)


def brecha_objetivo(pct_actual, objetivo):
	if pct_actual is None or objetivo is None:
		return None
	return round(pct_actual - objetivo, 1)


def concentracion(pares, total, top_n=3):
	"""Participación de los `top_n` grupos mayores sobre `total`.

	`pares` es una lista de (etiqueta, valor). `total` debe venir de una consulta
	sin agrupar: la suma de los grupos no siempre es el total, porque el GROUP BY
	se trunca con el LIMIT.
	"""
	if not total or total <= 0:
		return None
	mayores = sorted(
		((etiqueta, valor or 0) for etiqueta, valor in pares),
		key=lambda par: par[1],
		reverse=True,
	)[:top_n]
	return {
		"top_n": top_n,
		"participacion_pct": round(sum(v for _, v in mayores) / total * 100, 1),
		"total": total,
		"principales": [etiqueta for etiqueta, _ in mayores],
	}


def extremos(filas, min_base=0):
	"""Mejor y peor grupo por porcentaje, y valores atípicos.

	`filas` es una lista de dicts con "grupo", "pct" y "base" (la cantidad que
	sostiene el porcentaje). Los grupos con base menor a `min_base` no cuentan.
	Un atípico es un grupo a dos desviaciones estándar o más de la media; con
	menos de cinco grupos elegibles no se buscan atípicos.
	"""
	elegibles = [f for f in filas if f.get("pct") is not None and (f.get("base") or 0) >= min_base]
	if not elegibles:
		return {"mejor": None, "peor": None, "atipicos": []}

	def resumen(f):
		return {"grupo": f["grupo"], "pct": f["pct"], "base": f["base"]}

	mejor = max(elegibles, key=lambda f: f["pct"])
	peor = min(elegibles, key=lambda f: f["pct"])
	atipicos = []
	if len(elegibles) >= 5:
		valores = [f["pct"] for f in elegibles]
		media = sum(valores) / len(valores)
		desv = math.sqrt(sum((v - media) ** 2 for v in valores) / len(valores))
		if desv > 0:
			for f in elegibles:
				z = (f["pct"] - media) / desv
				if abs(z) >= 2:
					atipicos.append({**resumen(f), "z": round(z, 2)})
	return {"mejor": resumen(mejor), "peor": resumen(peor), "atipicos": atipicos}


def tendencia(serie):
	"""Serie cronológica [(etiqueta, valor)] con la diferencia contra la anterior.

	Para porcentajes, `diferencia` ya son puntos porcentuales.
	"""
	salida = []
	previo = None
	for etiqueta, valor in serie:
		absoluta, _ = variacion(valor, previo)
		salida.append({"periodo": etiqueta, "valor": valor, "diferencia": absoluta})
		previo = valor
	return salida
