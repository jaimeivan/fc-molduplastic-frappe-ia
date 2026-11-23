"""Siembra el modo de las preguntas sugeridas (dueño, 2026-11-23).

El modo se elige en Molduplastic IA Settings. Si el Single ya tiene un valor
guardado no se toca; si está vacío se deja el modo libre, que es el default
elegido por el dueño. Idempotente.
"""

import frappe


def execute():
	settings = frappe.get_doc("Molduplastic IA Settings")
	if settings.modo_sugerencias:
		return
	settings.modo_sugerencias = "Modelo libre"
	settings.save(ignore_permissions=True)
