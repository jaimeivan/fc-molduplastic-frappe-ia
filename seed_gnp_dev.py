"""Seed de desarrollo: usuario de pruebas para la aseguradora GNP (solo dev).

NO ejecutar en producción. La contraseña se pasa por variable de entorno.
"""

import os

import frappe

frappe.init(site="molduplastic.jarritohost.net")
frappe.connect()

EMAIL = "pruebas.gnp@molduplastic.test"
PASSWORD = os.environ.get("GNP_TEST_PASSWORD", "GNP-pruebas-2026")

if not frappe.db.exists("User", EMAIL):
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": EMAIL,
			"first_name": "Pruebas",
			"last_name": "GNP",
			"enabled": 1,
			"send_welcome_email": 0,
		}
	)
	user.insert(ignore_permissions=True)
	print("usuario creado:", EMAIL)
else:
	user = frappe.get_doc("User", EMAIL)
	print("usuario ya existía:", EMAIL)

user.new_password = PASSWORD
user.save(ignore_permissions=True)
user.add_roles("Molduplastic IA User", "Desk User")
print("roles:", frappe.get_roles(EMAIL))

if not frappe.db.exists("User Permission", {"user": EMAIL, "allow": "Aseguradora", "for_value": "6"}):
	frappe.get_doc(
		{
			"doctype": "User Permission",
			"user": EMAIL,
			"allow": "Aseguradora",
			"for_value": "6",
		}
	).insert(ignore_permissions=True)
	print("User Permission GNP creada")
else:
	print("User Permission GNP ya existía")

frappe.db.commit()
frappe.destroy()
