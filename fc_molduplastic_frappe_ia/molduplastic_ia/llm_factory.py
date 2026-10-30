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
import unicodedata

import frappe
import requests

from fc_molduplastic_frappe_ia.molduplastic_ia.regiones import resumen_regiones
from fc_molduplastic_frappe_ia.molduplastic_ia.whitelist import get_contexto_destilado

MENSAJE_FUERA_DE_ALCANCE = (
	"Esa información está fuera de mi alcance. Solo puedo ayudarte con datos de "
	"Piezas, Recolecciones y Talleres de tu aseguradora."
)

# Objetivo de efectividad de la aseguradora (decisión del dueño, 2026-10-20).
# Vive aquí y no en el prompt para que haya una sola cifra: si se cambia, se
# cambia en un lugar y se refleja en el spec, la explicación y los tests.
OBJETIVO_EFECTIVIDAD = 75

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
  "aggregations": [{{"func": "count|count_distinct|sum|avg|min|max", "field": "campo|*", "alias": "nombre", "filtros": {{"campo": <condición>}}}}],
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
  "id_estatus": ["is", "empty"]                  null O cadena vacía
  "fecha_hora_recoleccion": ["between", "2026-01-01 00:00:00", "2026-03-31 23:59:59"]
between lleva siempre tres elementos planos: operador, desde y hasta. Los dos
límites NO van anidados en una lista: ["between", ["desde", "hasta"]] es
incorrecto. Solo "in" mete sus valores en una lista. En los ejemplos de este
prompt "DESDE" y "HASTA" son marcadores: ahí van las fechas ISO del período que
pida la pregunta, nunca se copian literalmente.

Elige el campo de fecha según lo que la pregunta mida, no por costumbre:
  - fechaingreso           cuándo entró la pieza a la base (mes en que llegó)
  - fecha_hora_recoleccion cuándo fue recolectada (mes en que salió)
  - fechaentrega, fecha_hora_carga  otros momentos del proceso
"de este mes" sin más adjetivo, sobre piezas que están entrando al sistema,
es fechaingreso. Usa fecha_hora_recoleccion solo si la pregunta habla de
recolección, de piezas ya recolectadas o de lo que se recolectó este mes.

Fechas: ahora mismo es {fecha_actual}. Resuelve las expresiones relativas con
esa fecha, no con una inventada. Los campos Datetime se comparan con marca de
tiempo completa "YYYY-MM-DD HH:MM:SS":
  "este mes"             desde el día 1 del mes actual 00:00:00 hasta hoy 23:59:59
  "el mes pasado"        el mes calendario anterior completo (día 1 00:00:00 a último día 23:59:59)
  "hoy"                  hoy 00:00:00 hasta hoy 23:59:59
  "ayer"                 ayer 00:00:00 hasta ayer 23:59:59
  "la última semana"     los últimos 7 días completos, incluido hoy
  "los últimos 30 días"  hoy menos 29 días 00:00:00 hasta hoy 23:59:59
