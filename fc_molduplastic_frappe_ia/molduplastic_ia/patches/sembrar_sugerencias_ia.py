"""Preguntas sugeridas iniciales del chat (idempotente).

La lista vive en Molduplastic IA Settings > Sugerencias; este patch solo deja
la primera puesta para que el chat tenga algo que ofrecer al instalarse.
"""

import frappe

INICIALES = [
	"¿Las agencias recolectan mejor que los talleres?",
	"¿Qué tipo de pieza se recolecta menos?",
	"¿Cuáles son las 5 partes que menos se recolectan con al menos 100 piezas?",
	"¿Qué marcas tienen menor efectividad de recolección?",
	"¿Qué plaza tiene la efectividad más baja en la región Norte?",
	"muestra la efectividad por plaza de la region",
	"cuantas recolecciones pendientes hay por region",
	"¿Cuántas piezas tengo pendientes y cuánto valen?",
	"¿Qué talleres concentran más piezas no entregadas?",
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
