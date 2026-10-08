"""Lista blanca de DocTypes consultables por el asistente IA.

Se construye por introspección del DocType real (frappe.get_meta) y se
restringe a fieldtypes seguros. Resultado cacheado en Redis vía frappe.cache().
"""

import frappe

# DocTypes que el LLM puede ver y consultar. Cualquier otro se rechaza.
DOCTYPES_PERMITIDOS = ("Pieza", "Recoleccion", "Taller")

# Fieldtypes que nunca salen al LLM ni entran en specs (adjuntos, tablas, HTML...).
FIELDTYPES_PROHIBIDOS = {
	"Attach",
	"Attach Image",
	"Table",
	"Table MultiSelect",
	"HTML",
	"HTML Editor",
	"Image",
	"Signature",
	"Barcode",
	"Geolocation",
	"Password",
}

# Fieldtypes numéricos aptos para agregaciones sum/avg/min/max.
FIELDTYPES_NUMERICOS = {"Int", "Float", "Currency", "Percent"}

CACHE_KEY = "molduplastic_ia:whitelist"
CACHE_TTL = 24 * 60 * 60


def get_whitelist():
	"""Devuelve {doctype: {fields: {fieldname: {label, fieldtype}}, links: {...}}}."""
	whitelist = frappe.cache().get_value(CACHE_KEY)
	if whitelist:
		return whitelist
	whitelist = _construir_whitelist()
	frappe.cache().set_value(CACHE_KEY, whitelist, expires_in_sec=CACHE_TTL)
	return whitelist


def invalidar_whitelist():
	frappe.cache().delete_value(CACHE_KEY)


def _construir_whitelist():
	whitelist = {}
	for doctype in DOCTYPES_PERMITIDOS:
		meta = frappe.get_meta(doctype)
		fields = {}
		links = {}
		for df in meta.fields:
			if df.fieldtype in FIELDTYPES_PROHIBIDOS:
				continue
			if df.fieldtype in ("Section Break", "Column Break", "Tab Break", "Button", "Heading"):
				continue
			fields[df.fieldname] = {"label": df.label or df.fieldname, "fieldtype": df.fieldtype}
			if df.fieldtype == "Link" and df.options:
				links[df.fieldname] = df.options
		whitelist[doctype] = {"fields": fields, "links": links}
	return whitelist


def get_contexto_destilado():
	"""Texto compacto para el system prompt: solo doctypes, campos y relaciones."""
	whitelist = get_whitelist()
	lineas = []
	for doctype, info in whitelist.items():
		campos = ", ".join(
			f"{fn} ({v['fieldtype']})" for fn, v in sorted(info["fields"].items())
		)
		lineas.append(f"- {doctype}: {campos}")
		for campo, destino in sorted(info["links"].items()):
			lineas.append(f"  - {doctype}.{campo} -> {destino}")
	return "\n".join(lineas)
