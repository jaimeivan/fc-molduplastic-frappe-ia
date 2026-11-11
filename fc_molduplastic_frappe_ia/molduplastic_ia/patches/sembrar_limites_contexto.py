"""Límites iniciales del historial conversacional (idempotente).

Los dos campos son obligatorios en Molduplastic IA Settings y el código no
guarda valores de respaldo: este patch los deja puestos para que el chat
funcione desde el primer día y el formulario no los muestre vacíos.
"""

import frappe

TURNOS_INICIALES = 3
CHARS_INICIALES = 400


def execute():
	s = frappe.get_single("Molduplastic IA Settings")
	cambio = False
	if not s.max_turnos_contexto:
		s.max_turnos_contexto = TURNOS_INICIALES
		cambio = True
	if not s.max_chars_contexto:
		s.max_chars_contexto = CHARS_INICIALES
		cambio = True
	if cambio:
		s.save(ignore_permissions=True)
