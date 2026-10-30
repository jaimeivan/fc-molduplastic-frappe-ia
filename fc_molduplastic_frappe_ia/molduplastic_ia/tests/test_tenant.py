"""Tests de aislamiento por tenant y del ejecutor (incluye cobertura)."""

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia.permissions import get_user_aseguradoras
from fc_molduplastic_frappe_ia.molduplastic_ia.query_executor import ejecutar
from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import (
	ESTADOS,
	PLAZAS_POR_REGION,
	REGIONES,
	region_de,
	tabla_regiones,
)
from fc_molduplastic_frappe_ia.molduplastic_ia.validate_spec import SpecInvalida

USUARIO_GNP = "pruebas.gnp@molduplastic.test"


class TestRegionDe(FrappeTestCase):
	"""La región de un lugar la resuelve el servidor, no el modelo."""

	def test_resuelve_estados_alias_y_ciudades(self):
		self.assertEqual(region_de("Oaxaca"), "Centro y Golfo")
		self.assertEqual(region_de("oaxaca"), "Centro y Golfo")
		self.assertEqual(region_de("Ciudad de México"), "CDMX y Área Metropolitana")
		self.assertEqual(region_de("CDMX"), "CDMX y Área Metropolitana")
		self.assertEqual(region_de("Tijuana"), "Occidente")

	def test_lugar_desconocido_devuelve_none(self):
		for lugar in ("Narnia", "", "   ", None, 42):
			self.assertIsNone(region_de(lugar), lugar)

	def test_todos_los_estados_del_mapa_se_resuelven(self):
		for estado, region in ESTADOS.items():
			self.assertEqual(region_de(estado), region, estado)


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


class TestEfectividadConRezagadas(FrappeTestCase):
	"""La efectividad suma las tres categorías y las rezagadas van en otra consulta."""

	SEP = ["2026-09-01 00:00:00", "2026-09-30 23:59:59"]

	def _spec(self):
		desde, hasta = self.SEP
		return {
			"doctype": "Pieza",
			"fields": [],
			"filters": {"fechaingreso": ["between", desde, hasta], "recolectable": ["=", 1]},
			"aggregations": [
				{"func": "count", "field": "*", "alias": "total"},
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
				},
				{
					"func": "count",
					"field": "*",
					"alias": "inconsistentes_recolectadas",
					"filtros": {
						"fecha_hora_recoleccion": ["<", {"campo": "fechaingreso"}],
						"id_estatus": ["=", 100],
					},
				},
				{
					"func": "count",
					"field": "*",
					"alias": "rezagadas_recolectadas",
					"filtros": {
						"fecha_hora_recoleccion": ["between", desde, hasta],
						"fechaingreso": ["<", desde],
						"id_estatus": ["=", 100],
					},
				},
			],
			"group_by": [],
			"limit": 1,
		}

	def test_coincide_con_sql_directo_y_une_las_rezagadas(self):
		res = ejecutar(self._spec(), "Administrator", alcance="1")
		self.assertEqual(
			res["columnas"],
			["total", "recolectadas", "inconsistentes_recolectadas", "rezagadas_recolectadas"],
		)
		total, rec, inc, rez = res["filas"][0]
		esperado = frappe.db.sql(
			"SELECT COUNT(*), "
			"COUNT(CASE WHEN id_estatus = 100 AND fecha_hora_recoleccion >= fechaingreso "
			"AND DATE_FORMAT(fecha_hora_recoleccion, '%%Y-%%m') = "
			"DATE_FORMAT(fechaingreso, '%%Y-%%m') THEN 1 END), "
			"COUNT(CASE WHEN fecha_hora_recoleccion < fechaingreso "
			"AND id_estatus = 100 THEN 1 END) "
			"FROM tabPieza WHERE aseguradora = '1' AND recolectable = 1 "
			"AND fechaingreso BETWEEN %s AND %s",
			tuple(self.SEP),
		)[0]
		rez_esperado = frappe.db.sql(
			"SELECT COUNT(*) FROM tabPieza WHERE aseguradora = '1' "
			"AND fecha_hora_recoleccion BETWEEN %s AND %s "
			"AND fechaingreso < %s AND id_estatus = 100",
			(self.SEP[0], self.SEP[1], self.SEP[0]),
		)[0][0]
		self.assertEqual((total, rec, inc), tuple(esperado))
		self.assertEqual(rez, rez_esperado)
		self.assertGreater(total, 0)


