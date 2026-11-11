"""Configuración inicial del análisis de respuestas (idempotente).

- Objetivo de efectividad 75 % (antes una constante en código).
- Presupuesto de tiempo 60 s y modo de respuesta Breve: el comportamiento de
  hoy no cambia hasta que el administrador active el modo analítico.
- Glosario inicial en Borrador. Las causas y acciones no son del negocio: van
  marcadas como pendientes de validación. Solo las entradas Validadas se usan.
"""

import frappe

PENDIENTE = "[pendiente de validación del administrador]"

GLOSARIO_INICIAL = [
	{
		"intencion": "efectividad",
		"termino": "Efectividad",
		"definicion": "Recolectadas sobre ingresadas × 100.",
		"umbral": "Objetivo 75 % (configurable). Para comparar grupos se usa un mínimo de 50 ingresadas.",
		"causas": PENDIENTE,
		"accion": PENDIENTE,
	},
	{
		"intencion": "recolectadas",
		"termino": "Recolectadas",
		"definicion": "Recolectadas en periodo + inconsistentes recolectadas + rezagadas recolectadas (estatus 100, recolectable = 1).",
		"umbral": "",
		"causas": "",
		"accion": "",
	},
	{
		"intencion": "no_recolectadas",
		"termino": "No recolectadas",
		"definicion": "No recolectadas en periodo + inconsistentes no recolectadas + rezagadas no recolectadas (estatus 200 a 206).",
		"umbral": PENDIENTE,
		"causas": PENDIENTE,
		"accion": PENDIENTE,
	},
	{
		"intencion": "no_localizadas",
		"termino": "Pieza no localizada",
		"definicion": "Estatus 206 (Pieza no localizada).",
		"umbral": PENDIENTE,
		"causas": PENDIENTE,
		"accion": PENDIENTE,
	},
	{
		"intencion": "resumen",
		"termino": "Pendientes",
		"definicion": "Estatus vacío, fecha de recolección vacía y recolectable = 1.",
		"umbral": PENDIENTE,
		"causas": PENDIENTE,
		"accion": PENDIENTE,
	},
	{
		"intencion": "resumen",
		"termino": "Rezagada",
		"definicion": "Pieza recolectada en un mes posterior al de su ingreso.",
		"umbral": "",
		"causas": "",
		"accion": "",
	},
	{
		"intencion": "resumen",
		"termino": "Inconsistente",
		"definicion": "Pieza recolectada antes de su fecha de ingreso.",
		"umbral": "",
		"causas": PENDIENTE,
		"accion": "",
	},
	{
		"intencion": "general",
		"termino": "CDR",
		"definicion": "Taller (tipo de carga 10). Otros tipos: Agencia (11), Audavales (12), Equipo Pesado (13).",
		"umbral": "",
		"causas": "",
		"accion": "",
	},
	{
		"intencion": "general",
		"termino": "Ingresadas (volumen)",
		"definicion": "Piezas recolectables que ingresaron en el período.",
		"umbral": "",
		"causas": "",
		"accion": "",
	},
	{
		"intencion": "general",
		"termino": "Siniestro",
		"definicion": "Conteo de siniestros distintos en el período.",
		"umbral": "",
		"causas": PENDIENTE,
		"accion": "",
	},
]


def execute():
	s = frappe.get_single("Molduplastic IA Settings")
	cambio = False
	if not s.objetivo_efectividad:
		s.objetivo_efectividad = 75
		cambio = True
	if not s.presupuesto_analisis_seg:
		s.presupuesto_analisis_seg = 60
		cambio = True
	if not s.modo_respuesta:
		s.modo_respuesta = "Breve"
		cambio = True
	if not s.get("glosario"):
		for fila in GLOSARIO_INICIAL:
			s.append("glosario", {**fila, "estado": "Borrador"})
		cambio = True
	if cambio:
		s.save(ignore_permissions=True)
