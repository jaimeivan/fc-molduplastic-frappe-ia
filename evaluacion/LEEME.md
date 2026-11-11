# Evaluación de calidad del análisis (versión 1, 2026-11-11)

Set versionado para revisar a mano las respuestas analíticas. No se corre solo:
un revisor pregunta en modo Analítico y llena las columnas.

Criterios (valor `si`/`no`):
- `analiza`: la respuesta interpreta los datos (comparativo, concentración, brecha), no solo los repite.
- `sugiere_con_sustento`: si sugiere algo, la sugerencia sale de un hallazgo o del glosario Validado. Sin sustento = `no`.
- `inventa_cifras`: aparece alguna cifra que no está en el resultado de la consulta. Debe ser `no`.

Cómo usarlo: pregunta cada fila en modo Analítico, pega la respuesta en `notas` si falla algo, y registra `revisor` y `fecha`.
Para versionar cambios, copia el archivo con un sufijo de versión (p. ej. `preguntas_evaluacion_v2.csv`) en lugar de editar éste.
