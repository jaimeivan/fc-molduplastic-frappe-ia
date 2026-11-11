import frappe
from frappe import _
from frappe.model.document import Document


class IAEvaluacion(Document):
	def validate(self):
		if self.estado == "Revisada":
			if not (self.respuesta or "").strip():
				frappe.throw(_("Para marcar la evaluación como Revisada primero pregunta al asistente."))
			if not self.revisor:
				self.revisor = frappe.session.user
			if not self.fecha_revision:
				self.fecha_revision = frappe.utils.now_datetime()
