"""Actualiza la definición de "Recolectadas" en el glosario (dueño, 2026-11-21).

El total ya no suma las rezagadas recolectadas. Solo se toca la fila si sigue
con el texto que sembró `sembrar_analisis_ia`: si el administrador la editó, su
texto se respeta.
"""

import frappe

VIEJA = (
	"Recolectadas en periodo + inconsistentes recolectadas + rezagadas recolectadas "
	"(estatus 100, recolectable = 1)."
)
NUEVA = (
	"Recolectadas en periodo (carga y recolección dentro del período) + inconsistentes "
	"recolectadas (estatus 100, recolectable = 1). Las rezagadas recolectadas no se suman."
)


def execute():
	filas = frappe.get_all(
		"IA Glosario",
		filters={"termino": "Recolectadas", "definicion": VIEJA},
		pluck="name",
	)
	for name in filas:
		frappe.db.set_value("IA Glosario", name, "definicion", NUEVA)
	if filas:
		frappe.db.commit()
