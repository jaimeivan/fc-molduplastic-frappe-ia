"""Tests de la factory LLM (sin gastar API: requests simulado)."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from fc_molduplastic_frappe_ia.molduplastic_ia import llm_factory as F


class FakeResp:
	status_code = 200
	text = "{}"

	def __init__(self, payload, status=200):
		self._payload = payload
		self.status_code = status

	def json(self):
		return self._payload


def fila_zen(nombre="Eco test", rol="Económico", modelo="deepseek-v4.1-flash"):
	return frappe._dict(
		{
			"nombre": nombre,
			"rol": rol,
			"modo": "Opencode Zen",
			"modelo": modelo,
			"orden": 1,
			"costo_entrada_1k": 0.0003,
			"costo_salida_1k": 0.0012,
			"get_password": lambda k: "ZENKEY",
		}
	)


def settings_stub():
	return frappe._dict(
		{
			"timeout_llm": 120,
			"max_tokens_respuesta": 2000,
			"max_limit": 500,
			"proveedor_defecto": "Automático",
			"get_password": lambda k: "ZENKEY",
		}
	)


class TestZenAdapter(FrappeTestCase):
	@patch.object(F.requests, "post")
	def test_chat_zen_ok(self, mock_post):
		mock_post.return_value = FakeResp(
			{
				"choices": [{"message": {"content": '{"a": 1}'}}],
				"usage": {"prompt_tokens": 100, "completion_tokens": 10},
			}
		)
		with patch.object(F.frappe, "get_cached_doc", return_value=settings_stub()):
			a = F._adaptador(fila_zen())
			self.assertTrue(a.base_url.startswith("https://opencode.ai/zen"))
			texto, uso, lat, costo = a.chat([{"role": "user", "content": "hola"}], json_mode=True)
		self.assertEqual(texto, '{"a": 1}')
		self.assertEqual(uso, {"entrada": 100, "salida": 10})
		self.assertGreater(costo, 0)
		llamada = mock_post.call_args
		self.assertTrue(llamada[0][0].endswith("/chat/completions"))
		self.assertEqual(llamada[1]["json"]["model"], "deepseek-v4.1-flash")
		self.assertEqual(llamada[1]["headers"]["Authorization"], "Bearer ZENKEY")

	@patch.object(F.requests, "post")
	def test_chat_401_es_fallback(self, mock_post):
		mock_post.return_value = FakeResp({}, status=401)
		with patch.object(F.frappe, "get_cached_doc", return_value=settings_stub()):
			a = F._adaptador(fila_zen())
			with self.assertRaises(F.LLMProviderError):
				a.chat([{"role": "user", "content": "hola"}])

	def test_cadena_respeta_rol_y_orden(self):
		filas = [
			fila_zen("eco2", "Económico", "m-eco"),
			fila_zen("pro1", "Profundo", "m-pro"),
		]
		with patch.object(F, "_proveedores_activos", return_value=filas):
			with patch.object(F, "_es_compleja", return_value=False):
				cadena = F._cadena("¿cuántas piezas hay?")
				self.assertEqual(cadena[0].nombre, "eco2")
			with patch.object(F, "_es_compleja", return_value=True):
				cadena = F._cadena("analiza la tendencia por mes con promedios")
				self.assertEqual(cadena[0].nombre, "pro1")

	@patch.object(F, "_proveedores_activos", return_value=[])
	def test_sin_proveedores_error_config(self, mock_prov):
		with self.assertRaises(F.LLMConfigError):
			F._cadena("hola")


class FakeAdapter:
	def __init__(self):
		self.mensajes = None

	def chat(self, mensajes, **kwargs):
		self.mensajes = mensajes
		return "{}", {"entrada": 0, "salida": 0}, 0, 0.0


class TestPromptSpec(FrappeTestCase):
	"""El prompt es la única fuente de gramática de filtros y de fechas para el LLM."""

	def _prompt_generado(self, pregunta):
		capturado = {}

		def fake_fallback(cadena, fn):
			adapter = FakeAdapter()
			texto, _uso, _lat, _costo = fn(adapter)
			capturado["prompt"] = adapter.mensajes[0]["content"]
			return '{"out_of_scope": true}', {"proveedor": "test"}

		with patch.object(F.frappe, "get_cached_doc", return_value=settings_stub()):
			with patch.object(F, "_cadena", return_value=[fila_zen()]):
				with patch.object(F, "_con_fallback", side_effect=fake_fallback):
					F.generate_spec(pregunta)
		return capturado["prompt"]

	def test_prompt_documenta_entre_y_lista(self):
		prompt = self._prompt_generado("¿cómo va la recolección este mes?")
		self.assertIn('["between", "2026-01-01 00:00:00", "2026-03-31 23:59:59"]', prompt)
		self.assertIn('["in", ["Toyota", "Nissan"]]', prompt)

	def test_prompt_inyecta_fecha_actual(self):
		hoy = frappe.utils.now_datetime().strftime("%Y-%m-%d")
		self.assertIn(hoy, self._prompt_generado("piezas de hoy"))

	def test_prompt_lleva_el_objetivo_de_efectividad(self):
		prompt = self._prompt_generado("¿estamos por encima o por debajo del objetivo?")
		self.assertIn(f"El objetivo de efectividad es {F.OBJETIVO_EFECTIVIDAD}%", prompt)
		self.assertNotIn("{objetivo_efectividad}", prompt)

	def _mensajes(self, pregunta, historial=None):
		capturado = {}

		def fake_fallback(cadena, fn):
			adapter = FakeAdapter()
			fn(adapter)
			capturado["mensajes"] = adapter.mensajes
			return '{"out_of_scope": true}', {"proveedor": "test"}

		with patch.object(F.frappe, "get_cached_doc", return_value=settings_stub()):
			with patch.object(F, "_cadena", return_value=[fila_zen()]):
				with patch.object(F, "_con_fallback", side_effect=fake_fallback):
					F.generate_spec(pregunta, historial=historial)
		return capturado["mensajes"]

	def test_sin_historial_van_dos_mensajes(self):
		mensajes = self._mensajes("piezas de octubre")
		self.assertEqual([m["role"] for m in mensajes], ["system", "user"])

	def test_con_historial_se_agrega_un_system_con_el_contexto(self):
		mensajes = self._mensajes(
			"y por taller?",
			historial=[{"pregunta": "dame el resumen de piezas", "respuesta": "40 piezas"}],
		)
		self.assertEqual([m["role"] for m in mensajes], ["system", "system", "user"])
		self.assertIn("dame el resumen de piezas", mensajes[1]["content"])
		self.assertIn("y por taller?", mensajes[-1]["content"])


class TestBloqueContexto(FrappeTestCase):
	"""El contexto hereda referencias, pero prohíbe reutilizar cifras viejas."""

	def test_sin_historial_no_hay_bloque(self):
		self.assertEqual(F._bloque_contexto(None), "")
		self.assertEqual(F._bloque_contexto([]), "")

	def test_incluye_pregunta_y_respuesta(self):
		bloque = F._bloque_contexto([{"pregunta": "dame el resumen", "respuesta": "| a | b |\n|---|---|"}])
		self.assertIn("Usuario: dame el resumen", bloque)
		self.assertIn("Respuesta dada: | a | b |", bloque)

	def test_prohibe_reutilizar_cifras(self):
		self.assertIn("NO reutilices las cifras", F._bloque_contexto([{"pregunta": "x", "respuesta": "y"}]))

	def test_prompt_explica_los_seguimientos(self):
		self.assertIn("Seguimientos dentro de una conversación", F.SYSTEM_SPEC)
		self.assertIn("Seguimiento (", F.SYSTEM_EXPLAIN)


class TestPromptExplain(FrappeTestCase):
	"""`explain` marca el desglose para que el formato no dependa del criterio del modelo."""

	def _datos_enviados(self, spec, resultado=None, pregunta="muestra las piezas por tipo cdr"):
		capturado = {}

		def fake_fallback(cadena, fn):
			adapter = FakeAdapter()
			fn(adapter)
			capturado["mensajes"] = adapter.mensajes
			return "ok", {"proveedor": "test"}

		if resultado is None:
			resultado = {"columnas": ["tipo_carga", "count"], "filas": [["10", 40]]}
		with patch.object(F.frappe, "get_cached_doc", return_value=settings_stub()):
			with patch.object(F, "_cadena", return_value=[fila_zen()]):
				with patch.object(F, "_con_fallback", side_effect=fake_fallback):
					F.explain(pregunta, spec, resultado)
		contenido = capturado["mensajes"][-1]["content"]
		return json.loads(contenido.split("Datos: ", 1)[1])

	def test_tipo_carga_viaja_con_nombre_no_con_id(self):
		spec = {
			"group_by": ["tipo_carga"],
			"aggregations": [{"func": "count", "field": "*", "alias": "piezas"}],
		}
		resultado = {"columnas": ["tipo_carga", "piezas"], "filas": [["11", 285200]]}
		datos = self._datos_enviados(spec, resultado)
		self.assertEqual(datos["filas"][0][0], "Agencia")
		self.assertNotIn("11", [str(v) for v in datos["filas"][0]])

	def test_porcentajes_llevan_el_nombre_del_tipo_cdr(self):
		spec = {
			"group_by": ["tipo_carga"],
			"aggregations": [
				{"func": "count", "field": "*", "alias": "total"},
				{"func": "count", "field": "*", "alias": "recolectadas"},
			],
		}
		resultado = {"columnas": ["tipo_carga", "total", "recolectadas"], "filas": [["11", 100, 5]]}
		datos = self._datos_enviados(spec, resultado, pregunta="efectividad por tipo cdr")
		self.assertEqual(datos["porcentajes"][0]["grupo"], "Agencia")

	def test_desglose_marcado_con_group_by(self):
		self.assertTrue(self._datos_enviados({"group_by": ["tipo_carga"]})["desglose"])

	def test_total_sin_group_by_no_es_desglose(self):
		self.assertFalse(self._datos_enviados({"group_by": []})["desglose"])

	def test_prompt_pide_tabla_para_desglose(self):
		self.assertIn('"desglose": true', F.SYSTEM_EXPLAIN)
		self.assertIn('"Tipo CDR"', F.SYSTEM_EXPLAIN)

	def test_cobertura_al_cien_por_ciento_no_se_menciona(self):
		resultado = {
			"columnas": ["n"],
			"filas": [[40]],
			"cobertura": {"universo": 40, "campos": {"preciovale": {"con_datos": 40, "universo": 40, "pct": 100.0}}},
		}
		self.assertEqual(self._datos_enviados({"group_by": []}, resultado)["cobertura"], "")

	def test_cobertura_incompleta_si_se_menciona(self):
		resultado = {
			"columnas": ["n"],
			"filas": [[40]],
			"cobertura": {"universo": 40, "campos": {"preciovale": {"con_datos": 36, "universo": 40, "pct": 90.0}}},
		}
		cobertura = self._datos_enviados({"group_by": []}, resultado)["cobertura"]
		self.assertIn("preciovale", cobertura)
		self.assertIn("90.0%", cobertura)

	def test_objetivo_solo_en_preguntas_de_efectividad(self):
		spec = {
			"group_by": [],
			"aggregations": [
				{"func": "count", "field": "*", "alias": "total"},
				{"func": "count", "field": "*", "alias": "recolectadas"},
			],
		}
		resultado = {"columnas": ["total", "recolectadas"], "filas": [[40, 0]]}
		con_objetivo = self._datos_enviados(spec, resultado, pregunta="¿cómo vamos contra el objetivo?")
		self.assertEqual(con_objetivo["objetivo_efectividad"], F.OBJETIVO_EFECTIVIDAD)
		# Si las columnas son total + recolectadas, la consulta ES de efectividad
		# aunque la pregunta no lo diga: el objetivo también aplica.
		datos = self._datos_enviados(spec, resultado, pregunta="dame el total y las recolectadas")
		self.assertEqual(datos["objetivo_efectividad"], F.OBJETIVO_EFECTIVIDAD)

	def test_objetivo_no_viaja_en_consultas_ajenas(self):
		resultado = {"columnas": ["marca", "n"], "filas": [["Nissan", 17]]}
		datos = self._datos_enviados({"group_by": ["marca"]}, resultado, pregunta="muestra las piezas por marca")
		self.assertIsNone(datos["objetivo_efectividad"])

	def test_prompt_de_efectividad_usa_el_objetivo(self):
		self.assertIn('"objetivo_efectividad"', F.SYSTEM_EXPLAIN)

	def test_porcentajes_traen_la_columna_objetivo(self):
		spec = {
			"group_by": ["taller_asignado"],
			"aggregations": [
				{"func": "count", "field": "*", "alias": "total"},
				{"func": "count", "field": "*", "alias": "recolectadas"},
			],
		}
		resultado = {
			"columnas": ["taller_asignado", "total", "recolectadas"],
			"filas": [["A", 4, 4], ["B", 4, 1], ["C", 2, 0]],
		}
		datos = self._datos_enviados(spec, resultado, pregunta="porcentaje de efectividad por taller")
		self.assertEqual(
			[p["vs_objetivo"] for p in datos["porcentajes"]],
			["Por encima", "Por debajo", "Por debajo"],
		)
		self.assertEqual([p["grupo"] for p in datos["porcentajes"]], ["A", "B", "C"])

	def test_prompt_pide_la_columna_objetivo(self):
		self.assertIn('"Objetivo"', F.SYSTEM_EXPLAIN)
		self.assertIn("CINCO columnas", F.SYSTEM_EXPLAIN)

	def test_recorta_despues_de_ordenar(self):
		# El LIMIT viejo cortaba en MySQL antes de ordenar: el mejor grupo podía
		# quedar fuera. Ahora se ordena todo y después se muestran 50.
		spec = {
			"group_by": ["taller_asignado"],
			"aggregations": [
				{"func": "count", "field": "*", "alias": "total"},
				{"func": "count", "field": "*", "alias": "recolectadas"},
			],
		}
		filas = [[f"T{i}", 10, 0] for i in range(60)] + [["TOP", 2, 2]]
		resultado = {"columnas": ["taller_asignado", "total", "recolectadas"], "filas": filas}
		datos = self._datos_enviados(spec, resultado, pregunta="efectividad por taller")
		self.assertEqual(datos["total_filas"], 61)
		self.assertEqual(datos["mostradas"], 50)
		self.assertEqual(datos["corte"], "presentacion")
		self.assertEqual(datos["filas"][0][0], "TOP")

	def test_filtra_por_ingresadas_minimas(self):
		spec = {
			"group_by": ["taller_asignado"],
			"min_ingresadas": 5,
			"aggregations": [
				{"func": "count", "field": "*", "alias": "total"},
				{"func": "count", "field": "*", "alias": "recolectadas"},
			],
		}
		filas = [["A", 10, 1], ["B", 4, 4], ["C", 7, 0]]
		resultado = {"columnas": ["taller_asignado", "total", "recolectadas"], "filas": filas}
		datos = self._datos_enviados(spec, resultado, pregunta="talleres con 5 piezas o más")
		self.assertEqual([f[0] for f in datos["filas"]], ["A", "C"])
		self.assertEqual(datos["total_filas"], 2)
		self.assertEqual(datos["min_ingresadas"], 5)

	def test_top_n_recorta_al_limite_pedido(self):
		spec = {
			"group_by": ["taller_asignado"],
			"limit": 10,
			"order_by": [{"field": "recolectadas", "dir": "asc"}],
			"aggregations": [
				{"func": "count", "field": "*", "alias": "total"},
				{"func": "count", "field": "*", "alias": "recolectadas"},
			],
		}
		filas = [[f"T{i}", 60, i % 5] for i in range(30)]
		resultado = {"columnas": ["taller_asignado", "total", "recolectadas"], "filas": filas}
		datos = self._datos_enviados(spec, resultado, pregunta="los 10 peores talleres")
		self.assertEqual(datos["total_filas"], 30)
		self.assertEqual(datos["mostradas"], 10)
		self.assertEqual(datos["corte"], "top")


class TestOrdenDeFilas(FrappeTestCase):
	"""Sin orden explícito, la tabla va de mayor a menor (no alfabética)."""

	def test_ordena_por_efectividad(self):
		spec = {"group_by": ["taller"], "aggregations": [{"func": "count"}, {"func": "count"}]}
		filas = [["A", 10, 1], ["B", 4, 3], ["C", 2, 0]]
		porcentajes = F._porcentajes(
			{"group_by": ["taller"], "aggregations": [{"alias": "total"}, {"alias": "recolectadas"}]},
			{"columnas": ["taller", "total", "recolectadas"], "filas": filas},
		)
		ordenadas, orden_pcts, criterio = F._ordenar(spec, filas, porcentajes)
		self.assertEqual([f[0] for f in ordenadas], ["B", "A", "C"])
		self.assertEqual([p["pct"] for p in orden_pcts], [75.0, 10.0, 0.0])
		self.assertIn("efectividad", criterio)

	def test_peores_ordena_por_efectividad_ascendente(self):
		# "los 10 peores" trae order_by asc: la dirección se aplica al porcentaje,
		# no a recolectadas (el campo que nombre el modelo es un proxy).
		spec = {
			"group_by": ["taller"],
			"order_by": [{"field": "recolectadas", "dir": "asc"}],
			"aggregations": [{"func": "count"}, {"func": "count"}],
		}
		filas = [["A", 10, 1], ["B", 4, 3], ["C", 2, 0]]
		porcentajes = F._porcentajes(
			{"group_by": ["taller"], "aggregations": [{"alias": "total"}, {"alias": "recolectadas"}]},
			{"columnas": ["taller", "total", "recolectadas"], "filas": filas},
		)
		ordenadas, _, criterio = F._ordenar(spec, filas, porcentajes)
		self.assertEqual([f[0] for f in ordenadas], ["C", "A", "B"])
		self.assertIn("menor a mayor", criterio)

	def test_empate_en_cero_ordena_por_volumen_descendente(self):
		# "Peor efectividad": entre varios en 0.0%, primero el de más piezas.
		spec = {
			"group_by": ["taller"],
			"order_by": [{"field": "recolectadas", "dir": "asc"}],
			"aggregations": [{"func": "count"}, {"func": "count"}],
		}
		filas = [["CHICO", 10, 1], ["GRANDE", 100, 0], ["MEDIO", 50, 0]]
		porcentajes = F._porcentajes(
			{"group_by": ["taller"], "aggregations": [{"alias": "total"}, {"alias": "recolectadas"}]},
			{"columnas": ["taller", "total", "recolectadas"], "filas": filas},
		)
		ordenadas, _, _ = F._ordenar(spec, filas, porcentajes)
		self.assertEqual([f[0] for f in ordenadas], ["GRANDE", "MEDIO", "CHICO"])

	def _ordenadas(self, spec, filas, pregunta):
		porcentajes = F._porcentajes(spec, {"columnas": ["taller", "total", "recolectadas"], "filas": filas})
		asc = F._direccion_por_pregunta(pregunta)
		return F._ordenar(spec, filas, porcentajes, asc=asc)

	def test_peor_efectividad_ordena_ascendente(self):
		spec = {
			"group_by": ["taller"],
			"aggregations": [{"func": "count"}, {"func": "count"}],
		}
		filas = [["A", 10, 3], ["B", 10, 0], ["C", 10, 1]]
		ordenadas, _, criterio = self._ordenadas(spec, filas, "dame la peor efectividad por taller")
		self.assertEqual([f[0] for f in ordenadas], ["B", "C", "A"])
		self.assertIn("menor a mayor", criterio)

	def test_peor_gana_al_order_by_del_spec(self):
		# Aunque el modelo mande la dirección contraria, "peor" manda.
		spec = {
			"group_by": ["taller"],
			"order_by": [{"field": "recolectadas", "dir": "desc"}],
			"aggregations": [{"func": "count"}, {"func": "count"}],
		}
		filas = [["A", 10, 2], ["B", 10, 0], ["C", 10, 1]]
		ordenadas, _, _ = self._ordenadas(spec, filas, "los peores talleres")
		self.assertEqual([f[0] for f in ordenadas], ["B", "C", "A"])

	def test_dimension_nula_se_muestra_legible(self):
		resultado = {"columnas": ["taller_asignado", "total", "recolectadas"], "filas": [[None, 9, 0]]}
		spec = {"group_by": ["taller_asignado"], "aggregations": [{"alias": "total"}, {"alias": "recolectadas"}]}
		filas = F._filas_legibles(spec, resultado, resultado["filas"])
		self.assertEqual(filas[0][0], "Sin taller asignado")
		self.assertEqual(F._porcentajes(spec, resultado)[0]["grupo"], "Sin taller asignado")

	def test_desempata_por_ingresadas(self):
		# Todas en 0.0%: el orden debe seguir siendo por volumen, no alfabético.
		filas = [["CHICO", 1, 0], ["GRANDE", 7, 0]]
		spec = {"group_by": ["taller"], "aggregations": [{"alias": "total"}, {"alias": "recolectadas"}]}
		porcentajes = F._porcentajes(
			spec, {"columnas": ["taller", "total", "recolectadas"], "filas": filas}
		)
		ordenadas, _, _ = F._ordenar(spec, filas, porcentajes)
		self.assertEqual([f[0] for f in ordenadas], ["GRANDE", "CHICO"])

	def test_ordena_por_la_medida_si_es_conteo(self):
		spec = {"group_by": ["marca"], "aggregations": [{"func": "count", "field": "*"}]}
		filas = [["Ford", 9], ["Nissan", 17], ["Kia", 2]]
		ordenadas, _, criterio = F._ordenar(spec, filas, [])
		self.assertEqual([f[0] for f in ordenadas], ["Nissan", "Ford", "Kia"])
		self.assertIn("medida", criterio)

	def test_respeta_el_orden_pedido(self):
		spec = {
			"group_by": ["marca"],
			"aggregations": [{"func": "count", "field": "*"}],
			"order_by": [{"field": "piezas", "dir": "asc"}],
		}
		filas = [["Ford", 1], ["Nissan", 2]]
		ordenadas, _, criterio = F._ordenar(spec, filas, [])
		self.assertEqual([f[0] for f in ordenadas], ["Ford", "Nissan"])
		self.assertEqual(criterio, "")

	def test_sin_group_by_no_reordena_nada(self):
		spec = {"group_by": [], "aggregations": [{"func": "count", "field": "*"}]}
		filas = [[40]]
		self.assertEqual(F._ordenar(spec, filas, [])[0], [[40]])


class TestPorcentajes(FrappeTestCase):
	"""La efectividad la divide el código: el modelo solo transcribe el porcentaje."""

	def _spec(self):
		return {
			"group_by": ["taller_asignado"],
			"aggregations": [{"alias": "total"}, {"alias": "recolectadas"}],
		}

	def test_divide_recolectadas_entre_total(self):
		resultado = {
			"columnas": ["taller_asignado", "total", "recolectadas"],
			"filas": [["TALLER A", 4, 1], ["TALLER B", 7, 0]],
		}
		salida = F._porcentajes(self._spec(), resultado)
		self.assertEqual(salida[0]["grupo"], "TALLER A")
		self.assertEqual(salida[0]["pct"], 25.0)
		self.assertEqual(salida[1]["pct"], 0.0)

	def test_total_cero_no_divide_entre_cero(self):
		resultado = {"columnas": ["taller_asignado", "total", "recolectadas"], "filas": [["X", 0, 0]]}
		self.assertEqual(F._porcentajes(self._spec(), resultado)[0]["pct"], 0.0)

	def test_aliases_en_mayusculas(self):
		resultado = {"columnas": ["taller_asignado", "TOTAL", "RECOLECTADAS"], "filas": [["X", 3, 1]]}
		self.assertEqual(F._porcentajes(self._spec(), resultado)[0]["pct"], 33.3)

	def test_sin_agregacion_de_recolectadas_no_hay_porcentaje(self):
		resultado = {"columnas": ["marca", "count"], "filas": [["Nissan", 17]]}
		self.assertEqual(F._porcentajes({}, resultado), [])

	def test_prompt_pide_usar_el_porcentaje_calculado(self):
		self.assertIn('"porcentajes"', F.SYSTEM_EXPLAIN)

	def test_prompt_nombra_los_campos_de_cada_dimension(self):
		"""`plaza` no existe como campo: el prompt tiene que decir `id_plaza`."""
		self.assertIn('"por plaza" →\n["id_plaza"]', F.SYSTEM_SPEC)
		self.assertNotIn('group_by ["plaza"]', F.SYSTEM_SPEC)


class TestResumenEnDinero(FrappeTestCase):
	""""precio de las piezas por categoría" = el resumen de 8 categorías con sum(preciovale)."""

	def test_es_dinero_con_sum_de_preciovale(self):
		spec = {"aggregations": [{"func": "sum", "field": "preciovale", "alias": "ingresadas"}]}
		self.assertTrue(F._es_dinero(spec))

	def test_no_es_dinero_si_mezcla_conteos(self):
		spec = {
			"aggregations": [
				{"func": "sum", "field": "preciovale", "alias": "a"},
				{"func": "count", "field": "*", "alias": "b"},
			]
		}
		self.assertFalse(F._es_dinero(spec))

	def test_no_es_dinero_sin_agregaciones(self):
		self.assertFalse(F._es_dinero({"aggregations": []}))

	def test_prompt_define_la_columna_valor(self):
		self.assertIn('"dinero": true', F.SYSTEM_EXPLAIN)
		self.assertIn('la segunda columna se llama "Valor"', F.SYSTEM_EXPLAIN)

	def test_prompt_no_manda_categoria_a_tipo_carga(self):
		self.assertIn('"categoría" son las categorías\ndel resumen', F.SYSTEM_SPEC)


class TestDireccionPorPregunta(FrappeTestCase):
	def test_peor_es_ascendente(self):
		self.assertTrue(F._direccion_por_pregunta("los 10 talleres con peor efectividad"))
		self.assertTrue(F._direccion_por_pregunta("las 5 marcas con menor efectividad"))

	def test_mejor_es_descendente(self):
		self.assertFalse(F._direccion_por_pregunta("los 10 mejores talleres por efectividad"))

	def test_sin_preferencia_decide_el_spec(self):
		self.assertIsNone(F._direccion_por_pregunta("muestra la efectividad por taller"))


class TestPromptComparacion(FrappeTestCase):
	"""Comparar grupos sin período va al año, y la respuesta cierra con el veredicto."""

	def test_spec_manda_al_anio_en_comparaciones(self):
		self.assertIn("si la pregunta COMPARA dos grupos", F.SYSTEM_SPEC)
		self.assertIn("usa el año en curso", F.SYSTEM_SPEC)

	def test_explain_pide_la_frase_de_comparacion(self):
		self.assertIn("compara dos grupos por efectividad", F.SYSTEM_EXPLAIN)
		self.assertIn("la diferencia en puntos", F.SYSTEM_EXPLAIN)
