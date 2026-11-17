"""Intentos por llamada al modelo: 2 por defecto (idempotente).

El campo tiene default en el DocType, pero un Single sin valor guardado no deja
constancia en la tabla: el parche lo fija para que el administrador lo vea y lo
pueda cambiar. 2 = un segundo intento cuando la primera llamada falla.
"""

import frappe


def execute():
	guardado = frappe.db.sql(
		"SELECT value FROM `tabSingles` WHERE doctype = %s AND field = %s",
		("Molduplastic IA Settings", "intentos_respuesta"),
	)
	if guardado:
		return
	s = frappe.get_single("Molduplastic IA Settings")
	s.intentos_respuesta = 2
	s.save(ignore_permissions=True)
