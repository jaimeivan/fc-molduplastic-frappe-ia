"""Fase 3: presupuesto de tiempo con aviso, verificación de cifras y playbooks.

El LLM está simulado (mocks): estas pruebas no contestan con el modelo real.
Las consultas derivadas sí van a la base cuando no están parcheadas.
"""

import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import api, playbooks
from fc_molduplastic_frappe_ia.molduplastic_ia.analisis import TiempoAgotado, TiempoPrevisto
from fc_molduplastic_frappe_ia.molduplastic_ia.llm_factory import LLMTiempoAgotado

SETTINGS = "Molduplastic IA Settings"
USUARIO_GNP = "pruebas.gnp@molduplastic.test"
INFO = {"proveedor": "DeepSeek", "modelo": "deepseek-chat", "prompt": "p", "respuesta": "r",
	"tokens_entrada": 10, "tokens_salida": 20, "latencia_ms": 100, "costo_estimado_usd": 0.0,
	"es_fallback": False, "motivo_fallback": ""}
SPEC = {"doctype": "Pieza", "fields": ["folio", "marca"], "filters": {}, "aggregations": [],
	"group_by": [], "order_by": [], "limit": 5, "render_chart": None, "out_of_scope": False}
DATOS = {"columnas": ["marca"], "filas": [["Nissan"]], "total_filas": 1, "mostradas": 1}
PACK = {"analisis": {"tipo": "efectividad"}, "hallazgos": [], "sugerencias": [], "consultas": 1,
	"latencia_ms": 5}


def _info(texto_datos=None):
	return {**INFO, "datos": texto_datos or {"total": 5, "pct": 80.0}}


class ConfigFase3(FrappeTestCase):
	def setUp(self):
		s = frappe.get_doc(SETTINGS)
		self.previo = {k: s.get(k) for k in (
			"modo_respuesta", "verificacion_cifras", "preguntas_sugeridas_activas",
			"presupuesto_analisis_seg")}
		self.addCleanup(self._restaurar)
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		self.configurar(modo_respuesta="Analítico")

	def _restaurar(self):
		s = frappe.get_doc(SETTINGS)
		for k, v in self.previo.items():
			s.set(k, v)
		s.save(ignore_permissions=True)
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def configurar(self, **campos):
		s = frappe.get_doc(SETTINGS)
		for k, v in campos.items():
			s.set(k, v)
		s.save(ignore_permissions=True)
		frappe.clear_document_cache(SETTINGS, SETTINGS)


class TestPresupuesto(FrappeTestCase):
	def test_maximo_y_defecto_son_120(self):
		from fc_molduplastic_frappe_ia.molduplastic_ia.doctype.molduplastic_ia_settings import (
			molduplastic_ia_settings as m,
		)
		self.assertEqual(m.PRESUPUESTO_MAXIMO_SEG, 120)

	def test_sobre_120_no_se_guarda(self):
		s = frappe.get_doc(SETTINGS)
		s.presupuesto_analisis_seg = 121
		with self.assertRaises(frappe.ValidationError):
			s.save(ignore_permissions=True)
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def test_el_presupuesto_se_acota_a_120(self):
		with patch.object(api, "frappe") as fr:
			fr.get_cached_doc.return_value = frappe._dict(presupuesto_analisis_seg=500)
			self.assertEqual(api._presupuesto_analisis_seg(), 120)

	def test_limite_pasado_es_tiempo_agotado(self):
		with self.assertRaises(TiempoAgotado):
			api._Presupuesto(limite=time.time() - 1).verificar()

	def test_consulta_que_no_cabe_se_prevé_antes_de_empezar(self):
		presupuesto = api._Presupuesto(limite=time.time() + 10, estimado=60)
		with self.assertRaises(TiempoPrevisto) as ctx:
			presupuesto.verificar()
		self.assertEqual(ctx.exception.segundos, 60)

	def test_la_mayor_duracion_observada_manda_la_prevision(self):
		presupuesto = api._Presupuesto(limite=time.time() + 30, estimado=1)
		presupuesto.medir(lambda: time.sleep(0))
		presupuesto.observados.append(40.0)  # una consulta anterior tardó 40 s
		with self.assertRaises(TiempoPrevisto):
			presupuesto.verificar()

	def test_sin_limite_no_hay_control(self):
		api._verificar_tiempo(None)  # no lanza

	def test_medir_registra_la_duracion(self):
		presupuesto = api._Presupuesto(limite=time.time() + 100)
		self.assertEqual(presupuesto.medir(lambda: 7), 7)
		self.assertEqual(len(presupuesto.observados), 1)

	def test_tiempo_previsto_es_tiempo_agotado(self):
		self.assertTrue(issubclass(TiempoPrevisto, TiempoAgotado))


