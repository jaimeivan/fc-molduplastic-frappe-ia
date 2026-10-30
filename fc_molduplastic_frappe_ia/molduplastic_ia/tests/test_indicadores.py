"""Tests del tablero de indicadores: pertenencia, plantillas y refresco."""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import ui_api

USUARIO_GNP = "pruebas.gnp@molduplastic.test"

SPEC_VALIDO = {
	"doctype": "Taller",
	"aggregations": [{"func": "count", "field": "*", "alias": "total"}],
}


def _crear(usuario, indicador, plantilla=0):
	doc = frappe.get_doc(
		{
			"doctype": "IA Indicador",
			"indicador": indicador,
			"usuario": usuario,
			"plantilla": plantilla,
			"estado": "Activo",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


class TestIndicadoresPropios(FrappeTestCase):
	def setUp(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_crear_aparece_en_el_listado(self):
		res = ui_api.ui_guardar_indicador({"indicador": "Piezas del mes", "tipo": "KPI"})
		self.assertTrue(res["ok"])
		nombres = [i["name"] for i in ui_api.ui_listar_indicadores()]
		self.assertIn(res["indicador"]["name"], nombres)

	def test_usuario_del_payload_se_ignora(self):
		res = ui_api.ui_guardar_indicador({"indicador": "propia", "usuario": "Administrator"})
		self.assertEqual(res["indicador"]["usuario"], USUARIO_GNP)

	def test_no_puede_eliminar_uno_ajeno(self):
		ajeno = _crear("Administrator", "indicador del admin")
		with self.assertRaises(frappe.PermissionError):
			ui_api.ui_eliminar_indicador(ajeno.name)
		self.assertEqual(frappe.db.get_value("IA Indicador", ajeno.name, "estado"), "Activo")

	def test_eliminar_lo_quita_del_listado(self):
		nombre = ui_api.ui_guardar_indicador({"indicador": "efímero"})["indicador"]["name"]
		self.assertTrue([i for i in ui_api.ui_listar_indicadores() if i["name"] == nombre])
		ui_api.ui_eliminar_indicador(nombre)
		self.assertFalse([i for i in ui_api.ui_listar_indicadores() if i["name"] == nombre])
		self.assertEqual(frappe.db.get_value("IA Indicador", nombre, "estado"), "Eliminado")

	def test_aplicar_plantilla_crea_y_es_idempotente(self):
		plantilla = _crear("Administrator", "Plantilla de talleres", plantilla=1)
		primera = ui_api.ui_aplicar_plantilla(plantilla.name)
		segunda = ui_api.ui_aplicar_plantilla(plantilla.name)
		self.assertFalse(primera["ya_existia"])
		self.assertTrue(segunda["ya_existia"])
		self.assertEqual(primera["indicador"]["name"], segunda["indicador"]["name"])
		copia = primera["indicador"]
		self.assertEqual(copia["usuario"], USUARIO_GNP)
		self.assertEqual(copia["plantilla"], 0)
		self.assertEqual(str(copia["aseguradora"]), "6")

	def test_refresco_con_spec_invalido_devuelve_mensaje(self):
		nombre = ui_api.ui_guardar_indicador(
			{"indicador": "con spec", "spec_json": json.dumps(SPEC_VALIDO)}
		)["indicador"]["name"]
		malo = {
			"doctype": "Pieza",
			"aggregations": [{"func": "sum", "field": "noexiste", "alias": "x"}],
		}
		frappe.db.set_value("IA Indicador", nombre, "spec_json", json.dumps(malo))
		res = ui_api.ui_actualizar_indicador(nombre, reejecutar=1)
		self.assertFalse(res["ok"])
		self.assertIn("No pude ejecutar", res["mensaje"])

	def test_no_admin_no_refresca_plantilla_ajena(self):
		plantilla = _crear("Administrator", "Plantilla ajena", plantilla=1)
		with self.assertRaises(frappe.PermissionError):
			ui_api.ui_actualizar_indicador(plantilla.name, reejecutar=1)

	def test_no_admin_no_refresca_plantilla_marcada(self):
		"""Una plantilla con `usuario` del tenant tampoco se refresca sin ser admin."""
		propia = _crear(USUARIO_GNP, "Plantilla del tenant", plantilla=1)
		with self.assertRaises(frappe.PermissionError) as ctx:
			ui_api.ui_actualizar_indicador(propia.name, reejecutar=1)
		self.assertIn("plantillas", str(ctx.exception))
