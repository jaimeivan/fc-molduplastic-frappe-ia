"""Detectores de hallazgos (fase 2): puros, sin BD ni LLM."""

from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import hallazgos as h

CFG = {"min_base": 50, "umbral_pp": 5.0, "umbral_concentracion": 60.0}


def _efectividad(**over):
	base = {
		"actual_pct": 78.4,
		"actual_total": 400,
		"objetivo": 75,
		"anterior_pct": 80.0,
		"anterior_total": 300,
		"atipicos": [],
	}
	base.update(over)
	return base


def _codigos(lista):
	return [x["codigo"] for x in lista]


class TestEfectividadBajoObjetivo(FrappeTestCase):
	def test_por_encima_del_objetivo_no_alerta(self):
		hs = h.efectividad(_efectividad(actual_pct=80.0, anterior_pct=None), CFG)
		self.assertNotIn("efectividad_bajo_objetivo", _codigos(hs))

	def test_bajo_objetivo_pero_dentro_del_umbral_es_baja(self):
		# 3.4 puntos por debajo de 75 con umbral 5: se registra, sin alerta.
		hs = h.efectividad(_efectividad(actual_pct=71.6, anterior_pct=None), CFG)
		self.assertEqual(hs[0]["codigo"], "efectividad_bajo_objetivo")
		self.assertEqual(hs[0]["severidad"], "baja")

	def test_exactamente_en_el_umbral_es_media(self):
		hs = h.efectividad(_efectividad(actual_pct=70.0, anterior_pct=None), CFG)
		self.assertEqual(hs[0]["severidad"], "media")

	def test_el_doble_del_umbral_es_alta(self):
		hs = h.efectividad(_efectividad(actual_pct=65.0, anterior_pct=None), CFG)
		self.assertEqual(hs[0]["severidad"], "alta")

	def test_base_menor_al_minimo_no_produce_hallazgo(self):
		hs = h.efectividad(_efectividad(actual_pct=10.0, actual_total=49, anterior_pct=None), CFG)
		self.assertEqual(hs, [])

	def test_base_exactamente_en_el_minimo_cuenta(self):
		hs = h.efectividad(_efectividad(actual_pct=10.0, actual_total=50, anterior_pct=None), CFG)
		self.assertEqual(_codigos(hs), ["efectividad_bajo_objetivo"])

	def test_sin_porcentaje_no_hay_hallazgo(self):
		self.assertEqual(h.efectividad(_efectividad(actual_pct=None), CFG), [])


class TestCaidaContraPeriodoAnterior(FrappeTestCase):
	def test_caida_exacta_en_el_umbral_alerta_media(self):
		hs = h.efectividad(_efectividad(actual_pct=75.0, anterior_pct=80.0), CFG)
		caida = next(x for x in hs if x["codigo"] == "caida_vs_periodo_anterior")
		self.assertEqual(caida["severidad"], "media")
		self.assertEqual(caida["cifras"]["variacion_pp"], -5.0)

	def test_caida_al_doble_es_alta(self):
		hs = h.efectividad(_efectividad(actual_pct=70.0, anterior_pct=80.0), CFG)
		caida = next(x for x in hs if x["codigo"] == "caida_vs_periodo_anterior")
		self.assertEqual(caida["severidad"], "alta")

	def test_caida_de_4_9_puntos_no_alerta(self):
		hs = h.efectividad(_efectividad(actual_pct=75.1, anterior_pct=80.0), CFG)
		self.assertNotIn("caida_vs_periodo_anterior", _codigos(hs))

	def test_subida_no_es_hallazgo_de_caida(self):
		hs = h.efectividad(_efectividad(actual_pct=90.0, anterior_pct=60.0), CFG)
		self.assertNotIn("caida_vs_periodo_anterior", _codigos(hs))

	def test_periodo_anterior_con_base_chica_no_compara(self):
		hs = h.efectividad(_efectividad(actual_pct=60.0, anterior_pct=90.0, anterior_total=10), CFG)
		self.assertNotIn("caida_vs_periodo_anterior", _codigos(hs))

	def test_sin_periodo_anterior_no_hay_caida(self):
		hs = h.efectividad(_efectividad(actual_pct=60.0, anterior_pct=None), CFG)
		self.assertNotIn("caida_vs_periodo_anterior", _codigos(hs))


class TestAtipicos(FrappeTestCase):
	def test_atipico_se_registra_como_baja_severidad(self):
		atipico = {"grupo": "CRASH", "pct": 8.4, "base": 120, "z": -2.3}
		hs = h.efectividad(_efectividad(actual_pct=None, atipicos=[atipico]), CFG)
		self.assertEqual(hs[0]["codigo"], "grupo_atipico_efectividad")
		self.assertEqual(hs[0]["severidad"], "baja")

	def test_atipico_con_base_chica_no_se_registra(self):
		atipico = {"grupo": "X", "pct": 1.0, "base": 10, "z": -3.0}
		hs = h.efectividad(_efectividad(actual_pct=None, atipicos=[atipico]), CFG)
		self.assertEqual(hs, [])


