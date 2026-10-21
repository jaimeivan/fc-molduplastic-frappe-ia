"""Regiones del país derivadas de Pieza.estadolocalidad.

`estadolocalidad` no guarda solo estados: trae estados, ciudades y hasta
nombres de agencia (MONTERREY REMOTO, CANCUN AUTOPRONTO, PUEBLA BAJIO). Por eso
la región no es un campo de la tabla sino una expresión que este módulo
construye con un mapa que vive en el código, no en el prompt: el LLM nunca
decide a qué región pertenece un estado.

La expresión usa una lista de literales por región en vez de un CASE por
literal: son 7 comparaciones con IN (hash) por fila en lugar de cientos de
igualdades encadenadas. `estadolocalidad` viene en mayúsculas/minúsculas y en
dos codificaciones (la mojibake "MÃ©xico" junto a la correcta "México"), así
que cada nombre genera sus variantes.
"""

import frappe

# Las siete regiones acordadas con el dueño.
REGIONES = (
	"Norte",
	"CDMX y Área Metropolitana",
	"Occidente",
	"Centro y Golfo",
	"Bajío",
	"Sureste",
	"Pacífico",
)

# Estado -> región. Los 32 estados más sus abreviaturas habituales.
ESTADOS = {
	"Aguascalientes": "Bajío",
	"Baja California": "Occidente",
	"Baja California Sur": "Occidente",
	"Campeche": "Sureste",
	"Chiapas": "Sureste",
	"Chihuahua": "Norte",
	"Ciudad de México": "CDMX y Área Metropolitana",
	"Coahuila": "Norte",
	"Colima": "Occidente",
	"Durango": "Norte",
	"Estado de México": "CDMX y Área Metropolitana",
	"Guanajuato": "Bajío",
	"Guerrero": "Pacífico",
	"Hidalgo": "CDMX y Área Metropolitana",
	"Jalisco": "Occidente",
	"Michoacán": "Pacífico",
	"Morelos": "CDMX y Área Metropolitana",
	"Nayarit": "Occidente",
	"Nuevo León": "Norte",
	"Oaxaca": "Centro y Golfo",
	"Puebla": "Centro y Golfo",
	"Querétaro": "Bajío",
	"Quintana Roo": "Sureste",
	"San Luis Potosí": "Bajío",
	"Sinaloa": "Occidente",
	"Sonora": "Occidente",
	"Tabasco": "Sureste",
	"Tamaulipas": "Norte",
	"Tlaxcala": "CDMX y Área Metropolitana",
	"Veracruz": "Centro y Golfo",
	"Yucatán": "Sureste",
	"Zacatecas": "Norte",
}

# Abreviaturas y variantes que no son el nombre del estado. Apuntan al estado,
# no a la región: `_region_de` resuelve ese segundo salto.
ALIAS_ESTADO = {
	"CDMX": "Ciudad de México",
	"DISTRITO FEDERAL": "Ciudad de México",
	"DISTRITO FED": "Ciudad de México",
	"EDO MEX": "Estado de México",
	"EDO DE MEXICO": "Estado de México",
	"EDO MEXICO": "Estado de México",
	"EDOMEX": "Estado de México",
	"MEXICO": "Estado de México",
	"MÉXICO": "Estado de México",
	"SAN LUIS POTOSI": "San Luis Potosí",
	"SAN LUIS POTOSÍ": "San Luis Potosí",
	"SAN LUÍS POTOSÍ": "San Luis Potosí",
	"SAN LUIS POTOSÍ": "San Luis Potosí",
	"EDO DE MORELOS": "Morelos",
}

