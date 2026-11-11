"""Cálculos del análisis (fase 2): funciones puras, sin BD ni LLM."""

from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import analisis as calc


class TestPorcentajeYVariacion(FrappeTestCase):
	def test_porcentaje_redondea_a_un_decimal(self):
		self.assertEqual(calc.porcentaje(2, 3), 66.7)

	def test_porcentaje_sin_base_es_none_no_cero(self):
		self.assertIsNone(calc.porcentaje(5, 0))
		self.assertIsNone(calc.porcentaje(5, None))
		self.assertIsNone(calc.porcentaje(None, 10))

	def test_variacion_con_base(self):
		self.assertEqual(calc.variacion(120, 100), (20, 20.0))
		self.assertEqual(calc.variacion(80, 100), (-20, -20.0))

	def test_variacion_con_base_cero_no_da_porcentaje(self):
		self.assertEqual(calc.variacion(7, 0), (7, None))

	def test_variacion_sin_dato_es_none(self):
		self.assertEqual(calc.variacion(None, 10), (None, None))
		self.assertEqual(calc.variacion(10, None), (None, None))

	def test_puntos_pp_son_resta_directa(self):
		self.assertEqual(calc.puntos_pp(78.4, 81.5), -3.1)
		self.assertIsNone(calc.puntos_pp(None, 81.5))

	def test_brecha_contra_objetivo(self):
		self.assertEqual(calc.brecha_objetivo(78.4, 75), 3.4)
		self.assertIsNone(calc.brecha_objetivo(None, 75))


class TestConcentracion(FrappeTestCase):
	def test_participacion_de_los_mayores_sobre_el_total_dado(self):
		pares = [("A", 50), ("B", 30), ("C", 10), ("D", 5), ("E", 5)]
		res = calc.concentracion(pares, total=100, top_n=3)
		self.assertEqual(res["participacion_pct"], 90.0)
		self.assertEqual(res["principales"], ["A", "B", "C"])
		self.assertEqual(res["total"], 100)

	def test_el_total_no_sale_de_sumar_las_filas(self):
		# Las filas traen 60 aunque la base real tiene 200: el denominador manda.
		pares = [("A", 30), ("B", 30)]
		self.assertEqual(calc.concentracion(pares, total=200, top_n=3)["participacion_pct"], 30.0)

	def test_total_cero_o_nulo_no_produce_concentracion(self):
		self.assertIsNone(calc.concentracion([("A", 1)], total=0))
		self.assertIsNone(calc.concentracion([("A", 1)], total=None))

	def test_empates_se_resuelven_por_orden_de_entrada(self):
		pares = [("X", 10), ("Y", 10), ("Z", 10), ("W", 10)]
		res = calc.concentracion(pares, total=40, top_n=2)
		self.assertEqual(res["principales"], ["X", "Y"])
		self.assertEqual(res["participacion_pct"], 50.0)

	def test_un_solo_grupo(self):
		res = calc.concentracion([("Solo", 7)], total=7, top_n=3)
		self.assertEqual(res["participacion_pct"], 100.0)
		self.assertEqual(res["principales"], ["Solo"])

	def test_valores_nulos_cuentan_como_cero(self):
		res = calc.concentracion([("A", None), ("B", 4)], total=8, top_n=3)
		self.assertEqual(res["participacion_pct"], 50.0)


class TestExtremos(FrappeTestCase):
	def _filas(self, valores, base=100):
		return [{"grupo": f"G{i}", "pct": v, "base": base} for i, v in enumerate(valores)]

	def test_mejor_y_peor(self):
		res = calc.extremos(self._filas([50, 90, 70]))
		self.assertEqual(res["mejor"]["grupo"], "G1")
		self.assertEqual(res["peor"]["grupo"], "G0")

	def test_base_menor_al_minimo_no_cuenta(self):
		filas = [
			{"grupo": "chico", "pct": 0.0, "base": 3},
			{"grupo": "grande", "pct": 60.0, "base": 80},
		]
		res = calc.extremos(filas, min_base=50)
		self.assertEqual(res["peor"]["grupo"], "grande")
		self.assertEqual(res["mejor"]["grupo"], "grande")

	def test_sin_grupos_elegibles(self):
		self.assertEqual(calc.extremos([], 50), {"mejor": None, "peor": None, "atipicos": []})
		self.assertIsNone(calc.extremos([{"grupo": "x", "pct": None, "base": 90}])["mejor"])

	def test_atipico_exige_al_menos_cinco_grupos(self):
		res = calc.extremos(self._filas([50, 52, 48, 10]))
		self.assertEqual(res["atipicos"], [])

	def test_atipico_a_dos_desviaciones(self):
		valores = [70, 71, 69, 70, 70, 70, 70, 70, 70, 10]
		res = calc.extremos(self._filas(valores))
		self.assertEqual([a["grupo"] for a in res["atipicos"]], ["G9"])
		self.assertLessEqual(res["atipicos"][0]["z"], -2)

	def test_todos_iguales_no_tienen_atipicos(self):
		res = calc.extremos(self._filas([60] * 6))
		self.assertEqual(res["atipicos"], [])


class TestTendencia(FrappeTestCase):
	def test_diferencia_contra_el_periodo_anterior(self):
		serie = [("ago", 80.0), ("sep", 84.5), ("oct", 78.0)]
		res = calc.tendencia(serie)
		self.assertIsNone(res[0]["diferencia"])
		self.assertEqual(res[1]["diferencia"], 4.5)
		self.assertEqual(res[2]["diferencia"], -6.5)

	def test_periodo_anterior_sin_dato_no_inventa_diferencia(self):
		res = calc.tendencia([("ago", None), ("sep", 80.0)])
		self.assertIsNone(res[1]["diferencia"])

	def test_serie_vacia(self):
		self.assertEqual(calc.tendencia([]), [])
