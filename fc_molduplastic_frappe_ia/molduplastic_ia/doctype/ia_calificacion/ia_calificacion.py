import frappe
from frappe import _
from frappe.model.document import Document

LARGO_MAXIMO_COMENTARIO = 2000


class IACalificacion(Document):
	def validate(self):
		self.calificacion = (self.calificacion or "").strip()
		if self.calificacion not in ("Bien", "Mal"):
			frappe.throw(_("La calificación debe ser Bien o Mal."))
		self.comentario = (self.comentario or "").strip()[:LARGO_MAXIMO_COMENTARIO]
		if self.calificacion == "Mal" and not self.comentario:
			frappe.throw(_("Para calificar Mal escribe un comentario."))