# Ciudad -> estado. Solo las que tienen volumen en tabPieza; lo demás queda
# sin región (NULL) en vez de inventar una.
CIUDADES = {
	"Acapulco": "Guerrero",
	"Agua Prieta": "Chihuahua",
	"Aguascalientes": "Aguascalientes",
	"Ahome": "Sinaloa",
	"Apizaco": "Tlaxcala",
	"Atlacomulco": "Estado de México",
	"Benito Juarez": "Ciudad de México",
	"Cabo": "Baja California Sur",
	"Cancun": "Quintana Roo",
	"Campeche": "Campeche",
	"Celaya": "Guanajuato",
	"Champoton": "Campeche",
	"Chetumal": "Quintana Roo",
	"Chihuahua": "Chihuahua",
	"Chilpancingo": "Guerrero",
	"Ciudad del Carmen": "Campeche",
	"Ciudad Juarez": "Chihuahua",
	"Ciudad Obregon": "Sonora",
	"Ciudad Valles": "San Luis Potosí",
	"Coatzacoalcos": "Veracruz",
	"Colima": "Colima",
	"Comonfort": "Guanajuato",
	"Comalcalco": "Tabasco",
	"Copala": "Sinaloa",
	"Cordoba": "Veracruz",
	"Costa Esmeralda": "Veracruz",
	"Cozumel": "Quintana Roo",
	"Cuautitlan": "Estado de México",
	"Cuauhtemoc": "Chihuahua",
	"Cuernavaca": "Morelos",
	"Culiacan": "Sinaloa",
	"Coyoacan": "Ciudad de México",
	"Delicias": "Chihuahua",
	"Durango": "Durango",
	"Ecatepec": "Estado de México",
	"El Marquez": "Querétaro",
	"El Rosario": "Sinaloa",
	"Ensenada": "Baja California",
	"Guadalajara": "Jalisco",
	"Guamuchil": "Sinaloa",
	"Guanajuato": "Guanajuato",
	"Guasave": "Sinaloa",
	"Guaymas": "Sonora",
	"Hermosillo": "Sonora",
	"Huejutla": "Hidalgo",
	"Huixquilucan": "Estado de México",
	"Irapuato": "Guanajuato",
	"Isla Socorro": "Colima",
	"Ixtapa": "Guerrero",
	"Ixtapaluca": "Estado de México",
	"Iztacalco": "Ciudad de México",
	"Iztapalapa": "Ciudad de México",
	"Juchitan": "Oaxaca",
	"La Paz": "Baja California Sur",
	"Lagos de Moreno": "Jalisco",
	"La Magdalena": "Ciudad de México",
	"Leon": "Guanajuato",
	"Los Cabos": "Baja California Sur",
	"Los Mochis": "Sinaloa",
	"Mazatlan": "Sinaloa",
	"Matehuala": "San Luis Potosí",
	"Manzanillo": "Colima",
	"Merida": "Yucatán",
	"Mexicali": "Baja California",
	"Monclova": "Coahuila",
	"Monterrey": "Nuevo León",
	"Montemorelos": "Nuevo León",
	"Morelia": "Michoacán",
	"Metepec": "Estado de México",
	"Naucalpan": "Estado de México",
	"Navojoa": "Sonora",
	"Nezahualcoyotl": "Estado de México",
	"Nuevo Laredo": "Tamaulipas",
	"Oaxaca": "Oaxaca",
	"Ocotlan": "Jalisco",
	"Pachuca": "Hidalgo",
	"Piedra Negras": "Coahuila",
	"Playa del Carmen": "Quintana Roo",
	"Puerto Vallarta": "Jalisco",
	"Puebla": "Puebla",
	"Queretaro": "Querétaro",
	"Quesada": "Veracruz",
	"Reforma": "Veracruz",
	"Reynosa": "Tamaulipas",
	"Rio Bravo": "Tamaulipas",
	"Rosarito": "Baja California",
	"Salina Cruz": "Chiapas",
	"Saltillo": "Coahuila",
	"San Andres Cholula": "Puebla",
	"San Francisco del Rincon": "Guanajuato",
	"San Jose del Cabo": "Baja California Sur",
	"San Luis Rio Colorado": "Sonora",
	"San Pedro": "Nuevo León",
	"San Salvador": "Chiapas",
	"San Yamilo": "Guanajuato",
	"Santa Ana": "Sonora",
	"Sayula": "Jalisco",
	"Simon de Anguiano": "Veracruz",
	"Soledad de Doblado": "Veracruz",
	"Tampico": "Tamaulipas",
	"Tapachula": "Chiapas",
	"Tecate": "Baja California",
	"Tecoman": "Colima",
	"Tepic": "Nayarit",
	"Tepatitlan": "Jalisco",
	"Tequisquiapan": "Querétaro",
	"Tijuana": "Baja California",
	"Tizayuca": "Hidalgo",
	"Tizimin": "Yucatán",
	"Tlalnepantla": "Estado de México",
	"Tlalpan": "Ciudad de México",
	"Tlaxcala": "Tlaxcala",
	"Toluca": "Estado de México",
	"Torreon": "Coahuila",
	"Tula": "Estado de México",
	"Tulum": "Quintana Roo",
	"Tuxpan": "Veracruz",
	"Tuxtla": "Chiapas",
	"Uruapan": "Michoacán",
	"Valladolid": "Yucatán",
	"Veracruz": "Veracruz",
	"Viesca": "Coahuila",
	"Villahermosa": "Tabasco",
	"Xalapa": "Veracruz",
	"Zacatecas": "Zacatecas",
	"Zamora": "Michoacán",
	"Zapopan": "Jalisco",
}

