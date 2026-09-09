**Español** · [English](../../adr/0003-deterministic-rules-not-a-model.md)

# ADR 0003: Las decisiones se quedan en reglas deterministas

**Estado:** aceptado

## Contexto

La prosa de un documento se beneficia de la interpretación semántica, pero la
aritmética financiera, la elegibilidad y la aprobación exigen repetibilidad y
modos de fallo explicables.

## Decisión

Usar un proveedor semántico **sólo** para clasificación y propuestas
fundamentadas. Los valores de negocio que se persisten vienen de lectores
deterministas. Reglas versionadas hacen toda la aritmética y la validación entre
fuentes. Sólo una persona puede aprobar o rechazar.

## Consecuencias

Hace falta más código explícito de parsing y de reglas. El resultado es
reproducible, comprobable y seguro cuando el proveedor semántico está ausente, va
lento o se equivoca.
