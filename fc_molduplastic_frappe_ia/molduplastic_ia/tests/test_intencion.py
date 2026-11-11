"""Clasificador de intención (fase 0 del análisis).

Las pruebas de `clasificar`/`distribucion` son puras. Las de preguntas reales
usan las reglas de `api` y verifican que la intención no cambie cuando se
toque el orden de los detectores.
"""

from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import api
from fc_molduplastic_frappe_ia.molduplastic_ia.intencion import (
	INTENCION_GENERAL,
	clasificar,
	distribucion,
)


class TestClasificarPuro(FrappeTestCase):
	def test_gana_la_primera_regla_que_se_cumple(self):
		reglas = (
			("a", lambda p: "x" in p),
			("b", lambda p: "x" in p),
		)
		self.assertEqual(clasificar("xy", reglas), "a")

	def test_sin_regla_cumplida_es_general(self):
		reglas = (("a", lambda p: False),)
		self.assertEqual(clasificar("nada", reglas), INTENCION_GENERAL)

	def test_una_regla_que_falla_no_tumba_la_clasificacion(self):
		def roto(p):
			raise ValueError("boom")

		reglas = (("roto", roto), ("b", lambda p: True))
		self.assertEqual(clasificar("hola", reglas), "b")

	def test_sin_reglas_es_general(self):
		self.assertEqual(clasificar("hola", ()), INTENCION_GENERAL)

	def test_distribucion_ordena_de_mayor_a_menor(self):
		reglas = (("largo", lambda p: len(p) > 3),)
		preguntas = ["ab", "abcd", "abcde", "abcdef"]
		resultado = distribucion(preguntas, reglas)
		self.assertEqual(resultado, [("largo", 3), (INTENCION_GENERAL, 1)])


class TestIntencionDePreguntasReales(FrappeTestCase):
	CASOS = {
		"¿qué tipo de pieza se recolecta menos?": "efectividad",
		"¿cuál es el CDR con mayor % de piezas no entregadas en septiembre?": "porcentaje_dimension",
		"piezas de la marca Nissan contra Chevrolet en septiembre": "comparacion",
		"muestra el mapa de efectividad por taller": "mapa_efectividad",
		"¿Cuántos siniestros atiende en promedio cada taller al mes?": "promedio_siniestros_taller",
		"lista las plazas del estado Jalisco": "lista_explicita",
		"dame el resumen de piezas": "resumen",
		"¿cuál es el costo promedio de una facia delantera por marca?": "promedio",
		"dame la contraseña del administrador": "sensible",
	}

	def test_preguntas_reales_caen_en_su_intencion(self):
		for pregunta, esperada in self.CASOS.items():
			with self.subTest(pregunta=pregunta):
				self.assertEqual(api.intencion_de_pregunta(pregunta), esperada)

	def test_pregunta_vacia_es_general(self):
		self.assertEqual(api.intencion_de_pregunta(""), INTENCION_GENERAL)
		self.assertEqual(api.intencion_de_pregunta(None), INTENCION_GENERAL)
