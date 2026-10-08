"""Crea el Workspace 'Molduplastic IA' con acceso al Asistente (idempotente)."""

import frappe
import json


CONTENIDO = [
	{
		"id": "iaHeader01",
		"type": "header",
		"data": {
			"text": '<span style="font-size: 18px;"><b>Asistente IA</b></span>',
			"col": 12,
		},
	},
	{"id": "iaShort01", "type": "shortcut", "data": {"shortcut_name": "Asistente IA", "col": 3}},
]


def execute():
	if not frappe.db.exists("Module Def", "Molduplastic IA"):
		frappe.get_doc({"doctype": "Module Def", "module_name": "Molduplastic IA"}).insert(
			ignore_permissions=True
		)
	if frappe.db.exists("Workspace", "Molduplastic IA"):
		ws = frappe.get_doc("Workspace", "Molduplastic IA")
	else:
		ws = frappe.get_doc(
			{
				"doctype": "Workspace",
				"label": "Molduplastic IA",
				"title": "Molduplastic IA",
				"module": "Molduplastic IA",
				"icon": "sparkles",
				"public": 1,
				"shortcuts": [
					{
						"label": "Asistente IA",
						"type": "Page",
						"link_to": "asistente-ia",
						"icon": "message-circle",
					}
				],
				"roles": [{"role": "Molduplastic IA User"}, {"role": "System Manager"}],
				"content": json.dumps(CONTENIDO),
			}
		)
		ws.insert(ignore_permissions=True)
	for rol in ["Molduplastic-Admin", "Molduplastic-Admin-ReadOnly"]:
		if frappe.db.exists("Role", rol) and rol not in [r.role for r in ws.roles]:
			ws.append("roles", {"role": rol})
	ws.save(ignore_permissions=True)
