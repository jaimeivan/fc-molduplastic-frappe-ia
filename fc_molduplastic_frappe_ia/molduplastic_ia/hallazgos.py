"""Detectores de hallazgos (fase 2). Funciones puras: sin BD, sin LLM.

Cada detector recibe cifras ya calculadas (`analisis.py`) y los umbrales de
configuración, y devuelve una lista de hallazgos. El LLM no decide qué es
relevante: solo prioriza y redacta lo que aquí se detectó.

Reglas:
- Todo porcentaje (efectividad, variación en puntos, participación) exige una
  base mínima de piezas (`min_base`, configurable, 50 por defecto). Los
  conteos y listados no pasan por esta regla.
- Sin base suficiente o sin comparación, no hay hallazgo (nunca un 0 inventado).
- Severidad: `media` al cruzar el umbral; `alta` al doble del umbral.
"""

from fc_molduplastic_frappe_ia.molduplastic_ia import analisis as calc

SEVERIDADES = ("baja", "media", "alta")


def _hallazgo(codigo, severidad, texto_base, cifras):
	return {
		"codigo": codigo,
		"severidad": severidad,
		"texto_base": texto_base,
		"cifras": cifras,
	}


def _severidad_por_umbral(diferencia, umbral):
	"""Diferencia negativa: "alta" al doble del umbral, "media" al cruzarlo, "baja" si no.

	Una diferencia que no cruza el umbral se registra como baja: no es alerta.
	"""
	if diferencia <= -2 * umbral:
		return "alta"
	if diferencia <= -umbral:
		return "media"
	return "baja"


def efectividad(datos, cfg):
	"""Brecha contra el objetivo, caída contra el período anterior y atípicos.

	`datos`: actual_pct, actual_total, objetivo, anterior_pct, anterior_total,
	grupos (lista de dicts con grupo, pct, base) y el período anterior como texto.
	`cfg`: min_base, umbral_pp.
	"""
	salida = []
	minimo = cfg["min_base"]
	umbral = cfg["umbral_pp"]
	pct = datos.get("actual_pct")
	total = datos.get("actual_total") or 0

	if pct is not None and total >= minimo:
		brecha = calc.brecha_objetivo(pct, datos.get("objetivo"))
		if brecha is not None and brecha < 0:
			severidad = _severidad_por_umbral(brecha, umbral)
			salida.append(
				_hallazgo(
					"efectividad_bajo_objetivo",
					severidad,
					f"La efectividad es {pct}%, {abs(brecha)} puntos por debajo del objetivo de {datos.get('objetivo')}% ({total} piezas ingresadas).",
					{"pct": pct, "objetivo": datos.get("objetivo"), "brecha_pp": brecha, "base": total},
				)
			)

	anterior_pct = datos.get("anterior_pct")
	anterior_total = datos.get("anterior_total") or 0
	if (
		pct is not None
		and anterior_pct is not None
		and total >= minimo
		and anterior_total >= minimo
	):
		pp = calc.puntos_pp(pct, anterior_pct)
		if pp is not None and pp <= -umbral:
			severidad = _severidad_por_umbral(pp, umbral)
			salida.append(
				_hallazgo(
					"caida_vs_periodo_anterior",
					severidad,
					f"La efectividad bajó {abs(pp)} puntos contra el período anterior ({anterior_pct}% → {pct}%).",
					{"pct": pct, "pct_anterior": anterior_pct, "variacion_pp": pp,
						"base": total, "base_anterior": anterior_total},
				)
			)

	for atipico in datos.get("atipicos") or []:
		if (atipico.get("base") or 0) >= minimo:
			salida.append(
				_hallazgo(
					"grupo_atipico_efectividad",
					"baja",
					f"{atipico['grupo']} tiene {atipico['pct']}% de efectividad, fuera del patrón de los demás grupos ({atipico['base']} piezas).",
					{"grupo": atipico["grupo"], "pct": atipico["pct"], "base": atipico["base"], "z": atipico.get("z")},
				)
			)
	return salida


def concentracion(datos, cfg):
	"""Participación de los grupos principales en las piezas no recolectadas.

	`datos`: total (de una consulta sin agrupar), top (participación en %) y
	los nombres de los grupos principales. `cfg`: min_base, umbral_concentracion.
	"""
	total = datos.get("total") or 0
	participacion = datos.get("participacion_pct")
	if participacion is None or total < cfg["min_base"]:
		return []
	if participacion < cfg["umbral_concentracion"]:
		return []
	nombres = ", ".join(datos.get("principales") or [])
	return [
		_hallazgo(
			"concentracion_no_recolectadas",
			"media",
			f"Tres grupos concentran el {participacion}% de las {total} piezas no recolectadas: {nombres}.",
			{"participacion_pct": participacion, "base": total, "principales": datos.get("principales") or []},
		)
	]


TERMINO_DE_HALLAZGO = {
	"efectividad_bajo_objetivo": "Efectividad",
	"caida_vs_periodo_anterior": "Efectividad",
	"concentracion_no_recolectadas": "No recolectadas",
}
ORDEN_SEVERIDAD = {"alta": 0, "media": 1, "baja": 2}


def desde_analisis(analisis, cfg):
	"""Convierte el análisis calculado en entradas de los detectores."""
	if not analisis:
		return []
	hallazgos = []
	if analisis.get("tipo") != "efectividad":
		# Un conteo solo lleva su concentración como dato; el hallazgo de
		# concentración describe piezas no recolectadas, así que no aplica aquí.
		return hallazgos
	total = analisis["total"]
	anterior = analisis["comparativos"]["periodo_anterior"]
	hallazgos += efectividad(
		{
			"actual_pct": total.get("pct"),
			"actual_total": total.get("ingresadas"),
			"objetivo": analisis["objetivo"]["valor"],
			"anterior_pct": anterior.get("pct"),
			"anterior_total": anterior.get("ingresadas"),
			"atipicos": analisis["extremos"]["atipicos"],
		},
		cfg,
	)
	conc = analisis.get("concentracion")
	if conc:
		hallazgos += concentracion(
			{
				"total": conc["total"],
				"participacion_pct": conc["participacion_pct"],
				"principales": conc["principales"],
			},
			cfg,
		)
	return hallazgos


def construir(analisis, cfg, glosario_validado):
	"""Hallazgos ordenados por severidad y sugerencias sustentadas en el glosario.

	Una sugerencia solo sale si hay un hallazgo media o alta Y una entrada del
	glosario Validado con acción para ese término. Sin glosario validado no hay
	sugerencias: no se inventan acciones.
	"""
	hallazgos = sorted(desde_analisis(analisis, cfg), key=lambda h: ORDEN_SEVERIDAD[h["severidad"]])
	sugerencias = []
	for h in hallazgos:
		if h["severidad"] not in ("alta", "media"):
			continue
		termino = TERMINO_DE_HALLAZGO.get(h["codigo"])
		for fila in glosario_validado or []:
			if fila.get("termino") == termino and fila.get("accion"):
				sugerencias.append({"hallazgo": h["codigo"], "termino": termino, "accion": fila["accion"]})
	return {"hallazgos": hallazgos, "sugerencias": sugerencias}