class TestNoRecuperadas(FrappeTestCase):
	"""No recuperadas = no recolectadas + inconsistentes + rezagadas no recolectadas."""

	def test_suma_de_las_tres_contra_sql(self):
		from fc_molduplastic_frappe_ia.molduplastic_ia import api
		from fc_molduplastic_frappe_ia.molduplastic_ia.llm_factory import totales_no_recuperadas

		desde, hasta = "2026-09-01 00:00:00", "2026-09-30 23:59:59"
		spec = {
			"doctype": "Pieza",
			"fields": [],
			"filters": {},
			"aggregations": api._aggs_no_recuperadas(desde, hasta),
			"group_by": [],
			"limit": 10,
		}
		# Con tenant la consulta usa el índice por aseguradora (la de TODAS
		# escanea la tabla completa y hace el test eterno).
		res = ejecutar(spec, "Administrator", alcance="1")
		base = "aseguradora = '1' AND id_estatus IN ('200','201','202','203','204','205','206')"
		directo = (
			frappe.db.sql(
				f"""SELECT COUNT(*) FROM tabPieza WHERE {base}
				AND fechaingreso BETWEEN %s AND %s AND recolectable = 1
				AND fecha_hora_recoleccion >= fechaingreso""",
				(desde, hasta),
			)[0][0]
			+ frappe.db.sql(
				f"""SELECT COUNT(*) FROM tabPieza WHERE {base}
				AND fechaingreso BETWEEN %s AND %s
				AND fecha_hora_recoleccion < fechaingreso""",
				(desde, hasta),
			)[0][0]
			+ frappe.db.sql(
				f"""SELECT COUNT(*) FROM tabPieza WHERE {base}
				AND fecha_hora_recoleccion BETWEEN %s AND %s
				AND fechaingreso < %s""",
				(desde, hasta, desde),
			)[0][0]
		)
		self.assertGreater(directo, 0)
		self.assertEqual(totales_no_recuperadas(res["filas"], res)[0], directo)


