"""Playbooks de análisis (fase 3): por tipo de análisis, sus preguntas de seguimiento.

Las preguntas sugeridas salen de aquí, nunca del modelo: cada una se envía como una
pregunta normal a `ask()` (pasa por los mismos controles). Agregar un tipo de
análisis = una entrada en PLAYBOOKS y su caso en tests/test_playbooks.py, que
verifica que cada pregunta caiga en una intención soportada.
"""

MAX_SUGERIDAS = 3

# Modos de las preguntas sugeridas (dueño, 2026-11-23). Se eligen en
# Molduplastic IA Settings; el valor por defecto es el modelo libre.
MODOS_SUGERENCIAS = ("Playbook", "Hallazgos + catálogo", "Modelo libre")
MODO_SUGERENCIAS_DEFECTO = "Modelo libre"

PLAYBOOKS = {
	"efectividad": {
		"titulo": "Efectividad",
		"preguntas": (
			"¿Qué talleres tienen la efectividad más baja en este período?",
			"¿Cuál es la efectividad por región en este período?",
			"¿Qué estados tienen mucho volumen pero baja efectividad?",
		),
	},
	# Los conteos por dimensión todavía no tienen preguntas de seguimiento: sus
	# preguntas no caen en una intención propia (ver intencion.py).
	"conteo": {"titulo": "Conteo por dimensión", "preguntas": ()},
}


def preguntas_para(tipo):
	"""Preguntas de seguimiento del tipo de análisis (máximo MAX_SUGERIDAS)."""
	return list(PLAYBOOKS.get(tipo, {}).get("preguntas", ()))[:MAX_SUGERIDAS]


# Preguntas deterministas a partir de los hallazgos del análisis (modo
# "Hallazgos + catálogo"). Son preguntas que el motor ya sabe contestar: la
# dimensión y la medida están en la tabla de abajo, no las decide el modelo.
REGLAS_DE_HALLAZGO = {
	"caida_vs_periodo_anterior": "¿Qué talleres tienen la efectividad más baja en este período?",
	"efectividad_bajo_objetivo": "¿Cuál es la efectividad por región en este período?",
	"concentracion_no_recolectadas": "¿Qué talleres concentran las piezas no recolectadas en este período?",
	"grupo_atipico_efectividad": "¿Cuál es la efectividad por tipo de pieza en este período?",
}


def preguntas_por_hallazgos(hallazgos):
	"""Preguntas de seguimiento derivadas de los hallazgos detectados (C)."""
	codigos = {str(h.get("codigo") or "") for h in (hallazgos or [])}
	salida = []
	for codigo, pregunta in REGLAS_DE_HALLAZGO.items():
		if codigo in codigos and pregunta not in salida:
			salida.append(pregunta)
	return salida[:MAX_SUGERIDAS]


def completar_con_playbook(preguntas, tipo):
	"""Rellena hasta MAX_SUGERIDAS con el playbook, sin repetir."""
	salida = list(dict.fromkeys(p for p in (preguntas or []) if p))
	for pregunta in preguntas_para(tipo):
		if len(salida) >= MAX_SUGERIDAS:
			break
		if pregunta not in salida:
			salida.append(pregunta)
	return salida[:MAX_SUGERIDAS]
