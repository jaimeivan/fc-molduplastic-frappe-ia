"""Revisión del set de evaluación por el administrador (IA Evaluacion).

El administrador pregunta cada fila al asistente con el botón "Preguntar al
asistente", que usa su universo y el modo que esté configurado en ese momento, y
después marca si la respuesta analiza, sugiere con sustento o inventa cifras. Las
respuestas nunca se eligen a mano: las produce `ask()`.

Solo System Manager puede usar estas funciones.
"""

import csv
import os

import frappe

from fc_molduplastic_frappe_ia.molduplastic_ia import api

CRITERIOS = ("analiza", "sugiere_con_sustento", "inventa_cifras")


def _solo_administradores():
	# No se usa frappe.only_for: en pruebas (flags.in_test) no comprueba nada, y el
	# permiso tiene que probarse. Administrator tiene todos los roles.
	if frappe.session.user != "Administrator" and "System Manager" not in frappe.get_roles():
		raise frappe.PermissionError("Solo el administrador puede revisar el set de evaluación.")


@frappe.whitelist()
def preguntar_evaluacion(name):
	"""Pregunta la fila al asistente y guarda su respuesta. Reinicia la revisión."""
	_solo_administradores()
	doc = frappe.get_doc("IA Evaluacion", name)
	if not (doc.universo or "").strip():
		frappe.throw("Indica el universo (una aseguradora o TODAS) antes de preguntar.")
	previos = set(frappe.get_all("IA API Log", pluck="name"))
	res = api.ask(doc.pregunta, universo=doc.universo.strip())
	if not isinstance(res, dict) or not res.get("ok"):
		frappe.throw((res or {}).get("mensaje") or "El asistente no respondió.")
	nuevos = set(frappe.get_all("IA API Log", pluck="name")) - previos
	explicaciones = frappe.get_all(
		"IA API Log",
		filters={"name": ["in", list(nuevos)], "operacion": "explain"},
		fields=["modo_respuesta", "verificacion", "intencion"],
		order_by="fecha_hora desc",
	) if nuevos else []
	ultima = explicaciones[0] if explicaciones else {}
	doc.respuesta = res.get("respuesta") or res.get("mensaje") or ""
	doc.preguntas_sugeridas = "\n".join(res.get("preguntas_sugeridas") or [])
	doc.verificacion = ultima.get("verificacion") or ""
	doc.modo_configurado = ultima.get("modo_respuesta") or ""
	doc.intencion = ultima.get("intencion") or api.intencion_de_pregunta(doc.pregunta)
	doc.fecha_respuesta = frappe.utils.now_datetime()
	# Una respuesta nueva necesita una revisión nueva.
	doc.estado = "Pendiente"
	doc.revisor = None
	doc.fecha_revision = None
	for criterio in CRITERIOS:
		doc.set(criterio, 0)
	doc.save(ignore_permissions=True)
	return {"ok": True, "respuesta": doc.respuesta}


@frappe.whitelist()
def resumen_evaluacion():
	"""Conteos del set: cuántas revisadas y cuántas pasan cada criterio."""
	_solo_administradores()
	filas = frappe.get_all("IA Evaluacion", fields=["estado", "respuesta", *CRITERIOS])
	revisadas = [f for f in filas if f.estado == "Revisada"]
	n = len(revisadas)

	def pct(valor):
		return round(valor / n * 100, 1) if n else None

	return {
		"total": len(filas),
		"sin_respuesta": sum(1 for f in filas if not (f.respuesta or "").strip()),
		"pendientes": sum(1 for f in filas if f.estado != "Revisada"),
		"revisadas": n,
		"analiza_pct": pct(sum(1 for f in revisadas if f.analiza)),
		"sugiere_con_sustento_pct": pct(sum(1 for f in revisadas if f.sugiere_con_sustento)),
		"inventa_cifras_pct": pct(sum(1 for f in revisadas if f.inventa_cifras)),
	}


def sembrar_desde_csv():
	"""Carga las filas de evaluacion/preguntas_evaluacion.csv que falten (idempotente)."""
	app = frappe.get_app_path("fc_molduplastic_frappe_ia")
	ruta = os.path.join(os.path.dirname(app), "evaluacion", "preguntas_evaluacion.csv")
	if not os.path.exists(ruta):
		return 0
	creadas = 0
	with open(ruta, encoding="utf-8") as f:
		for fila in csv.DictReader(f):
			if frappe.db.exists("IA Evaluacion", fila["id"]):
				continue
			frappe.get_doc({
				"doctype": "IA Evaluacion",
				"id": fila["id"],
				"intencion": fila.get("intencion") or "",
				"pregunta": fila["pregunta"],
				"que_se_espera": fila.get("que_se_espera") or "",
				"estado": "Pendiente",
			}).insert(ignore_permissions=True)
			creadas += 1
	return creadas
