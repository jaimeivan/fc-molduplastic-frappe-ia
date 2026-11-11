"""Evidencia de pruebas del asistente (pregunta → respuesta) en CSV.

Dos fuentes, en un mismo archivo:

1. `casos_reales`: preguntas que se mandan a `ask()` con el LLM real (proveedor
   configurado en Molduplastic IA Settings). Se registra lo que devolvió el
   sistema y la fila de `IA API Log` que las respondió.
2. `inventario_pruebas`: las pruebas automáticas que hacen preguntas al
   asistente. Se clasifican por análisis del código: "simulada" si parchean el
   LLM (generate_spec, explain, requests.post, adaptadores), "sin_llm" si son
   deterministas. Ninguna prueba automática llama al LLM real.

Uso (desde /home/frappe/frappe-bench):

    bench --site molduplastic.jarritohost.net execute \
        fc_molduplastic_frappe_ia.molduplastic_ia.tests.evidencia.generar

El CSV se escribe en `evidencia/pruebas_asistente.csv` de la app.
"""

import ast
import csv
import os

import frappe

USUARIO_EVIDENCIA = "pruebas.gnp@molduplastic.test"
SETTINGS = "Molduplastic IA Settings"

COLUMNAS = [
	"origen", "tipo_llm", "caso", "archivo", "pregunta", "modo_configurado", "modo_efectivo",
	"intencion", "estado", "respuesta", "proveedor", "modelo", "tokens_entrada",
	"tokens_salida", "latencia_ms", "consultas_complementarias", "latencia_analisis_ms",
	"hallazgos", "analisis_omitido", "verificacion", "preguntas_sugeridas", "nota",
]

# Llamadas que simulan el LLM: si una prueba parchea alguna, es "simulada".
PARCHES_LLM = {
	"generate_spec", "explain", "_con_fallback", "_cadena", "requests", "post",
	"_adaptador", "OpenAICompatibleAdapter", "chat", "_llamar",
}

# Preguntas reales que se mandan al LLM. Cada caso fija el modo y, si hace
# falta, un cambio temporal de configuración.
CASOS_REALES = [
	{
		"caso": "efectividad_septiembre_breve",
		"pregunta": "¿cuál es la efectividad de septiembre?",
		"modo": "Breve",
	},
	{
		"caso": "efectividad_septiembre_analitico",
		"pregunta": "¿cuál es la efectividad de septiembre?",
		"modo": "Analítico",
	},
	{
		"caso": "efectividad_por_taller_analitico",
		"pregunta": "efectividad por taller en septiembre",
		"modo": "Analítico",
	},
	{
		"caso": "menor_efectividad_tipo_pieza_analitico",
		"pregunta": "¿qué tipo de pieza se recolecta menos?",
		"modo": "Analítico",
	},
	{
		"caso": "no_recolectadas_taller_analitico",
		"pregunta": "¿qué talleres concentran más piezas no entregadas en septiembre?",
		"modo": "Analítico",
	},
	{
		"caso": "piezas_por_marca_breve",
		"pregunta": "piezas por marca en septiembre",
		"modo": "Breve",
	},
	{
		"caso": "piezas_por_marca_analitico",
		"pregunta": "piezas por marca en septiembre",
		"modo": "Analítico",
	},
	{
		"caso": "conteo_ingresadas_breve",
		"pregunta": "¿cuántas piezas ingresaron en septiembre?",
		"modo": "Breve",
	},
	{
		"caso": "precio_promedio_por_marca_codigo",
		"pregunta": "¿cuál es el precio promedio por marca en septiembre?",
		"modo": "Analítico",
	},
	{
		"caso": "presupuesto_agotado_nota",
		"pregunta": "efectividad por taller en septiembre",
		"modo": "Analítico",
		"presupuesto_seg": 5,
	},
	{
		"caso": "efectividad_por_region_analitico",
		"pregunta": "efectividad por región en septiembre",
		"modo": "Analítico",
	},
	{
		"caso": "cliente_intenta_forzar_analitico",
		"pregunta": "¿cuál es la efectividad de septiembre?",
		"modo": "Breve",
		"parametro_cliente": {"modo": "Analítico"},
	},
]


