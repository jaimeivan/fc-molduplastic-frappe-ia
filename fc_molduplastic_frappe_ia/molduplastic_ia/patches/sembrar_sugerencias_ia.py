"""Preguntas sugeridas iniciales del chat (idempotente).

La lista vive en Molduplastic IA Settings > Sugerencias; este patch solo deja
la primera puesta para que el chat tenga algo que ofrecer al instalarse.
"""

import frappe

INICIALES = [
	"¿Las agencias recolectan mejor que los talleres?",
]


def execute():
	s = frappe.get_single("Molduplastic IA Settings")
	actuales = {(x.sugerencia or "").strip().lower() for x in (s.get("sugerencias") or [])}
	nuevas = [t for t in INICIALES if t.strip().lower() not in actuales]
	if not nuevas:
		return
	base = max([x.orden or 0 for x in (s.get("sugerencias") or [])] + [0])
	for i, texto in enumerate(nuevas, start=1):
		s.append("sugerencias", {"sugerencia": texto, "activo": 1, "orden": base + i})
	s.save(ignore_permissions=True)
