"""Filas iniciales de proveedores IA vía Opencode Zen (idempotente)."""

import frappe

INICIALES = [
	{
		"nombre": "DeepSeek eco (zen)",
		"rol": "Económico",
		"modo": "Opencode Zen",
		"modelo": "deepseek-v4.1-flash",
		"costo_entrada_1k": 0.0003,
		"costo_salida_1k": 0.0012,
		"orden": 1,
	},
	{
		"nombre": "GLM pro (zen)",
		"rol": "Profundo",
		"modo": "Opencode Zen",
		"modelo": "glm-5.3",
		"costo_entrada_1k": 0.0014,
		"costo_salida_1k": 0.0044,
		"orden": 1,
	},
]

# Nombres anteriores -> actuales (conservan activo/orden).
RENOMBRES = {
	"DeepSeek eco (opencode)": "DeepSeek eco (zen)",
	"GLM pro (opencode)": "GLM pro (zen)",
}


def execute():
	s = frappe.get_single("Molduplastic IA Settings")
	por_nombre = {p.nombre: p for p in (s.proveedores or [])}
	for viejo, nuevo in RENOMBRES.items():
		if viejo in por_nombre and nuevo not in por_nombre:
			por_nombre[viejo].nombre = nuevo
			por_nombre[nuevo] = por_nombre.pop(viejo)
	cambio = False
	for fila in INICIALES:
		existente = por_nombre.get(fila["nombre"])
		if existente is None:
			s.append("proveedores", {**fila, "activo": 1})
			cambio = True
		else:
			for k in ("rol", "modo", "modelo", "costo_entrada_1k", "costo_salida_1k"):
				if existente.get(k) != fila[k]:
					existente.set(k, fila[k])
					cambio = True
	if cambio:
		s.save(ignore_permissions=True)
