"""Verificador de cifras (fase 3): puro, sin BD ni LLM."""

from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import verificacion as v


class TestNumerosEnTexto(FrappeTestCase):
	def test_formatos_comunes(self):
		self.assertEqual(v.numeros_en("130.0% de 41,082 piezas, $3,444.78"), [130.0, 41082.0, 3444.78])

	def test_marcadores_de_lista_no_cuentan(self):
		self.assertEqual(v.numeros_en("1. Nissan\n2) Chevrolet"), [])

	def test_texto_vacio(self):
		self.assertEqual(v.numeros_en(""), [])
		self.assertEqual(v.numeros_en(None), [])

	def test_negativos_y_decimales(self):
		self.assertEqual(v.numeros_en("bajó -5.5 puntos"), [-5.5])


class TestSinRespaldo(FrappeTestCase):
	def setUp(self):
		self.permitidas = v.cifras_permitidas(
			"Del 1 de septiembre de 2026 al 30 de septiembre de 2026",
			{"pct": 130.0, "ingresadas": 41082, "brecha_pp": 55.0},
		)

	def test_cifras_de_los_datos_pasan(self):
		self.assertEqual(v.sin_respaldo("130.0% (41,082 ingresadas), 55.0 puntos", self.permitidas), [])

	def test_redondeo_a_un_decimal_pasa(self):
		self.assertEqual(v.sin_respaldo("130%", self.permitidas), [])

	def test_cifra_inventada_se_detecta(self):
		self.assertEqual(v.sin_respaldo("La efectividad fue 72.4%", self.permitidas), [72.4])

	def test_cifra_calculada_por_el_modelo_se_detecta(self):
		# 130.0 - 113.6 = 16.4: el modelo no debe restar; si lo hace, la cifra no está.
		self.assertEqual(v.sin_respaldo("Sube 16.4 puntos", self.permitidas), [16.4])

	def test_cifra_repetida_se_reporta_una_vez(self):
		self.assertEqual(v.sin_respaldo("72.4% y otra vez 72.4%", self.permitidas), [72.4])

	def test_fechas_del_periodo_pasan(self):
		self.assertEqual(v.sin_respaldo("Del 1 de septiembre de 2026 al 30 de septiembre de 2026", self.permitidas), [])


class TestRecortar(FrappeTestCase):
	def setUp(self):
		self.permitidas = v.cifras_permitidas(
			"Del 1 de septiembre de 2026 al 30 de septiembre de 2026",
			{"pct": 130.0, "ingresadas": 41082},
		)

	def test_conserva_el_encabezado_de_dos_lineas(self):
		texto = (
			"Efectividad\n\nDel 1 de septiembre de 2026 al 30 de septiembre de 2026\n\n"
			"130.0% de 41,082 piezas. La cifra 999 no existe."
		)
		recortado, quitados = v.recortar(texto, self.permitidas)
		self.assertEqual(
			recortado,
			"Efectividad\n\nDel 1 de septiembre de 2026 al 30 de septiembre de 2026\n\n"
			"130.0% de 41,082 piezas.",
		)
		self.assertEqual(quitados, ["La cifra 999 no existe."])

	def test_un_renglon_de_tabla_con_cifra_mala_sale_completo(self):
		texto = "| Taller | Piezas |\n|---|---|\n| A | 7 |\n| B | 41082 |"
		recortado, quitados = v.recortar(texto, self.permitidas)
		self.assertIn("| B | 41082 |", recortado) 
		self.assertIn("| A | 7 |", quitados)

	def test_todo_sin_respaldo_deja_texto_vacio(self):
		recortado, _ = v.recortar("Hay 999 piezas. Y 888 más.", self.permitidas)
		self.assertEqual(recortado, "")

	def test_sin_cifras_no_quita_nada(self):
		texto = "Sin cifras aquí.\n\nUna frase más."
		recortado, quitados = v.recortar(texto, self.permitidas)
		self.assertEqual(quitados, [])
		self.assertEqual(recortado, texto)
