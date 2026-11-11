"""Fase 1 del análisis: modo de respuesta, presupuesto de tiempo, glosario y objetivo.

El modo es solo de configuración del servidor. Estas pruebas fijan que el
cliente no lo cambia, que "Breve" no altera el prompt de siempre, que el
glosario solo usa entradas Validadas y que un análisis fuera de tiempo entrega
la respuesta breve con su nota.
"""

import time
from unittest.mock import patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import api, llm_factory as F
from fc_molduplastic_frappe_ia.molduplastic_ia.llm_factory import LLMProviderError, LLMTiempoAgotado

SETTINGS = "Molduplastic IA Settings"
USUARIO_GNP = "pruebas.gnp@molduplastic.test"

SPEC_OK = {
	"doctype": "Pieza",
	"fields": ["folio", "marca"],
	"filters": {},
	"aggregations": [],
	"group_by": [],
	"order_by": [],
	"limit": 5,
	"render_chart": None,
	"out_of_scope": False,
}
INFO = {
	"proveedor": "DeepSeek",
	"modelo": "deepseek-chat",
	"prompt": "p",
	"respuesta": "r",
	"tokens_entrada": 10,
	"tokens_salida": 20,
	"latencia_ms": 100,
	"costo_estimado_usd": 0.00001,
	"es_fallback": False,
	"motivo_fallback": "",
}
RESULTADO = {"columnas": ["marca", "total", "recolectadas"], "filas": [["Nissan", 40, 20]], "cobertura": {}}


def _instantanea():
	s = frappe.get_doc(SETTINGS)
	return {
		"modo_respuesta": s.modo_respuesta,
		"objetivo_efectividad": s.objetivo_efectividad,
		"presupuesto_analisis_seg": s.presupuesto_analisis_seg,
		"excepciones": [{"intencion": f.intencion, "modo": f.modo} for f in s.modo_excepciones or []],
		"glosario": {f.name: f.estado for f in s.glosario or []},
	}


def _restaurar(snap):
	s = frappe.get_doc(SETTINGS)
	s.modo_respuesta = snap["modo_respuesta"]
	s.objetivo_efectividad = snap["objetivo_efectividad"]
	s.presupuesto_analisis_seg = snap["presupuesto_analisis_seg"]
	s.set("modo_excepciones", [])
	for fila in snap["excepciones"]:
		s.append("modo_excepciones", fila)
	s.save(ignore_permissions=True)
	for nombre, estado in snap["glosario"].items():
		frappe.db.set_value("IA Glosario", nombre, "estado", estado)
	frappe.clear_document_cache(SETTINGS, SETTINGS)


class ConfigMixin:
	def setUp(self):
		self.snap = _instantanea()
		self.addCleanup(_restaurar, self.snap)

	def configurar(self, **campos):
		s = frappe.get_doc(SETTINGS)
		for clave, valor in campos.items():
			s.set(clave, valor)
		s.save(ignore_permissions=True)
		frappe.clear_document_cache(SETTINGS, SETTINGS)

	def excepciones(self, *pares):
		s = frappe.get_doc(SETTINGS)
		s.set("modo_excepciones", [])
		for intencion, modo in pares:
			s.append("modo_excepciones", {"intencion": intencion, "modo": modo})
		s.save(ignore_permissions=True)
		frappe.clear_document_cache(SETTINGS, SETTINGS)