class TestConcentracion(FrappeTestCase):
	def _datos(self, participacion, total=200):
		return {"total": total, "participacion_pct": participacion, "principales": ["A", "B", "C"]}

	def test_en_el_umbral_alerta(self):
		hs = h.concentracion(self._datos(60.0), CFG)
		self.assertEqual(hs[0]["codigo"], "concentracion_no_recolectadas")
		self.assertEqual(hs[0]["severidad"], "media")

	def test_debajo_del_umbral_no_alerta(self):
		self.assertEqual(h.concentracion(self._datos(59.9), CFG), [])

	def test_base_chica_no_alerta(self):
		self.assertEqual(h.concentracion(self._datos(90.0, total=49), CFG), [])


class TestDesdeAnalisisYConstruir(FrappeTestCase):
	def _analisis(self, **over):
		base = {
			"tipo": "efectividad",
			"total": {"ingresadas": 400, "recolectadas": 314, "no_recolectadas": 86, "pct": 78.5},
			"comparativos": {
				"periodo_anterior": {"sin_datos": False, "pct": 80.0, "ingresadas": 300},
				"mismo_periodo_ano_anterior": {"sin_datos": True},
			},
			"objetivo": {"valor": 75, "actual": 78.5, "brecha_pp": 3.5},
			"concentracion": {"participacion_pct": 65.0, "principales": ["A", "B", "C"], "total": 86},
			"extremos": {"mejor": None, "peor": None, "atipicos": []},
			"tendencia": [],
			"sin_base": [],
		}
		base.update(over)
		return base

	def test_conteo_no_genera_hallazgo_de_no_recolectadas(self):
		analisis = {"tipo": "conteo", "concentracion": {"participacion_pct": 90.0, "total": 500,
			"principales": ["X"]}}
		self.assertEqual(h.desde_analisis(analisis, CFG), [])

	def test_sin_analisis_no_hay_hallazgos(self):
		self.assertEqual(h.desde_analisis(None, CFG), [])

	def test_los_hallazgos_se_ordenan_por_severidad(self):
		analisis = self._analisis(
			total={"ingresadas": 400, "recolectadas": 200, "no_recolectadas": 200, "pct": 50.0},
			comparativos={"periodo_anterior": {"sin_datos": False, "pct": 80.0, "ingresadas": 300},
				"mismo_periodo_ano_anterior": {"sin_datos": True}},
			objetivo={"valor": 75, "actual": 50.0, "brecha_pp": -25.0},
		)
		res = h.construir(analisis, CFG, [])
		severidades = [x["severidad"] for x in res["hallazgos"]]
		self.assertEqual(severidades, sorted(severidades, key=lambda s: h.ORDEN_SEVERIDAD[s]))
		self.assertEqual(res["hallazgos"][0]["severidad"], "alta")

	def test_sin_glosario_validado_no_hay_sugerencias(self):
		res = h.construir(self._analisis(), CFG, [])
		self.assertTrue(res["hallazgos"])
		self.assertEqual(res["sugerencias"], [])

	def test_sugerencia_solo_con_glosario_validado_y_hallazgo_media_o_alta(self):
		glosario = [{"termino": "Efectividad", "accion": "Revisar los talleres con más pendientes."}]
		res = h.construir(self._analisis(), CFG, glosario)
		# Solo la concentración (media) genera sugerencia, y su término
		# ("No recolectadas") no tiene entrada en este glosario.
		self.assertEqual(res["sugerencias"], [])

	def test_sugerencia_del_termino_correcto(self):
		analisis = self._analisis(
			total={"ingresadas": 400, "recolectadas": 200, "no_recolectadas": 200, "pct": 50.0},
			objetivo={"valor": 75, "actual": 50.0, "brecha_pp": -25.0},
		)
		glosario = [
			{"termino": "Efectividad", "accion": "Priorizar los talleres con más pendientes."},
			{"termino": "No recolectadas", "accion": "Revisar CDR con concentración."},
		]
		res = h.construir(analisis, CFG, glosario)
		por_hallazgo = {s["hallazgo"]: s["accion"] for s in res["sugerencias"]}
		self.assertEqual(por_hallazgo["efectividad_bajo_objetivo"], "Priorizar los talleres con más pendientes.")
		self.assertEqual(por_hallazgo["concentracion_no_recolectadas"], "Revisar CDR con concentración.")

	def test_entrada_sin_accion_no_genera_sugerencia(self):
		glosario = [{"termino": "No recolectadas", "accion": ""}]
		res = h.construir(self._analisis(), CFG, glosario)
		self.assertEqual(res["sugerencias"], [])

	def test_texto_base_lleva_las_cifras_calculadas(self):
		res = h.construir(self._analisis(), CFG, [])
		texto = " ".join(x["texto_base"] for x in res["hallazgos"])
		self.assertIn("65.0%", texto)
