**Español** · [English](../../adr/0004-lexical-retrieval-not-rag.md)

# ADR 0004: Empezar por recuperación léxica de evidencia

**Estado:** aceptado

## Contexto

Quien revisa necesita localizar conceptos conocidos dentro de un expediente y
recibir una ubicación exacta de origen. El corpus es pequeño y el vocabulario del
dominio es estable.

## Decisión

Usar la búsqueda de texto completo en español de PostgreSQL sobre segmentos que
conservan sus localizadores de evidencia. No generar una respuesta a partir del
texto recuperado, y no describir esto como generación aumentada por recuperación.

## Consecuencias

El sistema es barato, local, determinista y fácil de inspeccionar. Se le
escaparán las paráfrasis semánticas. Añadir embeddings sólo después de que un
conjunto representativo de consultas muestre fallos léxicos materiales, y después
de diseñar los controles de privacidad, ciclo de vida y evaluación.
