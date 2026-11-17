import frappe
from frappe import _
from frappe.model.document import Document

# Tope del presupuesto de tiempo (2026-11-12: el doble, 120 → 240 s). El proxy
# corta a los 240 s: el análisis deja una reserva para la respuesta breve de respaldo.
PRESUPUESTO_MAXIMO_SEG = 240
# Tope de intentos por llamada al modelo (1 = sin reintento).
INTENTOS_MAXIMOS = 5


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
			if fila.estado == "Validado":
				self._rechazar_validado_sin_contenido(fila)
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
		dias = int(self.dias_maximos_analisis or 0)
		if not 1 <= dias <= 366:
			frappe.throw(_("Los días máximos para el análisis deben estar entre 1 y 366."))
		presupuesto = int(self.presupuesto_analisis_seg or 0)
		if not 5 <= presupuesto <= PRESUPUESTO_MAXIMO_SEG:
			frappe.throw(
				_("El presupuesto de tiempo debe estar entre 5 y {0} segundos.").format(
					PRESUPUESTO_MAXIMO_SEG
				)
			)
		# Un campo recién agregado llega vacío (0) en el Single ya guardado: eso no
		# es un valor del administrador y el parche lo siembra. Un valor puesto se
		# valida igual.
		intentos = int(self.intentos_respuesta or 0)
		if intentos and not 1 <= intentos <= INTENTOS_MAXIMOS:
			frappe.throw(
				_("Los intentos por llamada al modelo deben estar entre 1 y {0}.").format(
					INTENTOS_MAXIMOS
				)
			)

	def _rechazar_validado_sin_contenido(self, fila):
		"""Marcar Validado sin escribir el contenido no es validar (2026-11-17).

		El parche deja "[pendiente de validación del administrador]" en el umbral,
		las causas y la acción. Una fila así en Validado llegaba al chat como si
		fuera una recomendación, porque el modelo copia la acción "tal cual viene".
		Los campos con marcador ya no se inyectan ni generan sugerencias; además se
		rechaza el cambio de estado para que el aviso no se repita. Una fila que ya
		estaba Validada se puede seguir guardando (así se corrige sin quedar
		bloqueado).
		"""
		from fc_molduplastic_frappe_ia.molduplastic_ia.hallazgos import es_pendiente

		pendientes = [
			etiqueta
			for campo, etiqueta in (
				("umbral", _("umbral")),
				("causas", _("causas")),
				("accion", _("acción")),
			)
			if es_pendiente(fila.get(campo))
		]
		if not pendientes:
			return
		previo = frappe.db.get_value("IA Glosario", fila.name, "estado") if fila.name else None
		if previo == "Validado":
			return
		frappe.throw(
			_(
				"La entrada «{0}» no se puede marcar Validado: {1} sigue con el texto "
				"pendiente de validación. Escribe el contenido del negocio o déjala en "
				"Borrador."
			).format(fila.termino, _(" y ").join(pendientes))
		)