class TestAvisoDeTiempo(ConfigFase3):
	@patch.object(api, "explain", return_value=("Breve.", INFO))
	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_consulta_prevista_muestra_el_aviso_de_previsto(self, _spec, mock_exp):
		with patch.object(api, "_analisis_completo", side_effect=TiempoPrevisto(90)):
			res = api.ask("¿cuántas piezas hay?")
		self.assertIn("unos 90 s", res["respuesta"])
		self.assertIn("Reduce el período", res["respuesta"])
		self.assertNotIn(api.NOTA_TIEMPO_ANALISIS, res["respuesta"])

	@patch.object(api, "explain", return_value=("Breve.", INFO))
	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_tiempo_agotado_en_curso_muestra_el_aviso_de_curso(self, _spec, mock_exp):
		with patch.object(api, "_analisis_completo", side_effect=TiempoAgotado("x")):
			res = api.ask("¿cuántas piezas hay?")
		self.assertIn(api.NOTA_TIEMPO_ANALISIS, res["respuesta"])

	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_respaldo_sin_tiempo_da_mensaje_claro_no_error(self, _spec):
		with patch.object(api, "_analisis_completo", side_effect=TiempoAgotado("x")), \
			patch.object(api, "explain", side_effect=[LLMTiempoAgotado("sin tiempo")]):
			res = api.ask("¿cuántas piezas hay?")
		self.assertTrue(res["ok"])
		self.assertIn(api.NOTA_SIN_TIEMPO, res["respuesta"])

	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_la_respaldo_recibe_el_limite_de_los_120_segundos(self, _spec):
		with patch.object(api, "_analisis_completo", side_effect=TiempoAgotado("x")), \
			patch.object(api, "explain", return_value=("Breve.", INFO)) as mock_exp:
			api.ask("¿cuántas piezas hay?")
		self.assertIsNotNone(mock_exp.call_args.kwargs.get("deadline"))