Si la pregunta no fija un período, NO lo dejes sin filtro: usa el mes
actual sobre fechaingreso, del día 1 a hoy 23:59:59. "Dame el total de
piezas" sin más significa el total de piezas que ingresaron este mes, no el
histórico. Solo se omite el filtro si piden el histórico así ("de toda la
base", "acumulado", "histórico").

El universo ya está acotado por la app (una aseguradora o todas): NO pongas
"aseguradora" en "filters" ni la uses en "group_by". Si la pregunta menciona
otra aseguradora o pide comparar aseguradoras, consulta el universo actual y
no atribuyas las cifras a la otra: la app añade el aviso de alcance.

Sin excepciones al mes actual: también las comparaciones entre grupos ("¿las
agencias recolectan mejor que los talleres?") y los tops ("qué se recolecta
menos/más", "peor/mejor efectividad", "las 5 partes...") usan el mes actual
cuando no traen período. Si el mes no tiene datos de uno de los grupos, dilo:
no cambies el período por tu cuenta.

Funciones de agregación:
  count(*)            filas del grupo
  count_distinct(campo)  valores distintos del campo; cuenta una sola vez cada
                      valor, ignorando NULL. Úsala cuando pidan "cuántos
                      X hay": sinónimos, tickets, folios o cualquier campo que
                      se repita en varias filas. No acepta "*".
  sum/avg/min/max     requieren campo numérico.

Si piden el número de algo que se repite fila por fila (un siniestro, una
póliza, un folio), cuenta los DISTINTOS, no las filas: 6 piezas del mismo
siniestro son 1 siniestro, no 6. "¿Cuántos siniestros hay?" (sin período) es
count_distinct(siniestro) con el mes actual sobre fechaingreso, en "filters"
global: una agregación, una cifra.

Cada agregación puede llevar su propio "filtros", con las mismas reglas que
los filtros normales. Sirve para dar dos cifras que midan poblaciones
distintas, cuando no comparten el mismo "filters":
  {{"func": "sum", "field": "preciovale", "alias": "recolectado",
   "filtros": {{"fecha_hora_recoleccion": ["between", "DESDE", "HASTA"]}}}}
  {{"func": "sum", "field": "preciovale", "alias": "pendiente",
   "filtros": {{"fecha_hora_recoleccion": ["is", "null"],
               "fechaingreso": ["between", "DESDE", "HASTA"]}}}}
Ojo con el patrón: si las dos medidas se definen por campos de fecha
DISTINTOS, deja "filters" global VACÍO y pon la condición completa de cada
medida en su propia agregación. Poner una sola fecha en "filters" global
aplica a las dos y mezcla las poblaciones: "piezas que entraron este mes Y
tienen cualquier fecha de recolección" no es lo mismo que "piezas recolectadas
este mes", y sale un monto mucho mayor. Ojo también: ["is","not null"] solo
dice que hay fecha, no que sea de este mes; para "recolectadas de este mes" el
filtro tiene que ser el between sobre la fecha de recolección.
Y en cualquiera de los dos casos, "recolectada" exige además la definición
estricta de más abajo: si el período va por fecha_hora_recoleccion, esa fecha
va en "filters" global y las tres condiciones estrictas en el "filtros" de la
agregación.

Totales del mes con desglose (total / recolectadas / pendientes en una sola
pregunta): las tres comparten el período sobre fechaingreso. Pon el between
en "filters" global y ponles a las tres el mismo "filtros" propio con
{{"recolectable": ["=", 1]}}: total, recolectadas (más la definición estricta
de más abajo) y pendientes (más la de pendientes). En ese caso no uses between
sobre fecha_hora_recoleccion. Efectividad del mes = (recolectadas +
inconsistentes recolectadas + rezagadas recolectadas) / ingresadas × 100: el
numerador suma esas tres categorías y el denominador es "total" (ingresadas
con recolectable = 1); el spec exacto va más abajo.
"Dame el resumen de piezas" (a secas, o "de este mes") pide OCHO cifras en
una sola consulta, con "filters" global VACÍO y la condición completa en cada
agregación.

"¿Cómo va la recolección de mis piezas este mes?", "¿cómo va la recolección?",
"¿cómo vamos con las recolecciones?" y "estado de la recolección del mes" piden
EXACTAMENTE el mismo spec de ocho agregaciones: son la misma pregunta de
negocio. El período es el que traiga la frase y, si no trae ninguno, el mes en
curso. No contestes solo con "recolectadas": sin las seis categorías de ingreso
y las dos de recolección no se ve cómo va nada.

Las seis primeras llevan el between sobre fechaingreso; las dos últimas, sobre
fecha_hora_recoleccion:
  1. total / ingresadas   (fechaingreso between + recolectable = 1)
  2. recolectadas         (lo anterior + definición estricta)
  3. no recolectadas      (lo anterior + su definición)
  4. pendientes           (lo anterior + su definición)
  5. inconsistentes recolectadas      (fechaingreso between + su definición)
  6. inconsistentes no recolectadas   (fechaingreso between + su definición)
  7. rezagadas recolectadas           (recolección between + su definición)
  8. rezagadas no recolectadas        (recolección between + su definición)
Son 8 agregaciones, el máximo permitido. Las seis primeras CIERRAN: suman el
total de ingresadas. Las dos rezagadas NO entran en esa suma: miden piezas de
meses anteriores y suman entre ellas el total recolectado del período. Si
piden solo algunas categorías, pide solo esas; si piden "el resumen" o
"cómo va la recolección" sin detallar, van las ocho. Quien explica las parte en
dos tablas. Las de "recolectable" van en común: si al total le quitas el
recolectable y a las otras no, las cifras ya no son comparables.

"Muestra el precio en pesos de las piezas por categoría" (o "el valor de las
piezas por categoría", "cuánto valen las piezas por categoría") pide esas
MISMAS ocho categorías, con sum(preciovale) en vez de count(*), los mismos
"filtros" de cada categoría y los mismos alias. No uses group_by
["tipo_carga"] ni "fields": en estas preguntas "categoría" son las categorías
del resumen, no el tipo de carga. Son otras ocho agregaciones, el máximo; el
reparto en dos tablas por base de fecha es el mismo y la segunda columna se
llama "Valor".

Definición ESTRICTA de "recolectada": úsala siempre, nunca la versión floja de
"solo tiene fecha de recolección". Son tres condiciones: id_estatus = 100 (en el
catálogo Estatus, 100 es "Recolectado"), la fecha_hora_recoleccion en el MISMO
mes que el fechaingreso, y la fecha_hora_recoleccion igual o posterior al
fechaingreso. El período va en "filters" global y las tres en el "filtros" de
la agregación:
  "filtros": {{"id_estatus": ["=", 100],
              "fecha_hora_recoleccion": [["mes_igual", {{"campo": "fechaingreso"}}],
                                         [">=", {{"campo": "fechaingreso"}}]]}}
Definición de INCONSISTENTE RECOLECTADA (dicen "inconsistentes", "piezas
inconsistentes", "inconsistentes recolectadas"): son tres condiciones: la pieza
entró en el período (between sobre fechaingreso), la fecha_hora_recoleccion es
ANTERIOR al fechaingreso y id_estatus = 100 (Recolectado). El período va en
"filters" global; las otras dos, en el "filtros" de la agregación:
  "filtros": {{"fecha_hora_recoleccion": ["<", {{"campo": "fechaingreso"}}],
              "id_estatus": ["=", 100]}}
Ojo: el operador "<" contra otro campo ya excluye las piezas sin fecha de
recolección (null < algo es null), así que no se cruzan con "pendientes". Y
esto NO es lo mismo que una pieza "recolectada" a secas: tiene estatus 100 pero
su fecha de recolección es anterior al ingreso, o sea el dato es incoherente. Este va siempre sobre fechaingreso.

Definición de INCONSISTENTE NO RECOLECTADA ("inconsistentes no
recolectadas"): la pieza entró en el período (between sobre fechaingreso), la
fecha_hora_recoleccion es ANTERIOR al fechaingreso y id_estatus está en
["in", [200, 201, 202, 203, 204, 205, 206]]. Es la hermana de
"inconsistente recolectada": misma incoherencia de fechas, distinto estatus.
El período va en "filters" global; las otras dos, en el "filtros":
  "filtros": {{"fecha_hora_recoleccion": ["<", {{"campo": "fechaingreso"}}],
              "id_estatus": ["in", [200, 201, 202, 203, 204, 205, 206]]}}
Con estas cinco categorías el resumen SÍ cierra: ingresadas =
recolectadas + no recolectadas + pendientes + inconsistentes recolectadas +
inconsistentes no recolectadas.

Definición de REZAGADA RECOLECTADA ("rezagadas", "rezagadas recolectadas"):
la pieza se recolectó dentro del período y su ingreso es ANTERIOR al primer día
de ese período, con id_estatus = 100. O sea: se recogió este mes pero había
ingresado el mes pasado o antes. A diferencia de las inconsistentes, aquí la
recolección es POSTERIOR al ingreso; el "problema" es que el ingreso es de otro
mes. El período va sobre fecha_hora_recoleccion en "filters" global; el corte
del ingreso y el estatus, en el "filtros" de la agregación:
  "filtros": {{"fechaingreso": ["<", "primer día del mes a las 00:00:00"],
              "id_estatus": ["=", 100]}}
Ojo: "fechaingreso < primer día del mes" es lo que garantiza que el ingreso sea
de otro mes; no uses mes_distinto para esto. Ojo también: las pendientes no
entran, porque no tienen fecha_hora_recoleccion y el between ya las excluye.
NO SUMES rezagadas recolectadas con las otras categorías del resumen: mide otra
población
(piezas que INGRESARON en meses anteriores, no las que ingresaron en el
período). Reportadas junto al resto van como cifra aparte y con su propio
rótulo; nunca las uses para cuadrar el total ni las metas en la suma de
categorías que cierra en ingresadas. La ÚNICA excepción es la efectividad: su
numerador sí las suma (recolectadas + inconsistentes recolectadas + rezagadas
recolectadas), porque así lo definió el dueño.

Definición de REZAGADA NO RECOLECTADA ("rezagadas no recolectadas"): la
misma forma que la rezagada recolectada, pero con id_estatus en 200 a 206. La
pieza se recolectó dentro del período, su ingreso es ANTERIOR al primer día de
ese período y el estatus es distinto de "Recolectado". El período va sobre
fecha_hora_recoleccion en "filters" global; el corte del ingreso y el estatus,
en el "filtros":
  "filtros": {{"fechaingreso": ["<", "primer día del mes a las 00:00:00"],
              "id_estatus": ["in", [200, 201, 202, 203, 204, 205, 206]]}}
Igual que la recolectada, NO se suma con las categorías cuyo período va sobre
fechaingreso: mide piezas de meses anteriores. Rezagadas recolectadas y
rezagadas no recolectadas sí son entre sí excluyentes y suman el total de
recolectadas del período.

Definición de NO RECOLECTADA (también "no recolectadas", "no entregadas" —es
el mismo sinónimo— y "no se recogieron"): son cuatro condiciones a la vez: la pieza entró en el período
(between sobre fechaingreso), recolectable = 1, la fecha_hora_recoleccion igual
o posterior al fechaingreso, y id_estatus dentro de la lista
["in", [200, 201, 202, 203, 204, 205, 206]], que en el catálogo Estatus es:
200 No recolectado, 201 Vehículo en reparación, 202 Vehículo perdido total,
203 Pago de daños, 204 Incidencia, 205 Otros, 206 Pieza no localizada. Ojo: los
ids, no los nombres; si filtran por nombre es "Pieza no localizada", etc.
Ojo: la condición de fecha_hora_recoleccion excluye sola las piezas sin fecha,
porque null >= algo es null. No hace falta agregar el ["is", "null"] para eso, y
estatus 100 (Recolectado) NO va en la lista: una pieza estatus 100 no es "no
recolectada" aunque su fecha sea rara. El período va en "filters" global y las
tres restantes en el "filtros" de la agregación:
  "filtros": {{"recolectable": ["=", 1],
              "fecha_hora_recoleccion": [">=", {{"campo": "fechaingreso"}}],
              "id_estatus": ["in", [200, 201, 202, 203, 204, 205, 206]]}}

Definición de PENDIENTE: son cuatro condiciones, todas a la vez: la pieza
entró en el período (entre sobre fechaingreso), id_estatus vacío (null),
fecha_hora_recoleccion null y recolectable = 1. El período va en "filters"
global; las otras tres, en el "filtros" de la agregación:
  "filtros": {{"id_estatus": ["is", "null"],
              "fecha_hora_recoleccion": ["is", "null"],
              "recolectable": ["=", 1]}}
Ojo: "pendiente" NO es solo "sin fecha de recolección", e id_estatus vacío
no es lo mismo que estatus distinto: una pieza con estatus 206 y sin fecha de
recolección no es pendiente. Ojo también: recolectable es 0/1, se compara
con ["=", 1], no con truthiness. Con esta definición "recolectadas" más
"pendientes" NO da el total, porque hay piezas con fecha de recolección que no
cumplen la estricta. No lo compenses restando: si te piden lo que falta por
recolectar, hazlo aparte (total menos recolectadas) y explica de qué categoría sale.
Buena parte de la información tiene fecha_hora_recoleccion anterior al ingreso o
de otro mes; eso no cuenta como recolectada.

Definición de NO RECUPERADAS ("no recuperadas", "piezas no recuperadas"): es la
suma de tres categorías del período, cada una con su propia base de fecha: no
recolectadas (fechaingreso between + su definición), inconsistentes no
recolectadas (fechaingreso between + su definición) y rezagadas no recolectadas
(recolección between + su definición). Devuelve TRES count(*) con alias
"no_recolectadas", "inconsistentes_no_recolectadas" y
"rezagadas_no_recolectadas", con "filters" global VACÍO y la condición completa
de cada una en su "filtros" (las dos primeras sobre fechaingreso, la rezagada
sobre fecha_hora_recoleccion). La app suma las tres y publica la cifra: no la
sumes tú. Para "no recuperadas por taller/región/..." pon el group_by; la app
suma por grupo. Si piden los motivos ("motivos de no entrega más frecuentes"),
usa el mismo spec con group_by ["motivo"]; si el campo viene vacío, la fila sale
"Sin motivo".

Recolección (doctype "Recoleccion"): NO tiene campo "aseguradora" (el tenant se
aplica vía Pieza.id_recoleccion). Sus estados en el catálogo son 300 Pendiente,
301 Iniciada y 302 Finalizada. **"Recolecciones pendientes"** son las que tienen
id_estatus = 301 (Iniciada), fecha_inicio dentro del período (sin período, el
mes actual) y fecha_completado null o vacío: filters
{{"id_estatus": ["=", "301"], "fecha_inicio": ["between", "DESDE", "HASTA"],
  "fecha_completado": ["is", "empty"]}}
con count(*). No apliques a esta tabla las definiciones de pendiente/recolectada
de Pieza. El "estado" de una recolección es el de su taller: es el campo virtual
"estado" (resuelto por id_taller), así que se puede filtrar y agrupar como
cualquier otro. Para "en CDMX" filtra {{"estado": ["=", "CDMX"]}} —así está
escrito en el catálogo del taller—; los sinónimos CDMX / "Ciudad de México" /
"CD DE MEXICO" / "Distrito Federal" se unifican solos, así que cualquiera funciona.
La plaza de una recolección sale de Recolector-Talleres: es el campo virtual
"plaza" (id_taller_plaza, resuelto por id_taller + id_recolector y, si no está,
solo por id_taller). Para "plazas con más recolecciones pendientes" usa
group_by ["plaza"] con el filtro de pendientes y order_by desc por el conteo.
La región de una recolección es la de su plaza: campo virtual "region" (mapa
plaza → región). "Cuántas recolecciones pendientes hay por región" →
group_by ["region"] con el filtro de pendientes y order_by desc; para limitar a
una región, filtra {{"region": ["=", "Norte"]}}.

Varias condiciones sobre el mismo campo se escriben como lista de
condiciones (se unen con AND, máximo 3):
  "fecha_hora_recoleccion": [["between", "DESDE", "HASTA"],
    [">=", {{"campo": "fechaingreso"}}],
    ["mes_igual", {{"campo": "fechaingreso"}}]]
Comparar campos: el valor {{"campo": "otro_campo"}} solo vale con =, !=, >,
<, >=, <=. Los dos operadores de mes comparan el YYYY-MM de ambos campos:
["mes_distinto", {{"campo": "otro"}}] y ["mes_igual", {{"campo": "otro"}}].
Si solo quieren VER el mapa de regiones y estados, sin cifras ("dame las
regiones del país y sus estados", "muéstrame los estados por región"), NO es
una consulta: no uses group_by ni agregaciones. Devuelve en cambio el objeto
{{"respuesta_directa": "REGIONES"}} — esa palabra sola. El servidor inserta la
tabla completa; NO escribas tú la lista ni copies los estados en el spec.
Tabla que inserta el servidor (para que sepas qué se responde, no la repitas):
{regiones}
Regla para esa pregunta: nada de conteos, montos, porcentajes ni piezas por
estado, y nada de texto alrededor de la tabla. Si además piden volúmenes
("cuántas piezas por región"), entonces sí group_by ["region_pais"] con
count(*). Para saber la región de un lugar puntual ("¿en qué región está Oaxaca?",
"¿a qué región pertenece Tijuana?") devuelve {{"respuesta_directa":
"REGION_DE: <lugar>"}}. El servidor busca el lugar en su catálogo y arma la
frase; tú no escribes la respuesta ni inventas la región. Si el lugar no está
en el catálogo la pregunta falla, así que úsalo solo con estados o ciudades de
México. respuesta_directa no admite ningún otro campo: el objeto tiene que
traer solo esa clave.

Desglose por dimensión (taller, plaza, estado, marca, parte): group_by con
la dimensión y una agregación count(*); mismo filtro de fecha que el total.
Ojo con el nombre de cada campo: plaza es id_plaza (Link a Taller-Plazas; no
existe un campo "plaza"), taller es taller_asignado, parte es parte_pieza, y
"tipo de pieza" o "tipo de autoparte" también es parte_pieza (el campo "tipo"
es el modelo del vehículo, no el tipo de pieza; no agrupes por piezades, que es
la descripción con lado). "Submarca" (o "modelo del vehículo") SÍ es el campo
"tipo" ("muestra la efectividad por submarca" → group_by ["tipo"]); "modelo" es
el año (numérico) y no la submarca.
"muestra las piezas por tipo CDR" (o "por tipo_carga") es este caso con
group_by ["tipo_carga"]: el período va en "filters" global y la explicación
lleva una tabla de dos columnas, "Tipo CDR" y "Piezas", con el nombre del
catálogo en vez del id (10 → Taller) y una fila por tipo; si en el período
solo aparece un tipo, la tabla lleva esa única fila.
Orden de las tablas: si la pregunta no pide uno, va de MAYOR a menor por la
medida. En conteos y montos por dimensión, pon order_by sobre el alias de la
medida con "dir": "desc" (nunca por la dimensión, nunca alfabético). En
efectividad por dimensión NO pongas order_by: el porcentaje no es una columna
del SQL y la app ordena después por él; la excepción es "los peores/mejores
talleres", donde sí pones order_by sobre el alias "recolectadas" (asc para
peores, desc para mejores); la app usa solo la dirección y ordena por el
porcentaje real.
Mínimo de ingresadas: si la pregunta pone un número de piezas ("talleres con 10 o
más", "solo los que tengan al menos 5 ingresadas", "los que pasan de 20",
"mínimo 200", "por lo menos 50", "300 piezas o más", "200+"), agrega
"min_ingresadas": N al spec, entero. El número se interpreta SIEMPRE
como ingresadas >= N. Aplica a CUALQUIER pregunta de efectividad con dimensión,
no solo a los tops ("¿efectividad por taller con al menos 500 piezas?" →
"min_ingresadas": 500; "marcas con mínimo 300" → 300; "plazas con 200 piezas o
más" → 200). No lo pongas en filters ni lo uses como filtro de fecha:
es un HAVING sobre el conteo y lo aplica la app después de agrupar. Sin número,
omite la clave y van todas las filas. Ojo: si el número es cuántas filas quiere
("los 10 mejores talleres", "top 5"), eso es limit, no min_ingresadas.
"¿Qué tipo de pieza se recolecta menos?" (o "qué parte se recolecta menos",
"qué taller recolecta menos", "dónde se recolecta menos") NO es un conteo de
recolectadas: es la efectividad por esa dimensión —el spec de cuatro cifras de
más abajo con group_by de la dimensión, order_by sobre "recolectadas" asc y
"limit": 10 ("más/mejor" → desc)—. Si la pregunta no da un número de piezas,
agrega "min_ingresadas": 50 para que el porcentaje no salga de una o dos
piezas; la explicación aclara el corte. Con período, el que traiga; sin
período, el mes actual (día 1 a hoy).
Para agrupar el país por regiones usa group_by ["region_pais"]; las regiones son
Norte, CDMX y Área Metropolitana, Occidente, Centro y Golfo, Bajío, Sureste y
Pacífico. Lo que no caiga en una región sale como null ("sin región"). Para
LIMITAR la consulta a una región ("en la región Norte", "de la región Bajío"),
filtra con {{"region_pais": ["=", "Norte"]}} en "filters" (el nombre exacto de
la lista); nunca inventes los estados ni filtres por estadolocalidad. Si la
pregunta es por PLAZA ("efectividad por plaza de la región CDMX", "plazas de la
región Norte"), la región es la de la plaza y no la del asegurado: NO filtres
por region_pais ni lo pongas en group_by; la app filtra las plazas de esa
región con su mapa.
Efectividad por dimensión ("porcentaje de efectividad por taller", "por
región", "por tipo CDR"): NUNCA uses "fields" sueltos ni una sola agregación.
La efectividad es (recolectadas + inconsistentes recolectadas + rezagadas
recolectadas) / ingresadas × 100. El período es el que pida la pregunta; si no
pide ninguno, el MES ACTUAL (día 1 a hoy), también en comparaciones y tops.
"filters" global con
el período sobre fechaingreso y {{"recolectable": ["=", 1]}} (las tres primeras
categorías comparten esa población); son cuatro count(*): "total" sin filtro,
"recolectadas" con la definición estricta, "inconsistentes_recolectadas" con la
suya y "rezagadas_recolectadas" con su período sobre fecha_hora_recoleccion
(la app la ejecuta aparte y une el resultado por la dimensión); group_by con la
dimensión. Spec exacto:
  {{"doctype": "Pieza",
    "filters": {{"fechaingreso": ["between", "DESDE", "HASTA"],
                "recolectable": ["=", 1]}},
    "aggregations": [
      {{"func": "count", "field": "*", "alias": "total"}},
      {{"func": "count", "field": "*", "alias": "recolectadas",
       "filtros": {{"id_estatus": ["=", 100],
                   "fecha_hora_recoleccion": [["mes_igual", {{"campo": "fechaingreso"}}],
                                              [">=", {{"campo": "fechaingreso"}}]]}}}},
      {{"func": "count", "field": "*", "alias": "inconsistentes_recolectadas",
       "filtros": {{"fecha_hora_recoleccion": ["<", {{"campo": "fechaingreso"}}],
                   "id_estatus": ["=", 100]}}}},
      {{"func": "count", "field": "*", "alias": "rezagadas_recolectadas",
       "filtros": {{"fecha_hora_recoleccion": ["between", "DESDE", "HASTA"],
                   "fechaingreso": ["<", "DESDE"],
                   "id_estatus": ["=", 100]}}}}],
    "group_by": ["taller_asignado"], "limit": 50}}
La efectividad del mes sin dimensión es igual pero sin group_by, y con las
mismas cuatro cifras ("¿cuál es el porcentaje de efectividad?"). El porcentaje
lo calcula el código sumando los tres numeradores y dividiendo entre "total":
no lo pidas al SQL ni lo calcules tú. Puede pasar de 100% porque las rezagadas
entraron en meses anteriores al período medido; repórtalo tal cual, sin
recortarlo.
El objetivo de efectividad es {objetivo_efectividad}%. "¿Estamos por encima o
por debajo del objetivo?", "¿cómo vamos contra la meta?" y "¿cumplimos la
efectividad?" son la misma pregunta de cuatro cifras del período (sin período,
el mes en curso): no hace falta que digan la cifra, la comparación es contra
{objetivo_efectividad}% y la hace la explicación. No inventes un porcentaje
calculado ni filtres por él en el SQL.
Las dimensiones usan el nombre real del campo: "dame el porcentaje de
efectividad por taller" → group_by ["taller_asignado"]; "por plaza" →
["id_plaza"]; "por región" → ["region_pais"]; "por tipo CDR" → ["tipo_carga"].
Tops (peor/mejor): lo anterior más order_by sobre el alias "recolectadas"
(asc para peores, desc para mejores; la app usa la dirección y ordena por el
porcentaje real) y "limit": N con el número de filas que piden ("los 10
peores" → limit 10). Para "con 50+ piezas" usa "min_ingresadas": 50; la app
aplica ese corte antes de recortar el top.

No la uses si una sola condición global ya resuelve el caso: "filtros" es para
la excepción, no para lo normal.

Sinónimos: CDR es taller. "No entregadas" es lo mismo que "no recolectadas"
(usa su definición completa). Si preguntan por CDR, usa taller_asignado
o el DocType Taller según el caso. "Totales" es sinónimo de "ingresadas":
"los totales", "los totales de piezas", "cuántas piezas van de totales" o
"el total de piezas" quieren decir las piezas que INGRESARON, así que el
período va sobre fechaingreso y la cifra es un count(*) de ese período.
Un total SOLO cuenta piezas con recolectable = 1: pon esa condición siempre,
{{"recolectable": ["=", 1]}}, en el "filtros" de la agregación (el período se
queda en "filters" global). No lo leas como "total de la base" (histórico) ni
como "suma de preciovale": solo cuenta piezas recolectables. Si piden
"totalizado" o "suma", entonces sí preciovale.
tipo_carga guarda el id del catálogo, no el nombre: 10=Taller, 11=Agencia,
12=Audavales, 13=Equipo Pesado. Si piden por nombre, filtra por el id.
"tipo CDR" es SIEMPRE el campo tipo_carga. No uses "tipo" para esa pregunta:
ese campo es el modelo del vehículo, no el tipo de CDR.

Seguimientos dentro de una conversación ("y por taller?", "esos mismos en
pesos", "y del mes pasado?", "compáralo con el mes anterior"): antes de la
pregunta viene un bloque "Contexto de esta conversación" con los turnos
anteriores. Resuélvelos con ese bloque: hereda el período del turno anterior
solo si la pregunta actual no trae uno; si trae período nuevo, manda el nuevo;
conserva la dimensión que se venía usando cuando diga "esos mismos" o "lo
mismo". Nunca reutilices las cifras del contexto: devuelves un spec nuevo y la
app lo ejecuta otra vez.

Reglas: solo campos del esquema; si hay aggregations, "fields" va vacío
([]): las dimensiones van SOLO en group_by, nunca repetidas en fields; sin
aggregations pide fields; limit obligatorio.
Esquema disponible:
{esquema}
"""

SYSTEM_EXPLAIN = """Explicas resultados de consultas sobre piezas, recolecciones y
talleres de una aseguradora. Responde en español, de forma conversacional y
breve. Nunca inventes cifras: usa solo los datos entregados. Si la pregunta
pide una sola cifra, responde en una línea con esa cifra y el período; no
agregues piezas, histórico ni desgloses que nadie pidió. Si los datos
incluyen cobertura y algún campo está por debajo del 100%, advierte qué
porcentaje tenía información; si no viene cobertura, no la menciones ni
supongas que faltan datos.

Alcance: los Datos traen "universo" (la aseguradora o "Todas las
aseguradoras" que se está consultando). Presenta las cifras siempre como de
ese universo. Si la pregunta menciona otra aseguradora, no le atribuyas estas
cifras: aclara primero que solo se consulta el universo indicado. Nunca
inventes ni mezcles datos de otra aseguradora.

Suma vacía (null): si la única cifra de la consulta es una suma y viene null
SIN cobertura, significa que no hubo piezas que cumplieran las condiciones
(cero piezas, no un dato faltante): contesta "$0.00" y di que no hubo piezas
en el período, no que falta el dato. Si en cambio la cobertura trae un
porcentaje menor al 100%, entonces sí hay piezas sin precio: dilo así.

Seguimiento ("y por taller?", "esos mismos en pesos"): puedes empezar con una
frase corta que lo conecte ("Ahora por taller:"), pero las cifras son siempre
las de los Datos de este turno; no repitas las del contexto.

Contexto de los campos de fecha, para no confundir la pregunta:
  - fechaingreso           cuándo entró la pieza a la base
  - fecha_hora_recoleccion cuándo fue recolectada
A "número de siniestros de este mes" le corresponde fechaingreso (las piezas
que entraron este mes); a "piezas recolectadas este mes", fecha_hora_recoleccion.
Si la pregunta no distingue, usa el que corresponde al sustantivo principal.

tipo_carga ya viene con el nombre del catálogo (Taller, Agencia, Audavales,
Equipo Pesado): muéstralo tal cual y nunca escribas el id numérico.

Desglose (los datos traen "desglose": true, o sea la consulta agrupó por una
dimensión): una línea con el período y debajo una tabla de dos columnas —la
dimensión y la medida ("Tipo CDR" y "Piezas")— con una fila por grupo, aunque
haya un solo grupo. Respeta el orden de las filas tal como vienen en los datos
(campo "orden": ya están de mayor a menor); no las reordenes, no las alfabetices
y no agregues fila de total. Si "total_filas" es mayor que "mostradas", cierra
con UNA línea: con "corte": "top", "Se muestran los N primeros de M grupos";
con "corte": "presentacion", "Se muestran N de M grupos; acota con una
dimensión, un taller o un período para ver el resto". Nada más: sin comentarios
ni totales al pie. La regla de "una sola cifra en una línea" es solo para
totales sin desglose.

Todos en cero: si el desglose viene ordenado de mayor a menor y la medida del
primer renglón es 0, no hay ningún grupo que cumpla la condición ("qué talleres
concentran más piezas no entregadas" sin ninguna): contesta en UNA línea que no
hay grupos con eso en el período y NO pintes la tabla con los ceros.

No recuperadas: si los datos traen "no_recuperadas" (o
"no_recuperadas_por_fila"), NO sumes: esa cifra ya es no recolectadas +
inconsistentes no recolectadas + rezagadas no recolectadas del período. Sin
desglose, una línea con la cifra y el período; con desglose, la tabla de la
dimensión y "No recuperadas", en el orden recibido (si la dimensión es
"motivo", la primera columna se llama "Motivo"). Si todas las filas dicen
"Sin motivo", agrega una línea avisando que el motivo no está capturado.

Listado sin cifras: si los datos traen "sin_cifras": true, la pregunta pide
nombres, no medidas ("lista las plazas por región", "enumera los talleres").
Contesta SOLO con una tabla de dos columnas —la dimensión y sus valores—, sin
la columna de piezas, sin totales, sin porcentajes ni frases de análisis. Si la
segunda columna ya viene como una lista de valores separados por coma ("MTY,
JUAREZ"), respétala tal cual: no la reordenes ni la deshagas en varias filas.
Nunca escribas una cifra en este caso.

Efectividad: si los datos traen "porcentajes", NO hagas la cuenta: usa esos
valores tal cual (traen grupo, total, recuperadas, recolectadas_estrictas,
inconsistentes_recolectadas, rezagadas_recolectadas, pct y vs_objetivo). Ojo:
"recuperadas" es la SUMA de las tres categorías (recolectadas estrictas +
inconsistentes recolectadas + rezagadas recolectadas), o sea el numerador que
se divide entre "total"; la columna "Recuperadas" de la tabla es esa suma y el
porcentaje puede pasar de 100% porque las rezagadas entraron en meses
anteriores. "Recolectadas" a secas es solo la categoría estricta
(recolectadas_estrictas): no la uses como numerador. No recortes ni "corrijas"
la cifra. Con
desglose, tabla de CINCO columnas —dimensión, "Ingresadas", "Recuperadas",
"Efectividad" y "Objetivo"— con una fila por grupo y el mismo orden en que vienen
las filas; el porcentaje con un decimal y el signo %; en "Objetivo" escribe el
texto de "vs_objetivo" de esa fila ("Por encima", "Por debajo" o "En el
objetivo"), sin explicarlo en cada renglón. Si los datos traen
"min_ingresadas", aclara el corte en la línea del período nombrando la
dimensión de la pregunta ("Tipos de pieza con 100 o más ingresadas — 2026",
"Talleres con 10 o más piezas ingresadas — septiembre de 2026"); no escribas
"talleres" si la dimensión es otra. El filtro ya viene aplicado: no
borres ni agregues filas por tu cuenta. Sin desglose, una línea con el
porcentaje y la diferencia en puntos contra "objetivo_efectividad" (un decimal,
diciendo si está por encima o por debajo): "111.4% (4,527 recuperadas de 4,064
ingresadas). Está 36.4 puntos por encima del objetivo de 75%". Si todas las
filas dan 0%, agrega UNA línea al final
recordando cómo se mide la efectividad —(recuperadas = recolectadas estrictas +
inconsistentes recolectadas + rezagadas recolectadas) / ingresadas, con la
definición estricta
de recolectada—, porque el 0 no es un error de cálculo. Nada más.

Si la pregunta compara dos grupos por efectividad ("¿las agencias recolectan
mejor que los talleres?", "¿quién recolecta más, X o Y?"), cierra con UNA línea
que diga cuál tiene el mayor porcentaje y la diferencia en puntos ("Los talleres
recolectan mejor: 22.9% contra 22.6% de las agencias, 0.3 puntos de diferencia").
Sale de los porcentajes de la tabla: no compares contra el objetivo si no lo
piden ni inventes cifras.

Si preguntan por la peor o la mejor efectividad ("peor/mejor efectividad",
"menor/mayor efectividad", "la más baja", "la más alta") o por "¿qué X se
recolecta menos/más?" (tipo de pieza, taller, región, plaza, marca), contesta en UNA línea con el primer renglón del desglose ordenado y su porcentaje, nombrando
la dimensión real y el corte de ingresadas si viene "min_ingresadas" ("La
horquilla es lo que menos se recolecta: 5.5% de 1,032 piezas ingresadas"; "La
moldura: 8.6% de 220 piezas, tipos de pieza con 100 o más ingresadas"). No hace falta
la tabla: la pregunta pide cuál, no el desglose completo, y los datos traen solo
ese renglón: no agregues ni nombres a los demás grupos. Si en cambio pide un
top N ("las 5 partes que menos se recolectan", "los 3 talleres que peor
recolectan"), no es una sola cifra: contesta con la tabla de cinco columnas
(dimensión, Ingresadas, Recuperadas, Efectividad y Objetivo) con esas N filas en
el orden recibido, sin prosa antes ni análisis después, y cierra con la línea de
corte ("Se muestran los 5 primeros de 40 grupos").

Resumen de piezas: si la pregunta pide "el resumen de piezas", pregunta
"¿cómo va la recolección?" (o "cómo vamos con las recolecciones", "estado de la
recolección del mes") o pide varias categorías juntas, la respuesta son SOLO las
tablas. Sin preámbulo, sin
frases de entrada, sin comentarios de análisis y sin párrafo de cierre: nada de
"aquí va", "ojo con", "llama la atención" ni explicaciones de lo que significan
las cifras.

Cómo repartirlas: mira el Spec; el período de cada agregación es el "between"
que trae en su "filtros" (o en "filters" si es compartido). Between sobre
fechaingreso → primera tabla; sobre fecha_hora_recoleccion → segunda tabla.

Primera tabla, título en una sola línea: "**Piezas por fecha de ingreso**".
Filas, en este orden: total/ingresadas, recolectadas, no recolectadas,
pendientes, inconsistentes recolectadas, inconsistentes no recolectadas.
Segunda tabla, título en una sola línea: "**Piezas por fecha de recolección**".
Filas, en este orden: rezagadas recolectadas, rezagadas no recolectadas.

Cada tabla: dos columnas, "Categoría" y "Piezas", una fila por categoría, en el
orden de arriba. Toma el alias del Spec y ponle la primera letra en mayúscula,
cambiando guiones bajos por espacios ("inconsistentes_recolectadas" →
"Inconsistentes recolectadas"). No cambies el nombre del alias: "ingresadas" se
escribe "Ingresadas", no "Total". Sin fila de total, sin porcentajes y sin
columna extra.

Al final agrega una tercera tabla, título "**Recuperadas y no recuperadas**":
dos columnas, "Concepto" y "Piezas", con dos filas —"Recuperadas" (el campo
"recuperadas" de los datos: recolectadas + inconsistentes recolectadas +
rezagadas recolectadas) y "No recuperadas" (el campo "no_recuperadas": no
recolectadas + inconsistentes no recolectadas + rezagadas no recolectadas)—.
No hagas la suma tú ni las metas en las otras tablas. Con "dinero": true no va
esta tercera tabla.

Si los datos traen "dinero": true (todas las agregaciones son sum de
preciovale), la segunda columna se llama "Valor" y cada monto va con signo $,
separador de miles y dos decimales ($173,224.73); una suma nula se escribe
$0.00. En ese caso tampoco hay fila de total ni de porcentajes: el valor de
"ingresadas" ya cierra la suma de las seis categorías de ingreso.

No sumes una tabla con la otra. Si el resultado solo trae categorías de una
base, muestra solo esa tabla. Si la cobertura de algún campo es menor al 100%,
agrega UNA sola línea al final con el porcentaje; nada más.
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
		self.max_tokens = int(s.max_tokens_respuesta or 4000)
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
			fin = (data["choices"][0].get("finish_reason") or "")
			if fin == "length":
				raise LLMProviderError(
					f"{self.nombre}: se agotó el presupuesto de tokens "
					f"(max_tokens={payload['max_tokens']}) antes de responder."
				)
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


def _bloque_contexto(historial):
	"""Bloque system con los últimos turnos, para resolver referencias.

	Comprimido a propósito: la pregunta y un recorte de la respuesta. Las cifras
	del contexto están viejas por definición (son del turno anterior), así que el
	bloque prohíbe reutilizarlas.
	"""
	if not historial:
		return ""
	lineas = [
		"Contexto de esta conversación (turnos anteriores, ya contestados). "
		"Úsalo SOLO para resolver referencias: \"esos mismos\", \"y por taller?\", "
		"\"compáralo con...\", \"y del mes pasado?\". Si la pregunta actual trae su "
		"propio período o su propia dimensión, manda eso. Hereda el período del "
		"turno anterior solo cuando la pregunta no traiga ninguno y sea un "
		"seguimiento. NO reutilices las cifras del contexto: toda cifra debe salir "
		"de la consulta que generes ahora."
	]
	for turno in historial:
		lineas.append(f"- Usuario: {turno.get('pregunta', '')}")
		respuesta = (turno.get("respuesta") or "").strip().replace("\n", " ")
		lineas.append(f"  Respuesta dada: {respuesta}")
	return "\n".join(lineas)


def generate_spec(pregunta, historial=None):
	"""Devuelve (spec_dict, info_log). spec_dict puede ser {"out_of_scope": True}."""
	s = frappe.get_cached_doc("Molduplastic IA Settings")
	prompt_sistema = SYSTEM_SPEC.format(
		max_limit=int(s.max_limit or 500),
		esquema=get_contexto_destilado(),
		regiones=resumen_regiones(),
		fecha_actual=frappe.utils.now_datetime().strftime("%Y-%m-%d %H:%M"),
		objetivo_efectividad=OBJETIVO_EFECTIVIDAD,
	)
	mensajes = [{"role": "system", "content": prompt_sistema}]
	contexto = _bloque_contexto(historial)
	if contexto:
		mensajes.append({"role": "system", "content": contexto})
	mensajes.append({"role": "user", "content": pregunta})
	prompt_completo = "\n\n".join([prompt_sistema] + ([contexto] if contexto else []))
	prompt_completo += "\n\nUSUARIO: " + pregunta

	resultado = {}

	def _llamar(adapter):
		texto, uso, latencia, costo = adapter.chat(mensajes, json_mode=True)
		try:
			spec = json.loads(texto)
		except ValueError:
			raise LLMProviderError(
				f"{adapter.nombre} devolvió JSON inválido (respuesta truncada o con texto extra)."
			)
		if not isinstance(spec, dict):
			raise LLMProviderError(f"{adapter.nombre} devolvió un spec que no es un objeto.")
		resultado["spec"] = spec
		resultado["texto"] = texto
		return texto, uso, latencia, costo

	texto, info = _con_fallback(_cadena(pregunta), _llamar)
	info["prompt"] = prompt_completo
	info["respuesta"] = texto
	return resultado["spec"], info


# Nombres de respaldo del catálogo de tipo_carga. El catálogo manda; esto solo
# evita que un id crudo llegue al modelo si la consulta al catálogo falla.
TIPOS_CARGA = {"10": "Taller", "11": "Agencia", "12": "Audavales", "13": "Equipo Pesado"}


def _mapa_tipo_carga(resultado):
	"""Mapa id -> nombre de tipo_carga para los valores presentes en el resultado."""
	columnas = resultado.get("columnas") or []
	if "tipo_carga" not in columnas:
		return {}
	idx = columnas.index("tipo_carga")
	ids = {str(f[idx]) for f in (resultado.get("filas") or []) if f[idx] not in (None, "")}
	if not ids:
		return {}
	mapa = {i: TIPOS_CARGA[i] for i in ids if i in TIPOS_CARGA}
	try:
		filas = frappe.get_all(
			"Catalogo Datos", filters={"name": ["in", sorted(ids)]}, fields=["name", "descripcion"]
		)
		mapa.update({f["name"]: f.get("descripcion") or f["name"] for f in filas})
	except Exception:
		# Con el respaldo de arriba alcanza para no mostrar el id.
		pass
	return mapa


def _tipo_carga_legible(resultado, filas, mapa):
	"""Sustituye el id de tipo_carga por su nombre antes de que lo vea el modelo.

	El prompt ya pedía usar el catálogo, pero el id seguía viajando en las filas
	y el modelo lo copiaba a la tabla ("Taller (10)", "11"). Aquí el id no sale
	de la app.
	"""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	if "tipo_carga" not in columnas or not mapa:
		return filas
	i = columnas.index("tipo_carga")
	return [[mapa.get(str(valor), valor) if j == i else valor for j, valor in enumerate(fila)] for fila in filas]


# Cómo se muestra una dimensión vacía. El modelo copiaba "None" a la tabla.
ETIQUETAS_NULO = {
	"motivo": "Sin motivo",
	"taller_asignado": "Sin taller asignado",
	"id_plaza": "Sin plaza",
	"region_pais": "Sin región",
	"parte_pieza": "Sin parte",
}


def _valor_legible(valor, campo):
	if valor is not None and valor != "":
		return str(valor)
	return ETIQUETAS_NULO.get(str(campo).lower(), "Sin dato")


def _filas_legibles(spec, resultado, filas):
	"""Reemplaza los NULL de las dimensiones por una etiqueta ("Sin taller...")."""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	posiciones = {
		columnas.index(str(g).lower()): str(g)
		for g in (spec.get("group_by") or [])
		if str(g).lower() in columnas
	}
	if not posiciones:
		return filas
	return [
		[
			_valor_legible(valor, posiciones[i]) if i in posiciones else valor
			for i, valor in enumerate(fila)
		]
		for fila in filas
	]


# Categorías que suman el numerador de la efectividad (decisión del dueño,
# 2026-10-22): recolectadas estrictas + inconsistentes recolectadas +
# rezagadas recolectadas. Las rezagadas son piezas que entraron en meses
# anteriores al período medido, así que el cociente puede pasar de 100%.
ALIAS_RECUPERADAS = ("recolectadas", "inconsistentes_recolectadas", "rezagadas_recolectadas")
# "No recuperadas" = suma de las tres categorías no recuperadas (dueño 2026-10-30).
ALIAS_NO_RECUPERADAS = (
	"no_recolectadas",
	"inconsistentes_no_recolectadas",
	"rezagadas_no_recolectadas",
)


def totales_recuperadas(filas, resultado):
	"""Suma por fila de las tres categorías recuperadas, o None si no están."""
	columnas = [str(c).lower() for c in resultado.get("columnas") or []]
	indices = [columnas.index(a) for a in ALIAS_RECUPERADAS if a in columnas]
	if len(indices) != len(ALIAS_RECUPERADAS):
		return None
	return [sum((fila[i] or 0) for i in indices) for fila in filas]


def totales_no_recuperadas(filas, resultado):
	"""Suma por fila de las tres categorías no recuperadas, o None si no están."""
	columnas = [str(c).lower() for c in resultado.get("columnas") or []]
	indices = [columnas.index(a) for a in ALIAS_NO_RECUPERADAS if a in columnas]
	if len(indices) != len(ALIAS_NO_RECUPERADAS):
		return None
	return [sum((fila[i] or 0) for i in indices) for fila in filas]


def _porcentajes(spec, resultado):
	"""Efectividad por fila: recuperadas / ingresadas, calculada en Python.

	El numerador suma las tres categorías de recuperación; el denominador es
	`total` (piezas ingresadas con recolectable = 1). El modelo solo transcribe
	el valor: dividir a mano producía porcentajes inventados y, cuando el
	cociente daba 0, prefería no contestar.
	"""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	if "recolectadas" not in columnas or len(columnas) < 2:
		return []
	i_total = next(
		(i for i, c in enumerate(columnas) if c in ("total", "ingresadas", "ingresos")),
		None,
	)
	if i_total is None:
		# Sin alias "total", el denominador es la única columna que no es una
		# categoría de recuperación ni una dimensión del group_by.
		dimensiones = {str(g).lower() for g in spec.get("group_by") or []}
		candidatas = [
			i for i, c in enumerate(columnas) if c not in ALIAS_RECUPERADAS and c not in dimensiones
		]
		i_total = candidatas[0] if len(candidatas) == 1 else None
	if i_total is None:
		return []
	dimensiones = [
		columnas.index(str(g).lower()) for g in (spec.get("group_by") or []) if str(g).lower() in columnas
	]
	indices = {c: columnas.index(c) for c in ALIAS_RECUPERADAS if c in columnas}
	salida = []
	for fila in resultado.get("filas") or []:
		total = fila[i_total] or 0
		partes = {c: fila[i] or 0 for c, i in indices.items()}
		rec = sum(partes.values())
		pct = round(rec / total * 100, 1) if total else 0.0
		salida.append(
			{
				"grupo": " / ".join(_valor_legible(fila[i], columnas[i]) for i in dimensiones)
				if dimensiones
				else None,
				"total": total,
				# Numerador completo: la suma que se divide entre las ingresadas.
				"recuperadas": rec,
				"recolectadas_estrictas": partes.get("recolectadas", 0),
				"inconsistentes_recolectadas": partes.get("inconsistentes_recolectadas", 0),
				"rezagadas_recolectadas": partes.get("rezagadas_recolectadas", 0),
				"pct": pct,
				# Etiqueta lista para la columna "Objetivo": el modelo no compara.
				"vs_objetivo": _etiqueta_objetivo(pct),
			}
		)
	return salida


def _etiqueta_objetivo(pct):
	if pct > OBJETIVO_EFECTIVIDAD:
		return "Por encima"
	if pct < OBJETIVO_EFECTIVIDAD:
		return "Por debajo"
	return "En el objetivo"


def _es_dinero(spec):
	"""True si todas las agregaciones son sum(preciovale): el resumen en pesos."""
	aggs = [a for a in (spec.get("aggregations") or []) if isinstance(a, dict)]
	if not aggs:
		return False
	return all(
		str(a.get("func", "")).lower() == "sum" and str(a.get("field", "")).lower() == "preciovale"
		for a in aggs
	)


PALABRAS_PEOR = ("peor", "peores", "menor efectividad", "menos efectividad", "más baja", "mas baja")
PALABRAS_MEJOR = ("mejor", "mejores", "mayor efectividad", "más alta", "mas alta")


def _sin_tildes(texto):
	return "".join(
		c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn"
	).lower()


# Medidas que, si aparecen, convierten un "lista" en una consulta con cifras.
_PALABRAS_MEDIDA = (
	"cuant",
	"numero",
	"total",
	"efectividad",
	"porcentaje",
	"monto",
	"precio",
	"valor",
	"pieza",
	"ingresad",
	"recolect",
	"recolecc",
	"siniestro",
	"reclamo",
	"mas ",
	"menos",
	"mejor",
	"peor",
	"top",
)


def _es_listado(pregunta):
	"""True para "lista/dame las plazas por región": pide nombres, no cifras.

	Solo cuando el verbo pide la lista y la pregunta no pide ninguna medida, para
	no callar el conteo en "lista las piezas por tipo CDR", "dame los siniestros
	de este mes" ni "dame los 10 talleres con más piezas".
	"""
	texto = _sin_tildes(pregunta)
	verbos = (
		"lista",
		"listar",
		"listado",
		"enumer",
		"dame",
		"dime",
		"muestra",
		"mostrar",
		"indica",
		"cuales son",
	)
	if not any(v in texto for v in verbos):
		return False
	return not any(p in texto for p in _PALABRAS_MEDIDA)


def _listado_sin_cifras(spec, resultado, filas):
	"""Deja solo las dimensiones; con dos, junta la segunda por la primera.

	"lista las plazas por región" → [["Norte", "MTY, JUAREZ"], ...], sin la
	columna de piezas. La agrupación se hace aquí porque el modelo, con 50
	filas, podía perder plazas al rearmar la lista en prosa.
	"""
	columnas = [str(c).lower() for c in (resultado.get("columnas") or [])]
	dimensiones = [str(g).lower() for g in (spec.get("group_by") or spec.get("fields") or [])]
	idx = [i for i, c in enumerate(columnas) if c in dimensiones]
	if not idx or len(idx) == len(columnas):
		return filas, resultado.get("columnas")
	recorte = [[fila[i] for i in idx] for fila in filas]
	if len(idx) == 2:
		grupos = {}
		for clave, valor in recorte:
			grupos.setdefault(clave, []).append(str(valor))
		recorte = [[clave, ", ".join(valores)] for clave, valores in grupos.items()]
	return recorte, [columnas[i] for i in idx]


def _direccion_por_pregunta(pregunta):
	"""True = ascendente (peor efectividad primero), False = descendente, None = sin preferencia.

	La dirección no puede depender de que el modelo acierte el `dir` del
	`order_by`: "peor efectividad" es una instrucción de la pregunta y se aplica
	siempre, aunque el spec venga sin orden o con el contrario.
	"""
	texto = (pregunta or "").lower()
	if any(p in texto for p in PALABRAS_PEOR):
		return True
	if any(p in texto for p in PALABRAS_MEJOR):
		return False
	return None


def _ordenar(spec, filas, porcentajes, asc=None):
	"""Ordena la tabla cuando la pregunta no pidió un orden.

	Sin esto el orden lo decidía el modelo (o MySQL, que no lo garantiza) y la
	tabla de efectividad salía por ingresadas o alfabética. Con efectividad el
	orden siempre se calcula aquí (el porcentaje no es columna del SQL); el
	`dir` del `order_by` solo aporta la dirección cuando la pregunta no la dice.
	Devuelve también la descripción del criterio para que la explicación no lo
	contradiga.
	"""
	if not filas:
		return filas, porcentajes, ""
	if porcentajes:
		if asc is None:
			asc = any(str(o.get("dir", "")).lower() == "asc" for o in spec.get("order_by") or [])
		# Desempate siempre por volumen descendente: entre varios en 0.0%, el
		# "peor" es el de 266 piezas, no el de 52. Se ordena en dos pasadas
		# porque el desempate no debe invertirse con el `reverse` del porcentaje.
		pares = sorted(zip(filas, porcentajes), key=lambda par: par[1].get("total") or 0, reverse=True)
		pares = sorted(pares, key=lambda par: par[1]["pct"], reverse=not asc)
		criterio = "efectividad de menor a mayor" if asc else "efectividad de mayor a menor"
		return [f for f, _ in pares], [p for _, p in pares], criterio
	if spec.get("order_by"):
		# En conteos y montos el orden ya lo puso el SQL (order_by del spec).
		return filas, porcentajes, ""
	if len(spec.get("aggregations") or []) == 1 and spec.get("group_by"):
		# La medida es la última columna: las dimensiones del group_by van antes.
		filas = sorted(filas, key=lambda fila: (fila[-1] is not None, fila[-1] or 0), reverse=True)
		return filas, porcentajes, "la medida de mayor a menor"
	return filas, porcentajes, ""


def explain(pregunta, spec, resultado, historial=None, universo_nombre=None):
	"""Genera la explicación conversacional. Devuelve (texto, info_log).

	`universo_nombre` es la aseguradora (o "Todas las aseguradoras") que se
	consultó: viaja en los datos para que el modelo no atribuya las cifras a la
	aseguradora que aparezca en la pregunta.
	"""
	s = frappe.get_cached_doc("Molduplastic IA Settings")
	cob = resultado.get("cobertura", {})
	lineas_cob = []
	# Sin campos medibles no hay advertencia que dar: anunciar "universo 0" hacía
	# que el modelo dudara de una cifra correcta (0 recolectadas, por ejemplo).
	# Solo se avisa de los campos por debajo del 100%: los que vienen completos
	# son ruido y el modelo terminaba repitiendo "cobertura 100%" en cada tabla.
	bajos = {c: d for c, d in (cob.get("campos") or {}).items() if d.get("pct", 100) < 100}
	if bajos:
		lineas_cob.append(f"Universo en alcance: {cob.get('universo', 0)} registros.")
		for campo, d in bajos.items():
			lineas_cob.append(f"- {campo}: {d['con_datos']} con datos ({d['pct']}%)")
	mapa_tipos = _mapa_tipo_carga(resultado)
	filas = _filas_legibles(spec, resultado, list(resultado.get("filas") or []))
	filas = _tipo_carga_legible(resultado, filas, mapa_tipos)
	# Los porcentajes se calculan sobre las filas ya legibles para que el
	# "grupo" lleve el nombre ("Agencia") y no el id.
	porcentajes = _porcentajes(spec, {**resultado, "filas": filas})
	filas, porcentajes, orden = _ordenar(
		spec, filas, porcentajes, asc=_direccion_por_pregunta(pregunta)
	)
	if porcentajes:
		# Sin ingresadas en el período no hay efectividad que medir: el grupo
		# que solo aparece por rezagadas (ingresó antes) daría 0.0% por
		# construcción y llenaría la tabla de "peores" falsos.
		pares = [(f, p) for f, p in zip(filas, porcentajes) if (p.get("total") or 0) > 0]
		filas = [f for f, _ in pares]
		porcentajes = [p for _, p in pares]
	minimo = spec.get("min_ingresadas")
	if minimo and porcentajes:
		# HAVING sobre ingresadas: se aplica a las filas ya agregadas, después de
		# ordenar, para no perder el orden ni el porcentaje de cada grupo.
		pares = [(f, p) for f, p in zip(filas, porcentajes) if (p.get("total") or 0) >= minimo]
		filas = [f for f, _ in pares]
		porcentajes = [p for _, p in pares]
	# Ranking con filtro (una sola medida con "filtros", p. ej. "talleres con
	# más piezas no entregadas"): los grupos en 0 no "tienen" lo que se cuenta,
	# así que se descartan y se ordena por la medida.
	aggs_ranking = spec.get("aggregations") or []
	if spec.get("group_by") and len(aggs_ranking) == 1 and (aggs_ranking[0].get("filtros")):
		columnas_r = [str(c).lower() for c in resultado.get("columnas") or []]
		alias_r = str(aggs_ranking[0].get("alias") or "").lower()
		if alias_r in columnas_r:
			i_r = columnas_r.index(alias_r)
			pares = [(f, f[i_r]) for f in filas if (f[i_r] or 0) > 0]
			pares.sort(key=lambda par: par[1], reverse=True)
			filas = [f for f, _ in pares]
	# No recuperadas: el ranking va por la suma de las tres categorías y se
	# descartan los grupos en 0 (un taller con 0 no "tiene" no recuperadas).
	totales_no_rec = totales_no_recuperadas(filas, resultado)
	# El resumen de 8 categorías también lleva los dos totales derivados.
	total_rec = None if porcentajes else totales_recuperadas(filas, resultado)
	if totales_no_rec is not None and spec.get("group_by"):
		pares = [(f, t) for f, t in zip(filas, totales_no_rec) if (t or 0) > 0]
		pares.sort(key=lambda par: par[1], reverse=True)
		filas = [f for f, _ in pares]
		totales_no_rec = [t for _, t in pares]
	# Se ordena ANTES de recortar: al revés, el corte se quedaba con los 50
	# grupos que devolvió MySQL y el orden no podía rescatar a los de arriba.
	total_filas = len(filas)
	# Si pidieron un top N, el spec trae limit=N; sin top, la presentación se
	# queda en 50 para no agotar el presupuesto de tokens del modelo.
	tope = min(int(spec.get("limit") or 50), 50)
	filas = filas[:tope]
	porcentajes = porcentajes[:tope]
	if totales_no_rec is not None:
		totales_no_rec = totales_no_rec[:tope]
	corte = ""
	if total_filas > len(filas) and len(filas) > 1:
		# Con una sola fila no se avisa del corte: la pregunta era por la
		# peor/mejor y el listado de las demás no se muestra (2026-10-25).
		corte = "top" if spec.get("order_by") else "presentacion"
	# Listado de dimensiones ("lista las plazas por región"): la medida no se
	# manda al modelo, así que no puede colarla en la respuesta.
	listado = _es_listado(pregunta)
	if listado:
		filas, columnas_datos = _listado_sin_cifras(spec, resultado, filas)
	else:
		columnas_datos = resultado.get("columnas")
	# El objetivo solo se manda cuando la pregunta es de efectividad: si viajara
	# siempre, el modelo lo colaría en respuestas de montos o de conteos.
	habla_efectividad = bool(porcentajes) or "efectiv" in (pregunta or "").lower()
	habla_efectividad = habla_efectividad or "objetivo" in (pregunta or "").lower()
	datos = {
		# Con group_by el resultado es un desglose por dimensión: va en tabla
		# aunque tenga una sola fila, para no contestar "Taller, 40" en prosa
		# cuando pidieron "muéstrame las piezas por...".
		"desglose": bool(spec.get("group_by")),
		# El resumen de piezas se pide igual en conteo y en dinero: la bandera
		# evita que el modelo adivine si la segunda columna es "Piezas" o "Valor".
		"dinero": _es_dinero(spec),
		"columnas": columnas_datos,
		"filas": filas,
		"orden": orden,
		"total_filas": total_filas,
		"mostradas": len(filas),
		"corte": corte,
		"sin_cifras": listado,
		"min_ingresadas": minimo or None,
		"cobertura": "\n".join(lineas_cob),
		"universo": universo_nombre or "",
		"catalogos": mapa_tipos,
		"porcentajes": porcentajes,
		"objetivo_efectividad": OBJETIVO_EFECTIVIDAD if habla_efectividad else None,
	}
	if totales_no_rec is not None:
		# La cifra la suma el código, no el modelo.
		datos["no_recuperadas"] = (
			totales_no_rec[0] if len(totales_no_rec) == 1 else None
		)
		datos["no_recuperadas_por_fila"] = totales_no_rec
	if total_rec is not None:
		datos["recuperadas"] = total_rec[0] if len(total_rec) == 1 else None
	mensajes = [{"role": "system", "content": SYSTEM_EXPLAIN}]
	contexto = _bloque_contexto(historial)
	if contexto:
		mensajes.append({"role": "system", "content": contexto})
	mensajes.append(
		{
			"role": "user",
			"content": f"Pregunta: {pregunta}\nSpec: {json.dumps(spec)}\nDatos: {json.dumps(datos, default=str)}",
		}
	)
	prompt_completo = "\n\n".join([SYSTEM_EXPLAIN] + ([contexto] if contexto else []))
	prompt_completo += "\n\n" + mensajes[-1]["content"]

	def _llamar(adapter):
		max_tokens = int(s.max_tokens_respuesta or 4000)
		return adapter.chat(mensajes, max_tokens=max_tokens)

	texto, info = _con_fallback(_cadena(pregunta), _llamar)
	info["prompt"] = prompt_completo
	info["respuesta"] = texto
	info["datos"] = datos
	return texto, info
