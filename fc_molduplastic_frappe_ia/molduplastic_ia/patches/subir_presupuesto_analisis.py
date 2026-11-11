"""Presupuesto de tiempo del análisis: 60 → 120 s (idempotente).

El dueño lo pidió el 2026-11-11 tras medir el análisis en GNP. Solo sube el valor
si todavía está en el anterior: un valor que el administrador haya elegido se respeta.
"""

import frappe


def execute():
	s = frappe.get_single("Molduplastic IA Settings")
	if int(s.presupuesto_analisis_seg or 0) == 60:
		s.presupuesto_analisis_seg = 120
		s.save(ignore_permissions=True)
