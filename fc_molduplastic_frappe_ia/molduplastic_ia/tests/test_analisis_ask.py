"""Análisis dentro de ask() (fase 2).

Las consultas derivadas (comparativos, totales, concentración) sí van a la base
real. El LLM está simulado con mocks: estas pruebas no contestan con el modelo.
"""

import datetime
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import api
from fc_molduplastic_frappe_ia.molduplastic_ia.analisis import TiempoAgotado
from fc_molduplastic_frappe_ia.molduplastic_ia.llm_factory import LLMTiempoAgotado
from fc_molduplastic_frappe_ia.molduplastic_ia.validate_spec import SpecInvalida, validar_spec

SETTINGS = "Molduplastic IA Settings"
USUARIO_GNP = "pruebas.gnp@molduplastic.test"
INFO = {"proveedor": "DeepSeek", "modelo": "deepseek-chat", "prompt": "p", "respuesta": "r",
	"tokens_entrada": 10, "tokens_salida": 20, "latencia_ms": 100, "costo_estimado_usd": 0.0,
	"es_fallback": False, "motivo_fallback": ""}

SPEC_EFECTIVIDAD = {
	"doctype": "Pieza",
	"fields": [],
	"filters": {"fechaingreso": ["between", "2026-09-01 00:00:00", "2026-09-30 23:59:59"]},
	"aggregations": [],
	"group_by": ["taller_asignado"],
	"order_by": [{"field": "recolectadas", "dir": "desc"}],
	"limit": 5,
	"render_chart": None,
	"out_of_scope": False,
	"min_ingresadas": 50,
}


def _spec_efectividad():
	spec = dict(SPEC_EFECTIVIDAD)
	spec["aggregations"] = api._aggs_efectividad("2026-09-01 00:00:00", "2026-09-30 23:59:59")
	spec["filters"] = {}
	return spec


class ConfigAnalisis(FrappeTestCase):
	def setUp(self):
		super().setUp()
		s = frappe.get_doc(SETTINGS)
		self.previo = {
			"modo_respuesta": s.modo_respuesta,
			"presupuesto_analisis_seg": s.presupuesto_analisis_seg,
			"tendencia_periodos": s.tendencia_periodos,
		}
		self.addCleanup(self._restaurar)

	def _restaurar(self):
		s = frappe.get_doc(SETTINGS)
		for k, v in self.previo.items():
			s.set(k, v)
		s.set("modo_excepciones", [])
		s.save(ignore_permissions=True)
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def configurar(self, **campos):
		s = frappe.get_doc(SETTINGS)
		for k, v in campos.items():
			s.set(k, v)
		s.save(ignore_permissions=True)
		frappe.clear_document_cache(SETTINGS, SETTINGS)