def _fila(**campos):
	return {columna: campos.get(columna, "") for columna in COLUMNAS}


def _config(**valores):
	s = frappe.get_doc(SETTINGS)
	previo = {k: s.get(k) for k in valores}
	for k, v in valores.items():
		s.set(k, v)
	s.save(ignore_permissions=True)
	frappe.clear_document_cache(SETTINGS, SETTINGS)
	return previo


def _nuevos_logs(previos):
	"""Filas de IA API Log que no existían antes de la pregunta (sin depender de la hora)."""
	return set(frappe.get_all("IA API Log", pluck="name")) - previos


def _logs_de_la_pregunta(nuevos):
	"""Filas `explain` que creó la pregunta, con la que produjo la respuesta.

	Una pregunta analítica puede dejar dos filas: la del análisis omitido (Fallo) y
	la de la breve que la reemplazó (Éxito). La respuesta se describe con la de
	éxito, y el análisis omitido se toma de cualquiera de las dos.
	"""
	if not nuevos:
		return {}
	filas = frappe.get_all(
		"IA API Log",
		filters={"name": ["in", list(nuevos)], "operacion": "explain"},
		fields=[
			"estado", "proveedor", "modelo", "tokens_entrada", "tokens_salida", "latencia_ms",
			"consultas_complementarias", "latencia_analisis_ms", "hallazgos", "analisis_omitido",
			"modo_respuesta", "intencion", "error", "verificacion",
		],
		order_by="fecha_hora asc",
	)
	if not filas:
		return {}
	exito = next((f for f in filas if f["estado"] == "Éxito"), filas[-1])
	omitido = next((f for f in filas if f["analisis_omitido"]), None)
	return {
		**exito,
		"analisis_omitido": 1 if omitido else 0,
		"error": (omitido or {}).get("error") or exito.get("error") or "",
		"consultas_complementarias": max(f["consultas_complementarias"] or 0 for f in filas),
		"latencia_analisis_ms": max(f["latencia_analisis_ms"] or 0 for f in filas),
	}


def _ejecutar_caso(caso):
	from fc_molduplastic_frappe_ia.molduplastic_ia import api

	previos = set(frappe.get_all("IA API Log", pluck="name"))
	previo = _config(modo_respuesta=caso["modo"])
	if caso.get("presupuesto_seg"):
		previo.update(_config(presupuesto_analisis_seg=caso["presupuesto_seg"]))
	res = {}
	try:
		frappe.set_user(USUARIO_EVIDENCIA)
		kwargs = caso.get("parametro_cliente") or {}
		res = api.ask(caso["pregunta"], **kwargs)
		respuesta = res.get("respuesta") or res.get("mensaje") or ""
		estado_ask = "ok" if res.get("ok") else "rechazada"
	except Exception as e:  # noqa: BLE001 - la evidencia registra el error tal cual
		respuesta = f"EXCEPCIÓN: {e}"
		estado_ask = "excepción"
	finally:
		frappe.set_user("Administrator")
		_config(**previo)
	explain = _logs_de_la_pregunta(_nuevos_logs(previos))
	return _fila(
		origen="evidencia_real", tipo_llm="real", caso=caso["caso"],
		archivo="tests/evidencia.py", pregunta=caso["pregunta"],
		modo_configurado=caso["modo"], modo_efectivo=explain.get("modo_respuesta", ""),
		intencion=explain.get("intencion", ""), estado=estado_ask,
		respuesta=" ".join(respuesta.split()),
		proveedor=explain.get("proveedor", ""), modelo=explain.get("modelo", ""),
		tokens_entrada=explain.get("tokens_entrada", 0),
		tokens_salida=explain.get("tokens_salida", 0),
		latencia_ms=explain.get("latencia_ms", 0),
		consultas_complementarias=explain.get("consultas_complementarias", 0),
		latencia_analisis_ms=explain.get("latencia_analisis_ms", 0),
		hallazgos=explain.get("hallazgos", ""),
		analisis_omitido=explain.get("analisis_omitido", 0),
		verificacion=explain.get("verificacion", ""),
		preguntas_sugeridas=" | ".join(res.get("preguntas_sugeridas") or []) if isinstance(res, dict) else "",
		nota=explain.get("error", "") or "",
	)


