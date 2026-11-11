"""Valores iniciales de la verificación de cifras y las preguntas sugeridas (idempotente).

Ambos quedan activos por defecto. Un Check sin valor guardado se lee como 0 en un
Single, así que se mira la tabla de valores y no el atributo del documento.
"""

import frappe


def execute():
	s = frappe.get_single("Molduplastic IA Settings")
	for campo in ("verificacion_cifras", "preguntas_sugeridas_activas"):
		guardado = frappe.db.sql(
			"SELECT value FROM `tabSingles` WHERE doctype = %s AND field = %s",
			("Molduplastic IA Settings", campo),
		)
		if not guardado:
			s.set(campo, 1)
			s.save(ignore_permissions=True)
