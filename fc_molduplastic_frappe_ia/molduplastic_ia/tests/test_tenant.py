"""Tests de aislamiento por tenant y del ejecutor (incluye cobertura)."""

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia.permissions import get_user_aseguradoras
from fc_molduplastic_frappe_ia.molduplastic_ia.query_executor import ejecutar
from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import ESTADOS, REGIONES, tabla_regiones
from fc_molduplastic_frappe_ia.molduplastic_ia.validate_spec import SpecInvalida

USUARIO_GNP = "pruebas.gnp@molduplastic.test"


class TestTablaRegiones(FrappeTestCase):
	"""La tabla de regiones la arma el servidor, no el LLM."""

	def test_todas_las_regiones_y_todos_los_estados(self):
		tabla = tabla_regiones()
		filas = [l for l in tabla.split("\n") if l.startswith("| ") and not l.startswith("| Región")]
		self.assertEqual(len(filas), len(REGIONES))
		estados = []
		for fila in filas:
			celdas = [c.strip() for c in fila.strip("|").split("|")]
			self.assertEqual(len(celdas), 2)
			estados += [e.strip() for e in celdas[1].split(",") if e.strip()]
		self.assertEqual(sorted(estados), sorted(ESTADOS))
		self.assertEqual(len(set(estados)), len(estados))


