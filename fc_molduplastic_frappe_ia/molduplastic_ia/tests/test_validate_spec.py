"""Tests de validate_spec: specs válidas e intentos adversariales."""

import frappe
from frappe.tests.utils import FrappeTestCase

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
