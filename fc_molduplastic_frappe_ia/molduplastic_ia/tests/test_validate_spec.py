"""Tests de validate_spec: specs válidas e intentos adversariales."""

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia.query_executor import (
	_limite_sql,
	_valores_con_docname,
)
from fc_molduplastic_frappe_ia.molduplastic_ia.validate_spec import (
	MAX_VALORES_IN,
	SpecInvalida,
	validar_spec,
)


def spec_base(**kwargs):
	base = {
		"doctype": "Pieza",
		"fields": ["folio", "marca"],
		"filters": {},
		"aggregations": [],
		"group_by": [],
		"order_by": [],
		"limit": 10,
		"render_chart": None,
		"out_of_scope": False,
	}
	base.update(kwargs)
	return base


def spec_distinct(**kwargs):
	return spec_base(
		fields=[],
		aggregations=[{"func": "count_distinct", "field": "siniestro", "alias": "n"}],
		**kwargs,
	)


class TestCountDistinct(FrappeTestCase):
	def test_count_distinct_valida(self):
		s = validar_spec(spec_distinct())
		self.assertEqual(s["aggregations"][0]["func"], "count_distinct")
		self.assertEqual(s["aggregations"][0]["field"], "siniestro")

	def test_count_distinct_rechaza_asterisco(self):
		"""COUNT(DISTINCT *) no existe en SQL."""
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					fields=[],
					aggregations=[{"func": "count_distinct", "field": "*", "alias": "n"}],
				)
			)

	def test_count_distinct_rechaza_campo_fuera_de_whitelist(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					fields=[],
					aggregations=[{"func": "count_distinct", "field": "password", "alias": "n"}],
				)
			)

	def test_count_distinct_acepta_campo_no_numerico(self):
		"""Cuenta valores distintos: no tiene sentido exigir numérico como en sum/avg."""
		s = validar_spec(spec_distinct())
		self.assertEqual(s["aggregations"][0]["alias"], "n")

	def test_otros_agregados_siguen_exigiendo_numerico(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					fields=[],
					aggregations=[{"func": "sum", "field": "siniestro", "alias": "n"}],
				)
			)


class TestFiltroDeAgregacion(FrappeTestCase):
	"""El filtro anidado abre una segunda puerta de SQL: tiene que estar igual de cerrada."""

	def test_agg_con_filtro_valida(self):
		s = validar_spec(
			spec_base(
				fields=[],
				aggregations=[
					{
						"func": "sum",
						"field": "preciovale",
						"alias": "pendiente",
						"filtros": {"fecha_hora_recoleccion": ["is", "null"]},
					}
				],
			)
		)
		self.assertEqual(
			s["aggregations"][0]["filtros"]["fecha_hora_recoleccion"], ["is", "null"]
		)

	def test_agg_sin_filtro_normaliza_a_vacio(self):
		s = validar_spec(
			spec_base(
				fields=[],
				aggregations=[{"func": "count", "field": "*", "alias": "n"}],
			)
		)
		self.assertEqual(s["aggregations"][0]["filtros"], {})

	def test_filtro_agg_rechaza_campo_fuera_de_whitelist(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					fields=[],
					aggregations=[
						{"func": "sum", "field": "preciovale", "alias": "n", "filtros": {"password": "x"}}
					],
				)
			)

	def test_filtro_agg_rechaza_operador_prohibido(self):
		"""Un operador no permitido dentro del filtro anidado también es inyección."""
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					fields=[],
					aggregations=[
						{
							"func": "sum",
							"field": "preciovale",
							"alias": "n",
							"filtros": {"marca": ["regexp", ".*"]},
						}
					],
				)
			)

	def test_filtro_agg_rechaza_valor_anidado(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					fields=[],
					aggregations=[
						{
							"func": "sum",
							"field": "preciovale",
							"alias": "n",
							"filtros": {"marca": {"$ne": ""}},
						}
					],
				)
			)

	def test_filtro_agg_rechaza_is_invalido(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					fields=[],
					aggregations=[
						{
							"func": "sum",
							"field": "preciovale",
							"alias": "n",
							"filtros": {"fecha_hora_recoleccion": ["is", "cualquier"]},
						}
					],
				)
			)

	def test_clave_desconocida_en_agg_rechazada(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					fields=[],
					aggregations=[
						{"func": "sum", "field": "preciovale", "alias": "n", "where": "1=1"}
					],
				)
			)


