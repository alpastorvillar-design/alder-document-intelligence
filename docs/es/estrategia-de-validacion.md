**Español** · [English](../validation-strategy.md)

# Estrategia de validación

## Separación de responsabilidades

Los extractores responden a «¿qué valor aparece y dónde?». Los proveedores
semánticos pueden clasificar un documento y proponer candidatos fundamentados.
Las reglas deterministas versionadas responden a «¿es este expediente
internamente consistente?». Sólo una persona puede aprobarlo.

Las reglas cubren la evidencia obligatoria, la confianza del OCR, los libros de
Excel con fórmulas, las entregas y los números de factura duplicados, la
aritmética y las fechas de las facturas, los límites y la aritmética de los
partes horarios, las comprobaciones de identidad, tarifa y contrato contra el
registro, la elegibilidad y el máximo de la convocatoria, los totales entre
documentos, las lecturas ambiguas, los indicadores de instrucción maliciosa y las
fuentes externas no disponibles.

El fallo de un conector externo es un **bloqueante**, no un conjunto de datos
vacío. Un documento fallido es un bloqueante, no un documento ausente. Los
candidatos de extracción rechazados no participan en la aritmética. Cuando varias
lecturas vivas discrepan, la ambigüedad es visible y bloquea la aprobación.

## La puerta humana

Toda ejecución de procesamiento correcta termina en `NEEDS_REVIEW`. Quien revisa
puede confirmar o corregir campos y aceptar o descartar incidencias, siempre con
actor y motivo. Las correcciones preservan el valor original. Los números de
revisión hacen que una edición sobre un campo obsoleto falle con un conflicto en
lugar de sobrescribir en silencio una decisión concurrente.

La aprobación falla mientras algún campo necesite revisión o algún bloqueante
esté abierto o aceptado. Un bloqueante aceptado significa que la incidencia es
real; no es una dispensa. Descartar un bloqueante falso positivo exige un motivo.
Los expedientes aprobados son inmutables.

## Pruebas y métricas

Las pruebas unitarias fijan el resultado que pretende cada regla. Las pruebas
contra PostgreSQL cubren restricciones, transiciones condicionales, idempotencia,
workers en competencia, vallado por lease y recuperación. El camino de OCR se
prueba con Tesseract en CI. El arnés de evaluación compara los campos extraídos y
los ids de regla de las incidencias contra una verdad de referencia escrita de
forma independiente, e informa de precisión de campo y de precisión/exhaustividad
de incidencias, incluidos falsos positivos y falsos negativos.
