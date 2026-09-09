**Español** · [English](../measured-results.md)

# Resultados medidos

En el repositorio no se copia ningún resultado fijo. La evidencia canónica es la
salida de una ejecución nueva:

```bash
python -m corpus.generate --out corpus/out
python -m evaluation.run_eval --corpus corpus/out --out evaluation/out
```

La evaluación reconstruye los expedientes, ejecuta el proveedor semántico
determinista y el OCR real de Tesseract, y escribe JSON legible por máquina más
un resumen legible por personas. CI publica esos ficheros como el artefacto
`evaluation-results`.

Informa por separado de:

- precisión de campo contra una verdad de referencia independiente;
- verdaderos positivos, falsos positivos, falsos negativos, precisión y
  exhaustividad de las incidencias esperadas, por id de regla;
- confianza de palabra de OCR y resultados de campo de los justificantes
  escaneados;
- acierto de recuperación sobre consultas de evidencia fijas;
- tiempo de reloj por etapa y total;
- bytes de entrada y pico de reserva de memoria de Python observado por
  `tracemalloc`;
- igualdad en la repetición, incluidos ids, valores, localizadores, estados y
  detalle de incidencias;
- una estimación de tokens y coste alojados **etiquetada explícitamente como
  estimación**;
- escenarios de impacto parametrizados, en lugar de ahorros afirmados.

Los resultados dependen del commit registrado, del runner, de la CPU, del
paquete de Tesseract y sus datos de idioma, y de la versión del corpus. Las
cifras de latencia y memoria son observaciones de esa ejecución, no afirmaciones
de capacidad ni de nivel de servicio. Un test en verde prueba el comportamiento
sobre el fixture, no una precisión general sobre documentos reales no vistos.
