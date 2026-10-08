"""Crea el rol 'Molduplastic IA User' y sus permisos (idempotente).

- Lectura sobre Aseguradora, Pieza, Recoleccion y Taller (el tenant lo filtra
  permission_query_conditions en hooks.py, no estos permisos).
- Crear/leer/escribir (solo propias, if_owner) sobre IA Conversacion e IA Reporte.
- IA API Log y Molduplastic IA Settings quedan solo para System Manager.
"""

import frappe

ROL = "Molduplastic IA User"

LECTURA_TENANT = ["Aseguradora", "Pieza", "Recoleccion", "Taller"]
PROPIAS = ["IA Conversacion", "IA Reporte"]


def execute():
	if not frappe.db.exists("Role", ROL):
		frappe.get_doc({"doctype": "Role", "role_name": ROL, "desk_access": 1}).insert(
			ignore_permissions=True
		)

	for doctype in LECTURA_TENANT:
		_asegurar_perm(doctype, {"read": 1, "report": 1, "export": 1})

	for doctype in PROPIAS:
		_asegurar_perm(doctype, {"read": 1, "write": 1, "create": 1, "if_owner": 1})


def _asegurar_perm(doctype, valores):
	existente = frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": ROL})
	if existente:
		doc = frappe.get_doc("Custom DocPerm", existente)
		cambio = False
		for k, v in valores.items():
			if doc.get(k) != v:
				doc.set(k, v)
				cambio = True
		if cambio:
			doc.save(ignore_permissions=True)
		return
	doc = frappe.get_doc({"doctype": "Custom DocPerm", "parent": doctype, "role": ROL, **valores})
	doc.insert(ignore_permissions=True)
