"""Aislamiento por tenant (Aseguradora).

Fuente de verdad: User Permissions (allow=Aseguradora) del usuario en sesión.
Los System Manager / Administrator no se filtran (retornan None = sin filtro).
"""

import frappe


def get_user_aseguradoras(user=None):
	"""Names de Aseguradora visibles para el usuario.

	None = ve todas (admin). Lista vacía = no ve ninguna (denegar).
	"""
	user = user or frappe.session.user
	if user == "Administrator" or "System Manager" in (frappe.get_roles(user) or []):
		return None
	return list(
		frappe.get_all(
			"User Permission",
			filters={"user": user, "allow": "Aseguradora"},
			pluck="for_value",
		)
	)


def _literales(aseguradoras):
	"""Literales escapados para las query conditions (Frappe no admite params ahí)."""
	return ", ".join(frappe.db.escape(a) for a in aseguradoras)


def condicion_pieza(user=None):
	aseguradoras = get_user_aseguradoras(user)
	if aseguradoras is None:
		return ""
	if not aseguradoras:
		return "1 = 0"
	return f"`tabPieza`.`aseguradora` IN ({_literales(aseguradoras)})"


def condicion_recoleccion(user=None):
	"""Recoleccion no tiene link directo: filtra por las piezas de sus aseguradoras."""
	aseguradoras = get_user_aseguradoras(user)
	if aseguradoras is None:
		return ""
	if not aseguradoras:
		return "1 = 0"
	return (
		"`tabRecoleccion`.`name` IN (SELECT DISTINCT `id_recoleccion` FROM `tabPieza` "
		f"WHERE `aseguradora` IN ({_literales(aseguradoras)}) "
		"AND `id_recoleccion` IS NOT NULL)"
	)


def condicion_taller(user=None):
	"""Taller se filtra solo vía Pieza (taller_asignado + aseguradora).

	Subconsulta IN (materializada una vez) en lugar de EXISTS correlacionado:
	con 3M+ piezas, un probe por taller es inviable. Requiere los índices
	idx_ia_aseg_taller en tabPieza para ser index-only.
	"""
	aseguradoras = get_user_aseguradoras(user)
	if aseguradoras is None:
		return ""
	if not aseguradoras:
		return "1 = 0"
	return (
		"`tabTaller`.`name` IN (SELECT DISTINCT `taller_asignado` FROM `tabPieza` "
		f"WHERE `aseguradora` IN ({_literales(aseguradoras)}) "
		"AND `taller_asignado` IS NOT NULL)"
	)


def condicion_conversacion(user=None):
	user = user or frappe.session.user
	if user == "Administrator" or "System Manager" in (frappe.get_roles(user) or []):
		return ""
	return f"`tabIA Conversacion`.`owner` = {frappe.db.escape(user)}"


def condicion_reporte(user=None):
	user = user or frappe.session.user
	if user == "Administrator" or "System Manager" in (frappe.get_roles(user) or []):
		return ""
	return f"`tabIA Reporte`.`owner` = {frappe.db.escape(user)}"


def _en_tenant(aseguradora_valor, user):
	aseguradoras = get_user_aseguradoras(user)
	if aseguradoras is None:
		return True
	return str(aseguradora_valor) in [str(a) for a in aseguradoras]


def has_permission_pieza(doc, ptype="read", user=None):
	return _en_tenant(doc.get("aseguradora"), user or frappe.session.user)


def has_permission_recoleccion(doc, ptype="read", user=None):
	user = user or frappe.session.user
	if get_user_aseguradoras(user) is None:
		return True
	if not doc.get("name") or doc.get("__islocal"):
		return True  # la aseguradora se valida al guardar vía Pieza; creación restringida por rol
	return bool(
		frappe.db.exists(
			"Pieza",
			{"id_recoleccion": doc.get("name"), "aseguradora": ["in", get_user_aseguradoras(user)]},
		)
	)


def has_permission_taller(doc, ptype="read", user=None):
	"""Taller visible solo si alguna Pieza del tenant lo tiene asignado."""
	user = user or frappe.session.user
	aseguradoras = get_user_aseguradoras(user)
	if aseguradoras is None:
		return True
	if not aseguradoras:
		return False
	if not doc.get("name") or doc.get("__islocal"):
		return True
	return bool(
		frappe.db.exists(
			"Pieza", {"taller_asignado": doc.get("name"), "aseguradora": ["in", aseguradoras]}
		)
	)


def _alcance_efectivo(user, alcance):
	"""Lista de aseguradoras del alcance, o None si es total (admin/todas).

	Lanza PermissionError si el usuario pide un alcance que no le corresponde.
	"""
	propias = get_user_aseguradoras(user)
	if alcance in (None, ""):
		return propias
	if alcance == "TODAS":
		if propias is not None:
			raise frappe.PermissionError("Solo un administrador puede consultar todas las aseguradoras.")
		return None
	if propias is not None and str(alcance) not in [str(a) for a in propias]:
		raise frappe.PermissionError("No tienes acceso a esa aseguradora.")
	if not frappe.db.exists("Aseguradora", str(alcance)):
		raise frappe.PermissionError("Aseguradora inexistente.")
	return [str(alcance)]


def es_admin(user=None):
	return get_user_aseguradoras(user or frappe.session.user) is None


def lista_aseguradoras():
	return frappe.get_all("Aseguradora", fields=["name", "nombre"], order_by="nombre")


def get_tenant_scope(doctype, user=None, alcance=None):
	"""Filtro de tenant parametrizado para el executor. Retorna (where_sql, params).

	where_sql "" = sin filtro (admin/todas). "1 = 0" = acceso denegado.
	alcance: None = según permisos del usuario; "TODAS" = sin filtro (solo admin);
	"6" = solo esa aseguradora (admin o el propio tenant).
	"""
	aseguradoras = _alcance_efectivo(user, alcance)
	if aseguradoras is None:
		return "", []
	if not aseguradoras:
		return "1 = 0", []
	ph = ", ".join(["%s"] * len(aseguradoras))
	if doctype == "Pieza":
		return f"`tabPieza`.`aseguradora` IN ({ph})", list(aseguradoras)
	if doctype == "Recoleccion":
		return (
			"`tabRecoleccion`.`name` IN (SELECT DISTINCT `id_recoleccion` FROM `tabPieza` "
			f"WHERE `aseguradora` IN ({ph}) AND `id_recoleccion` IS NOT NULL)",
			list(aseguradoras),
		)
	if doctype == "Taller":
		return (
			"`tabTaller`.`name` IN (SELECT DISTINCT `taller_asignado` FROM `tabPieza` "
			f"WHERE `aseguradora` IN ({ph}) AND `taller_asignado` IS NOT NULL)",
			list(aseguradoras),
		)
	frappe.throw(f"DocType fuera de la lista blanca: {doctype}")
