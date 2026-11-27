"""Hooks de migrate: validación de dependencias y restauración de DocTypes.

`remove_orphan_doctypes` borra el registro de un DocType cuando su controlador
no importa. El hook `before_migrate` corta antes de ese borrado y el
`after_migrate` reimporta lo que ya se había perdido.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import instalacion

APPS = ["frappe", "fc_molduplastic_frappe_app"]


class TestVerificarEntorno(FrappeTestCase):
	def test_con_pkg_resources_continua(self):
		with patch.object(instalacion, "import_module") as importar:
			instalacion.verificar_dependencias_entorno()
			importar.assert_called_once_with("pkg_resources")

	def test_sin_pkg_resources_aborta_con_el_comando(self):
		with patch.object(
			instalacion,
			"import_module",
			side_effect=ImportError("No module named 'pkg_resources'"),
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				instalacion.verificar_dependencias_entorno()
		mensaje = str(ctx.exception)
		self.assertIn("setuptools", mensaje)
		self.assertIn("Dropbox", mensaje)


class TestVerificarDependencias(FrappeTestCase):
	def test_si_la_app_hermana_no_esta_instalada_no_hace_nada(self):
		with (
			patch.object(frappe, "get_installed_apps", return_value=["frappe"]),
			patch.object(instalacion, "import_module") as importar,
		):
			instalacion.verificar_dependencias_hermana()
			importar.assert_not_called()

	def test_con_las_dependencias_instaladas_continua(self):
		with (
			patch.object(frappe, "get_installed_apps", return_value=APPS),
			patch.object(instalacion, "import_module") as importar,
		):
			instalacion.verificar_dependencias_hermana()
			importar.assert_called_once_with("fc_molduplastic_frappe_app.utils")

	def test_si_el_controlador_no_importa_aborta_antes_del_borrado(self):
		with (
			patch.object(frappe, "get_installed_apps", return_value=APPS),
			patch.object(
				instalacion,
				"import_module",
				side_effect=ImportError("No module named 'thefuzz'"),
			),
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				instalacion.verificar_dependencias_hermana()
		mensaje = str(ctx.exception)
		self.assertIn("thefuzz", mensaje)
		self.assertIn("requirements.txt", mensaje)
		self.assertIn("borra Pieza", mensaje)


class TestRestaurarDoctypes(FrappeTestCase):
	def test_si_la_app_hermana_no_esta_instalada_no_hace_nada(self):
		with (
			patch.object(frappe, "get_installed_apps", return_value=["frappe"]),
			patch.object(frappe, "reload_doc") as recargar,
		):
			instalacion.restaurar_doctypes_hermana()
			recargar.assert_not_called()

	def test_si_estan_todos_no_toca_nada(self):
		with (
			patch.object(frappe, "get_installed_apps", return_value=APPS),
			patch.object(
				instalacion,
				"_doctypes_hermana",
				return_value=[("Fc Molduplastic Frappe App", "pieza", "Pieza")],
			),
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe, "reload_doc") as recargar,
			patch.object(instalacion, "_asegurar_indices"),
		):
			instalacion.restaurar_doctypes_hermana()
			recargar.assert_not_called()

	def test_reimporta_solo_los_que_faltan(self):
		existe = lambda doctype, name=None, **kwargs: name == "Taller"  # noqa: E731
		with (
			patch.object(frappe, "get_installed_apps", return_value=APPS),
			patch.object(
				instalacion,
				"_doctypes_hermana",
				return_value=[
					("Fc Molduplastic Frappe App", "pieza", "Pieza"),
					("Fc Molduplastic Frappe App", "taller", "Taller"),
				],
			),
			patch.object(frappe.db, "exists", side_effect=existe),
			patch.object(frappe, "reload_doc") as recargar,
			patch.object(frappe, "log_error"),
			patch.object(instalacion, "_asegurar_indices"),
			patch("builtins.print"),
		):
			instalacion.restaurar_doctypes_hermana()
		recargar.assert_called_once_with("Fc Molduplastic Frappe App", "doctype", "pieza", force=True)

	def test_si_una_recarga_falla_sigue_con_las_demas(self):
		with (
			patch.object(frappe, "get_installed_apps", return_value=APPS),
			patch.object(
				instalacion,
				"_doctypes_hermana",
				return_value=[
					("Fc Molduplastic Frappe App", "pieza", "Pieza"),
					("Fc Molduplastic Frappe App", "taller", "Taller"),
				],
			),
			patch.object(frappe.db, "exists", return_value=False),
			patch.object(
				frappe,
				"reload_doc",
				side_effect=[Exception("falla pieza"), None],
			) as recargar,
			patch.object(frappe, "log_error") as log,
			patch.object(instalacion, "_asegurar_indices"),
			patch("builtins.print"),
		):
			instalacion.restaurar_doctypes_hermana()
		self.assertEqual(recargar.call_count, 2)
		self.assertEqual(log.call_count, 2)
		titulos = [llamada.kwargs.get("title") for llamada in log.call_args_list]
		self.assertIn("IA: no se pudieron restaurar DocTypes", titulos)
		self.assertIn("IA: DocTypes de la app hermana restaurados", titulos)

	def test_encuentra_los_doctypes_de_la_app_hermana(self):
		if "fc_molduplastic_frappe_app" not in frappe.get_installed_apps():
			self.skipTest("sin app hermana")
		reglas = {(carpeta, nombre) for _, carpeta, nombre in instalacion._doctypes_hermana()}
		self.assertIn(("pieza", "Pieza"), reglas)
		self.assertIn(("taller", "Taller"), reglas)
		self.assertIn(("recoleccion", "Recoleccion"), reglas)


class TestIndices(FrappeTestCase):
	def test_sin_la_tabla_no_hace_nada(self):
		with (
			patch.object(frappe.db, "table_exists", return_value=False),
			patch.object(frappe.db, "sql_ddl") as ddl,
		):
			instalacion._asegurar_indices()
			ddl.assert_not_called()

	def test_no_crea_nada_si_ya_estan(self):
		with (
			patch.object(frappe.db, "table_exists", return_value=True),
			patch.object(instalacion, "_indice_existe", return_value=True),
			patch.object(frappe.db, "sql_ddl") as ddl,
		):
			instalacion._asegurar_indices()
			ddl.assert_not_called()

	def test_crea_solo_el_que_falta(self):
		existe = lambda nombre: nombre != "idx_ia_parte"  # noqa: E731
		with (
			patch.object(frappe.db, "table_exists", return_value=True),
			patch.object(instalacion, "_indice_existe", side_effect=existe),
			patch.object(frappe.db, "sql_ddl") as ddl,
			patch.object(frappe, "log_error"),
			patch("builtins.print"),
		):
			instalacion._asegurar_indices()
		ddl.assert_called_once()
		self.assertIn("idx_ia_parte", ddl.call_args.args[0])
		self.assertIn("ALGORITHM=INPLACE, LOCK=NONE", ddl.call_args.args[0])

	def test_los_indices_coinciden_con_el_sql(self):
		def normalizar(pares):
			return tuple((nombre, " ".join(columnas.split())) for nombre, columnas in pares)

		self.assertEqual(normalizar(instalacion.INDICES_IA), normalizar(instalacion.indices_del_sql()))