class TestModoDeRespuesta(ConfigMixin, FrappeTestCase):
	def test_por_defecto_es_breve(self):
		self.configurar(modo_respuesta="Breve")
		self.excepciones()
		self.assertEqual(api._modo_de_respuesta("efectividad"), "Breve")

	def test_modo_global_analitico(self):
		self.configurar(modo_respuesta="Analítico")
		self.excepciones()
		self.assertEqual(api._modo_de_respuesta("resumen"), "Analítico")

	def test_excepcion_activa_analitico_con_global_breve(self):
		self.configurar(modo_respuesta="Breve")
		self.excepciones(("efectividad", "Analítico"))
		self.assertEqual(api._modo_de_respuesta("efectividad"), "Analítico")
		self.assertEqual(api._modo_de_respuesta("resumen"), "Breve")

	def test_excepcion_breve_gana_al_global_analitico(self):
		self.configurar(modo_respuesta="Analítico")
		self.excepciones(("resumen", "Breve"))
		self.assertEqual(api._modo_de_respuesta("resumen"), "Breve")
		self.assertEqual(api._modo_de_respuesta("efectividad"), "Analítico")

	def test_valor_invalido_cae_a_breve(self):
		frappe.db.set_single_value(SETTINGS, "modo_respuesta", "Verbose")
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		self.assertEqual(api._modo_de_respuesta("efectividad"), "Breve")

	def test_intencion_desconocida_en_excepciones_no_se_guarda(self):
		s = frappe.get_doc(SETTINGS)
		s.append("modo_excepciones", {"intencion": "inventada", "modo": "Analítico"})
		with self.assertRaises(frappe.ValidationError):
			s.save(ignore_permissions=True)

	def test_intencion_desconocida_en_glosario_no_se_guarda(self):
		s = frappe.get_doc(SETTINGS)
		s.append("glosario", {"intencion": "inventada", "termino": "X", "estado": "Borrador"})
		with self.assertRaises(frappe.ValidationError):
			s.save(ignore_permissions=True)

	def test_objetivo_y_presupuesto_dentro_de_rango(self):
		for campos in ({"objetivo_efectividad": 0}, {"objetivo_efectividad": 101},
			{"presupuesto_analisis_seg": 61}, {"presupuesto_analisis_seg": 4}):
			with self.subTest(campos=campos):
				s = frappe.get_doc(SETTINGS)
				for k, v in campos.items():
					s.set(k, v)
				with self.assertRaises(frappe.ValidationError):
					s.save(ignore_permissions=True)
				frappe.clear_document_cache(SETTINGS, SETTINGS)


class TestObjetivoEnConfiguracion(ConfigMixin, FrappeTestCase):
	def test_se_lee_de_settings(self):
		self.configurar(objetivo_efectividad=80)
		self.assertEqual(F.objetivo_efectividad(), 80)
		self.assertEqual(api._etiqueta_objetivo(78), "Por debajo")

	def test_decimal_se_devuelve_como_float(self):
		self.configurar(objetivo_efectividad=72.5)
		self.assertEqual(F.objetivo_efectividad(), 72.5)

	def test_entero_se_devuelve_como_entero(self):
		self.configurar(objetivo_efectividad=75)
		self.assertIsInstance(F.objetivo_efectividad(), int)

	def test_la_constante_ya_no_existe(self):
		self.assertFalse(hasattr(F, "OBJETIVO_EFECTIVIDAD"))


class TestExplainModos(ConfigMixin, FrappeTestCase):
	"""Se captura el prompt que viaja, sin llamar al proveedor."""

	def _capturar(self, texto="Respuesta de prueba."):
		capturado = {}

		class Fake:
			def chat(self, mensajes, max_tokens=None, timeout=None):
				capturado["mensajes"] = mensajes
				capturado["timeout"] = timeout
				return texto, {"entrada": 1, "salida": 1}, 5, 0.0

		def con_fallback(cadena, fn):
			texto_salida, uso, latencia, costo = fn(Fake())
			return texto_salida, {"proveedor": "F", "modelo": "m", "tokens_entrada": 1,
				"tokens_salida": 1, "latencia_ms": latencia, "costo_estimado_usd": costo,
				"es_fallback": False, "motivo_fallback": ""}

		for parche in (
			patch.object(F, "_cadena", return_value=[]),
			patch.object(F, "_con_fallback", side_effect=con_fallback),
		):
			parche.start()
			self.addCleanup(parche.stop)
		return capturado

	def test_breve_manda_el_prompt_de_siempre(self):
		capturado = self._capturar()
		F.explain("¿cuántas piezas hay?", SPEC_OK, RESULTADO, modo="Breve")
		sistema = capturado["mensajes"][0]["content"]
		self.assertEqual(sistema, F.SYSTEM_EXPLAIN)
		self.assertNotIn("MODO ANALÍTICO", sistema)

	def test_analitico_agrega_el_bloque_del_modo(self):
		capturado = self._capturar()
		F.explain("¿cuántas piezas hay?", SPEC_OK, RESULTADO, modo="Analítico")
		sistema = capturado["mensajes"][0]["content"]
		self.assertIn("MODO ANALÍTICO", sistema)
		self.assertTrue(sistema.startswith(F.SYSTEM_EXPLAIN))

	def test_glosario_solo_con_entradas_validadas_de_la_intencion(self):
		filas = frappe.get_all(
			"IA Glosario", filters={"intencion": "efectividad"}, pluck="name"
		)
		self.assertTrue(filas, "el glosario inicial debe tener la efectividad")
		frappe.db.set_value("IA Glosario", filas[0], "estado", "Validado")
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		capturado = self._capturar()
		F.explain("¿efectividad por taller?", SPEC_OK, RESULTADO, modo="Analítico", intencion="efectividad")
		contenido = "\n".join(m["content"] for m in capturado["mensajes"])
		self.assertIn("Glosario de negocio validado", contenido)
		self.assertIn("Recolectadas sobre ingresadas", contenido)

	def test_borrador_no_entra_al_prompt(self):
		capturado = self._capturar()
		F.explain("¿pendientes por resumen?", SPEC_OK, RESULTADO, modo="Analítico", intencion="resumen")
		contenido = "\n".join(m["content"] for m in capturado["mensajes"])
		self.assertNotIn("Glosario de negocio validado", contenido)
		self.assertNotIn("[pendiente de validación", contenido)

	def test_breve_nunca_inyecta_glosario(self):
		filas = frappe.get_all("IA Glosario", filters={"intencion": "efectividad"}, pluck="name")
		frappe.db.set_value("IA Glosario", filas[0], "estado", "Validado")
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		capturado = self._capturar()
		F.explain("¿efectividad?", SPEC_OK, RESULTADO, modo="Breve", intencion="efectividad")
		contenido = "\n".join(m["content"] for m in capturado["mensajes"])
		self.assertNotIn("Glosario de negocio validado", contenido)

	def test_presupuesto_agotado_no_llama_al_modelo(self):
		capturado = self._capturar()
		with self.assertRaises(LLMTiempoAgotado):
			F.explain("¿x?", SPEC_OK, RESULTADO, modo="Analítico", deadline=time.time() - 1)
		self.assertNotIn("mensajes", capturado)

	def test_el_presupuesto_acota_el_timeout_de_la_llamada(self):
		capturado = self._capturar()
		F.explain("¿x?", SPEC_OK, RESULTADO, modo="Analítico", deadline=time.time() + 10)
		self.assertIsNotNone(capturado["timeout"])
		self.assertLessEqual(capturado["timeout"], 10)


