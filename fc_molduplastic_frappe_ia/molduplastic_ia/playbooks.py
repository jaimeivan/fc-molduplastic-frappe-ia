"""Playbooks de análisis (fase 3): por tipo de análisis, sus preguntas de seguimiento.

Las preguntas sugeridas salen de aquí, nunca del modelo: cada una se envía como una
pregunta normal a `ask()` (pasa por los mismos controles). Agregar un tipo de
análisis = una entrada en PLAYBOOKS y su caso en tests/test_playbooks.py, que
verifica que cada pregunta caiga en una intención soportada.
"""

MAX_SUGERIDAS = 3

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
