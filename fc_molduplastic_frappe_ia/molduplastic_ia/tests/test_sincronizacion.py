from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import sincronizacion


class TestHelpersSincronizacion(FrappeTestCase):
	def test_consulta_lote_de_carga_inicial_usa_paginacion_por_name(self):
		query, params = sincronizacion._consulta_lote(
			"tabPieza", ["name", "modified"], 500, True, cursor_name="P-100"
		)
		self.assertIn("WHERE `name` > %s ORDER BY `name` LIMIT %s", query)
		self.assertEqual(params, ("P-100", 500))

	def test_consulta_lote_incremental_usa_modified_y_name(self):
		query, params = sincronizacion._consulta_lote(
			"tabPieza",
			["name", "modified"],
			500,
			False,
			cursor_name="P-100",
			desde="2026-11-02 10:00:00",
			cursor_modified="2026-11-02 10:05:00",
		)
		self.assertIn("`modified` > %s OR (`modified` = %s AND `name` > %s)", query)
		self.assertEqual(
			params,
			("2026-11-02 10:00:00", "2026-11-02 10:05:00", "2026-11-02 10:05:00", "P-100", 500),
		)

	def test_nombre_de_tabla_no_acepta_sql(self):
		with self.assertRaises(frappe.ValidationError):
			sincronizacion._identificador("tabPieza`; DROP TABLE users; --")

	def test_nombre_unicode_y_espacio_se_delimita(self):
		self.assertEqual(sincronizacion._identificador("custom_notas_recolección"), "`custom_notas_recolección`")
		self.assertEqual(sincronizacion._identificador("tabCatalogo Datos"), "`tabCatalogo Datos`")

	@patch.object(sincronizacion, "_ejecutar_origen")
	def test_resuelve_dependencias_recursivas_de_las_vistas_configuradas(self, ejecutar):
		roots = [
			{"TABLE_NAME": "VW_PIEZAS", "VIEW_DEFINITION": "SELECT * FROM tabPieza JOIN vw_intermedia"},
			{"TABLE_NAME": "vwPiezas", "VIEW_DEFINITION": "SELECT * FROM tabTaller"},
			{
				"TABLE_NAME": "vw_Recoleccion",
				"VIEW_DEFINITION": "SELECT * FROM tabRecoleccion JOIN `tabRecolector-Talleres`",
			},
			{"TABLE_NAME": "vw_Recolecciones", "VIEW_DEFINITION": "SELECT * FROM tabRecoleccion"},
		]
		definitions = {
			"vw_intermedia": {"TABLE_NAME": "vw_intermedia", "VIEW_DEFINITION": "SELECT * FROM tabCatalogo"},
		}
		base_tables = {
			name.lower(): name
			for name in ("tabPieza", "tabTaller", "tabRecoleccion", "tabRecolector-Talleres", "tabCatalogo")
		}

		def responder(_conn, query, params=()):
			if "LOWER(TABLE_NAME) IN" in query:
				return roots
			if "FROM information_schema.TABLES" in query:
				name = params[1]
				if name == "vw_intermedia":
					return [{"TABLE_NAME": "vw_intermedia", "TABLE_TYPE": "VIEW"}]
				return [{"TABLE_NAME": base_tables[name], "TABLE_TYPE": "BASE TABLE"}] if name in base_tables else []
			if "FROM information_schema.VIEWS" in query:
				return [definitions[params[1]]] if params[1] in definitions else []
			self.fail(f"Consulta inesperada: {query}")

		ejecutar.side_effect = responder
		tables, dependencies = sincronizacion._tablas_de_vistas(object(), "origen")

		self.assertEqual(
			tables,
			sorted(
				{
					"tabPieza",
					"tabTaller",
					"tabRecoleccion",
					"tabCatalogo",
					"tabRecolector-Talleres",
				},
				key=str.lower,
			),
		)
		self.assertEqual(dependencies["VW_PIEZAS"], {"tabPieza", "tabCatalogo"})
		self.assertEqual(dependencies["vw_Recoleccion"], {"tabRecoleccion", "tabRecolector-Talleres"})
