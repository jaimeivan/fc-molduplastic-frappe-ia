"""Crea la Page de uso de sugerencias y la enlaza en el Workspace administrativo.

Idempotente: si la Page ya existe solo actualiza el JSON estándar; si el
shortcut ya está, no lo duplica.
"""

import frappe


def execute():
	frappe.reload_doc("molduplastic_ia", "page", "sugerencias-ia")
	workspace_name = "Administración Molduplastic IA"
	if not frappe.db.exists("Workspace", workspace_name):
		return
	workspace = frappe.get_doc("Workspace", workspace_name)
	if any(
		shortcut.type == "Page" and shortcut.link_to == "sugerencias-ia"
		for shortcut in workspace.shortcuts
	):
		return
	workspace.append(
		"shortcuts",
		{
			"label": "Uso de sugerencias",
			"type": "Page",
			"link_to": "sugerencias-ia",
			"icon": "bar-chart-2",
		},
	)
	workspace.save(ignore_permissions=True)