class TestVerificacionEnAsk(ConfigFase3):
	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_cifras_del_datos_quedan_aprobadas(self, _spec):
		with patch.object(api, "_analisis_completo", return_value=PACK), \
			patch.object(api, "explain", return_value=("El total es 80.0%.", _info())):
			res = api.ask("¿cuántas piezas hay?")
		self.assertEqual(res["respuesta"].split("\n\n")[-1], "El total es 80.0%.")

	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_cifra_sin_respaldo_dispara_un_reintento_con_correccion(self, _spec):
		respuestas = [("La cifra es 999.", _info()), ("El total es 80.0%.", _info())]
		with patch.object(api, "_analisis_completo", return_value=PACK), \
			patch.object(api, "explain", side_effect=respuestas) as mock_exp:
			res = api.ask("¿cuántas piezas hay?")
		self.assertEqual(mock_exp.call_count, 2)
		self.assertEqual(mock_exp.call_args.kwargs["correccion"], [999.0])
		self.assertIn("El total es 80.0%.", res["respuesta"])
		self.assertNotIn("999", res["respuesta"])

	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_si_el_reintento_persiste_se_recorta_la_frase(self, _spec):
		respuestas = [("El total es 80.0%. La cifra 999 no existe.", _info()),
			("Otra vez 999.", _info())]
		with patch.object(api, "_analisis_completo", return_value=PACK), \
			patch.object(api, "explain", side_effect=respuestas):
			res = api.ask("¿cuántas piezas hay?")
		self.assertIn("El total es 80.0%.", res["respuesta"])
		self.assertNotIn("999", res["respuesta"])
		quitadas = frappe.get_all("IA API Log", filters={"verificacion": "Recortada"},
			fields=["error"], limit=1)
		self.assertIn("La cifra 999 no existe.", quitadas[0].error)

	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_si_todo_carece_de_respaldo_se_degrada_a_titulo_y_periodo(self, _spec):
		with patch.object(api, "_analisis_completo", return_value=PACK), \
			patch.object(api, "explain", side_effect=[("Hay 999.", _info()), ("Hay 888.", _info())]):
			res = api.ask("¿cuántas piezas hay?")
		self.assertNotIn("999", res["respuesta"])
		self.assertNotIn("888", res["respuesta"])

	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_verificacion_desactivada_no_toca_la_respuesta(self, _spec):
		self.configurar(verificacion_cifras=0)
		with patch.object(api, "_analisis_completo", return_value=PACK), \
			patch.object(api, "explain", return_value=("Hay 999.", _info())) as mock_exp:
			res = api.ask("¿cuántas piezas hay?")
		self.assertEqual(mock_exp.call_count, 1)
		self.assertIn("Hay 999.", res["respuesta"])

	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_el_veredicto_queda_en_el_registro(self, _spec):
		with patch.object(api, "_analisis_completo", return_value=PACK), \
			patch.object(api, "explain", return_value=("El total es 80.0%.", _info())):
			api.ask("¿cuántas piezas hay?")
		veredictos = frappe.get_all("IA API Log", filters={"verificacion": "Aprobada", "operacion": "explain"},
			limit=1)
		self.assertTrue(veredictos)

	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_breve_no_se_verifica_pero_registra_si_tiene_cifras_sin_respaldo(self, _spec):
		self.configurar(modo_respuesta="Breve")
		with patch.object(api, "explain", return_value=("Hay 999 piezas.", _info())) as mock_exp:
			res = api.ask("¿cuántas piezas hay?")
		self.assertEqual(mock_exp.call_count, 1)
		self.assertIn("Hay 999 piezas.", res["respuesta"])
		self.assertTrue(frappe.get_all("IA API Log",
			filters={"verificacion": "Con cifras sin respaldo"}, limit=1))


class TestPreguntasSugeridas(ConfigFase3):
	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_analitico_con_analisis_trae_preguntas_del_playbook(self, _spec):
		with patch.object(api, "_analisis_completo", return_value=PACK), \
			patch.object(api, "explain", return_value=("El total es 80.0%.", _info())):
			res = api.ask("¿cuántas piezas hay?")
		self.assertEqual(res["preguntas_sugeridas"], playbooks.preguntas_para("efectividad"))

	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_breve_no_trae_preguntas(self, _spec):
		self.configurar(modo_respuesta="Breve")
		with patch.object(api, "explain", return_value=("Hay 5.", _info())):
			res = api.ask("¿cuántas piezas hay?")
		self.assertEqual(res["preguntas_sugeridas"], [])

	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_con_tiempo_agotado_no_hay_preguntas(self, _spec):
		with patch.object(api, "_analisis_completo", side_effect=TiempoAgotado("x")), \
			patch.object(api, "explain", return_value=("Breve.", INFO)):
			res = api.ask("¿cuántas piezas hay?")
		self.assertEqual(res["preguntas_sugeridas"], [])

	@patch.object(api, "generate_spec", return_value=(SPEC, INFO))
	def test_desactivadas_por_configuracion(self, _spec):
		self.configurar(preguntas_sugeridas_activas=0)
		with patch.object(api, "_analisis_completo", return_value=PACK), \
			patch.object(api, "explain", return_value=("El total es 80.0%.", _info())):
			res = api.ask("¿cuántas piezas hay?")
		self.assertEqual(res["preguntas_sugeridas"], [])


