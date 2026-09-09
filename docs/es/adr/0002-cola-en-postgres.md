**Español** · [English](../../adr/0002-postgres-job-queue.md)

# ADR 0002: Usar PostgreSQL como cola de trabajos

**Estado:** aceptado para esta carga de trabajo

## Contexto

Encolar y cambiar el estado del expediente tienen que confirmarse juntos. Se
esperan volúmenes de trabajos moderados y cada operación de OCR es relativamente
lenta.

## Decisión

Guardar los trabajos en PostgreSQL. Los workers reclaman con `FOR UPDATE SKIP
LOCKED`, mantienen un lease y vallan el éxito o el fallo por identidad de worker.
Los trabajos expirados se recuperan hasta alcanzar su techo acotado de intentos.
La idempotencia es única por expediente.

## Consecuencias

El diseño evita un broker y el problema de la doble escritura, y admite varios
workers. También pone el sondeo y la carga de cola sobre PostgreSQL. Una
contención, un rendimiento, un aislamiento o unas necesidades de entrega medidas
pueden justificar un broker más adelante.
