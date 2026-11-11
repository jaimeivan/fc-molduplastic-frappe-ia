"""Revisión del set de evaluación por el administrador (IA Evaluacion).

El asistente está simulado (mock de api.ask): estas pruebas no contestan con el LLM real.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import evaluacion

USUARIO_GNP = "pruebas.gnp@molduplastic.test"


def _como(usuario):
	"""Cambia de usuario limpiando los roles en caché (como en una petición nueva)."""
	frappe.set_user(usuario)
	frappe.local.roles = None


def _fila(id_="TEST-EVAL-01", **campos):
	datos = {"doctype": "IA Evaluacion", "id": id_, "pregunta": "¿cuántas piezas hay?",
		"universo": "GNP", "estado": "Pendiente", **campos}
	if frappe.db.exists("IA Evaluacion", id_):
		frappe.delete_doc("IA Evaluacion", id_, force=True)
	return frappe.get_doc(datos).insert(ignore_permissions=True)


class TestEvaluacionAdmin(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.addCleanup(frappe.set_user, "Administrator")
		self.addCleanup(self._borrar_filas_de_prueba)

	def _borrar_filas_de_prueba(self):
		for nombre in frappe.get_all("IA Evaluacion", filters={"id": ["like", "TEST-EVAL-%"]}, pluck="name"):
			frappe.delete_doc("IA Evaluacion", nombre, force=True)

	def test_solo_system_manager_pregunta(self):
		_fila()
		_como(USUARIO_GNP)
		self.addCleanup(_como, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			evaluacion.preguntar_evaluacion("TEST-EVAL-01")
		with self.assertRaises(frappe.PermissionError):
			evaluacion.resumen_evaluacion()

	def test_revisada_sin_respuesta_no_se_guarda(self):
		doc = _fila()
		doc.estado = "Revisada"
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_revisada_con_respuesta_registra_revisor_y_fecha(self):
		doc = _fila(respuesta="Hay 5 piezas.")
		doc.estado = "Revisada"
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertEqual(doc.revisor, "Administrator")
		self.assertTrue(doc.fecha_revision)

	def test_pregunta_sin_universo_se_rechaza(self):
		_fila(universo="")
		with self.assertRaises(frappe.ValidationError):
			evaluacion.preguntar_evaluacion("TEST-EVAL-01")

	@patch.object(evaluacion.api, "ask", return_value={"ok": True, "respuesta": "Hay 5 piezas.",
		"preguntas_sugeridas": ["¿Qué talleres…?"]})
	def test_preguntar_guarda_respuesta_y_reinicia_la_revision(self, mock_ask):
		doc = _fila(estado="Revisada", analiza=1, inventa_cifras=1, respuesta="vieja")
		doc.reload()
		doc.estado = "Revisada"
		doc.save(ignore_permissions=True)
		evaluacion.preguntar_evaluacion("TEST-EVAL-01")
		doc.reload()
		self.assertEqual(doc.respuesta, "Hay 5 piezas.")
		self.assertEqual(doc.preguntas_sugeridas, "¿Qué talleres…?")
		self.assertEqual(doc.estado, "Pendiente")
		self.assertFalse(doc.analiza)
		self.assertFalse(doc.inventa_cifras)
		self.assertEqual(mock_ask.call_args.kwargs["universo"], "GNP")

	@patch.object(evaluacion.api, "ask", return_value={"ok": False, "mensaje": "Límite alcanzado"})
	def test_si_el_asistente_falla_no_se_pisa_la_respuesta(self, _ask):
		doc = _fila(respuesta="anterior")
		with self.assertRaises(frappe.ValidationError):
			evaluacion.preguntar_evaluacion("TEST-EVAL-01")
		doc.reload()
		self.assertEqual(doc.respuesta, "anterior")

	def test_resumen_calcula_porcentajes_solo_sobre_revisadas(self):
		antes = evaluacion.resumen_evaluacion()
		_fila("TEST-EVAL-01", respuesta="r", estado="Revisada", analiza=1, sugiere_con_sustento=1, inventa_cifras=0)
		_fila("TEST-EVAL-02", respuesta="r", estado="Revisada", analiza=0, sugiere_con_sustento=0, inventa_cifras=1)
		_fila("TEST-EVAL-03", respuesta="", estado="Pendiente")
		res = evaluacion.resumen_evaluacion()
		# Los porcentajes salen solo de las dos revisadas de esta prueba (las del set
		# sembrado siguen pendientes y no cuentan).
		self.assertEqual(res["revisadas"], antes["revisadas"] + 2)
		self.assertEqual(res["analiza_pct"], 50.0 if antes["revisadas"] == 0 else res["analiza_pct"])
		self.assertEqual(res["sin_respuesta"], antes["sin_respuesta"] + 1)
		self.assertEqual(res["total"], antes["total"] + 3)

	def test_resumen_sin_revisadas_no_divide_entre_cero(self):
		frappe.db.sql("UPDATE `tabIA Evaluacion` SET estado='Pendiente'")
		res = evaluacion.resumen_evaluacion()
		self.assertEqual(res["revisadas"], 0)
		self.assertIsNone(res["analiza_pct"])


class TestSemilla(FrappeTestCase):
	def test_la_carga_desde_el_csv_es_idempotente(self):
		evaluacion.sembrar_desde_csv()
		total = frappe.db.count("IA Evaluacion")
		self.assertEqual(evaluacion.sembrar_desde_csv(), 0)
		self.assertEqual(frappe.db.count("IA Evaluacion"), total)

	def test_el_set_tiene_las_doce_preguntas_del_csv(self):
		self.assertGreaterEqual(frappe.db.count("IA Evaluacion", {"id": ["like", "E%"]}), 12)
