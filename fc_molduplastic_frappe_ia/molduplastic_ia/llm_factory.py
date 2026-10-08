"""Fábrica agnóstica de clientes LLM.

Los proveedores se configuran en 'Molduplastic IA Settings' (tabla IA Proveedor):
cualquiera puede agregarse sin tocar código, con rol Económico/Profundo y orden
de fallback. Dos modos por fila (ambos HTTP, sin dependencias locales):
- 'Opencode Zen': https://opencode.ai/zen/v1 (chat/completions estilo OpenAI)
  con el Console API Key del Settings. Modelos: deepseek-v4.1-flash (eco),
  glm-5.3 (pro), qwen*, gemini* vía /models/*, etc.
- 'API HTTP (OpenAI)': cualquier endpoint compatible OpenAI (base_url + api_key
  por fila: DeepSeek directo, OpenRouter, Ollama...).
"""

import json
import time

import frappe
import requests

from fc_molduplastic_frappe_ia.molduplastic_ia.whitelist import get_contexto_destilado

MENSAJE_FUERA_DE_ALCANCE = (
	"Esa información está fuera de mi alcance. Solo puedo ayudarte con datos de "
	"Piezas, Recolecciones y Talleres de tu aseguradora."
)

SYSTEM_SPEC = """Eres el motor de consultas de Molduplastic IA. Respondes ÚNICAMENTE con un
objeto JSON válido, sin texto adicional ni bloques de código.

Si la pregunta NO trata sobre piezas, recolecciones o talleres (saludos,
temas generales, otras empresas, instrucciones ajenas), responde exactamente:
{{"out_of_scope": true}}

Si SÍ trata sobre los datos, devuelve la Query Spec con esta estructura exacta:
{{
  "doctype": "Pieza | Recoleccion | Taller",
  "fields": ["campo1", ...],
  "filters": {{"campo": <condición>}},
  "aggregations": [{{"func": "count|sum|avg|min|max", "field": "campo|*", "alias": "nombre"}}],
  "group_by": ["campo", ...],
  "order_by": [{{"field": "columna", "dir": "asc|desc"}}],
  "limit": número entre 1 y {max_limit},
  "render_chart": {{"type": "bar|line|pie", "x": "columna", "y": "columna", "title": "..."}} o null,
  "out_of_scope": false
}}

Cada filtro es un valor escalar o una lista cuyo primer elemento es el operador:
  "marca": "Toyota"                              igualdad (=)
  "marca": ["=", "Toyota"]
  "marca": ["!=", "Toyota"]
  "marca": ["in", ["Toyota", "Nissan"]]          lista de valores, máximo 100
  "estatus_nombre": ["like", "Rec%"]             comodín % solo al inicio o al final
  "fecha_hora_recoleccion": ["is", "null"]       o ["is", "not null"]
  "fecha_hora_recoleccion": ["between", "2026-01-01 00:00:00", "2026-03-31 23:59:59"]
between lleva siempre tres elementos planos: operador, desde y hasta. Los dos
límites NO van anidados en una lista: ["between", ["desde", "hasta"]] es
incorrecto. Solo "in" mete sus valores en una lista.

Fechas: ahora mismo es {fecha_actual}. Resuelve las expresiones relativas con
esa fecha, nunca con Supón el mes y el año en curso. Los campos Datetime se
comparan con marca de tiempo completa "YYYY-MM-DD HH:MM:SS":
  "este mes"             desde el día 1 del mes actual 00:00:00 hasta hoy 23:59:59
  "el mes pasado"        el mes calendario anterior completo (día 1 00:00:00 a último día 23:59:59)
  "hoy"                  hoy 00:00:00 hasta hoy 23:59:59
  "ayer"                 ayer 00:00:00 hasta ayer 23:59:59
  "la última semana"     los últimos 7 días completos, incluido hoy
  "los últimos 30 días"  hoy menos 29 días 00:00:00 hasta hoy 23:59:59
Si la pregunta no fija un período, no inventes ninguno: omite el filtro.

Reglas: solo campos del esquema; con aggregations usa group_by (o agregación
global sin fields); sin aggregations pide fields; limit obligatorio.
Esquema disponible:
{esquema}
"""