class TestAnalisisDeEfectividad(ConfigAnalisis):
	def setUp(self):
		super().setUp()
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		self.configurar(modo_respuesta="Analítico", tendencia_periodos=0, comparar_ano_anterior=1)
		# Estas pruebas miden la lógica del análisis, no el reloj: el presupuesto
		# real (60 s) lo prueban las de TestPresupuestoYDegradacion.
		parche = patch.object(api, "_presupuesto_analisis_seg", return_value=600)
		parche.start()
		self.addCleanup(parche.stop)

	def test_comparativos_y_totales_salen_de_consultas_reales(self):
		with patch.object(api, "generate_spec", return_value=(_spec_efectividad(), INFO)), \
			patch.object(api, "explain", return_value=("Respuesta.", INFO)) as mock_exp:
			res = api.ask("efectividad de septiembre por taller")
		self.assertTrue(res["ok"])
		analisis = mock_exp.call_args.kwargs["analisis"]["analisis"]
		self.assertEqual(analisis["tipo"], "efectividad")
		# Mismo total que el SQL directo del alcance GNP (aseguradora = '6').
		directo = frappe.db.sql(
			"SELECT COUNT(*) FROM tabPieza WHERE aseguradora='6' AND recolectable=1 "
			"AND fechaingreso BETWEEN '2026-09-01 00:00:00' AND '2026-09-30 23:59:59'"
		)[0][0]
		self.assertEqual(analisis["total"]["ingresadas"], directo)
		self.assertIn("periodo_anterior", analisis["comparativos"])
		self.assertIn("mismo_periodo_ano_anterior", analisis["comparativos"])
		# Agrupada por taller: actual, grupos, período anterior y año anterior.
		self.assertEqual(analisis["consultas"], 4)
		self.assertEqual(analisis["objetivo"]["valor"], 75)

	def test_consultas_derivadas_pasan_por_validar_spec_y_el_mismo_alcance(self):
		with patch.object(api, "generate_spec", return_value=(_spec_efectividad(), INFO)), \
			patch.object(api, "explain", return_value=("Respuesta.", INFO)), \
			patch.object(api, "ejecutar", wraps=api.ejecutar) as espia, \
			patch.object(api, "validar_spec", wraps=api.validar_spec) as valida:
			api.ask("efectividad de septiembre por taller")
		self.assertTrue(valida.called)
		for llamada in espia.call_args_list:
			self.assertIsNone(llamada.kwargs.get("alcance"))
			spec = llamada.args[0]
			self.assertNotIn("aseguradora", spec.get("filters") or {})
			for agg in spec.get("aggregations") or []:
				self.assertNotIn("aseguradora", agg.get("filtros") or {})

	def test_admin_con_universo_no_consulta_otro_universo(self):
		frappe.set_user("Administrator")
		with patch.object(api, "generate_spec", return_value=(_spec_efectividad(), INFO)), \
			patch.object(api, "explain", return_value=("Respuesta.", INFO)), \
			patch.object(api, "ejecutar", wraps=api.ejecutar) as espia:
			api.ask("efectividad de septiembre por taller", universo="6")
		self.assertTrue(espia.call_args_list)
		for llamada in espia.call_args_list:
			self.assertEqual(llamada.kwargs.get("alcance"), "6")

	def test_breve_no_calcula_el_analisis(self):
		self.configurar(modo_respuesta="Breve")
		with patch.object(api, "generate_spec", return_value=(_spec_efectividad(), INFO)), \
			patch.object(api, "explain", return_value=("Respuesta.", INFO)) as mock_exp, \
			patch.object(api, "_analisis_completo") as mock_analisis:
			api.ask("efectividad de septiembre por taller")
		mock_analisis.assert_not_called()
		self.assertNotIn("analisis", mock_exp.call_args.kwargs)


class TestPresupuestoYDegradacion(ConfigAnalisis):
	def setUp(self):
		super().setUp()
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		self.configurar(modo_respuesta="Analítico", tendencia_periodos=0, comparar_ano_anterior=0)

	def test_tiempo_agotado_entrega_breve_con_nota(self):
		with patch.object(api, "generate_spec", return_value=(_spec_efectividad(), INFO)), \
			patch.object(api, "_calcular_analisis", side_effect=TiempoAgotado("x")), \
			patch.object(api, "explain", side_effect=[("Breve.", INFO)]) as mock_exp:
			res = api.ask("efectividad de septiembre por taller")
		self.assertIn("Breve.", res["respuesta"])
		self.assertIn(api.NOTA_TIEMPO_ANALISIS, res["respuesta"])
		self.assertNotIn("modo", mock_exp.call_args.kwargs)

	def test_presupuesto_agotado_durante_la_llamada_al_modelo(self):
		with patch.object(api, "generate_spec", return_value=(_spec_efectividad(), INFO)), \
			patch.object(api, "_calcular_analisis", return_value=None), \
			patch.object(api, "explain", side_effect=[LLMTiempoAgotado("x"), ("Breve.", INFO)]):
			res = api.ask("efectividad de septiembre por taller")
		self.assertIn(api.NOTA_TIEMPO_ANALISIS, res["respuesta"])

	def test_error_inesperado_del_analisis_degrada_sin_nota(self):
		with patch.object(api, "generate_spec", return_value=(_spec_efectividad(), INFO)), \
			patch.object(api, "_calcular_efectividad", side_effect=ValueError("boom")), \
			patch.object(api.frappe, "log_error") as log, \
			patch.object(api, "explain", return_value=("Breve.", INFO)) as mock_exp:
			res = api.ask("efectividad de septiembre por taller")
		self.assertTrue(log.called)
		self.assertIn("Breve.", res["respuesta"])
		self.assertNotIn(api.NOTA_TIEMPO_ANALISIS, res["respuesta"])
		self.assertIsNone(mock_exp.call_args.kwargs.get("analisis"))

	def test_el_analisis_no_puede_consultar_otra_aseguradora_aunque_la_pregunta_la_nombre(self):
		# Un usuario de GNP que menciona Afirme sigue consultando GNP.
		with patch.object(api, "generate_spec", return_value=(_spec_efectividad(), INFO)), \
			patch.object(api, "explain", return_value=("Respuesta.", INFO)), \
			patch.object(api, "ejecutar", wraps=api.ejecutar) as espia:
			api.ask("efectividad de septiembre por taller, compáralo con Afirme")
		for llamada in espia.call_args_list:
			self.assertIsNone(llamada.kwargs.get("alcance"))