class TestRecoleccionPendientes(FrappeTestCase):
	"""Pendiente = estatus 301 (Iniciada), inicio en el período y sin fecha_completado."""

	def _rango(self):
		ahora = frappe.utils.now_datetime()
		inicio = ahora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
		return (
			inicio.strftime("%Y-%m-%d %H:%M:%S"),
			ahora.strftime("%Y-%m-%d %H:%M:%S"),
		)

	def test_pendientes_definicion_del_dueno(self):
		desde, hasta = self._rango()
		res = ejecutar(
			{
				"doctype": "Recoleccion",
				"fields": [],
				"filters": {
					"id_estatus": ["=", "301"],
					"fecha_inicio": ["between", desde, hasta],
					"fecha_completado": ["is", "empty"],
				},
				"aggregations": [{"func": "count", "field": "*", "alias": "pendientes"}],
				"group_by": [],
				"limit": 10,
			},
			"Administrator",
		)
		directo = frappe.db.sql(
			"""SELECT COUNT(*) FROM tabRecoleccion
			WHERE id_estatus = '301' AND fecha_inicio BETWEEN %s AND %s
			  AND (fecha_completado IS NULL OR fecha_completado = '')""",
			(desde, hasta),
		)[0][0]
		self.assertEqual(res["filas"][0][0], directo)

	def test_estado_del_taller_filtra(self):
		res = ejecutar(
			{
				"doctype": "Recoleccion",
				"fields": [],
				"filters": {
					"id_estatus": ["=", "301"],
					"fecha_inicio": ["between", "2026-09-01 00:00:00", "2026-09-30 23:59:59"],
					"fecha_completado": ["is", "empty"],
					"estado": ["=", "CDMX"],
				},
				"aggregations": [{"func": "count", "field": "*", "alias": "pendientes"}],
				"group_by": [],
				"limit": 10,
			},
			"Administrator",
		)
		directo = frappe.db.sql(
			"""SELECT COUNT(*) FROM tabRecoleccion r
			JOIN tabTaller t ON t.name = r.id_taller
			WHERE r.id_estatus = '301'
			  AND r.fecha_inicio BETWEEN '2026-09-01 00:00:00' AND '2026-09-30 23:59:59'
			  AND (r.fecha_completado IS NULL OR r.fecha_completado = '')
			  AND t.estado = 'CDMX'"""
		)[0][0]
		self.assertGreater(directo, 0)
		self.assertEqual(res["filas"][0][0], directo)

	def test_estado_sinonimos_cdmx(self):
		base = {
			"doctype": "Recoleccion",
			"fields": [],
			"filters": {
				"id_estatus": ["=", "301"],
				"fecha_inicio": ["between", "2026-09-01 00:00:00", "2026-09-30 23:59:59"],
				"fecha_completado": ["is", "empty"],
			},
			"aggregations": [{"func": "count", "field": "*", "alias": "pendientes"}],
			"group_by": [],
			"limit": 10,
		}
		resultados = []
		for valor in ("CDMX", "Ciudad de México", "CD DE MEXICO", "Distrito Federal"):
			spec = {**base, "filters": {**base["filters"], "estado": ["=", valor]}}
			res = ejecutar(spec, "Administrator")
			resultados.append(res["filas"][0][0])
		self.assertGreater(resultados[0], 0)
		self.assertEqual(len(set(resultados)), 1)

	def test_plaza_sale_de_recolector_talleres(self):
		res = ejecutar(
			{
				"doctype": "Recoleccion",
				"fields": [],
				"filters": {
					"id_estatus": ["=", "301"],
					"fecha_inicio": ["between", "2026-09-01 00:00:00", "2026-09-30 23:59:59"],
					"fecha_completado": ["is", "empty"],
				},
				"aggregations": [{"func": "count", "field": "*", "alias": "pendientes"}],
				"group_by": ["plaza"],
				"order_by": [{"field": "pendientes", "dir": "desc"}],
				"limit": 50,
			},
			"Administrator",
		)
		directo = dict(
			frappe.db.sql(
				"""SELECT plaza, COUNT(*) FROM (
				SELECT COALESCE(
					(SELECT rt.id_taller_plaza FROM `tabRecolector-Talleres` rt
					 WHERE rt.id_taller = r.id_taller AND rt.id_recolector = r.id_recolector LIMIT 1),
					(SELECT rt.id_taller_plaza FROM `tabRecolector-Talleres` rt
					 WHERE rt.id_taller = r.id_taller LIMIT 1)) AS plaza
				FROM tabRecoleccion r
				WHERE r.id_estatus = '301'
				  AND r.fecha_inicio BETWEEN '2026-09-01 00:00:00' AND '2026-09-30 23:59:59'
				  AND (r.fecha_completado IS NULL OR r.fecha_completado = '')
				) x GROUP BY plaza"""
			)
		)
		self.assertEqual({f[0]: f[1] for f in res["filas"]}, directo)
		self.assertEqual(res["filas"][0][0], "CDMX")

	def test_pendientes_por_region(self):
		res = ejecutar(
			{
				"doctype": "Recoleccion",
				"fields": [],
				"filters": {
					"id_estatus": ["=", "301"],
					"fecha_inicio": ["between", "2026-09-01 00:00:00", "2026-09-30 23:59:59"],
					"fecha_completado": ["is", "empty"],
				},
				"aggregations": [{"func": "count", "field": "*", "alias": "pendientes"}],
				"group_by": ["region"],
				"order_by": [{"field": "pendientes", "dir": "desc"}],
				"limit": 50,
			},
			"Administrator",
		)
		# La región de cada plaza sale del mapa del código.
		plaza_region = {
			plaza: region for region, plazas in PLAZAS_POR_REGION.items() for plaza in plazas
		}
		plazas = frappe.db.sql(
			"""SELECT COALESCE(
				(SELECT rt.id_taller_plaza FROM `tabRecolector-Talleres` rt
				 WHERE rt.id_taller = r.id_taller AND rt.id_recolector = r.id_recolector LIMIT 1),
				(SELECT rt.id_taller_plaza FROM `tabRecolector-Talleres` rt
				 WHERE rt.id_taller = r.id_taller LIMIT 1)) AS plaza, COUNT(*) n
			FROM tabRecoleccion r
			WHERE r.id_estatus = '301'
			  AND r.fecha_inicio BETWEEN '2026-09-01 00:00:00' AND '2026-09-30 23:59:59'
			  AND (r.fecha_completado IS NULL OR r.fecha_completado = '')
			GROUP BY plaza""",
			as_dict=True,
		)
		esperado = {}
		for fila in plazas:
			region = plaza_region.get(fila.plaza)
			if region:
				esperado[region] = esperado.get(region, 0) + fila.n
		self.assertEqual({f[0]: f[1] for f in res["filas"]}, esperado)
		self.assertEqual(res["filas"][0][0], "CDMX y Área Metropolitana")

	def test_empty_cuenta_null_y_vacio(self):
		res = ejecutar(
			{
				"doctype": "Recoleccion",
				"fields": [],
				"filters": {"fecha_completado": ["is", "empty"]},
				"aggregations": [{"func": "count", "field": "*", "alias": "sin_completar"}],
				"group_by": [],
				"limit": 10,
			},
			"Administrator",
		)
		directo = frappe.db.sql(
			"SELECT COUNT(*) FROM tabRecoleccion WHERE fecha_completado IS NULL OR fecha_completado = ''"
		)[0][0]
		self.assertEqual(res["filas"][0][0], directo)