class TestValidarSpec(FrappeTestCase):
	def test_spec_simple_valida(self):
		s = validar_spec(spec_base())
		self.assertEqual(s["doctype"], "Pieza")
		self.assertEqual(s["limit"], 10)

	def test_doctype_prohibido(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(doctype="User"))

	def test_campo_prohibido_en_fields(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(fields=["folio", "imagen_evidencia"]))

	def test_campo_prohibido_en_filtros(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"password": "x"}))

	def test_operador_prohibido(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"marca": ["regexp", ".*"]}))

	def test_between_plano(self):
		s = validar_spec(
			spec_base(filters={"fecha_hora_recoleccion": ["between", "2026-10-01", "2026-10-31"]})
		)
		self.assertEqual(
			s["filters"]["fecha_hora_recoleccion"], ["between", "2026-10-01", "2026-10-31"]
		)

	def test_between_anidado_se_normaliza(self):
		"""Los modelos anidan los límites; se aplana en vez de rechazar la consulta."""
		s = validar_spec(
			spec_base(
				filters={"fecha_hora_recoleccion": ["between", ["2026-10-01", "2026-10-31"]]}
			)
		)
		self.assertEqual(
			s["filters"]["fecha_hora_recoleccion"], ["between", "2026-10-01", "2026-10-31"]
		)

	def test_between_con_un_solo_valor(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"fecha_hora_recoleccion": ["between", "2026-10-01"]}))

	def test_between_con_tres_valores_anidados(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					filters={
						"fecha_hora_recoleccion": [
							"between",
							["2026-01-01", "2026-02-01", "2026-03-01"],
						]
					}
				)
			)

	def test_in_acepta_lista(self):
		s = validar_spec(spec_base(filters={"marca": ["in", ["Toyota", "Nissan"]]}))
		self.assertEqual(s["filters"]["marca"], ["in", ["Toyota", "Nissan"]])

	def test_in_lista_vacia(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"marca": ["in", []]}))

	def test_in_excede_maximo(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"marca": ["in", ["x"] * (MAX_VALORES_IN + 1)]}))

	def test_in_no_admite_lista_anidada(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"marca": ["in", [["Toyota"]]]}))

	def test_is_rechaza_operando_raro(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"fecha_hora_recoleccion": ["is", "0"]}))

	def test_valor_anidado_rechazado(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"marca": ["=", {"campo": "otro"}]}))

	def test_clave_extra(self):
		s = spec_base()
		s["sql"] = "select * from tabPieza"
		with self.assertRaises(SpecInvalida):
			validar_spec(s)

	def test_agregacion_en_campo_texto(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					fields=[],
					aggregations=[{"func": "sum", "field": "marca", "alias": "t"}],
					group_by=["folio"],
				)
			)

	def test_agregacion_valida_con_group_by(self):
		s = validar_spec(
			spec_base(
				fields=["marca"],
				aggregations=[{"func": "count", "field": "*", "alias": "total"}],
				group_by=["marca"],
				order_by=[{"field": "total", "dir": "desc"}],
			)
		)
		self.assertEqual(s["aggregations"][0]["func"], "count")

	def test_group_by_sin_agregacion(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(group_by=["marca"]))

	def test_limit_excesivo(self):
		maximo = int(frappe.get_cached_doc("Molduplastic IA Settings").max_limit or 500)
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(limit=maximo + 1))

	def test_alias_malicioso(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					fields=[],
					aggregations=[{"func": "count", "field": "*", "alias": "t; DROP TABLE x"}],
					group_by=["marca"],
				)
			)

	def test_render_chart_con_columna_inexistente(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(render_chart={"type": "bar", "x": "marca", "y": "otra"}))

	def test_out_of_scope(self):
		with self.assertRaises(SpecInvalida):
			validar_spec({"out_of_scope": True})


class TestRespuestaDirecta(FrappeTestCase):
	"""La vía sin consulta: el texto llega al chat sin pasar por el ejecutor."""

	def test_acepta_texto_solo(self):
		s = validar_spec({"respuesta_directa": "  - Norte: Coahuila  "})
		self.assertEqual(s, {"respuesta_directa": "- Norte: Coahuila"})

	def test_rechaza_vacia_o_no_texto(self):
		for valor in ("", "   ", None, 42, ["- Norte"]):
			with self.assertRaises(SpecInvalida):
				validar_spec({"respuesta_directa": valor})

	def test_no_mezcla_con_una_consulta(self):
		"""Sin este rechazo, serviría para colar cifras inventadas."""
		with self.assertRaises(SpecInvalida):
			validar_spec(
				{
					"respuesta_directa": "40 piezas",
					"doctype": "Pieza",
					"aggregations": [{"func": "count", "field": "*", "alias": "n"}],
				}
			)


