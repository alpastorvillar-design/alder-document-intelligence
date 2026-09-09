**Español** · [English](../demo.md)

# Demostración local de cinco minutos

> Si lo que buscas es **cómo se lanza** la demostración paso a paso, está en
> [recorrido.md](recorrido.md). Este documento es el guion de qué enseñar.

## Preparación

Lanza `scripts/demo.ps1` desde PowerShell o sigue el arranque rápido del README.
Ten abiertos el documento de arquitectura, la documentación de la API, la cola de
revisión y un informe ya generado. El corpus es sintético y la demostración no
necesita Internet una vez las imágenes están disponibles en local.

## Recorrido

1. **Problema (30 segundos).** Enseña el expediente como PDF, justificante
   escaneado, libro de Excel, respuesta del registro y página local de
   convocatoria. Explica que el requisito duro es la conciliación trazable, no
   el resumen de documentos.
2. **Arquitectura (45 segundos).** Señala una imagen, procesos api/worker
   separados, cola en PostgreSQL, objetos direccionados por contenido y workflow
   opcional.
3. **Camino feliz de la evidencia (60 segundos).** Abre el expediente
   consistente. Enseña un rango de caracteres de PDF, una caja de palabra de OCR
   con su confianza, una celda de Excel, una ruta JSON de API, un selector HTML y
   un total derivado.
4. **Camino de revisión (90 segundos).** Abre el expediente defectuoso, corrige
   un campo con motivo, resuelve una incidencia, y enseña que los bloqueantes sin
   resolver o los campos pendientes impiden aprobar.
5. **Fiabilidad y seguridad (60 segundos).** Enseña la repetición idempotente,
   las pruebas de lease y recuperación de trabajos, las entradas maliciosas o
   inválidas rechazadas, y los errores estructurados.
6. **Informe y límites (45 segundos).** Abre el informe y la exportación
   JSON/CSV. Termina con el documento de brecha con producción y el comando de
   resultados medidos.

## Recorrido de noventa segundos

Enuncia el problema de conciliación; enseña un valor localizado por OCR, una
celda de Excel, un descuadre determinista, una corrección humana que preserva el
original, y el informe resultante. Cierra con: el modelo puede ayudar a
interpretar texto, pero las reglas y las personas conservan la validación
financiera y la aprobación.

## Plan B si no se puede ejecutar

Usa el diagrama de arquitectura, un resumen pequeño y revisado de
`evaluation-results` del commit correspondiente, el documento OpenAPI y un
informe generado. No cites métricas de otra revisión. Explica con claridad qué
prerrequisito local ha fallado y ofrece el comando exacto de reproducción.
