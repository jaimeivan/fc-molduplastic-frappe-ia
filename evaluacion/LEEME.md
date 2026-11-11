# Evaluación de calidad del análisis (versión 1, 2026-11-11)

El set vive en el Desk, en **IA Evaluacion** (solo System Manager). Las filas se cargan
desde `preguntas_evaluacion.csv` al instalar (parche `sembrar_evaluacion_ia`); después
se editan en el Desk. El CSV es la versión de referencia: no se edita para versionar.

## Cómo revisar (administrador)

1. Abre **IA Evaluacion** y elige una fila pendiente.
2. Escribe el **Universo a consultar** (una aseguradora, p. ej. GNP, o TODAS). Es el único dato que la fila no trae.
3. Pulsa **Preguntar al asistente**. La app responde con el universo y el modo que esté
   configurado en ese momento (Molduplastic IA Settings > Modo de respuesta, o la excepción
   de la intención). Se guardan: respuesta, preguntas sugeridas, veredicto de verificación
   de cifras, modo al preguntar e intención.
4. Lee la respuesta y marca los tres criterios:
   - **¿Analiza?**: interpreta los datos (comparativo, concentración, brecha), no solo los repite.
   - **¿Sugiere con sustento?**: si sugiere algo, sale de un hallazgo o del glosario Validado. Sin sustento, no marcar.
   - **¿Inventa cifras?**: aparece una cifra que no está en el resultado de la consulta. Debe quedar sin marcar.
5. Cambia **Estado** a *Revisada*. Exige que la fila tenga respuesta. Al revisar quedan
   registrados el revisor y la fecha.

Si vuelves a preguntar una fila revisada, su estado vuelve a *Pendiente* y los criterios se
limpian: una respuesta nueva necesita una revisión nueva.

## Resumen

Desde la consola del servidor (solo administradores):

    bench --site <site> console
    >>> from fc_molduplastic_frappe_ia.molduplastic_ia.evaluacion import resumen_evaluacion
    >>> resumen_evaluacion()

Devuelve total, sin respuesta, pendientes, revisadas y el porcentaje de revisadas que
analizan, sugieren con sustento e inventan cifras (este último debe bajar, no subir).

## Notas

- Las preguntas de tipo *conteo* no tienen sugeridas: sus preguntas no caen en una intención propia.
- Las respuestas que arma el código (rankings, promedios, listados) no pasan por el modelo: si una fila cae ahí, la revisión mide el formato, no la lectura.
- Una pregunta con rango de más de un mes responde en breve con aviso: esa fila no mide el análisis.
