import frappe
from frappe import _
from frappe.model.document import Document

# Tope del presupuesto de tiempo (2026-11-11, doble del anterior). El proxy corta
# a los 120 s: el análisis deja una reserva para la respuesta breve de respaldo.
PRESUPUESTO_MAXIMO_SEG = 120


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
		minimo = int(self.min_piezas_porcentaje or 0)
		if minimo < 1:
			frappe.throw(_("El mínimo de piezas para porcentajes debe ser al menos 1."))
		if float(self.umbral_caida_pp or 0) <= 0:
			frappe.throw(_("El umbral de caída debe ser mayor que cero."))
		if not 0 < float(self.umbral_concentracion_pct or 0) <= 100:
			frappe.throw(_("El umbral de concentración debe estar entre 0 y 100."))
		if not 0 <= int(self.tendencia_periodos or 0) <= 6:
			frappe.throw(_("Los períodos de tendencia deben estar entre 0 y 6."))
		presupuesto = int(self.presupuesto_analisis_seg or 0)
		if not 5 <= presupuesto <= PRESUPUESTO_MAXIMO_SEG:
			frappe.throw(
				_("El presupuesto de tiempo debe estar entre 5 y {0} segundos.").format(
					PRESUPUESTO_MAXIMO_SEG
				)
			)