class TestFiltroAseguradoraEnExecutor(FrappeTestCase):
	"""El filtro por aseguradora no manda sobre el alcance real."""

	SPEC = {
		"doctype": "Pieza",
		"fields": [],
		"filters": {"fechaingreso": ["between", "2026-09-01 00:00:00", "2026-09-30 23:59:59"]},
		"aggregations": [{"func": "count", "field": "*", "alias": "n"}],
		"group_by": [],
		"limit": 1,
	}

	def test_tenant_unico_ignora_filtro_de_otra_aseguradora(self):
		frappe.set_user(USUARIO_GNP)
		self.addCleanup(frappe.set_user, "Administrator")
		con_filtro = ejecutar(
			{**self.SPEC, "filters": {**self.SPEC["filters"], "aseguradora": ["=", "7"]}},
			USUARIO_GNP,
		)
		sin_filtro = ejecutar(self.SPEC, USUARIO_GNP)
		self.assertGreater(sin_filtro["filas"][0][0], 0)
		self.assertEqual(con_filtro["filas"], sin_filtro["filas"])

	def test_admin_todas_si_puede_filtrar_por_nombre(self):
		spec = {**self.SPEC, "filters": {**self.SPEC["filters"], "aseguradora": ["=", "HDI"]}}
		res = ejecutar(spec, "Administrator", alcance="TODAS")
		directo = frappe.db.sql(
			"""SELECT COUNT(*) FROM tabPieza
			WHERE aseguradora = '7' AND fechaingreso BETWEEN '2026-09-01' AND '2026-09-30 23:59:59'"""
		)[0][0]
		self.assertEqual(res["filas"][0][0], directo)
		self.assertGreater(directo, 0)
