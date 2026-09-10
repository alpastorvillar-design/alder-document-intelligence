**Español** · [English](../rag.md)

# Recuperación híbrida y RAG opcional

## Qué existe

Cada fila de `document_chunks` guarda texto, su localizador de evidencia, el
`tsvector` español de PostgreSQL y un `vector(512)` anulable. Tres modos comparten
el mismo endpoint:

| Modo | Señal | Uso adecuado |
| --- | --- | --- |
| `lexical` | términos y raíces de PostgreSQL FTS | referencias exactas y vocabulario conocido |
| `vector` | similitud coseno exacta en pgvector | similitud de formulación con proveedor aprendido |
| `hybrid` | fusión por rango recíproco de ambas listas | identificadores exactos y preguntas naturales |

Cada consulta SQL filtra por `dossier_id`. Las vectoriales exigen además el mismo
hash de configuración que se usó para embeber la pregunta. Así no se mezcla en
silencio un vector de otro modelo o dimensión.

El proveedor predeterminado `hashing` es una proyección determinista de
características de palabra y caracteres. Permite ejecutar offline migraciones,
almacenamiento, reindexado idempotente y SQL vectorial. Sirve como prueba de
ingeniería y aporta cierta tolerancia a erratas; **no es un modelo semántico
aprendido**. La API devuelve `learned_model: false` para hacerlo observable.

## Ejecutar la recuperación offline

Procesa el corpus normalmente. Los chunks nuevos reciben el baseline por hashing.
Los existentes se refrescan sin repetir OCR ni validación:

```bash
docker compose exec -T api iep reindex --reference INN-2025-042
```

Abre `/docs`, selecciona `GET /dossiers/{dossier_id}/evidence` y compara la misma
consulta con `mode=lexical`, `mode=vector` y `mode=hybrid`. La respuesta siempre
incluye documento, texto y localizador original.

La representación en base es inspeccionable:

```sql
SELECT document_id,
       ordinal,
       embedding_provider,
       embedding_model,
       vector_dims(embedding),
       embedded_at
FROM document_chunks
ORDER BY document_id, ordinal
LIMIT 10;
```

No hay índice aproximado. La búsqueda exacta es simple y suficiente para este
corpus pequeño. HNSW o IVFFlat se añadirían después de que un benchmark con
volumen y filtros representativos demostrase una necesidad de latencia.

## Qué convierte el endpoint de preguntas en RAG

`POST /dossiers/{dossier_id}/questions` ejecuta las tres fases:

1. recupera los top-k mediante búsqueda léxica, vectorial o híbrida;
2. aumenta una instrucción versionada con esos chunks como JSON no confiable;
3. pide a un generador alojado una respuesta estructurada con ids de cita.

No es un camino de aprobación. No tiene herramientas ni cambia la base, limita
top-k y contexto, pide abstención si falta evidencia, valida el esquema y rechaza
una cita que no estuviera entre los chunks recuperados. La respuesta sigue siendo
un borrador generado: validar ids no demuestra que cada frase esté implicada por
el texto citado.

## Prueba alojada opcional

Por defecto `IEP_RAG_PROVIDER=disabled`; ningún test ni demo normal llama a un
endpoint de pago. Una ejecución deliberada necesita:

```text
IEP_EMBEDDING_PROVIDER=openai
IEP_RAG_PROVIDER=openai
IEP_ALLOW_EXTERNAL_AI=true
IEP_OPENAI_API_KEY=<aportada fuera de Git>
IEP_OPENAI_EMBEDDING_MODEL=text-embedding-3-small
IEP_OPENAI_RAG_MODEL=gpt-4o-mini
```

Configura antes un techo de gasto en el proveedor y usa solo datos sintéticos.
No pegues la clave en código, documentación, comandos que queden en historial ni
incidencias. Un `.env` no versionado es aceptable para esta demo local, aunque las
variables pueden verse inspeccionando el contenedor; producción necesita un
gestor o secreto montado.

Tras cambiar el proveedor ejecuta `iep reindex` por expediente. El reindexado es
idempotente: un chunk con el hash actual no se factura ni escribe otra vez.
Cambiar modelo o dimensión es una migración de datos y exige repetir la evaluación
representativa de recuperación.

## Qué se demuestra y qué no

Se demuestra localmente: extensión y migración pgvector, vectores de 512
dimensiones, coseno exacto, aislamiento entre expedientes, fusión híbrida
determinista, reindexado sensible a configuración, contratos alojados contra
dobles, salida estricta, allowlist de citas, versión/hash de prompt y salida de
datos desactivada por defecto.

Eso no demuestra: calidad semántica de un modelo alojado, corrección en
expedientes reales, inmunidad general a prompt injection, términos de privacidad
de producción, throughput, beneficio de un índice aproximado ni impacto real.
