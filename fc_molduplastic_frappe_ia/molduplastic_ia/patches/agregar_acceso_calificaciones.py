"""Enlaza las calificaciones de respuestas en el Workspace administrativo.

Idempotente: si el shortcut ya está, no se duplica y no toca nada más.
"""

import frappe


def execute():
	workspace_name = "Administración Molduplastic IA"
	if not frappe.db.exists("Workspace", workspace_name):
		return
	workspace = frappe.get_doc("Workspace", workspace_name)
	if any(
		shortcut.type == "DocType" and shortcut.link_to == "IA Calificacion"
		for shortcut in workspace.shortcuts
	):
		return
	workspace.append(
		"shortcuts",
		{
			"label": "Calificaciones",
			"type": "DocType",
			"link_to": "IA Calificacion",
			"icon": "star",
		},
	)
	workspace.save(ignore_permissions=True)