class TestMedicionConFakes(ConfigAnalisis):
	"""Lógica de comparativos sin base: cifras controladas, sin consultas."""

	def _medidas(self, actual, anterior):
		def medir(filtros, rango, user, alcance, deadline, consultas):
			consultas.append(rango)
			return actual if rango[0].startswith("2026-09") else anterior
		return medir

	def test_comparativo_anual_desactivado_no_consulta_el_ano_anterior(self):
		actual = {"ingresadas": 100, "recolectadas": 80, "pct": 80.0}
		anterior = {"ingresadas": 100, "recolectadas": 60, "pct": 60.0}
		llamadas = []

		def medir(filtros, rango, user, alcance, deadline, consultas):
			llamadas.append(rango[0])
			return actual if rango[0].startswith("2026-09") else anterior

		with patch.object(api, "_medir_efectividad", side_effect=medir), \
			patch.object(api, "_inicio_de_datos", return_value=None):
			res = api._calcular_efectividad(
				_spec_efectividad(), {"columnas": [], "filas": []}, None, USUARIO_GNP, None, None,
				{"min_base": 50, "umbral_pp": 5.0, "umbral_concentracion": 60.0,
					"tendencia_periodos": 0, "comparar_ano_anterior": False},
				[],
			)
		self.assertFalse(any(r.startswith("2025") for r in llamadas))
		self.assertTrue(res["comparativos"]["mismo_periodo_ano_anterior"]["desactivado"])
		self.assertEqual(res["sin_base"], [])

	def test_sin_datos_para_el_ano_anterior_no_inventa_cifra(self):
		actual = {"ingresadas": 100, "recolectadas": 80, "pct": 80.0}
		anterior = {"ingresadas": 100, "recolectadas": 60, "pct": 60.0}
		with patch.object(api, "_medir_efectividad", side_effect=self._medidas(actual, anterior)), \
			patch.object(api, "_inicio_de_datos", return_value=datetime.date(2026, 1, 1)):
			res = api._calcular_efectividad(
				_spec_efectividad(), {"columnas": [], "filas": []}, None, USUARIO_GNP, None, None,
				{"min_base": 50, "umbral_pp": 5.0, "umbral_concentracion": 60.0,
					"tendencia_periodos": 0, "comparar_ano_anterior": True},
				[],
			)
		self.assertTrue(res["comparativos"]["mismo_periodo_ano_anterior"]["sin_datos"])
		self.assertIsNone(res["comparativos"]["mismo_periodo_ano_anterior"]["pct"])
		self.assertFalse(res["comparativos"]["periodo_anterior"]["sin_datos"])
		self.assertEqual(res["comparativos"]["periodo_anterior"]["variacion_pp"], 20.0)
		self.assertEqual(res["objetivo"]["brecha_pp"], 5.0)
		self.assertEqual([s["comparativo"] for s in res["sin_base"]], ["mismo_periodo_ano_anterior"])

	def test_sin_agrupacion_no_hay_concentracion_ni_extremos(self):
		actual = {"ingresadas": 100, "recolectadas": 80, "pct": 80.0}
		anterior = {"ingresadas": 100, "recolectadas": 60, "pct": 60.0}
		spec = _spec_efectividad()
		spec["group_by"] = []
		resultado = {"columnas": [], "filas": []}
		with patch.object(api, "_medir_efectividad", side_effect=self._medidas(actual, anterior)), \
			patch.object(api, "_inicio_de_datos", return_value=None):
			res = api._calcular_efectividad(
				spec, resultado, None, USUARIO_GNP, None, None,
				{"min_base": 50, "umbral_pp": 5.0, "umbral_concentracion": 60.0, "tendencia_periodos": 0},
				[],
			)
		self.assertIsNone(res["concentracion"])
		self.assertIsNone(res["extremos"]["mejor"])


