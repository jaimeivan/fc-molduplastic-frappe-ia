"""Tests de aislamiento por tenant y del ejecutor (incluye cobertura)."""

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia.permissions import get_user_aseguradoras
from fc_molduplastic_frappe_ia.molduplastic_ia.query_executor import ejecutar
from fc_molduplastic_frappe_ia.molduplastic_ia.validate_spec import SpecInvalida

USUARIO_GNP = "pruebas.gnp@molduplastic.test"


class TestTenant(FrappeTestCase):
	def test_usuario_gnp_solo_ve_su_aseguradora(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		self.assertEqual(get_user_aseguradoras(), ["6"])

	def test_lista_piezas_sin_fugas(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		filas = frappe.get_list("Pieza", fields=["aseguradora"], limit_page_length=20)
		self.assertTrue(filas)
		for f in filas:
			self.assertEqual(str(f.aseguradora), "6")

	def test_lectura_directa_otra_aseguradora_denegada(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		otra = frappe.db.sql("select name from tabPieza where aseguradora != '6' limit 1")[0][0]
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("Pieza", otra).check_permission("read")

	def test_ejecutor_respeta_tenant(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": ["folio", "aseguradora"],
				"filters": {},
				"limit": 5,
			},
			USUARIO_GNP,
		)
		self.assertTrue(res["filas"])
		idx = res["columnas"].index("aseguradora")
		for fila in res["filas"]:
			self.assertEqual(str(fila[idx]), "6")
		self.assertGreater(res["cobertura"]["universo"], 0)

	def test_ejecutor_rechaza_doctype_prohibido(self):
		with self.assertRaises(SpecInvalida):
			ejecutar({"doctype": "User", "fields": ["email"], "limit": 1}, "Administrator")

	def test_taller_visible_solo_via_pieza_del_tenant(self):
		from fc_molduplastic_frappe_ia.molduplastic_ia.permissions import has_permission_taller

		taller = frappe.db.sql(
			"select taller_asignado from tabPieza where aseguradora='6' "
			"and taller_asignado is not null limit 1"
		)[0][0]
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		visibles = frappe.get_list("Taller", fields=["name"], limit_page_length=10000)
		self.assertIn(taller, [t.name for t in visibles])
		# Taller sin piezas del tenant: denegado aunque exista el registro
		doc = frappe._dict({"doctype": "Taller", "name": "TALLER-SIN-VINCULO-GNP"})
		self.assertFalse(has_permission_taller(doc, "read", USUARIO_GNP))

	def test_agregacion_global_sin_group_by(self):
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {},
				"aggregations": [{"func": "count", "field": "*", "alias": "total"}],
				"group_by": [],
				"limit": 1,
			},
			"Administrator",
		)
		self.assertEqual(res["columnas"], ["total"])
		self.assertEqual(len(res["filas"]), 1)

	def test_usuario_sin_aseguradora_no_ve_nada(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.delete("User Permission", {"user": USUARIO_GNP})
		try:
			res = ejecutar({"doctype": "Pieza", "fields": ["folio"], "limit": 5}, USUARIO_GNP)
			self.assertEqual(res["filas"], [])
			self.assertEqual(res["cobertura"]["universo"], 0)
		finally:
			frappe.get_doc(
				{"doctype": "User Permission", "user": USUARIO_GNP, "allow": "Aseguradora", "for_value": "6"}
			).insert(ignore_permissions=True)
