"""Tests del flujo ask() con LLM simulado (sin gastar API)."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import api

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


class TestAsk(FrappeTestCase):
	def setUp(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")

	@patch.object(api, "explain", return_value=("Hay 5 piezas.", INFO))
	@patch.object(api, "generate_spec", return_value=(SPEC_OK, INFO))
	def test_ask_ok_guarda_conversacion_y_log(self, mock_spec, mock_exp):
		res = api.ask("¿cuántas piezas hay?")
		self.assertTrue(res["ok"])
		self.assertIn("columnas", res)
		self.assertGreater(res["cobertura"]["universo"], 0)
		conv = frappe.get_doc("IA Conversacion", res["conversacion"])
		self.assertEqual(conv.usuario, USUARIO_GNP)
		self.assertEqual(len(conv.mensajes), 2)
		logs = frappe.get_all("IA API Log", filters={"consulta_id": ["like", "%"]}, limit=5)
		self.assertTrue(logs)

	@patch.object(api, "generate_spec", return_value=({"out_of_scope": True}, INFO))
	def test_ask_fuera_de_alcance(self, mock_spec):
		res = api.ask("recomiéndame un restaurante")
		self.assertTrue(res.get("fuera_de_alcance"))
		self.assertIn("fuera de mi alcance", res["mensaje"])
		estados = frappe.get_all("IA API Log", filters={"estado": "Fuera de alcance"}, limit=1)
		self.assertTrue(estados)

	def test_ask_sin_aseguradora(self):
		with patch.object(api, "get_user_aseguradoras", return_value=[]):
			res = api.ask("hola")
		self.assertFalse(res["ok"])

	def test_admin_pregunta_universo_primero(self):
		frappe.set_user("Administrator")
		res = api.ask("¿cuántas piezas hay?")
		self.assertTrue(res.get("pregunta_universo"))
		self.assertEqual(len(res["aseguradoras"]), 12)

	@patch.object(api, "explain", return_value=("Listo.", INFO))
	@patch.object(api, "generate_spec", return_value=(SPEC_OK, INFO))
	def test_admin_universo_una_aseguradora(self, mock_spec, mock_exp):
		frappe.set_user("Administrator")
		res = api.ask("dame piezas", universo="6")
		self.assertTrue(res["ok"])
		self.assertEqual(res["universo"], "6")
		self.assertGreater(res["cobertura"]["universo"], 0)
		self.assertLess(res["cobertura"]["universo"], frappe.db.count("Pieza"))

	@patch.object(api, "explain", return_value=("Listo.", INFO))
	@patch.object(api, "generate_spec", return_value=(SPEC_OK, INFO))
	def test_admin_universo_todas(self, mock_spec, mock_exp):
		frappe.set_user("Administrator")
		res = api.ask("dame piezas", universo="TODAS")
		self.assertTrue(res["ok"])
		self.assertEqual(res["cobertura"]["universo"], frappe.db.count("Pieza"))

	@patch.object(api, "explain", return_value=("Listo.", INFO))
	@patch.object(api, "generate_spec", return_value=(SPEC_OK, INFO))
	def test_no_admin_no_puede_forzar_otro_universo(self, mock_spec, mock_exp):
		res = api.ask("dame piezas", universo="7")
		self.assertTrue(res["ok"])
		self.assertEqual(res["universo"], "6")


SNAPSHOT = {"columnas": ["folio"], "filas": [["A1"]]}


class TestReportes(FrappeTestCase):
	def setUp(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_guardar_y_reabrir_cobertura_texto_plano(self):
		"""Regresión: el JS mandaba cobertura como texto y reabrir tronaba con JSONDecodeError."""
		res = api.guardar_reporte(
			titulo="rep texto",
			spec=dict(SPEC_OK),
			snapshot=dict(SNAPSHOT),
			cobertura="Universo: 10 registros de tu aseguradora.",
			universo=10,
		)
		datos = api.reabrir_reporte(res["reporte"])
		self.assertEqual(datos["columnas"], ["folio"])
		self.assertEqual(datos["filas"], [["A1"]])
		self.assertFalse(datos["en_vivo"])

	def test_guardar_y_reabrir_cobertura_dict(self):
		cob = {"universo": 10, "campos": {}}
		res = api.guardar_reporte(
			titulo="rep dict", spec=dict(SPEC_OK), snapshot=dict(SNAPSHOT), cobertura=cob, universo=10
		)
		datos = api.reabrir_reporte(res["reporte"])
		self.assertEqual(datos["cobertura"], cob)

	def test_reabrir_reporte_viejo_con_texto_crudo(self):
		"""Filas guardadas antes del fix (texto crudo en cobertura_json) deben abrir sin tronar."""
		res = api.guardar_reporte(
			titulo="rep viejo", spec=dict(SPEC_OK), snapshot=dict(SNAPSHOT), cobertura=None
		)
		frappe.db.set_value("IA Reporte", res["reporte"], "cobertura_json", "Universo: 5 registros.")
		datos = api.reabrir_reporte(res["reporte"])
		self.assertEqual(datos["cobertura"], "Universo: 5 registros.")


class TestBorradoLogico(FrappeTestCase):
	def setUp(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")

	def _reporte(self):
		return api.guardar_reporte(titulo="rep x", spec=dict(SPEC_OK), snapshot=dict(SNAPSHOT))["reporte"]

	def test_eliminar_reporte_oculta_y_bloquea_reabrir(self):
		nombre = self._reporte()
		self.assertTrue([r for r in api.listar_reportes() if r["name"] == nombre])
		api.eliminar_reporte(nombre)
		self.assertFalse([r for r in api.listar_reportes() if r["name"] == nombre])
		with self.assertRaises(frappe.PermissionError):
			api.reabrir_reporte(nombre)

	@patch.object(api, "explain", return_value=("Hay 5 piezas.", INFO))
	@patch.object(api, "generate_spec", return_value=(SPEC_OK, INFO))
	def test_eliminar_conversacion_oculta_y_bloquea(self, mock_spec, mock_exp):
		conv = api.ask("¿cuántas piezas hay?")["conversacion"]
		self.assertTrue([c for c in api.historial() if c["name"] == conv])
		api.eliminar_conversacion(conv)
		self.assertFalse([c for c in api.historial() if c["name"] == conv])
		with self.assertRaises(frappe.PermissionError):
			api.obtener_conversacion(conv)
		with self.assertRaises(frappe.PermissionError):
			api._get_conversacion(conv, USUARIO_GNP)

	def test_otro_usuario_no_puede_eliminar(self):
		frappe.set_user("Administrator")
		doc = frappe.get_doc(
			{
				"doctype": "IA Conversacion",
				"titulo": "conv admin",
				"usuario": "Administrator",
				"estado": "Abierta",
				"ultima_actividad": api._ahora(),
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.set_user(USUARIO_GNP)
		with self.assertRaises(frappe.PermissionError):
			api.eliminar_conversacion(doc.name)
		with self.assertRaises(frappe.PermissionError):
			api.eliminar_reporte(self._reporte_admin(doc.name))

	def _reporte_admin(self, conversacion):
		frappe.set_user("Administrator")
		nombre = api.guardar_reporte(
			titulo="rep admin", spec=dict(SPEC_OK), snapshot=dict(SNAPSHOT), conversacion=conversacion
		)["reporte"]
		frappe.set_user(USUARIO_GNP)
		return nombre