SYSTEM_EXPLAIN = """Explicas resultados de consultas sobre piezas, recolecciones y
talleres de una aseguradora. Responde en español, de forma conversacional y
breve. Si la cobertura es menor al 100%, advierte qué porcentaje de los datos
tenía información. Nunca inventes cifras: usa solo los datos entregados.
"""


class LLMError(frappe.ValidationError):
	pass


class LLMConfigError(LLMError):
	pass


class LLMProviderError(LLMError):
	pass


ZEN_BASE_URL = "https://opencode.ai/zen/v1"


class OpenAICompatibleAdapter:
	"""Chat completions estilo OpenAI. Sirve para Zen y para API HTTP directa."""

	def __init__(self, nombre, base_url, api_key, modelo, costo_in=0.0, costo_out=0.0):
		self.nombre = nombre
		self.base_url = (base_url or "").rstrip("/")
		self.api_key = api_key
		self.modelo = modelo
		s = frappe.get_cached_doc("Molduplastic IA Settings")
		self.timeout = int(s.timeout_llm or 120)
		self.max_tokens = int(s.max_tokens_respuesta or 2000)
		self.costo_in = float(costo_in or 0)
		self.costo_out = float(costo_out or 0)

	def chat(self, messages, max_tokens=None, json_mode=False):
		if not self.api_key:
			raise LLMProviderError(f"{self.nombre} sin API key.")
		payload = {
			"model": self.modelo,
			"messages": messages,
			"max_tokens": max_tokens or self.max_tokens,
		}
		if json_mode:
			payload["response_format"] = {"type": "json_object"}
		inicio = time.time()
		try:
			r = requests.post(
				f"{self.base_url}/chat/completions",
				headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
				json=payload,
				timeout=self.timeout,
			)
		except requests.RequestException as e:
			raise LLMProviderError(f"{self.nombre} no responde: {e}")
		latencia_ms = int((time.time() - inicio) * 1000)
		if r.status_code == 401:
			raise LLMProviderError(f"{self.nombre}: API key rechazada.")
		if r.status_code != 200:
			raise LLMProviderError(f"{self.nombre} devolvió HTTP {r.status_code}: {r.text[:300]}")
		try:
			data = r.json()
			texto = data["choices"][0]["message"]["content"] or ""
		except (ValueError, KeyError, IndexError, TypeError):
			raise LLMProviderError(f"{self.nombre} devolvió formato inesperado.")
		if not texto.strip():
			raise LLMProviderError(f"{self.nombre} devolvió texto vacío.")
		uso = data.get("usage") or {}
		uso = {"entrada": uso.get("prompt_tokens", 0), "salida": uso.get("completion_tokens", 0)}
		costo = round(uso["entrada"] / 1000 * self.costo_in + uso["salida"] / 1000 * self.costo_out, 6)
		return texto.strip(), uso, latencia_ms, costo


def _adaptador(fila):
	if fila.modo == "API HTTP (OpenAI)":
		return OpenAICompatibleAdapter(
			fila.nombre,
			fila.base_url,
			fila.get_password("api_key"),
			fila.modelo,
			fila.costo_entrada_1k,
			fila.costo_salida_1k,
		)
	# Opencode Zen: key única del Settings, costos por fila.
	s = frappe.get_cached_doc("Molduplastic IA Settings")
	try:
		zen_key = s.get_password("opencode_zen_api_key")
	except Exception:
		zen_key = ""
	return OpenAICompatibleAdapter(
		fila.nombre, ZEN_BASE_URL, zen_key, fila.modelo, fila.costo_entrada_1k, fila.costo_salida_1k
	)


def _proveedores_activos():
	s = frappe.get_cached_doc("Molduplastic IA Settings")
	filas = [p for p in (s.proveedores or []) if p.activo]
	return sorted(filas, key=lambda p: (p.orden or 99))