class TestTimeoutDelAdaptador(FrappeTestCase):
	def test_timeout_con_presupuesto_es_tiempo_agotado(self):
		adapter = F.OpenAICompatibleAdapter("Zen", "http://x", "k", "m")
		with patch.object(F.requests, "post", side_effect=requests.Timeout("lento")):
			with self.assertRaises(F.LLMTiempoAgotado):
				adapter.chat([{"role": "user", "content": "hola"}], timeout=5)

	def test_timeout_sin_presupuesto_sigue_siendo_proveedor(self):
		adapter = F.OpenAICompatibleAdapter("Zen", "http://x", "k", "m")
		with patch.object(F.requests, "post", side_effect=requests.Timeout("lento")):
			with self.assertRaises(LLMProviderError) as ctx:
				adapter.chat([{"role": "user", "content": "hola"}])
			self.assertNotIsInstance(ctx.exception, LLMTiempoAgotado)

	def test_presupuesto_solo_acorta_el_timeout(self):
		adapter = F.OpenAICompatibleAdapter("Zen", "http://x", "k", "m")
		with patch.object(F.requests, "post") as post:
			post.return_value.status_code = 200
			post.return_value.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
			adapter.chat([{"role": "user", "content": "hola"}], timeout=10_000)
			self.assertEqual(post.call_args.kwargs["timeout"], adapter.timeout)
			adapter.chat([{"role": "user", "content": "hola"}], timeout=1)
			self.assertEqual(post.call_args.kwargs["timeout"], 1)


SPEC_CONTEO = {
	"doctype": "Pieza",
	"fields": [],
	"filters": {"recolectable": ["=", 1]},
	"aggregations": [{"func": "count", "field": "*", "alias": "piezas", "filtros": {}}],
	"group_by": [],
	"order_by": [],
	"limit": 1,
	"render_chart": None,
	"out_of_scope": False,
}