class TestComparacionEntreCampos(FrappeTestCase):
	"""Referencias {"campo": ...} y mes_distinto: identificadores de whitelist, nunca parámetros."""

	def test_comparacion_entre_campos_valida(self):
		s = validar_spec(
			spec_base(filters={"fecha_hora_recoleccion": [">", {"campo": "fechaingreso"}]})
		)
		self.assertEqual(
			s["filters"]["fecha_hora_recoleccion"], [">", {"campo": "fechaingreso"}]
		)

	def test_mes_distinto_valido(self):
		s = validar_spec(
			spec_base(
				filters={
					"fecha_hora_recoleccion": ["mes_distinto", {"campo": "fechaingreso"}]
				}
			)
		)
		self.assertEqual(
			s["filters"]["fecha_hora_recoleccion"],
			["mes_distinto", {"campo": "fechaingreso"}],
		)

	def test_mes_igual_valido(self):
		s = validar_spec(
			spec_base(
				filters={
					"fecha_hora_recoleccion": ["mes_igual", {"campo": "fechaingreso"}]
				}
			)
		)
		self.assertEqual(
			s["filters"]["fecha_hora_recoleccion"],
			["mes_igual", {"campo": "fechaingreso"}],
		)

	def test_mes_igual_necesita_referencia(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"fecha_hora_recoleccion": ["mes_igual"]}))
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(filters={"fecha_hora_recoleccion": ["mes_igual", "fechaingreso"]})
			)

	def test_mes_igual_rechaza_campo_fuera_de_whitelist(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"fecha_hora_recoleccion": ["mes_igual", {"campo": "marca2"}]}))

	def test_mes_igual_no_es_comparacion_regular(self):
		"""mes_igual no acepta un literal, solo otra columna."""
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					filters={"fecha_hora_recoleccion": ["mes_igual", {"campo": "password"}]}
				)
			)

	def test_referencia_a_campo_fuera_de_whitelist(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"marca": ["=", {"campo": "password"}]}))

	def test_referencia_a_campo_inexistente(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"marca": ["=", {"campo": "otro"}]}))

	def test_comparacion_consigo_mismo(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(filters={"marca": ["=", {"campo": "marca"}]}))

	def test_referencia_solo_en_comparacion(self):
		"""between/in/like/is no admiten referencias: solo literales."""
		for cond in (
			["between", {"campo": "fechaingreso"}, "2026-10-31"],
			["in", [{"campo": "marca"}]],
			["like", {"campo": "marca"}],
			["is", {"campo": "marca"}],
		):
			with self.assertRaises(SpecInvalida):
				validar_spec(spec_base(filters={"marca": cond}))

	def test_varias_condiciones_por_campo(self):
		s = validar_spec(
			spec_base(
				filters={
					"fecha_hora_recoleccion": [
						["between", "2026-10-01 00:00:00", "2026-10-16 23:59:59"],
						[">", {"campo": "fechaingreso"}],
						["mes_distinto", {"campo": "fechaingreso"}],
					]
				}
			)
		)
		self.assertEqual(len(s["filters"]["fecha_hora_recoleccion"]), 3)

	def test_varias_condiciones_rechaza_subcondicion_mala(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					filters={"fecha_hora_recoleccion": [["between", "2026-10-01"], ["regexp", ".*"]]}
				)
			)

	def test_ocho_agregaciones_para_resumen(self):
		"""El resumen de 8 cifras (ingresadas/recolectadas/no recolectadas/
		pendientes/inconsistentes recolectadas/inconsistentes no recolectadas/
		rezagadas recolectadas/rezagadas no recolectadas) necesita 8."""
		aggs = [
			{"func": "count", "field": "*", "alias": f"m{i}"} for i in range(8)
		]
		s = validar_spec(spec_base(fields=[], aggregations=aggs))
		self.assertEqual(len(s["aggregations"]), 8)
		aggs.append({"func": "count", "field": "*", "alias": "m8"})
		with self.assertRaises(SpecInvalida):
			validar_spec(spec_base(fields=[], aggregations=aggs))


