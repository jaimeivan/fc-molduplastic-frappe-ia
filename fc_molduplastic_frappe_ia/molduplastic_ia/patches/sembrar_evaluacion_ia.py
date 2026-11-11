"""Carga inicial del set de evaluación en el Desk (idempotente)."""

import frappe

from fc_molduplastic_frappe_ia.molduplastic_ia.evaluacion import sembrar_desde_csv


def execute():
	sembrar_desde_csv()
	frappe.db.commit()