class TestAskConModo(ConfigMixin, FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		# Estas pruebas miden el camino que llama al modelo; las respuestas que
		# arma el código tienen su propia prueba al final.
		parche = patch.object(api, "_explicacion_en_codigo", return_value=False)
		parche.start()
		self.addCleanup(parche.stop)

	def _registros(self):
		return set(frappe.get_all("IA API Log", pluck="name"))

	@patch.object(api, "explain", return_value=("Hay 5 piezas.", INFO))
	@patch.object(api, "generate_spec", return_value=(SPEC_OK, INFO))
	def test_el_cliente_no_elige_el_modo(self, _spec, mock_exp):
		self.configurar(modo_respuesta="Breve")
		self.excepciones()
		res = api.ask("¿cuántas piezas hay?", modo="Analítico")
		self.assertTrue(res["ok"])
		self.assertNotEqual(mock_exp.call_args.kwargs.get("modo"), "Analítico")

	@patch.object(api, "explain", return_value=("Hay 5 piezas.", INFO))
	@patch.object(api, "generate_spec", return_value=(SPEC_OK, INFO))
	def test_modo_analitico_del_servidor_llega_al_explain(self, _spec, mock_exp):
		self.configurar(modo_respuesta="Analítico")
		self.excepciones()
		api.ask("¿cuántas piezas hay?")
		kwargs = mock_exp.call_args.kwargs
		self.assertEqual(kwargs["modo"], "Analítico")
		self.assertIsNotNone(kwargs["deadline"])
		self.assertEqual(kwargs["intencion"], api.intencion_de_pregunta("¿cuántas piezas hay?"))

	@patch.object(api, "explain")
	@patch.object(api, "generate_spec", return_value=(SPEC_CONTEO, INFO))
	def test_sin_tiempo_entrega_breve_con_nota(self, _spec, mock_exp):
		self.configurar(modo_respuesta="Analítico")
		self.excepciones()
		mock_exp.side_effect = [LLMTiempoAgotado("se agotó"), ("Respuesta breve.", INFO)]
		antes = self._registros()
		res = api.ask("¿cuántas piezas hay?")
		self.assertTrue(res["ok"])
		self.assertIn("Respuesta breve.", res["respuesta"])
		self.assertIn(api.NOTA_TIEMPO_ANALISIS, res["respuesta"])
		self.assertNotIn("modo", mock_exp.call_args.kwargs)
		nuevos = self._registros() - antes
		omitidos = frappe.get_all(
			"IA API Log", filters={"name": ["in", list(nuevos)], "analisis_omitido": 1}, pluck="name"
		)
		self.assertTrue(omitidos, "el análisis omitido debe quedar en el registro")

	@patch.object(api, "explain")
	@patch.object(api, "generate_spec", return_value=(SPEC_OK, INFO))
	def test_fallo_del_proveedor_no_muestra_la_nota(self, _spec, mock_exp):
		self.configurar(modo_respuesta="Analítico")
		self.excepciones()
		mock_exp.side_effect = [LLMProviderError("zen devolvió HTTP 500"), ("Respuesta breve.", INFO)]
		res = api.ask("¿cuántas piezas hay?")
		self.assertIn("Respuesta breve.", res["respuesta"])
		self.assertNotIn(api.NOTA_TIEMPO_ANALISIS, res["respuesta"])

	@patch.object(api, "explain", return_value=("Hay 5 piezas.", INFO))
	@patch.object(api, "generate_spec", return_value=(SPEC_OK, INFO))
	def test_breve_no_agrega_nota(self, _spec, mock_exp):
		self.configurar(modo_respuesta="Breve")
		self.excepciones()
		res = api.ask("¿cuántas piezas hay?")
		self.assertNotIn(api.NOTA_TIEMPO_ANALISIS, res["respuesta"])



class TestRespuestaDeCodigo(ConfigMixin, FrappeTestCase):
	def setUp(self):
		super().setUp()
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")

	@patch.object(api, "explain")
	@patch.object(api, "generate_spec", return_value=(SPEC_OK, INFO))
	def test_respuesta_armada_por_codigo_no_llama_al_modelo(self, _spec, mock_exp):
		self.configurar(modo_respuesta="Analítico")
		self.excepciones()
		antes = set(frappe.get_all("IA API Log", pluck="name"))
		api.ask("¿cuál es el precio promedio por marca?")
		mock_exp.assert_not_called()
		nuevos = set(frappe.get_all("IA API Log", pluck="name")) - antes
		codigo = frappe.get_all(
			"IA API Log", filters={"name": ["in", list(nuevos)], "modo_respuesta": "Código"}, pluck="name"
		)
		self.assertTrue(codigo, "la respuesta de código debe registrar su modo")


class TestGlosarioInicial(FrappeTestCase):
	def test_el_glosario_inicial_esta_en_borrador(self):
		estados = frappe.get_all("IA Glosario", pluck="estado")
		self.assertTrue(estados)
		self.assertTrue(all(e == "Borrador" for e in estados) or "Validado" in estados)

	def test_las_causas_sin_validar_van_marcadas(self):
		filas = frappe.get_all("IA Glosario", fields=["causas", "accion", "estado"])
		for fila in filas:
			if fila.estado == "Borrador" and fila.causas:
				self.assertIn("pendiente de validación", fila.causas)
