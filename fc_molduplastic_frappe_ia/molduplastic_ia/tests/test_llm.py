"""Tests de la factory LLM (sin gastar API: requests simulado)."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import llm_factory as F


class FakeResp:
	status_code = 200
	text = "{}"

	def __init__(self, payload, status=200):
		self._payload = payload
		self.status_code = status

	def json(self):
		return self._payload


def fila_zen(nombre="Eco test", rol="Económico", modelo="deepseek-v4.1-flash"):
	return frappe._dict(
		{
			"nombre": nombre,
			"rol": rol,
			"modo": "Opencode Zen",
			"modelo": modelo,
			"orden": 1,
			"costo_entrada_1k": 0.0003,
			"costo_salida_1k": 0.0012,
			"get_password": lambda k: "ZENKEY",
		}
	)


def settings_stub():
	return frappe._dict(
		{
			"timeout_llm": 120,
			"max_tokens_respuesta": 2000,
			"max_limit": 500,
			"proveedor_defecto": "Automático",
			"get_password": lambda k: "ZENKEY",
		}
	)


class TestZenAdapter(FrappeTestCase):
	@patch.object(F.requests, "post")
	def test_chat_zen_ok(self, mock_post):
		mock_post.return_value = FakeResp(
			{
				"choices": [{"message": {"content": '{"a": 1}'}}],
				"usage": {"prompt_tokens": 100, "completion_tokens": 10},
			}
		)
		with patch.object(F.frappe, "get_cached_doc", return_value=settings_stub()):
			a = F._adaptador(fila_zen())
			self.assertTrue(a.base_url.startswith("https://opencode.ai/zen"))
			texto, uso, lat, costo = a.chat([{"role": "user", "content": "hola"}], json_mode=True)
		self.assertEqual(texto, '{"a": 1}')
		self.assertEqual(uso, {"entrada": 100, "salida": 10})
		self.assertGreater(costo, 0)
		llamada = mock_post.call_args
		self.assertTrue(llamada[0][0].endswith("/chat/completions"))
		self.assertEqual(llamada[1]["json"]["model"], "deepseek-v4.1-flash")
		self.assertEqual(llamada[1]["headers"]["Authorization"], "Bearer ZENKEY")

	@patch.object(F.requests, "post")
	def test_chat_401_es_fallback(self, mock_post):
		mock_post.return_value = FakeResp({}, status=401)
		with patch.object(F.frappe, "get_cached_doc", return_value=settings_stub()):
			a = F._adaptador(fila_zen())
			with self.assertRaises(F.LLMProviderError):
				a.chat([{"role": "user", "content": "hola"}])

	def test_cadena_respeta_rol_y_orden(self):
		filas = [
			fila_zen("eco2", "Económico", "m-eco"),
			fila_zen("pro1", "Profundo", "m-pro"),
		]
		with patch.object(F, "_proveedores_activos", return_value=filas):
			with patch.object(F, "_es_compleja", return_value=False):
				cadena = F._cadena("¿cuántas piezas hay?")
				self.assertEqual(cadena[0].nombre, "eco2")
			with patch.object(F, "_es_compleja", return_value=True):
				cadena = F._cadena("analiza la tendencia por mes con promedios")
				self.assertEqual(cadena[0].nombre, "pro1")

	@patch.object(F, "_proveedores_activos", return_value=[])
	def test_sin_proveedores_error_config(self, mock_prov):
		with self.assertRaises(F.LLMConfigError):
			F._cadena("hola")


class FakeAdapter:
	def __init__(self):
		self.mensajes = None

	def chat(self, mensajes, **kwargs):
		self.mensajes = mensajes
		return "{}", {"entrada": 0, "salida": 0}, 0, 0.0


class TestPromptSpec(FrappeTestCase):
	"""El prompt es la única fuente de gramática de filtros y de fechas para el LLM."""

	def _prompt_generado(self, pregunta):
		capturado = {}

		def fake_fallback(cadena, fn):
			adapter = FakeAdapter()
			texto, _uso, _lat, _costo = fn(adapter)
			capturado["prompt"] = adapter.mensajes[0]["content"]
			return '{"out_of_scope": true}', {"proveedor": "test"}

		with patch.object(F.frappe, "get_cached_doc", return_value=settings_stub()):
			with patch.object(F, "_cadena", return_value=[fila_zen()]):
				with patch.object(F, "_con_fallback", side_effect=fake_fallback):
					F.generate_spec(pregunta)
		return capturado["prompt"]

	def test_prompt_documenta_entre_y_lista(self):
		prompt = self._prompt_generado("¿cómo va la recolección este mes?")
		self.assertIn('["between", "2026-01-01 00:00:00", "2026-03-31 23:59:59"]', prompt)
		self.assertIn('["in", ["Toyota", "Nissan"]]', prompt)

	def test_prompt_inyecta_fecha_actual(self):
		hoy = frappe.utils.now_datetime().strftime("%Y-%m-%d")
		self.assertIn(hoy, self._prompt_generado("piezas de hoy"))