def _llama_ask(nodo):
	"""True si el nodo es una llamada a `.ask(...)` o a `api.ask(...)`."""
	return (
		isinstance(nodo, ast.Call)
		and isinstance(nodo.func, ast.Attribute)
		and nodo.func.attr == "ask"
	)


def _usa_llm_simulado(funcion):
	for nodo in ast.walk(funcion):
		if isinstance(nodo, ast.Attribute) and nodo.attr in PARCHES_LLM:
			return True
		if isinstance(nodo, ast.Constant) and nodo.value in PARCHES_LLM:
			return True
		if isinstance(nodo, ast.Name) and nodo.id in PARCHES_LLM:
			return True
	return False


def inventario_pruebas(carpeta_tests):
	"""Una fila por prueba que pregunta al asistente, clasificada por su origen."""
	filas = []
	for nombre in sorted(os.listdir(carpeta_tests)):
		if not (nombre.startswith("test_") and nombre.endswith(".py")):
			continue
		ruta = os.path.join(carpeta_tests, nombre)
		arbol = ast.parse(open(ruta, encoding="utf-8").read())
		for clase in [n for n in arbol.body if isinstance(n, ast.ClassDef)]:
			for funcion in [n for n in clase.body if isinstance(n, ast.FunctionDef)]:
				if not funcion.name.startswith("test_"):
					continue
				preguntas = []
				for nodo in ast.walk(funcion):
					if _llama_ask(nodo) and nodo.args and isinstance(nodo.args[0], ast.Constant):
						preguntas.append(str(nodo.args[0].value))
				if not preguntas and not _usa_llm_simulado(funcion):
					continue
				simulada = _usa_llm_simulado(funcion)
				for pregunta in preguntas or [""]:
					filas.append(_fila(
						origen="prueba_automatica",
						tipo_llm="simulada" if simulada else "sin_llm",
						caso=f"{clase.name}.{funcion.name}",
						archivo=f"tests/{nombre}",
						pregunta=pregunta,
						estado="no ejecutada en esta corrida (inventario estático)",
						nota=(
							"LLM simulado con mocks: no contesta con el modelo real"
							if simulada
							else "sin LLM: consulta real a la base o cálculo determinista"
						) + ("" if preguntas else " (prueba de prompt o adaptador, sin pregunta)"),
					))
	return filas


def generar(incluir_reales=True):
	"""Escribe el CSV de evidencia. `incluir_reales=False` solo hace el inventario."""
	app = frappe.get_app_path("fc_molduplastic_frappe_ia")
	carpeta_tests = os.path.join(app, "molduplastic_ia", "tests")
	filas = []
	if incluir_reales:
		filas += [_ejecutar_caso(caso) for caso in CASOS_REALES]
	filas += inventario_pruebas(carpeta_tests)
	raiz = os.path.dirname(app)  # raíz del repositorio de la app
	carpeta = os.path.join(raiz, "evidencia")
	os.makedirs(carpeta, exist_ok=True)
	ruta = os.path.join(carpeta, "pruebas_asistente.csv")
	with open(ruta, "w", encoding="utf-8", newline="") as f:
		escritor = csv.DictWriter(f, fieldnames=COLUMNAS)
		escritor.writeheader()
		escritor.writerows(filas)
	print(f"{len(filas)} filas en {ruta}")
	return ruta


# --- Captura durante la corrida de pruebas -----------------------------------
#
# Con MOLDUPLASTIC_EVIDENCIA=1 (ver tests/__init__.py), cada llamada a `ask()` que
# hacen las pruebas se registra con su pregunta, su respuesta y su origen real:
#   - "simulada": el LLM estaba parcheado (generate_spec / explain / _con_fallback /
#     requests.post con mock) durante esa llamada;
#   - "real": no hay mock y la llamada escribió consumo real en IA API Log;
#   - "sin_llm": la respuesta la armó el código sin llamar al modelo.
# El resultado va a evidencia/pruebas_ejecutadas.csv al terminar la corrida.

