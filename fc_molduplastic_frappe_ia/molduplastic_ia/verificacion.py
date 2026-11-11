"""Verificación de cifras de la respuesta del modelo (fase 3). Funciones puras.

Regla: toda cifra que el modelo escribe debe aparecer en los Datos de la consulta
(datos, análisis, hallazgos y el título/período que ya armó el servidor). Lo que
no aparece es una cifra sin respaldo: el modelo la calculó, la redondeó de otra
forma o la inventó.

Limitaciones conocidas (se ven en la evidencia):
- Un número escrito como lista ("1. ", "2) ") se ignora.
- Una cifra que coincide por casualidad con un dato no se detecta.
"""

import json
import re

RE_NUMERO = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w])")
RE_MARCADOR_LISTA = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
TOLERANCIA_ABS = 0.051  # redondeo a un decimal


def numeros_en(texto):
	"""Valores numéricos de un texto. Maneja miles con coma, decimales y signo."""
	if not texto:
		return []
	limpio = RE_MARCADOR_LISTA.sub("", str(texto))
	valores = []
	for token in RE_NUMERO.findall(limpio):
		try:
			valores.append(float(token.replace(",", "")))
		except ValueError:
			continue
	return valores


def cifras_permitidas(*fuentes):
	"""Todas las cifras de los Datos: se toman de su forma serializada y de su texto."""
	permitidas = []
	for fuente in fuentes:
		if fuente is None:
			continue
		if isinstance(fuente, str):
			permitidas += numeros_en(fuente)
		else:
			permitidas += numeros_en(json.dumps(fuente, default=str, ensure_ascii=False))
	return permitidas


def _respaldada(valor, permitidas):
	return any(abs(valor - p) <= max(TOLERANCIA_ABS, abs(valor) * 0.0005) for p in permitidas)


def sin_respaldo(texto, permitidas):
	"""Cifras del texto que no aparecen en los Datos, sin repetir."""
	faltan = []
	for valor in numeros_en(texto):
		if not _respaldada(valor, permitidas) and not _en_lista(valor, faltan):
			faltan.append(valor)
	return faltan


def _en_lista(valor, lista):
	return any(abs(valor - v) < 1e-9 for v in lista)


def recortar(texto, permitidas):
	"""Texto sin las partes que tienen cifras sin respaldo.

	Trabaja línea por línea para conservar el formato (encabezado de dos líneas,
	líneas en blanco, tablas). Dentro de una línea de texto corrido quita solo las
	frases afectadas; un renglón de tabla con una cifra mala sale completo, porque
	no se puede quitar una celda sola. Devuelve (texto, quitados).
	"""
	salida, quitados = [], []
	for linea in str(texto or "").split("\n"):
		if not linea.strip():
			salida.append("")
			continue
		if linea.lstrip().startswith("|"):
			if sin_respaldo(linea, permitidas):
				quitados.append(linea.strip())
			else:
				salida.append(linea)
			continue
		frases = [f for f in re.split(r"(?<=[.!?])\s+", linea.strip()) if f.strip()]
		buenas = []
		for frase in frases:
			if sin_respaldo(frase, permitidas):
				quitados.append(frase.strip())
			else:
				buenas.append(frase.strip())
		if buenas:
			salida.append(" ".join(buenas))
	# Se colapsan los bloques vacíos repetidos que deja quitar líneas.
	texto_final = re.sub(r"\n{3,}", "\n\n", "\n".join(salida)).strip()
	return texto_final, quitados
