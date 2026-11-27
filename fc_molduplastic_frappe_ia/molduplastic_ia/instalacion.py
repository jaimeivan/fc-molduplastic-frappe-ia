"""Hooks de `bench migrate` para los DocTypes y los índices de la app hermana.

`remove_orphan_doctypes` corre al final del migrate y borra el registro del
DocType cuando su controlador no se puede importar. En la app hermana, `Pieza`
y `Carga Archivo` importan `fc_molduplastic_frappe_app.utils`, que necesita
`thefuzz` y `unidecode`; si faltan en el env, el migrate termina borrando
`Pieza` (la tabla y los datos no se tocan) y el asistente arranca con
"DocType Pieza not found".

- `before_migrate` valida que el módulo de la app hermana importe y corta con
  un mensaje accionable **antes** del borrado.
- `after_migrate` corre **después** de `remove_orphan_doctypes`, reimporta el
  JSON de los DocTypes que ya se habían perdido y vuelve a asegurar los índices
  analíticos de `tabPieza` (al recrear el DocType, `updatedb` borra los índices
  de una sola columna: `idx_ia_parte` e `idx_ia_tipo`).

Los índices viven también en `scripts/indices_ia.sql` (para ejecutarlos a mano
en una base nueva). El test `test_los_indices_coinciden_con_el_sql` compara
las dos listas para que no se separen.
"""

import json
import os
import re
from importlib import import_module

import frappe

APP_HERMANA = "fc_molduplastic_frappe_app"
TABLA_PIEZA = "tabPieza"
MENSAJE_RESTAURADOS = "Se restauraron DocTypes de la app hermana que Frappe había borrado: "

# Mismo contenido que scripts/indices_ia.sql. `query_executor._indice_forzado`
# usa estos nombres con FORCE INDEX: sin ellos la consulta falla con ERROR 1176.
INDICES_IA = (
	("idx_ia_aseg_taller", "aseguradora, taller_asignado"),
	("idx_ia_aseg_recoleccion", "aseguradora, id_recoleccion"),
	(
		"idx_ia_an_ing",
		"aseguradora, fecha_hora_carga, recolectable, rutas_validas, id_estatus, "
		"fecha_hora_recoleccion, taller_asignado, parte_pieza, modelo, preciovale",
	),
	(
		"idx_ia_an_rec",
		"aseguradora, fecha_hora_recoleccion, fecha_hora_carga, recolectable, "
		"rutas_validas, id_estatus, taller_asignado, parte_pieza, preciovale",
	),
	(
		"idx_ia_an_marca",
		"aseguradora, fecha_hora_carga, recolectable, rutas_validas, id_estatus, "
		"marca, estadolocalidad, fecha_hora_recoleccion, preciovale",
	),
	(
		"idx_ia_an_rec_marca",
		"aseguradora, fecha_hora_recoleccion, fecha_hora_carga, recolectable, "
		"rutas_validas, id_estatus, marca, estadolocalidad, preciovale",
	),
	("idx_ia_aseg_carga_siniestro", "aseguradora, fecha_hora_carga, siniestro"),
	("idx_ia_aseg_creation", "aseguradora, creation"),
	("idx_ia_aseg_modified", "aseguradora, modified"),
	("idx_ia_creation", "creation"),
	("idx_ia_parte", "parte_pieza"),
	("idx_ia_tipo", "tipo"),
)


def verificar_dependencias_entorno():
	"""Corta el migrate si falta pkg_resources (setuptools).

	`sync_all` importa el controlador de `Dropbox Settings` de Frappe, que hace
	`import dropbox` y el SDK necesita `pkg_resources`. Sin setuptools el migrate
	aborta a media sincronización con un ImportError poco claro.
	"""
	try:
		import_module("pkg_resources")
	except ImportError:
		frappe.throw(
			"No se puede migrar: falta `pkg_resources` (setuptools) en el env. "
			"El DocType Dropbox Settings de Frappe importa el SDK de Dropbox y "
			"sync_all aborta. Instálalo antes de migrar: "
			"`./env/bin/pip install setuptools==80.9.0`."
		)


