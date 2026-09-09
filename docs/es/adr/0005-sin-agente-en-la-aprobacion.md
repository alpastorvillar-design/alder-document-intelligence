**Español** · [English](../../adr/0005-no-agent-in-the-approval-path.md)

# ADR 0005: Ningún agente autónomo en el camino de aprobación

**Estado:** aceptado

## Contexto

Un agente abierto puede elegir herramientas y siguientes acciones, lo cual es
útil para algunas tareas exploratorias pero choca con un flujo acotado de control
de evidencia.

## Decisión

Usar una máquina de estados explícita y un pipeline fijo. La automatización de
workflow opcional puede iniciar y enrutar trabajo, pero no puede cambiar la
lógica de validación ni aprobar un expediente. Las acciones humanas son
explícitas, motivadas y auditadas.

## Consecuencias

El sistema renuncia a la flexibilidad autónoma. Gana una superficie de ataque
finita, repetición reproducible, caminos de error predecibles y una frontera de
responsabilidad clara.