class TestPlaybooks(FrappeTestCase):
	def test_cada_pregunta_cae_en_la_familia_de_su_playbook(self):
		for tipo, playbook in playbooks.PLAYBOOKS.items():
			for pregunta in playbook["preguntas"]:
				with self.subTest(pregunta=pregunta):
					intencion = api.intencion_de_pregunta(pregunta)
					self.assertNotEqual(intencion, "general")
					self.assertIn("efectividad", intencion)

	def test_maximo_tres_preguntas(self):
		for tipo in playbooks.PLAYBOOKS:
			self.assertLessEqual(len(playbooks.preguntas_para(tipo)), playbooks.MAX_SUGERIDAS)

	def test_tipo_sin_playbook_no_sugiere_nada(self):
		self.assertEqual(playbooks.preguntas_para("inventado"), [])
		self.assertEqual(playbooks.preguntas_para("conteo"), [])


SPEC_ANIO = {**SPEC, "filters": {"fechaingreso": ["between", "2026-01-01 00:00:00", "2026-12-31 23:59:59"]}}
SPEC_MES = {**SPEC, "filters": {"fechaingreso": ["between", "2026-09-01 00:00:00", "2026-09-30 23:59:59"]}}


class TestPeriodoLargo(ConfigFase3):
	def test_un_mes_no_es_largo_y_un_anio_si(self):
		self.assertFalse(api._periodo_es_largo(SPEC_MES))
		self.assertTrue(api._periodo_es_largo(SPEC_ANIO))

	def test_treinta_y_uno_dias_no_es_largo_treinta_y_dos_si(self):
		treinta_y_uno = {**SPEC, "filters": {"fechaingreso": ["between", "2026-01-01 00:00:00", "2026-01-31 23:59:59"]}}
		treinta_y_dos = {**SPEC, "filters": {"fechaingreso": ["between", "2026-01-01 00:00:00", "2026-02-01 23:59:59"]}}
		self.assertFalse(api._periodo_es_largo(treinta_y_uno))
		self.assertTrue(api._periodo_es_largo(treinta_y_dos))

	def test_sin_fechas_no_es_largo(self):
		self.assertFalse(api._periodo_es_largo(SPEC))

	@patch.object(api, "generate_spec", return_value=(SPEC_ANIO, INFO))
	def test_anual_en_analitico_responde_breve_con_aviso_y_sin_analisis(self, _spec):
		with patch.object(api, "_analisis_completo") as analisis, \
			patch.object(api, "explain", return_value=("Breve.", INFO)) as mock_exp:
			res = api.ask("piezas recolectadas en el año 2026")
		analisis.assert_not_called()
		self.assertNotIn("analisis", mock_exp.call_args.kwargs)
		self.assertIn(api.NOTA_PERIODO_LARGO, res["respuesta"])
		self.assertEqual(res["preguntas_sugeridas"], [])

	@patch.object(api, "generate_spec", return_value=(SPEC_ANIO, INFO))
	def test_anual_en_codigo_tambien_avisa(self, _spec):
		with patch.object(api, "_explicacion_en_codigo", return_value=True), \
			patch.object(api, "explain") as mock_exp:
			res = api.ask("piezas recolectadas en el año 2026")
		mock_exp.assert_not_called()
		self.assertIn(api.NOTA_PERIODO_LARGO, res["respuesta"])

	@patch.object(api, "generate_spec", return_value=(SPEC_MES, INFO))
	def test_mes_en_analitico_si_analiza(self, _spec):
		with patch.object(api, "_analisis_completo", return_value=PACK) as analisis, \
			patch.object(api, "explain", return_value=("El total es 80.0%.", _info())):
			res = api.ask("piezas recolectadas en septiembre de 2026")
		analisis.assert_called_once()
		self.assertNotIn(api.NOTA_PERIODO_LARGO, res["respuesta"])

	@patch.object(api, "generate_spec", return_value=(SPEC_ANIO, INFO))
	def test_anual_en_breve_no_avisa(self, _spec):
		self.configurar(modo_respuesta="Breve")
		with patch.object(api, "explain", return_value=("Breve.", INFO)):
			res = api.ask("piezas recolectadas en el año 2026")
		self.assertNotIn(api.NOTA_PERIODO_LARGO, res["respuesta"])