class TestRegionPais(FrappeTestCase):
	"""`region_pais` es una columna virtual: la acepta, pero no es una tabla real."""

	def test_region_pais_aceptada_en_group_by(self):
		s = validar_spec(
			spec_base(
				fields=[],
				aggregations=[{"func": "count", "field": "*", "alias": "piezas"}],
				group_by=["region_pais"],
			)
		)
		self.assertEqual(s["group_by"], ["region_pais"])

	def test_region_pais_no_existe_en_otros_doctypes(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(
				spec_base(
					doctype="Taller",
					fields=[],
					aggregations=[{"func": "count", "field": "*", "alias": "n"}],
					group_by=["region_pais"],
				)
			)


class TestEfectividadPorDimension(FrappeTestCase):
	"""El spec que `SYSTEM_SPEC` escribe para "porcentaje de efectividad por taller"."""

	def _spec(self):
		return spec_base(
			fields=[],
			filters={
				"fechaingreso": ["between", "2026-10-01 00:00:00", "2026-10-19 23:59:59"],
				"recolectable": ["=", 1],
			},
			aggregations=[
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
			],
			group_by=["taller_asignado"],
			limit=50,
		)

	def test_spec_de_la_receta_valida(self):
		s = validar_spec(self._spec())
		self.assertEqual([a["alias"] for a in s["aggregations"]], ["total", "recolectadas"])
		self.assertEqual(s["group_by"], ["taller_asignado"])
		self.assertEqual(s["aggregations"][0]["filtros"], {})

	def test_recolectadas_conserva_sus_tres_condiciones(self):
		s = validar_spec(self._spec())
		filtros = s["aggregations"][1]["filtros"]
		self.assertEqual(filtros["id_estatus"], ["=", 100])
		self.assertEqual(len(filtros["fecha_hora_recoleccion"]), 2)


class TestMinIngresadas(FrappeTestCase):
	"""Umbral de ingresadas en efectividad: es un HAVING, no un filtro de piezas."""

	def _spec(self, **cambios):
		spec = {
			"doctype": "Pieza",
			"fields": [],
			"filters": {},
			"aggregations": [
				{"func": "count", "field": "*", "alias": "total"},
				{"func": "count", "field": "*", "alias": "recolectadas"},
			],
			"group_by": ["taller_asignado"],
			"limit": 500,
		}
		spec.update(cambios)
		return spec

	def test_acepta_entero(self):
		self.assertEqual(validar_spec(self._spec(min_ingresadas=10))["min_ingresadas"], 10)

	def test_sin_numero_queda_en_none(self):
		self.assertIsNone(validar_spec(self._spec())["min_ingresadas"])

	def test_rechaza_texto(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(self._spec(min_ingresadas="10"))

	def test_rechaza_cero(self):
		with self.assertRaises(SpecInvalida):
			validar_spec(self._spec(min_ingresadas=0))

	def test_rechaza_sin_efectividad(self):
		spec = self._spec(
			min_ingresadas=10,
			aggregations=[{"func": "count", "field": "*", "alias": "piezas"}],
		)
		with self.assertRaises(SpecInvalida):
			validar_spec(spec)


class TestLimiteSqlEfectividad(FrappeTestCase):
	"""El LIMIT se amplía para poder ordenar por efectividad, sin perder el top N."""

	def _spec(self, **cambios):
		spec = {
			"doctype": "Pieza",
			"fields": [],
			"filters": {},
			"aggregations": [
				{"func": "count", "field": "*", "alias": "total"},
				{"func": "count", "field": "*", "alias": "recolectadas"},
			],
			"group_by": ["taller_asignado"],
			"limit": 50,
		}
		spec.update(cambios)
		return spec

	def _max_limit(self):
		return int(frappe.get_cached_doc("Molduplastic IA Settings").max_limit or 500)

	def test_amplia_en_efectividad(self):
		self.assertEqual(_limite_sql(self._spec(limit=50)), self._max_limit())

	def test_tambien_con_top_n_sin_perder_el_limite_pedido(self):
		spec = self._spec(limit=10, order_by=[{"field": "recolectadas", "dir": "asc"}])
		self.assertEqual(_limite_sql(spec), self._max_limit())
		self.assertEqual(spec["limit"], 10)  # el top N se recorta después, en Python

	def test_conteo_normal_no_se_toca(self):
		spec = self._spec(
			limit=10,
			aggregations=[{"func": "count", "field": "*", "alias": "piezas"}],
		)
		self.assertEqual(_limite_sql(spec), 10)


class TestFiltroAseguradora(FrappeTestCase):
	"""El nombre ("GNP") tiene que viajar como docname ("6") o la consulta sale vacía."""

	MAPA = {"gnp": "6", "afirme": "1"}

	def test_nombre_se_cambia_por_docname(self):
		self.assertEqual(_valores_con_docname(["=", "GNP"], self.MAPA), ["=", "6"])

	def test_in_y_varias_condiciones_por_campo(self):
		self.assertEqual(
			_valores_con_docname(["in", ["GNP", "Afirme"]], self.MAPA),
			["in", ["6", "1"]],
		)
		self.assertEqual(
			_valores_con_docname([["=", "GNP"], ["!=", "Afirme"]], self.MAPA),
			[["=", "6"], ["!=", "1"]],
		)

	def test_like_con_el_nombre_es_una_igualdad(self):
		self.assertEqual(_valores_con_docname(["like", "%GNP%"], self.MAPA), ["=", "6"])
		self.assertEqual(_valores_con_docname(["like", "%ZZZ%"], self.MAPA), ["like", "%ZZZ%"])

	def test_docname_y_referencia_a_campo_intactos(self):
		self.assertEqual(_valores_con_docname(["=", "6"], self.MAPA), ["=", "6"])
		self.assertEqual(
			_valores_con_docname(["mes_igual", {"campo": "fechaingreso"}], self.MAPA),
			["mes_igual", {"campo": "fechaingreso"}],
		)
