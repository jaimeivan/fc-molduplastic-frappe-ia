# Borrador del glosario: causas y acciones (2026-11-17)

Documento para que el **administrador del Asistente IA** revise y valide el contenido
de negocio del glosario (`Molduplastic IA Settings > Glosario`).

**Regla que rige el glosario:** una entrada en `Validado` se usa tal cual en la
respuesta analítica. Lo que aquí está marcado como *propuesta* no es una decisión
tomada: hay que leerlo, corregirlo si hace falta y solo entonces pasar la fila a
`Validado`. Los campos que sigan diciendo `[pendiente de validación del
administrador]` ya no llegan al chat (ver "Guardia de código" al final), pero
mientras estén pendientes **el asistente no dará sugerencias** para ese tema.

Las definiciones y umbrales que aparecen abajo **no son propuestas**: son la
definición vigente del asistente. Lo que se propone son las **causas** y las
**acciones**.

---

## 1. Efectividad (`efectividad` → término "Efectividad")

- **Definición (vigente):** Recolectadas sobre ingresadas × 100.
- **Umbral (vigente):** Objetivo 75 % (configurable). Para comparar grupos se usa un mínimo de 50 ingresadas.

**Causas típicas — propuesta para validar:**

> La efectividad baja cuando ingresan piezas que no se recolectan en el mismo mes:
> rezago de recolección de los CDR, piezas pendientes que siguen sin estatus y
> piezas no localizadas. El indicador puede pasar de 100 % porque cuenta rezagadas
> de meses anteriores sobre las ingresadas del período; por eso una caída contra el
> mes anterior no siempre significa que se recolecte menos, sino que cambió la
> mezcla de rezago.

**Acción recomendada — propuesta para validar:**

> Revisar los CDR con más piezas pendientes y no localizadas del período y pedir la
> recolección de las rezagadas del mes anterior; si la caída es de un solo grupo,
> revisar ese taller antes que el promedio general.

**Decisión que necesita el dueño:** ¿la caída se explica por rezago (dato
estructural) o por gestión de los CDR? La respuesta cambia la acción.

---

## 2. No recolectadas (`no_recolectadas` → término "No recolectadas")

- **Definición (vigente):** No recolectadas en periodo + inconsistentes no recolectadas + rezagadas no recolectadas (estatus 200 a 206).
- **Umbral:** hoy dice `[pendiente…]`. Propuesta: *"Se vigila por concentración: si los grupos principales concentran más del umbral configurado (60 % por defecto), el problema es de pocos CDR."*

**Causas típicas — propuesta para validar:**

> El estatus es el único motivo que queda registrado en los datos (200 No
> recolectado, 201 Vehículo en reparación, 202 Pérdida total, 203 Pago de daños,
> 204 Incidencia, 205 Otros, 206 Pieza no localizada); el campo de motivo de la
> pieza no se captura, así que no se puede afirmar una causa distinta al estatus.
> La concentración en pocos CDR suele acompañarse de piezas sin estatus o no
> localizadas.

**Acción recomendada — propuesta para validar:**

> Pedir a los CDR con más piezas no recolectadas el estatus de cada una y dar
> seguimiento a las de estatus 200 y 206; las de 201/202/203 dependen del proceso
> del siniestro y no de la recolección.

**Decisión que necesita el dueño:** confirmar si "no recolectada" con estatus 201
(vehículo en reparación) debe seguir contando como problema de recolección o
excluirse del indicador.

---

## 3. Pieza no localizada (`no_localizadas` → término "Pieza no localizada")

- **Definición (vigente):** Estatus 206 (Pieza no localizada).
- **Umbral:** hoy dice `[pendiente…]`. Propuesta: *"Mínimo 50 ingresadas para hablar de porcentaje; en conteos y listados no aplica."*

**Causas típicas — propuesta para validar:**

> La pieza no se encontró físicamente en el CDR al momento de la recolección, o se
> capturó el estatus sin haberla ubicado. Es el único motivo con nombre propio en
> el catálogo, así que no se puede desglosar más sin captura adicional.

**Acción recomendada — propuesta para validar:**

> Pedir al CDR la ubicación física de la pieza y, si no aparece, escalar con el
> asegurado antes de cerrar el siniestro; revisar primero los CDR que acumulan más
> estatus 206 en el período.

