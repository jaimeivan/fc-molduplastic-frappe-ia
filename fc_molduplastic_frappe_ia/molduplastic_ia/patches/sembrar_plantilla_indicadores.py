"""Siembra los indicadores de plantilla del tablero (idempotente).

Las plantillas son del sistema (`usuario=Administrator`, sin aseguradora) y no
traen datos: el endpoint que las publique las copia al usuario y las rellena.
Se crean una sola vez; si ya existen no se tocan, para no pisar lo que el
administrador haya editado.
"""

import frappe

PLANTILLAS = [
	{
		"indicador": "Piezas del mes",
		"descripcion": "Piezas que ingresaron en el mes en curso.",
		"tipo": "KPI",
		"formato": "numero",
		"color": "primary",
		"icono": "layers",
	},
	{
		"indicador": "Recolecciones activas",
		"descripcion": "Piezas en proceso de recolección.",
		"tipo": "KPI",
		"formato": "numero",
		"color": "success",
		"icono": "refresh-cw",
	},
	{
		"indicador": "Talleres con piezas",
		"descripcion": "Talleres con al menos una pieza asignada.",
		"tipo": "KPI",
		"formato": "numero",
		"color": "info",
		"icono": "wrench",
	},
	{
		"indicador": "Cobertura de datos",
		"descripcion": "Porcentaje de registros con dato en los campos consultados.",
		"tipo": "KPI",
		"formato": "porcentaje",
		"color": "warning",
		"icono": "database",
	},
	{
		"indicador": "Piezas por marca (top 10)",
		"descripcion": "Las diez marcas con más piezas del período.",
		"tipo": "Tabla",
		"formato": "texto",
		"color": "default",
		"icono": "tag",
	},
	{
		"indicador": "Piezas por estado",
		"descripcion": "Distribución de las piezas por estatus.",
		"tipo": "Gráfico",
		"formato": "texto",
		"color": "info",
		"icono": "pie-chart",
	},
]

COMUNES = {
	"plantilla": 1,
	"usuario": "Administrator",
	"aseguradora": "",
	"estado": "Activo",
	"fuente": "Manual",
	"valor_num": 0,
	"valor_texto": "—",
	"visible": 1,
	"universo": 0,
}


def execute():
	for orden, plantilla in enumerate(PLANTILLAS, start=1):
		_crear(plantilla, orden)


def _crear(plantilla, orden):
	if frappe.db.exists("IA Indicador", {"indicador": plantilla["indicador"], "plantilla": 1}):
		return
	doc = frappe.get_doc(
		{
			"doctype": "IA Indicador",
			**COMUNES,
			**plantilla,
			"orden": orden,
			"actualizado": frappe.utils.now_datetime(),
		}
	)
	doc.insert(ignore_permissions=True)
