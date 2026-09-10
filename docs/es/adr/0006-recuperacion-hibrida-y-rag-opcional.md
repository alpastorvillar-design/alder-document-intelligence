**Español** · [English](../../adr/0006-hybrid-retrieval-and-opt-in-rag.md)

# ADR 0006: Añadir recuperación híbrida y generación fundamentada opcional

**Estado:** aceptado

## Contexto

La búsqueda de texto completo en español sigue siendo un buen baseline para
referencias, números de factura y términos conocidos del dominio. No resuelve
bien errores tipográficos o formulaciones semánticamente relacionadas. Además,
un revisor puede necesitar un borrador breve que apunte a las fuentes en vez de
otro resumen sin trazabilidad.

## Decisión

Se conserva la búsqueda textual y se añade un vector anulable de 512 dimensiones
a los fragmentos de evidencia. PostgreSQL usa pgvector 0.8.6. Con el tamaño de
este corpus se aplica distancia coseno exacta; un índice aproximado HNSW o
IVFFlat añadiría ajuste y compromisos con filtros sin una necesidad de latencia
medida.

La búsqueda híbrida fusiona las posiciones lexical y vectorial mediante
reciprocal-rank fusion. Toda consulta filtra primero por id de expediente y por
hash de configuración del embedding. Así no puede entrar otro expediente ni un
vector generado con un modelo anterior.

El modo offline predeterminado es un vectorizador determinista por hashing de
características. Prueba almacenamiento, migraciones, reindexado, distancia y
ranking híbrido, pero no es un modelo de embeddings aprendido y la API lo indica.
El proveedor alojado opcional usa el contrato documentado de embeddings y no se
activa sin clave y consentimiento explícito de salida de datos. Cambiar modelo o
dimensión cambia el hash y el reprocesado sustituye solo vectores obsoletos.

La generación fundamentada es un endpoint separado y de solo lectura. Recupera
los top-k, los serializa como datos no confiables, no entrega herramientas, pide
un esquema estricto y solo admite ids de cita recuperados. No puede editar,
aprobar ni cambiar el estado del expediente. Está desactivada por defecto.

## Consecuencias

El core de extracción, validación y revisión funciona sin Internet ni modelo de
lenguaje. PostgreSQL pasa a requerir la extensión pgvector. Activar un proveedor
alojado introduce obligaciones de transferencia, retención, residencia, coste,
latencia y evaluación. Los tests del adaptador no demuestran calidad de respuesta;
antes de producción sigue haciendo falta una evaluación representativa.
