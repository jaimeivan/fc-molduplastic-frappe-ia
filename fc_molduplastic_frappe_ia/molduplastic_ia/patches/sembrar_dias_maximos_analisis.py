"""Días máximos del análisis completo: 31 por defecto (idempotente).

El campo tiene default en el DocType, pero un Single sin valor guardado no deja
constancia en la tabla: el parche lo fija para que el administrador lo vea y lo
pueda cambiar.
"""

import frappe


def execute():
	guardado = frappe.db.sql(
		"SELECT value FROM `tabSingles` WHERE doctype = %s AND field = %s",
		("Molduplastic IA Settings", "dias_maximos_analisis"),
	)
	if guardado:
		return
	s = frappe.get_single("Molduplastic IA Settings")
	s.dias_maximos_analisis = 31
	s.save(ignore_permissions=True)
