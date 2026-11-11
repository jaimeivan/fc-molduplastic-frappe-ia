"""Umbrales iniciales del análisis (idempotente).

Valores de la decisión del dueño: mínimo de 50 piezas solo para porcentajes.
Caída de 5 puntos, concentración de 60 % y tendencia de 0 períodos (apagada por costo) son valores
iniciales de la fase 2; el administrador los ajusta en Molduplastic IA Settings.
"""

import frappe

INICIALES = {
	"min_piezas_porcentaje": 50,
	"umbral_caida_pp": 5,
	"umbral_concentracion_pct": 60,
	"tendencia_periodos": 0,
	"comparar_ano_anterior": 1,
}


def execute():
	s = frappe.get_single("Molduplastic IA Settings")
	cambio = False
	for campo, valor in INICIALES.items():
		if s.get(campo) in (None, ""):  # 0 es un valor válido para tendencia y el check
			s.set(campo, valor)
			cambio = True
	if cambio:
		s.save(ignore_permissions=True)