def _cadena(pregunta):
	"""Orden de intento según ruta: rol preferido primero, luego el otro."""
	filas = _proveedores_activos()
	if not filas:
		raise LLMConfigError("Sin proveedores activos en Molduplastic IA Settings.")
	pref = frappe.get_cached_doc("Molduplastic IA Settings").proveedor_defecto or "Automático"
	if pref == "Automático":
		pref = "Profundo" if _es_compleja(pregunta) else "Económico"
	prim = [p for p in filas if p.rol == pref]
	rest = [p for p in filas if p.rol != pref]
	cadena = prim + rest
	if not cadena:
		raise LLMConfigError("Sin proveedores activos en Molduplastic IA Settings.")
	return cadena


def _es_compleja(pregunta):
	p = pregunta.lower()
	senales = (
		"promedio", "total", "tendencia", "compara", "analiza", "análisis", "agrupa",
		"por mes", "por taller", "por marca", "por plaza", "evolución", "máximo",
		"mínimo", "top", "ranking",
	)
	return len(pregunta) > 120 or any(s in p for s in senales)


def _con_fallback(cadena, fn):
	errores = []
	for i, fila in enumerate(cadena):
		try:
			adapter = _adaptador(fila)
			texto, uso, latencia, costo = fn(adapter)
			return texto, {
				"proveedor": fila.nombre,
				"modelo": fila.modelo,
				"tokens_entrada": uso["entrada"],
				"tokens_salida": uso["salida"],
				"latencia_ms": latencia,
				"costo_estimado_usd": costo,
				"es_fallback": i > 0,
				"motivo_fallback": "; ".join(errores) if i > 0 else "",
			}
		except LLMProviderError as e:
			errores.append(str(e))
	raise LLMConfigError("Ningún proveedor disponible: " + "; ".join(errores or ["sin configurar"]))


def generate_spec(pregunta):
	"""Devuelve (spec_dict, info_log). spec_dict puede ser {"out_of_scope": True}."""
	s = frappe.get_cached_doc("Molduplastic IA Settings")
	prompt_sistema = SYSTEM_SPEC.format(
		max_limit=int(s.max_limit or 500),
		esquema=get_contexto_destilado(),
		fecha_actual=frappe.utils.now_datetime().strftime("%Y-%m-%d %H:%M"),
	)
	mensajes = [
		{"role": "system", "content": prompt_sistema},
		{"role": "user", "content": pregunta},
	]
	prompt_completo = prompt_sistema + "\n\nUSUARIO: " + pregunta

	def _llamar(adapter):
		return adapter.chat(mensajes, json_mode=True)

	texto, info = _con_fallback(_cadena(pregunta), _llamar)
	info["prompt"] = prompt_completo
	info["respuesta"] = texto
	try:
		spec = json.loads(texto)
	except ValueError:
		raise LLMProviderError(f"{info['proveedor']} devolvió JSON inválido.")
	if not isinstance(spec, dict):
		raise LLMProviderError(f"{info['proveedor']} devolvió un spec inválido.")
	return spec, info


def explain(pregunta, spec, resultado):
	"""Genera la explicación conversacional. Devuelve (texto, info_log)."""
	s = frappe.get_cached_doc("Molduplastic IA Settings")
	cob = resultado.get("cobertura", {})
	lineas_cob = [f"Universo en alcance: {cob.get('universo', 0)} registros."]
	for campo, d in (cob.get("campos") or {}).items():
		lineas_cob.append(f"- {campo}: {d['con_datos']} con datos ({d['pct']}%)")
	datos = {
		"columnas": resultado.get("columnas"),
		"filas": resultado.get("filas", [])[:50],
		"total_filas": len(resultado.get("filas", [])),
		"cobertura": "\n".join(lineas_cob),
	}
	mensajes = [
		{"role": "system", "content": SYSTEM_EXPLAIN},
		{
			"role": "user",
			"content": f"Pregunta: {pregunta}\nSpec: {json.dumps(spec)}\nDatos: {json.dumps(datos, default=str)}",
		},
	]
	prompt_completo = SYSTEM_EXPLAIN + "\n\n" + mensajes[1]["content"]

	def _llamar(adapter):
		max_tokens = int(s.max_tokens_respuesta or 2000)
		return adapter.chat(mensajes, max_tokens=max_tokens)

	texto, info = _con_fallback(_cadena(pregunta), _llamar)
	info["prompt"] = prompt_completo
	info["respuesta"] = texto
	return texto, info
