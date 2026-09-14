**Español** · [English](../README.md)

# Índice de documentación

Este índice reúne la documentación en español. Cada documento bilingüe enlaza
con su equivalente en inglés desde la primera línea.

## Empieza por aquí

| Documento | Qué responde |
| --- | --- |
| [Recuperación híbrida y RAG opcional](rag.md) | pgvector, embeddings, modos de búsqueda, respuestas con citas y prueba alojada segura |
| [Demostración del LLM](demostracion-llm.md) | Cómo ver funcionando el camino del modelo alojado — gratis contra un simulador local, o contra un modelo real por céntimos |
| [Guion de demo de cinco minutos](demostracion.md) | Qué enseñar, en qué orden, y qué decir de ello |

## Cómo está construido

| Documento | Qué responde |
| --- | --- |
| [Arquitectura](arquitectura.md) | La forma del sistema, sus módulos, y por qué es un monolito |
| [Modelo de dominio](modelo-de-dominio.md) | Cada entidad, cada campo, y por qué existe |
| [Flujo y máquina de estados](workflow.md) | Los estados, la transición que falta a propósito, ciclo de vida y recuperación de trabajos |
| [Ingesta y procedencia](ingesta-y-procedencia.md) | Cómo se aceptan ficheros no confiables, y cómo cada valor conserva su origen |
| [Estrategia de validación](estrategia-de-validacion.md) | Qué comprueban las reglas, y dónde está la puerta humana |

## Qué afirma y qué no

| Documento | Qué responde |
| --- | --- |
| [Resultados medidos](resultados-medidos.md) | Qué informa la evaluación, y qué **no** significan esas cifras |
| [Seguridad de la IA](seguridad-ia.md) | Qué puede y qué no puede tocar el proveedor semántico |
| [Modelo de amenazas](modelo-de-amenazas.md) | Activos, amenazas, controles presentes, trabajo pendiente |
| [Limitaciones](limitaciones.md) | La lista honesta |
| [Brecha con producción](brecha-produccion.md) | Qué tendría que cambiar antes de expedientes reales |
| [Operación](operacion.md) | Arrancar, inspeccionar, fallo y recuperación, observabilidad |
| [Impacto de negocio](impacto-negocio.md) | Una calculadora de escenarios, no una promesa de ahorro |

## Registros de decisión (ADR)

| ADR | Decisión |
| --- | --- |
| [0001](adr/0001-localizadores-de-evidencia.md) | Cada valor lleva de dónde salió |
| [0002](adr/0002-cola-en-postgres.md) | La cola es una tabla, no un broker |
| [0003](adr/0003-reglas-deterministas-no-un-modelo.md) | El modelo no hace aritmética |
| [0004](adr/0004-recuperacion-lexica-no-rag.md) | Búsqueda léxica, y qué lo cambiaría |
| [0005](adr/0005-sin-agente-en-la-aprobacion.md) | Ningún agente entre un documento y una aprobación |
| [0006](adr/0006-recuperacion-hibrida-y-rag-opcional.md) | Recuperación híbrida y generación fundamentada opcional |

Además: [`automation/n8n/README.es.md`](../../automation/n8n/README.es.md) para
el workflow opcional.
