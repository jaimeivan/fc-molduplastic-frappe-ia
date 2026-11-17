"""Presupuesto de tiempo del análisis: 120 → 240 s (idempotente).

El dueño lo pidió el 2026-11-12, después de que el proxy cortara una respuesta
analítica a la mitad. Solo sube el valor si todavía está en el anterior: un
valor que el administrador haya elegido se respeta.
"""

import frappe


def execute():
	s = frappe.get_single("Molduplastic IA Settings")
	if int(s.presupuesto_analisis_seg or 0) == 120:
		s.presupuesto_analisis_seg = 240
		s.save(ignore_permissions=True)
