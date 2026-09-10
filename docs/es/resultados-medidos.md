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

Necesita el simulador de fuentes de desarrollo en marcha: a diferencia de las
pruebas, que inyectan dobles, el arnés usa los conectores reales. Pasa
`--call-page-url http://127.0.0.1:8080/public/convocatoria.html` cuando el
simulador esté en el host en lugar de en Compose.

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

## Lo que se niega a informar

Hay dos cifras que se retienen en lugar de imprimirse cuando medirían otra cosa
distinta del pipeline.

**La detección, cuando falta una fuente.** Varias reglas cruzan el parte
horario contra el registro de personal, o lo reclamado contra la convocatoria
publicada. Si no se puede llegar a ellas se levanta
`EXTERNAL_SOURCE_UNAVAILABLE` —que es el comportamiento correcto, porque una
fuente que falta es un bloqueante y no un conjunto de datos vacío— y esas
reglas no pueden saltar. La exhaustividad y la precisión bajan entonces por un
motivo que no tiene nada que ver con la detección, así que el resumen dice que
las cifras no son medibles en esa ejecución, y por qué. Una cifra que en unos
entornos significa otra cosa en silencio es peor que no dar cifra, porque quien
la lee no puede saber cuál tiene delante.

**La repetición, cuando las dos ejecuciones no vieron las mismas fuentes.** El
simulador falla y limita el ritmo según un calendario deliberado. Si una
ejecución perdió una fuente y la otra no, los estados difieren legítimamente y
la comparación no dice nada sobre la idempotencia, así que se informa como no
comparable en lugar de como un fallo.

Los resultados dependen del commit registrado, del runner, de la CPU, del
paquete de Tesseract y sus datos de idioma, y de la versión del corpus. Las
cifras de latencia y memoria son observaciones de esa ejecución, no afirmaciones
de capacidad ni de nivel de servicio. Un test en verde prueba el comportamiento
sobre el fixture, no una precisión general sobre documentos reales no vistos.