class TestValidacionDeNuevosCampos(ConfigAnalisis):
	def test_umbrales_fuera_de_rango_no_se_guardan(self):
		casos = [
			{"min_piezas_porcentaje": 0},
			{"umbral_caida_pp": 0},
			{"umbral_concentracion_pct": 101},
			{"tendencia_periodos": 7},
		]
		for campos in casos:
			with self.subTest(campos=campos):
				s = frappe.get_doc(SETTINGS)
				for k, v in campos.items():
					s.set(k, v)
				with self.assertRaises(frappe.ValidationError):
					s.save(ignore_permissions=True)
				frappe.clear_document_cache(SETTINGS, SETTINGS)

	def test_desplazar_anios_conserva_horas(self):
		self.assertEqual(
			api._desplazar_anios(("2026-09-01 00:00:00", "2026-09-30 23:59:59"), -1),
			("2025-09-01 00:00:00", "2025-09-30 23:59:59"),
		)

	def test_desplazar_anios_en_bisiesto(self):
		desde, hasta = api._desplazar_anios(("2024-02-29 00:00:00", "2024-02-29 23:59:59"), -1)
		self.assertEqual(desde[:10], "2023-02-28")

	def test_filtros_sin_fecha_quita_fechas_y_recolectable(self):
		spec = {"filters": {"fechaingreso": ["between", "a", "b"], "recolectable": ["=", 1],
			"taller_asignado": ["=", "CELLINI"]}}
		self.assertEqual(api._filtros_sin_fecha(spec), {"taller_asignado": ["=", "CELLINI"]})


class TestValidarSpecDerivado(FrappeTestCase):
	def test_spec_de_efectividad_derivado_es_valido(self):
		spec = validar_spec({
			"doctype": "Pieza", "fields": [],
			"filters": {},
			"aggregations": api._aggs_efectividad("2026-08-01 00:00:00", "2026-08-31 23:59:59"),
			"group_by": [], "order_by": [], "limit": 1,
			"render_chart": None, "out_of_scope": False,
		})
		self.assertTrue(spec["aggregations"])

	def test_filtro_de_aseguradora_en_una_agregacion_no_amplia_el_alcance(self):
		# validar_spec lo acepta; el ejecutor impone el alcance del usuario y el
		# filtro solo puede restringirlo. Un usuario de GNP que pida Afirme (7) ve
		# solo sus propias piezas (6).
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		from fc_molduplastic_frappe_ia.molduplastic_ia.query_executor import ejecutar
		rango = ["between", "2026-09-01 00:00:00", "2026-09-30 23:59:59"]
		spec = validar_spec({
			"doctype": "Pieza", "fields": [], "filters": {},
			"aggregations": [{"func": "count", "field": "*", "alias": "x",
				"filtros": {"aseguradora": ["=", "7"], "fechaingreso": rango, "recolectable": ["=", 1]}}],
			"group_by": [], "order_by": [], "limit": 1,
			"render_chart": None, "out_of_scope": False,
		})
		res = ejecutar(spec, USUARIO_GNP)
		gnp = frappe.db.sql(
			"SELECT COUNT(*) FROM tabPieza WHERE aseguradora='6' AND recolectable=1 "
			"AND fechaingreso BETWEEN '2026-09-01 00:00:00' AND '2026-09-30 23:59:59'"
		)[0][0]
		self.assertEqual(res["filas"][0][0] or 0, gnp)
