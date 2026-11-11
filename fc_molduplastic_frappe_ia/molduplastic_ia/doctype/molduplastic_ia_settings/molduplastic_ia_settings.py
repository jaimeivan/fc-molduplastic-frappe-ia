import frappe
from frappe import _
from frappe.model.document import Document

# Tope del presupuesto de tiempo: el proxy corta a los 120 s y la respuesta
# breve de respaldo también necesita su propia llamada.
PRESUPUESTO_MAXIMO_SEG = 60


class MolduplasticIASettings(Document):
	def validate(self):
		self._validar_analisis()

	def _validar_analisis(self):
		from fc_molduplastic_frappe_ia.molduplastic_ia.api import intenciones_conocidas

		conocidas = intenciones_conocidas()
		for fila in self.get("modo_excepciones") or []:
			if fila.intencion not in conocidas:
				frappe.throw(
					_("Intención desconocida en excepciones: {0}. Válidas: {1}").format(
						fila.intencion, ", ".join(sorted(conocidas))
					)
				)
		for fila in self.get("glosario") or []:
			if fila.intencion not in conocidas:
				frappe.throw(_("Intención desconocida en el glosario: {0}").format(fila.intencion))
		objetivo = float(self.objetivo_efectividad or 0)
		if not 0 < objetivo <= 100:
			frappe.throw(_("El objetivo de efectividad debe estar entre 0 y 100."))
		presupuesto = int(self.presupuesto_analisis_seg or 0)
		if not 5 <= presupuesto <= PRESUPUESTO_MAXIMO_SEG:
			frappe.throw(
				_("El presupuesto de tiempo debe estar entre 5 y {0} segundos.").format(
					PRESUPUESTO_MAXIMO_SEG
				)
			)
