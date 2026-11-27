"""Completa la configuración del análisis en un Single que ya existía.

Un Single viejo no recibe los defaults de los campos nuevos: `load_from_db`
solo carga lo guardado. Los parches de siembra corren una sola vez, así que en
una instalación que ya los tenía registrados podían quedar valores en 0 o
vacíos (en staging, `objetivo_efectividad` = 0 tumbaba el primer parche que
guardaba el Single y, si se saltaba, el armado del prompt fallaba).

`MolduplasticIASettings.before_validate` rellena lo que falta cuando corre un
parche; este parche solo fuerza un guardado para que se aplique también en
instalaciones donde ningún otro parche vuelve a guardar el Single. Es
idempotente: si todo está puesto, guarda los mismos valores.
"""

import frappe


def execute():
	settings = frappe.get_single("Molduplastic IA Settings")
	settings.save(ignore_permissions=True)
