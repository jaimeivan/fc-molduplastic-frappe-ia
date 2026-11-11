"""Crea un Workspace administrativo con acceso a la sincronización manual."""

import frappe


def execute():
	workspace_name = "Administración Molduplastic IA"
	if frappe.db.exists("Workspace", workspace_name):
		workspace = frappe.get_doc("Workspace", workspace_name)
	else:
		workspace = frappe.get_doc(
			{
				"doctype": "Workspace",
				"label": workspace_name,
				"title": workspace_name,
				"module": "Molduplastic IA",
				"icon": "database",
				"public": 0,
				"roles": [{"role": "System Manager"}],
				"shortcuts": [],
				"content": "[]",
			}
		)
		workspace.insert(ignore_permissions=True)
	if any(
		shortcut.type == "Page" and shortcut.link_to == "sincronizacion-datos"
		for shortcut in workspace.shortcuts
	):
		return
	workspace.append(
		"shortcuts",
		{
			"label": "Sincronización de datos",
			"type": "Page",
			"link_to": "sincronizacion-datos",
			"icon": "refresh-cw",
		},
	)
	workspace.save(ignore_permissions=True)