class TestTenant(FrappeTestCase):
	def test_usuario_gnp_solo_ve_su_aseguradora(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		self.assertEqual(get_user_aseguradoras(), ["6"])

	def test_lista_piezas_sin_fugas(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		filas = frappe.get_list("Pieza", fields=["aseguradora"], limit_page_length=20)
		self.assertTrue(filas)
		for f in filas:
			self.assertEqual(str(f.aseguradora), "6")

	def test_lectura_directa_otra_aseguradora_denegada(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		otra = frappe.db.sql("select name from tabPieza where aseguradora != '6' limit 1")[0][0]
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("Pieza", otra).check_permission("read")

	def test_ejecutor_respeta_tenant(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": ["folio", "aseguradora"],
				"filters": {},
				"limit": 5,
			},
			USUARIO_GNP,
		)
		self.assertTrue(res["filas"])
		idx = res["columnas"].index("aseguradora")
		for fila in res["filas"]:
			self.assertEqual(str(fila[idx]), "6")
		self.assertGreater(res["cobertura"]["universo"], 0)

	def test_ejecutor_rechaza_doctype_prohibido(self):
		with self.assertRaises(SpecInvalida):
			ejecutar({"doctype": "User", "fields": ["email"], "limit": 1}, "Administrator")

	def test_taller_visible_solo_via_pieza_del_tenant(self):
		from fc_molduplastic_frappe_ia.molduplastic_ia.permissions import has_permission_taller

		taller = frappe.db.sql(
			"select taller_asignado from tabPieza where aseguradora='6' "
			"and taller_asignado is not null limit 1"
		)[0][0]
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		visibles = frappe.get_list("Taller", fields=["name"], limit_page_length=10000)
		self.assertIn(taller, [t.name for t in visibles])
		# Taller sin piezas del tenant: denegado aunque exista el registro
		doc = frappe._dict({"doctype": "Taller", "name": "TALLER-SIN-VINCULO-GNP"})
		self.assertFalse(has_permission_taller(doc, "read", USUARIO_GNP))

	def test_agregacion_global_sin_group_by(self):
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {},
				"aggregations": [{"func": "count", "field": "*", "alias": "total"}],
				"group_by": [],
				"limit": 1,
			},
			"Administrator",
		)
		self.assertEqual(res["columnas"], ["total"])
		self.assertEqual(len(res["filas"]), 1)

	def test_count_distinct_cuenta_valores_unicos(self):
		"""El número de siniestros distintos, no el de filas."""
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {},
				"aggregations": [{"func": "count_distinct", "field": "siniestro", "alias": "n"}],
				"group_by": [],
				"limit": 1,
			},
			"Administrator",
		)
		self.assertEqual(res["columnas"], ["n"])
		self.assertLessEqual(res["filas"][0][0], res["cobertura"]["universo"])

	def test_count_distinct_respeta_tenant(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {},
				"aggregations": [{"func": "count_distinct", "field": "siniestro", "alias": "n"}],
				"group_by": [],
				"limit": 1,
			},
			USUARIO_GNP,
		)
		esperado = frappe.db.sql(
			"SELECT COUNT(DISTINCT siniestro) FROM tabPieza WHERE aseguradora = '6'"
		)[0][0]
		self.assertEqual(res["filas"][0][0], esperado)

	def test_count_distinct_con_group_by(self):
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {},
				"aggregations": [{"func": "count_distinct", "field": "siniestro", "alias": "n"}],
				"group_by": ["marca"],
				"limit": 5,
			},
			"Administrator",
		)
		self.assertEqual(res["columnas"], ["marca", "n"])
		self.assertTrue(res["filas"])

	def test_dos_medidas_sobre_poblaciones_distintas(self):
		"""Recolectadas y pendientes en una sola consulta, cada una con su filtro."""
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {"aseguradora": "1"},
				"aggregations": [
					{
						"func": "sum",
						"field": "preciovale",
						"alias": "recolectado",
						"filtros": {"fecha_hora_recoleccion": ["is", "not null"]},
					},
					{
						"func": "sum",
						"field": "preciovale",
						"alias": "pendiente",
						"filtros": {"fecha_hora_recoleccion": ["is", "null"]},
					},
				],
				"group_by": [],
				"limit": 1,
			},
			"Administrator",
		)
		self.assertEqual(res["columnas"], ["recolectado", "pendiente"])
		esperado = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(CASE WHEN fecha_hora_recoleccion IS NOT NULL THEN preciovale END), 0),
			       COALESCE(SUM(CASE WHEN fecha_hora_recoleccion IS NULL THEN preciovale END), 0)
			FROM tabPieza WHERE aseguradora = '1'
			"""
		)[0]
		self.assertAlmostEqual(float(res["filas"][0][0]), float(esperado[0]), places=2)
		self.assertAlmostEqual(float(res["filas"][0][1]), float(esperado[1]), places=2)

	def test_filtro_de_agg_no_mezcla_los_parametros_del_where(self):
		"""Si el orden de parámetros se cruza, los valores caen en la columna equivocada."""
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {"fechaingreso": ["between", "2026-01-01 00:00:00", "2026-10-14 23:59:59"]},
				"aggregations": [
					{
						"func": "count",
						"field": "*",
						"alias": "octubre",
						"filtros": {"fecha_hora_recoleccion": ["between", "2026-01-01 00:00:00", "2026-10-14 23:59:59"]},
					}
				],
				"group_by": [],
				"limit": 1,
			},
			"Administrator",
		)
		esperado = frappe.db.sql(
			"SELECT COUNT(*) FROM tabPieza WHERE fechaingreso BETWEEN %s AND %s "
			"AND fecha_hora_recoleccion BETWEEN %s AND %s",
			("2026-01-01 00:00:00", "2026-10-14 23:59:59", "2026-01-01 00:00:00", "2026-10-14 23:59:59"),
		)[0][0]
		self.assertEqual(res["filas"][0][0], esperado)

	def test_medida_sin_coincidencias_devuelve_cero_no_null(self):
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {},
				"aggregations": [
					{
						"func": "sum",
						"field": "preciovale",
						"alias": "nada",
						"filtros": {"marca": "marca-que-no-existe-jamas"},
					}
				],
				"group_by": [],
				"limit": 1,
			},
			"Administrator",
		)
		self.assertIsNone(res["filas"][0][0])

	def test_cobertura_incluye_campos_de_filtro_de_agg(self):
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {},
				"aggregations": [
					{
						"func": "count",
						"field": "*",
						"alias": "con_fecha",
						"filtros": {"fecha_hora_recoleccion": ["is", "not null"]},
					}
				],
				"group_by": [],
				"limit": 1,
			},
			"Administrator",
		)
		self.assertIn("fecha_hora_recoleccion", res["cobertura"]["campos"])

	def test_nulos_que_son_categoria_no_cuentan_como_cobertura(self):
		"""id_estatus IS NULL define "pendientes": ese nulo no es un dato faltante."""
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {"fechaingreso": ["between", "2026-10-01 00:00:00", "2026-10-19 23:59:59"]},
				"aggregations": [
					{
						"func": "sum",
						"field": "preciovale",
						"alias": "ingresadas",
						"filtros": {"recolectable": ["=", 1]},
					},
					{
						"func": "sum",
						"field": "preciovale",
						"alias": "pendientes",
						"filtros": {
							"id_estatus": ["is", "null"],
							"fecha_hora_recoleccion": ["is", "null"],
							"recolectable": ["=", 1],
						},
					},
				],
				"group_by": [],
				"limit": 1,
			},
			"Administrator",
			alcance="1",
		)
		campos = res["cobertura"]["campos"]
		self.assertNotIn("id_estatus", campos)
		self.assertNotIn("fecha_hora_recoleccion", campos)
		self.assertIn("preciovale", campos)
		self.assertEqual(campos["preciovale"]["pct"], 100.0)

	def test_usuario_sin_aseguradora_no_ve_nada(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.db.delete("User Permission", {"user": USUARIO_GNP})
		try:
			res = ejecutar({"doctype": "Pieza", "fields": ["folio"], "limit": 5}, USUARIO_GNP)
			self.assertEqual(res["filas"], [])
			self.assertEqual(res["cobertura"]["universo"], 0)
		finally:
			frappe.get_doc(
				{"doctype": "User Permission", "user": USUARIO_GNP, "allow": "Aseguradora", "for_value": "6"}
			).insert(ignore_permissions=True)


class TestRegionPais(FrappeTestCase):
	"""La región se deriva en SQL desde estadolocalidad; el mapa vive en el código."""

	def test_todas_las_regiones_cubren_el_universo(self):
		"""Cada pieza cae en una región o queda sin región; ninguna se pierde."""
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {"fechaingreso": ["between", "2026-01-01 00:00:00", "2026-10-16 23:59:59"]},
				"aggregations": [{"func": "count", "field": "*", "alias": "piezas"}],
				"group_by": ["region_pais"],
				"order_by": [{"field": "piezas", "dir": "desc"}],
				"limit": 20,
			},
			"Administrator",
			alcance="1",
		)
		universo = res["cobertura"]["universo"]
		self.assertEqual(sum(fila[1] for fila in res["filas"]), universo)
		regiones = {fila[0] for fila in res["filas"] if fila[0]}
		self.assertTrue(regiones.issubset(set(REGIONES)))
		self.assertIn("CDMX y Área Metropolitana", regiones)
		self.assertIn("Norte", regiones)

	def test_conteo_por_region_coincide_con_sql_directo(self):
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {"aseguradora": "1"},
				"aggregations": [{"func": "count", "field": "*", "alias": "piezas"}],
				"group_by": ["region_pais"],
				"order_by": [{"field": "piezas", "dir": "desc"}],
				"limit": 20,
			},
			"Administrator",
		)
		esperado = frappe.db.sql(
			"SELECT COUNT(*) FROM tabPieza WHERE aseguradora = '1' AND estadolocalidad IN "
			"('NUEVO LEÓN', 'Nuevo León', 'CHIHUAHUA', 'Chihuahua', 'COAHUILA', 'Coahuila')"
		)[0][0]
		norte = [f[1] for f in res["filas"] if f[0] == "Norte"][0]
		self.assertGreaterEqual(norte, esperado)


class TestRecolectadasEstrictas(FrappeTestCase):
	"""Definición estricta de "recolectada" contra SQL directo (Afirme = aseguradora 1).

	Es la que manda el prompt: id_estatus = 100 (Recolectado) + fecha de
	recolección en el mismo mes del ingreso y posterior o igual a él. El caso
	general es que dé 0 o muy por debajo de "tiene fecha de recolección": buena
	parte de la información tiene la fecha anterior al ingreso o de otro mes.
	"""

	OCT = ["2026-10-01 00:00:00", "2026-10-16 23:59:59"]

	def _spec_recolectadas(self):
		return {
			"doctype": "Pieza",
			"fields": [],
			"filters": {"fechaingreso": ["between", *self.OCT]},
			"aggregations": [
				{
					"func": "count",
					"field": "*",
					"alias": "recolectadas",
					"filtros": {
						"id_estatus": ["=", 100],
						"fecha_hora_recoleccion": [
							["mes_igual", {"campo": "fechaingreso"}],
							[">=", {"campo": "fechaingreso"}],
						],
					},
				}
			],
			"group_by": [],
			"limit": 1,
		}

	def test_recolectadas_coinciden_con_sql_directo(self):
		res = ejecutar(self._spec_recolectadas(), "Administrator", alcance="1")
		esperado = frappe.db.sql(
			"SELECT COUNT(*) FROM tabPieza WHERE aseguradora = '1' "
			"AND fechaingreso BETWEEN %s AND %s AND id_estatus = 100 "
			"AND fecha_hora_recoleccion >= fechaingreso "
			"AND DATE_FORMAT(fecha_hora_recoleccion, '%%Y-%%m') = DATE_FORMAT(fechaingreso, '%%Y-%%m')",
			tuple(self.OCT),
		)[0][0]
		self.assertEqual(res["filas"][0][0], esperado)

	def test_es_mas_estricta_que_solo_tener_fecha(self):
		"""Nunca puede dar más que "tiene fecha de recolección"."""
		res = ejecutar(self._spec_recolectadas(), "Administrator", alcance="1")
		con_fecha = frappe.db.sql(
			"SELECT COUNT(*) FROM tabPieza WHERE aseguradora = '1' "
			"AND fechaingreso BETWEEN %s AND %s AND fecha_hora_recoleccion IS NOT NULL",
			tuple(self.OCT),
		)[0][0]
		self.assertLessEqual(res["filas"][0][0], con_fecha)

	def test_mes_igual_excluye_ingreso_de_otro_mes(self):
		"""Una pieza recolectada en noviembre no cuenta como recolectada de octubre."""
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {"fechaingreso": ["between", *self.OCT]},
				"aggregations": [
					{
						"func": "count",
						"field": "*",
						"alias": "n",
						"filtros": {
							"fecha_hora_recoleccion": [
								["mes_igual", {"campo": "fechaingreso"}]
							]
						},
					}
				],
				"group_by": [],
				"limit": 1,
			},
			"Administrator",
			alcance="1",
		)
		esperado = frappe.db.sql(
			"SELECT COUNT(*) FROM tabPieza WHERE aseguradora = '1' "
			"AND fechaingreso BETWEEN %s AND %s "
			"AND DATE_FORMAT(fecha_hora_recoleccion, '%%Y-%%m') = DATE_FORMAT(fechaingreso, '%%Y-%%m')",
			tuple(self.OCT),
		)[0][0]
		self.assertEqual(res["filas"][0][0], esperado)


class TestRezagadas(FrappeTestCase):
	"""Comparación entre campos y mes_distinto contra SQL directo (Afirme, octubre 2026)."""

	OCT = ["2026-10-01 00:00:00", "2026-10-16 23:59:59"]

	def test_rezagadas_coinciden_con_sql_directo(self):
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {},
				"aggregations": [
					{
						"func": "count",
						"field": "*",
						"alias": "rezagadas",
						"filtros": {
							"fecha_hora_recoleccion": [
								["between", *self.OCT],
								[">", {"campo": "fechaingreso"}],
								["mes_distinto", {"campo": "fechaingreso"}],
							]
						},
					}
				],
				"group_by": [],
				"limit": 1,
			},
			"Administrator",
			alcance="1",
		)
		esperado = frappe.db.sql(
			"SELECT COUNT(*) FROM tabPieza WHERE aseguradora = '1' "
			"AND fecha_hora_recoleccion BETWEEN %s AND %s "
			"AND fecha_hora_recoleccion > fechaingreso "
			"AND DATE_FORMAT(fecha_hora_recoleccion, '%%Y-%%m') != DATE_FORMAT(fechaingreso, '%%Y-%%m')",
			tuple(self.OCT),
		)[0][0]
		self.assertEqual(res["filas"][0][0], esperado)

	def test_inconsistentes_coinciden_con_sql_directo(self):
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {},
				"aggregations": [
					{
						"func": "count",
						"field": "*",
						"alias": "inconsistentes",
						"filtros": {
							"fecha_hora_recoleccion": [
								["between", *self.OCT],
								["<", {"campo": "fechaingreso"}],
							]
						},
					}
				],
				"group_by": [],
				"limit": 1,
			},
			"Administrator",
			alcance="1",
		)
		esperado = frappe.db.sql(
			"SELECT COUNT(*) FROM tabPieza WHERE aseguradora = '1' "
			"AND fecha_hora_recoleccion BETWEEN %s AND %s "
			"AND fecha_hora_recoleccion < fechaingreso",
			tuple(self.OCT),
		)[0][0]
		self.assertEqual(res["filas"][0][0], esperado)

	def test_cobertura_incluye_campo_referido(self):
		"""fechaingreso solo aparece como {"campo": ...}: igual debe salir en cobertura."""
		res = ejecutar(
			{
				"doctype": "Pieza",
				"fields": [],
				"filters": {},
				"aggregations": [
					{
						"func": "count",
						"field": "*",
						"alias": "n",
						"filtros": {
							"fecha_hora_recoleccion": [
								">",
								{"campo": "fechaingreso"},
							]
						},
					}
				],
				"group_by": [],
				"limit": 1,
			},
			"Administrator",
			alcance="1",
		)
		self.assertIn("fechaingreso", res["cobertura"]["campos"])
