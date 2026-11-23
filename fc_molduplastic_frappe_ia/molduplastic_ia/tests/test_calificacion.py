"""Calificación de respuestas (Bien/Mal) y replicación de la consulta."""

import copy
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


class TestCalificarRespuesta(FrappeTestCase):
	def setUp(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")

	def _preguntar(self, spec=None, **kwargs):
		with patch.object(api, "generate_spec", return_value=(copy.deepcopy(spec or SPEC_OK), INFO)), \
			patch.object(api, "explain", return_value=("Hay 5 piezas.", INFO)):
			return api.ask("¿cuántas piezas hay?", **kwargs)

	def _calificar(self, res, calificacion, comentario=None):
		return api.calificar_respuesta(
			res["conversacion"], res["mensaje_id"], calificacion, comentario
		)

	def _registro(self, mensaje_id):
		nombres = frappe.get_all(
			"IA Calificacion", filters={"mensaje_id": mensaje_id}, pluck="name"
		)
		self.assertEqual(len(nombres), 1)
		return frappe.get_doc("IA Calificacion", nombres[0])

	def test_ask_devuelve_el_id_de_la_respuesta(self):
		res = self._preguntar()
		self.assertTrue(res.get("mensaje_id"))
		conv = frappe.get_doc("IA Conversacion", res["conversacion"])
		fila = next((m for m in conv.mensajes if m.name == res["mensaje_id"]), None)
		self.assertIsNotNone(fila)
		self.assertEqual(fila.rol, "asistente")
		# Sin universo de administrador, el alcance queda vacío: las
		# aseguradoras propias del usuario.
		self.assertEqual(fila.alcance_id, "")

	def test_el_alcance_del_admin_queda_guardado_para_replicar(self):
		frappe.set_user("Administrator")
		aseguradora = frappe.get_all("Aseguradora", pluck="name", limit=1)[0]
		res = self._preguntar(universo=aseguradora)
		conv = frappe.get_doc("IA Conversacion", res["conversacion"])
		fila = next(m for m in conv.mensajes if m.name == res["mensaje_id"])
		self.assertEqual(fila.alcance_id, str(aseguradora))

	def test_calificar_bien_sin_comentario(self):
		res = self._preguntar()
		out = self._calificar(res, "Bien")
		self.assertTrue(out["ok"])
		reg = self._registro(res["mensaje_id"])
		self.assertEqual(reg.calificacion, "Bien")
		self.assertEqual(reg.comentario, "")
		self.assertEqual(reg.usuario, USUARIO_GNP)
		self.assertEqual(reg.pregunta, "¿cuántas piezas hay?")
		self.assertTrue(reg.universo)
		self.assertTrue(reg.fecha_hora)

	def test_mal_exige_comentario(self):
		res = self._preguntar()
		with self.assertRaises(frappe.ValidationError):
			self._calificar(res, "Mal", "   ")

	def test_mal_con_comentario_lo_guarda(self):
		res = self._preguntar()
		out = self._calificar(res, "Mal", "  La cifra no coincide.  ")
		self.assertEqual(out["calificacion"], "Mal")
		self.assertEqual(out["comentario"], "La cifra no coincide.")

	def test_volver_a_calificar_actualiza_sin_duplicar(self):
		res = self._preguntar()
		self._calificar(res, "Bien")
		self._calificar(res, "Mal", "Mejoró la explicación")
		reg = self._registro(res["mensaje_id"])
		self.assertEqual(reg.calificacion, "Mal")
		self.assertEqual(reg.comentario, "Mejoró la explicación")

	def test_no_se_puede_calificar_una_conversacion_ajena(self):
		res = self._preguntar()
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.PermissionError):
			self._calificar(res, "Bien")

	def test_conversacion_eliminada_no_se_califica(self):
		res = self._preguntar()
		frappe.db.set_value("IA Conversacion", res["conversacion"], "estado", "Eliminada")
		with self.assertRaises(frappe.PermissionError):
			self._calificar(res, "Bien")

	def test_solo_se_califica_una_respuesta_del_asistente(self):
		res = self._preguntar()
		conv = frappe.get_doc("IA Conversacion", res["conversacion"])
		mensaje_usuario = next(m.name for m in conv.mensajes if m.rol == "usuario")
		with self.assertRaises(frappe.ValidationError):
			api.calificar_respuesta(res["conversacion"], mensaje_usuario, "Bien")

	def test_calificacion_invalida(self):
		res = self._preguntar()
		with self.assertRaises(frappe.ValidationError):
			self._calificar(res, "Regular")

	def test_obtener_conversacion_devuelve_la_calificacion(self):
		res = self._preguntar()
		self._calificar(res, "Mal", "Faltan datos")
		datos = api.obtener_conversacion(res["conversacion"])
		fila = next(m for m in datos["mensajes"] if m["name"] == res["mensaje_id"])
		self.assertEqual(fila["calificacion"], "Mal")
		self.assertEqual(fila["comentario"], "Faltan datos")

	def test_replicar_la_respuesta_vuelve_a_ejecutar_la_consulta(self):
		res = self._preguntar()
		self._calificar(res, "Bien")
		reg = self._registro(res["mensaje_id"])
		frappe.set_user("Administrator")
		out = api.replicar_calificacion(reg.name)
		self.assertTrue(out["ok"])
		self.assertEqual(out["pregunta"], "¿cuántas piezas hay?")
		self.assertEqual(out["respuesta"], "Hay 5 piezas.")
		self.assertTrue(out["columnas"])
		self.assertTrue(out["filas"])
		self.assertTrue(out["original"]["columnas"])

	def test_replicar_una_respuesta_sin_consulta_avisa(self):
		with patch.object(api, "generate_spec", return_value=({"out_of_scope": True}, INFO)):
			res = api.ask("recomiéndame un restaurante")
		self._calificar(res, "Bien")
		reg = self._registro(res["mensaje_id"])
		frappe.set_user("Administrator")
		out = api.replicar_calificacion(reg.name)
		self.assertFalse(out["ok"])
		self.assertIn("no tuvo una consulta", out["mensaje"])

	def test_replicar_una_respuesta_directa_avisa_que_no_hay_consulta(self):
		with patch.object(api, "generate_spec", return_value=({"respuesta_directa": "REGIONES"}, INFO)):
			res = api.ask("dame las regiones del país y sus estados")
		self._calificar(res, "Bien")
		reg = self._registro(res["mensaje_id"])
		frappe.set_user("Administrator")
		out = api.replicar_calificacion(reg.name)
		self.assertFalse(out["ok"])
		self.assertIn("sin consulta", out["mensaje"])

	def test_replicar_solo_administradores(self):
		res = self._preguntar()
		self._calificar(res, "Bien")
		reg = self._registro(res["mensaje_id"])
		with self.assertRaises(frappe.PermissionError):
			api.replicar_calificacion(reg.name)
