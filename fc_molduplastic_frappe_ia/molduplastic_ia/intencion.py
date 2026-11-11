"""Clasificador de intención (fase 0 del análisis).

Solo nombra la intención de una pregunta. No cambia el flujo de `ask()`: el
orden de los `_completar_*` sigue igual. Sirve para medir la cobertura de las
intenciones y, en fases siguientes, para elegir el playbook, el modo de
respuesta y las preguntas sugeridas.

Las reglas (nombre, predicado) viven en `api._reglas_de_intencion()` porque
los predicados están en `api.py`; aquí no se importa nada para evitar ciclos.
"""

INTENCION_GENERAL = "general"


def clasificar(pregunta, reglas):
	"""Primera intención cuyo predicado se cumple; `general` si ninguno.

	Un predicado que lance excepción cuenta como no cumplido: clasificar no debe
	tumbar el chat.
	"""
	for nombre, predicado in reglas:
		try:
			if predicado(pregunta):
				return nombre
		except Exception:
			continue
	return INTENCION_GENERAL


def distribucion(preguntas, reglas):
	"""Cuántas preguntas caen en cada intención, de mayor a menor."""
	conteo = {}
	for pregunta in preguntas:
		nombre = clasificar(pregunta, reglas)
		conteo[nombre] = conteo.get(nombre, 0) + 1
	return sorted(conteo.items(), key=lambda par: par[1], reverse=True)