def verificar_dependencias_hermana():
	"""Corta el migrate si la app hermana no puede importar sus controladores."""
	if APP_HERMANA not in frappe.get_installed_apps():
		return
	try:
		import_module(f"{APP_HERMANA}.utils")
	except ImportError as e:
		frappe.throw(
			f"No se puede migrar: la app hermana {APP_HERMANA} no importa ({e}). "
			"Instala sus dependencias antes de migrar: "
			f"`./env/bin/pip install -r apps/{APP_HERMANA}/requirements.txt`. "
			"Si migras así, remove_orphan_doctypes borra Pieza del site: la tabla "
			"y los datos no se tocan, pero el asistente deja de encontrarla."
		)


def restaurar_doctypes_hermana():
	"""Repara lo que `remove_orphan_doctypes` haya borrado y asegura los índices."""
	if APP_HERMANA not in frappe.get_installed_apps():
		return
	faltantes = [
		(modulo, carpeta, nombre)
		for modulo, carpeta, nombre in _doctypes_hermana()
		if not frappe.db.exists("DocType", nombre)
	]
	restaurados = []
	errores = []
	for modulo, carpeta, nombre in faltantes:
		try:
			frappe.reload_doc(modulo, "doctype", carpeta, force=True)
			restaurados.append(nombre)
		except Exception as e:
			errores.append(f"{nombre}: {e}")
	if restaurados:
		aviso = MENSAJE_RESTAURADOS + ", ".join(restaurados) + "."
		print(aviso)
		frappe.log_error(title="IA: DocTypes de la app hermana restaurados", message=aviso)
	if errores:
		error = "No se pudieron restaurar DocTypes de la app hermana: " + "; ".join(errores)
		print(error)
		frappe.log_error(title="IA: no se pudieron restaurar DocTypes", message=error)
	_asegurar_indices()


def _doctypes_hermana():
	"""(módulo, carpeta, nombre) de cada JSON de doctype de la app hermana."""
	for modulo in frappe.get_module_list(APP_HERMANA):
		ruta = frappe.get_module_path(modulo, "doctype")
		if not os.path.isdir(ruta):
			continue
		for carpeta in sorted(os.listdir(ruta)):
			archivo = os.path.join(ruta, carpeta, f"{carpeta}.json")
			if not os.path.isfile(archivo):
				continue
			with open(archivo, encoding="utf-8") as f:
				nombre = json.load(f).get("name")
			if nombre:
				yield modulo, carpeta, nombre


def _asegurar_indices():
	"""Crea los índices analíticos que falten en tabPieza (idempotente)."""
	if not frappe.db.table_exists("Pieza", cached=False):
		return
	creados = []
	for nombre, columnas in INDICES_IA:
		if _indice_existe(nombre):
			continue
		frappe.db.sql_ddl(
			f"ALTER TABLE `{TABLA_PIEZA}` ADD INDEX `{nombre}` ({columnas}), "
			"ALGORITHM=INPLACE, LOCK=NONE"
		)
		creados.append(nombre)
	if creados:
		aviso = "Se crearon índices analíticos que faltaban en tabPieza: " + ", ".join(creados) + "."
		print(aviso)
		frappe.log_error(title="IA: índices analíticos creados", message=aviso)


def _indice_existe(nombre):
	return bool(
		frappe.db.sql(
			"SELECT 1 FROM information_schema.STATISTICS "
			"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
			"AND INDEX_NAME = %s LIMIT 1",
			(TABLA_PIEZA, nombre),
		)
	)


def indices_del_sql():
	"""Índices declarados en scripts/indices_ia.sql (para el test de coincidencia)."""
	app = frappe.get_app_path("fc_molduplastic_frappe_ia")
	ruta = os.path.join(os.path.dirname(app), "scripts", "indices_ia.sql")
	with open(ruta, encoding="utf-8") as f:
		texto = f.read()
	pares = re.findall(r"CALL _ia_crear_indice\('([^']+)',\s*'([^']+)'\)", texto)
	return tuple((nombre, re.sub(r"\s+", " ", columnas).strip()) for nombre, columnas in pares)
