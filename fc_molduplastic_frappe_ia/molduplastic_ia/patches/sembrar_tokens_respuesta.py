"""Tokens máximos de respuesta: 4000 por defecto (idempotente).

El default viejo (2000) dejaba a DeepSeek sin presupuesto para los specs
largos: devolvía `content` vacío y la pregunta moría con "servicio no
disponible". El código ya acota cualquier valor a 4000 (`OpenAICompatibleAdapter`),
pero el Single recién creado seguía arrancando en 2000.

Solo se sube si el valor guardado está vacío o en el default viejo: un valor
que el administrador haya elegido se respeta.
"""

import frappe

NUEVO = 4000
VIEJO = 2000


def execute():
	guardado = frappe.db.sql(
		"SELECT value FROM `tabSingles` WHERE doctype = %s AND field = %s",
		("Molduplastic IA Settings", "max_tokens_respuesta"),
	)
	valor = int((guardado[0][0] if guardado else 0) or 0)
	if valor not in (0, VIEJO):
		return
	s = frappe.get_single("Molduplastic IA Settings")
	s.max_tokens_respuesta = NUEVO
	s.save(ignore_permissions=True)
