"""Campo de fecha de ingreso y universo de Pieza.

El 2026-11-17 el dueño alineó el asistente con los tableros de BI:

- la fecha de ingreso es ``fecha_hora_carga`` (la fecha en que la pieza se cargó
  al sistema), no ``fechaingreso``;
- el universo son las piezas recolectables con una ruta válida
  (``recolectable = 1`` y ``rutas_validas = '1'``), igual que las vistas
  ``VW_PIEZAS`` / ``vwPiezas``.

``rutas_validas`` es un contador (0, 1, 2), no un booleano: se compara como
texto para que un índice pueda usarlo.
"""

# Campo de período de Pieza. El resto del código lo arma con esta constante.
CAMPO_INGRESO = "fecha_hora_carga"

# Campo de período por DocType (Taller no tiene fecha propia).
CAMPO_INGRESO_POR_DOCTYPE = {
	"Pieza": CAMPO_INGRESO,
	"Recoleccion": "fecha_inicio",
	"Taller": None,
}

# Condiciones que definen el universo de Pieza, iguales a las de VW_PIEZAS.
# Se inyectan en el WHERE del ejecutor para que ninguna medida ni conteo pueda
# salirse de la población publicada por BI.
UNIVERSO_PIEZA = (
	("recolectable", "=", 1),
	("rutas_validas", "=", "1"),
)

# Campo anterior. Solo se usa para reescribir specs guardados.
CAMPO_INGRESO_ANTERIOR = "fechaingreso"


def normalizar_ingreso(spec):
	"""Reescribe ``fechaingreso`` a ``fecha_hora_carga`` en un spec de Pieza.

	Las conversaciones y los reportes guardados traen el campo viejo en
	``filters``, en los ``filtros`` de cada agregación, en ``group_by``, en
	``fields``, en ``order_by`` y en las referencias ``{"campo": ...}``. Se
	reescriben todos para que esos specs sigan funcionando sin migración de
	datos. Los specs de Recoleccion y Taller no se tocan.
	"""
	if not isinstance(spec, dict) or spec.get("doctype") not in (None, "Pieza"):
		return spec
	return _renombrar(spec)


def _renombrar(nodo):
	if isinstance(nodo, dict):
		return {
			(CAMPO_INGRESO if clave == CAMPO_INGRESO_ANTERIOR else clave): _renombrar(valor)
			for clave, valor in nodo.items()
		}
	if isinstance(nodo, list):
		return [_renombrar(valor) for valor in nodo]
	if nodo == CAMPO_INGRESO_ANTERIOR:
		return CAMPO_INGRESO
	return nodo