# Sufijos que la fuente de datos agrega a la localidad ("MONTERREY REMOTO",
# "CANCUN AUTOPRONTO"). Se generan las dos variantes de cada literal.
SUFIJOS = ("", " REMOTO", " AUTOPRONTO")

CACHE_KEY = "molduplastic_ia:regiones:sql"
CACHE_TTL = 24 * 60 * 60


def _variantes(nombre):
	"""Nombre en mayúsculas, y su versión mojibake (UTF-8 leído como latin-1)."""
	arriba = nombre.upper()
	try:
		mojibake = arriba.encode("utf-8").decode("latin-1").upper()
	except UnicodeError:
		mojibake = arriba
	return [arriba] + ([mojibake] if mojibake != arriba else [])


def _region_de(nombre):
	"""Región de un estado, alias o ciudad. None si no se conoce."""
	estado = ALIAS_ESTADO.get(nombre) or CIUDADES.get(nombre) or nombre
	return ESTADOS.get(estado)


def _literales_por_region():
	"""región -> lista de literales (sin escapar) que caen en ella."""
	mapa = {region: [] for region in REGIONES}
	origenes = list(ESTADOS) + list(ALIAS_ESTADO) + list(CIUDADES)
	for nombre in origenes:
		region = _region_de(nombre)
		if region not in mapa:
			continue
		for base in _variantes(nombre):
			for sufijo in SUFIJOS:
				lit = base + sufijo
				if lit not in mapa[region]:
					mapa[region].append(lit)
	return mapa


def resumen_regiones():
	"""Texto para el system prompt: región -> estados. Se arma desde ESTADOS.

	Única fuente de verdad: si se edita ESTADOS, el prompt se entera solo. El
	mapa NO se escribe a mano en llm_factory, porque las dos copias se separan
	en silencio (el mismo problema que los otros lugares que deben coincidir).
	"""
	por_region = {region: [] for region in REGIONES}
	for estado, region in ESTADOS.items():
		por_region.setdefault(region, []).append(estado)
	lineas = []
	for region in REGIONES:
		estados = sorted(por_region.get(region, []))
		lineas.append(f"- {region}: " + ", ".join(estados))
	return "\n".join(lineas)


def tabla_regiones():
	"""Tabla Markdown región -> estados para `respuesta_directa`.

	Se arma desde ESTADOS, igual que `resumen_regiones`: el LLM no la escribe,
	así no puede omitir estados ni inventar en qué región cae uno. El chat la
	renderiza como tabla HTML (ver `mdAHtml` en la página asistente-ia).
	"""
	por_region = {region: [] for region in REGIONES}
	for estado, region in ESTADOS.items():
		por_region.setdefault(region, []).append(estado)
	lineas = ["| Región | Estados |", "|---|---|"]
	for region in REGIONES:
		estados = sorted(por_region.get(region, []))
		lineas.append(f"| {region} | {', '.join(estados)} |")
	return "\n".join(lineas)


def expresion_region():
	"""CASE ... END que devuelve la región o NULL. Cacheada en Redis."""
	from fc_molduplastic_frappe_ia.molduplastic_ia.query_executor import _col

	col = _col("tabPieza", "estadolocalidad")
	expresion = frappe.cache().get_value(CACHE_KEY)
	if expresion:
		return expresion
	expresion = "CASE"
	for region in REGIONES:
		lits = _literales_por_region()[region]
		valores = ", ".join(frappe.db.escape(lit) for lit in lits)
		expresion += f" WHEN UPPER(TRIM({col})) IN ({valores}) THEN {frappe.db.escape(region)}"
	expresion += " END"
	frappe.cache().set_value(CACHE_KEY, expresion, expires_in_sec=CACHE_TTL)
	return expresion