**Decisión que necesita el dueño:** ¿hay un procedimiento formal de búsqueda/escalamiento?
Si existe, va aquí con los pasos concretos.

---

## 4. Pendientes (`resumen` → término "Pendientes")

- **Definición (vigente):** Estatus vacío, fecha de recolección vacía y recolectable = 1.
- **Umbral:** hoy dice `[pendiente…]`. Propuesta: *"Se prioriza por valor (`preciovale`) y por antigüedad del ingreso."*

**Causas típicas — propuesta para validar:**

> Piezas que ingresaron y todavía no tienen estatus ni fecha de recolección: el CDR
> no las ha procesado o el estatus no se capturó. Al no tener estatus, no se puede
> saber si están en reparación, perdidas o no localizadas.

**Acción recomendada — propuesta para validar:**

> Priorizar por precio y antigüedad (la pregunta de prioridad ya devuelve el top 10
> por valor) y pedir al CDR que capture estatus y fecha de recolección.

**Decisión que necesita el dueño:** ¿la priorización es por valor, por antigüedad o
por ambos? Hoy el asistente ordena por valor cuando se pide "prioridad de piezas
pendientes".

---

## 5. Inconsistente (`resumen` → término "Inconsistente")

- **Definición (vigente):** Pieza recolectada antes de su fecha de ingreso.
- **Causas:** hoy dice `[pendiente…]`.
- **Acción:** vacía.

**Recomendación:** dejarla **sin causas**. Desde el 2026-11-17 el período se mide
con `fecha_hora_carga` (fecha del lote), y recolectar antes de que el lote se cargue
es imposible: las inconsistencias quedaron en 0 de forma estructural. Escribir una
causa sería describir un caso que ya no ocurre. Si se quiere conservar el
concepto, la causa correcta es: *"dato histórico: con la fecha de carga como base
del período el caso no puede presentarse"*.

---

## 6. Siniestro (`general` → término "Siniestro")

- **Definición (vigente):** Conteo de siniestros distintos en el período.
- **Causas:** hoy dice `[pendiente…]`.
- **Acción:** vacía.

**Recomendación:** dejarla **sin causas y sin acción**. Un siniestro es una unidad
de conteo, no un problema a corregir: no hay causa ni acción de negocio que
declarar. El campo de causas existe en la tabla porque es común a todas las
entradas, no porque cada término lo necesite.

---

## 7. Entradas ya completas (no requieren trabajo)

| Intención | Término | Estado |
|---|---|---|
| `recolectadas` | Recolectadas | definición completa, sin causas/acción (correcto) |
| `resumen` | Rezagada | definición completa, sin causas/acción (correcto) |
| `general` | CDR | definición completa (Taller 10, Agencia 11, Audavales 12, Equipo Pesado 13) |
| `general` | Ingresadas (volumen) | definición completa |

---

## Cómo aplicarlo en el Desk

1. Ir a **Molduplastic IA Settings > Glosario**.
2. Por cada fila: pegar el texto aprobado en `Causas` y/o `Accion` (y `Umbral` si se
   propone uno), borrando el `[pendiente de validación del administrador]`.
3. Guardar con la fila en `Borrador` mientras se revisa y pasarla a `Validado`
   cuando el texto ya sea el definitivo.
4. El asistente usa el glosario solo en modo **Analítico** y solo para la intención
   de la pregunta.

**Guardia de código (2026-11-17):** un campo que todavía contenga "pendiente de
validación" no se inyecta en el prompt ni se usa como sugerencia, y el sistema
rechaza marcar `Validado` una fila que sigue con el relleno (una fila que ya estaba
Validada se puede corregir sin bloqueos). Aun así, la decisión de qué decir es del
dueño: el código no inventa causas ni acciones.

---

## Resumen de decisiones que se piden

1. Efectividad: ¿la caída se explica por rezago estructural o por gestión de los CDR?
2. No recolectadas: ¿el estatus 201 (vehículo en reparación) cuenta como problema de recolección?
3. Pieza no localizada: ¿existe un procedimiento de búsqueda/escalamiento que deba citarse?
4. Pendientes: ¿priorizar por valor, por antigüedad o por ambos?
5. Inconsistente y Siniestro: confirmar que se quedan sin causas (recomendado).
