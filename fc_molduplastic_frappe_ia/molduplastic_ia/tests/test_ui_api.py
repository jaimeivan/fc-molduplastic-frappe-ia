"""Tests de ui_bootstrap y del aislamiento de tenant en los indicadores."""

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import ui_api
from fc_molduplastic_frappe_ia.molduplastic_ia.permissions import lista_aseguradoras

USUARIO_GNP = "pruebas.gnp@molduplastic.test"


def _usuario(email, roles=()):
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": "Ui",
			"last_name": "Test",
			"user_type": "System User",
			"send_welcome_email": 0,
		}
	).insert(ignore_permissions=True)
	for rol in roles:
		doc.add_roles(rol)
	return doc.name


class TestBootstrap(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")

	def test_admin_ve_todas_las_aseguradoras(self):
		datos = ui_api.ui_bootstrap()
		self.assertTrue(datos["es_admin"])
		self.assertEqual(datos["universo_por_defecto"], "TODAS")
		self.assertEqual(len(datos["aseguradoras"]), len(lista_aseguradoras()))
		self.assertTrue(datos["modulos"]["plantillas"])

	def test_usuario_normal_solo_ve_las_suyas(self):
		frappe.set_user(USUARIO_GNP)
		datos = ui_api.ui_bootstrap()
		self.assertFalse(datos["es_admin"])
		self.assertEqual(datos["universo_por_defecto"], "6")
		self.assertEqual([str(a["name"]) for a in datos["aseguradoras"]], ["6"])
		self.assertFalse(datos["modulos"]["plantillas"])

	def test_system_manager_cuenta_como_admin(self):
		usuario = _usuario("ui.sm@molduplastic.test", roles=("System Manager",))
		frappe.set_user(usuario)
		datos = ui_api.ui_bootstrap()
		self.assertTrue(datos["es_admin"])
		self.assertEqual(datos["universo_por_defecto"], "TODAS")
		self.assertEqual(len(datos["aseguradoras"]), len(lista_aseguradoras()))

	def test_guest_no_entra(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			ui_api.ui_bootstrap()


class TestTenantEnIndicadores(FrappeTestCase):
	def setUp(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")

	def test_no_escapa_de_su_tenant(self):
		with self.assertRaises(frappe.PermissionError):
			ui_api.ui_guardar_indicador({"indicador": "ajena", "aseguradora": "7"})

	def test_toma_su_aseguradora_si_no_manda(self):
		res = ui_api.ui_guardar_indicador({"indicador": "propia"})
		self.assertEqual(str(res["indicador"]["aseguradora"]), "6")

	def test_admin_puede_dejarla_vacia(self):
		frappe.set_user("Administrator")
		res = ui_api.ui_guardar_indicador({"indicador": "global"})
		self.assertFalse(res["indicador"]["aseguradora"])

	def test_admin_puede_elegir_una(self):
		frappe.set_user("Administrator")
		res = ui_api.ui_guardar_indicador({"indicador": "de hdi", "aseguradora": "7"})
		self.assertEqual(str(res["indicador"]["aseguradora"]), "7")