CAPTURA = []
_PARCHEABLES = ("generate_spec", "explain")


def _pipeline_simulado():
	from unittest import mock

	from fc_molduplastic_frappe_ia.molduplastic_ia import api, llm_factory

	objetos = [getattr(api, n) for n in _PARCHEABLES] + [
		llm_factory._con_fallback,
		llm_factory.requests.post,
	]
	return any(isinstance(o, mock.Base) for o in objetos)


def instalar_captura():
	import atexit

	from fc_molduplastic_frappe_ia.molduplastic_ia import api

	original = api.ask

	def ask_capturado(*args, **kwargs):
		antes = set(frappe.get_all("IA API Log", pluck="name"))
		try:
			res = original(*args, **kwargs)
			error = ""
		except Exception as e:  # noqa: BLE001 - se registra y se vuelve a lanzar
			CAPTURA.append(_fila_captura(args, kwargs, antes, None, f"EXCEPCIÓN: {e}"))
			raise
		CAPTURA.append(_fila_captura(args, kwargs, antes, res, error))
		return res

	api.ask = ask_capturado
	# El envoltorio sigue siendo el endpoint: se registra como whitelisted igual que ask.
	if original in frappe.whitelisted:
		frappe.whitelisted.append(ask_capturado)
	atexit.register(volcar_captura)


def _caso_de_la_prueba():
	"""Clase.metodo de la prueba que está corriendo (sale del marco de ejecución)."""
	import inspect

	for marco in inspect.stack():
		instancia = marco.frame.f_locals.get("self")
		if instancia is not None and getattr(instancia, "_testMethodName", None):
			return f"{type(instancia).__name__}.{instancia._testMethodName}"
	return ""


def _fila_captura(args, kwargs, antes, res, error):
	pregunta = args[0] if args else kwargs.get("pregunta", "")
	nuevos = set(frappe.get_all("IA API Log", pluck="name")) - antes
	consumo = frappe.get_all(
		"IA API Log",
		filters={"name": ["in", list(nuevos)], "tokens_entrada": [">", 0]},
		fields=["proveedor", "modelo", "tokens_entrada", "tokens_salida", "latencia_ms", "respuesta"],
		limit=1,
	) if nuevos else []
	if _pipeline_simulado():
		tipo = "simulada"
	elif consumo:
		tipo = "real"
	else:
		tipo = "sin_llm"
	texto = ""
	if isinstance(res, dict):
		texto = res.get("respuesta") or res.get("mensaje") or ""
	c = consumo[0] if consumo else {}
	return _fila(
		origen="prueba_ejecutada",
		tipo_llm=tipo,
		caso=_caso_de_la_prueba(),
		archivo="",
		pregunta=str(pregunta),
		estado="ok" if isinstance(res, dict) and res.get("ok") else ("error" if error else "rechazada"),
		respuesta=" ".join(str(texto).split()),
		proveedor=c.get("proveedor", ""),
		modelo=c.get("modelo", ""),
		tokens_entrada=c.get("tokens_entrada", 0),
		tokens_salida=c.get("tokens_salida", 0),
		latencia_ms=c.get("latencia_ms", 0),
		nota=error or ("" if tipo != "simulada" else "LLM simulado con mocks"),
	)


def volcar_captura():
	if not CAPTURA:
		return
	app = frappe.get_app_path("fc_molduplastic_frappe_ia")
	carpeta = os.path.join(os.path.dirname(app), "evidencia")
	os.makedirs(carpeta, exist_ok=True)
	ruta = os.path.join(carpeta, "pruebas_ejecutadas.csv")
	with open(ruta, "w", encoding="utf-8", newline="") as f:
		escritor = csv.DictWriter(f, fieldnames=COLUMNAS)
		escritor.writeheader()
		escritor.writerows(CAPTURA)
