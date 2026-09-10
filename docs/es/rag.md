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

## El buzón de preguntas en la pantalla

La pantalla de revisión y la de evidencia llevan el mismo panel, porque
incluyen el mismo fragmento y envían la misma petición al mismo endpoint de
sólo lectura. Se muestra esté encendida o apagada la generación, y cuando está
apagada dice qué interruptor falta. Es a propósito: una funcionalidad oculta no
enseña nada, y «está apagada, y este es el interruptor» es justo lo que
necesita quien ve esta frontera por primera vez.

![El buzón de preguntas en la pantalla de evidencia](img/06-ask.png)

Lo que muestra el panel cuando llega una respuesta:

- si el modelo consideró **suficiente** la evidencia recuperada, con esas
  palabras, porque una respuesta sacada de evidencia escasa es una pista y no
  un dato;
- cada cita en su propio bloque, con el documento, el sitio dentro de él y el
  fragmento citado. El modelo nunca da el enlace: nombra un `evidence_id`, y
  ese identificador se resuelve contra lo que realmente se le envió;
- cuántos fragmentos recuperados se **retiraron** porque su documento está
  marcado por llevar instrucciones dirigidas a un lector automático;
- el proveedor, el modelo, el modo de recuperación, el número de citas y el
  hash del prompt, para poder emparejar la respuesta de la pantalla con la
  fila de la auditoría.

Preguntar queda registrado. `EVIDENCE_QUESTION_ANSWERED` lleva la pregunta, el
proveedor y el modelo, cuántos fragmentos se recuperaron y se retiraron, las
citas, si el modelo declaró suficiencia, y la versión y el hash del prompt. Una
llamada de sólo lectura es la más fácil de dejar sin rastro, y entonces el
único sitio donde un modelo ha tocado el expediente es el único sin registro.

## Responder a través de un CLI de asistente

`IEP_RAG_PROVIDER=cli` responde usando `claude` o `codex` en esta misma
máquina, en lugar de un endpoint de pago. Existe para poder demostrar el punto
de integración sin clave de API, es **sólo para desarrollo**, y viene apagado.

```text
IEP_RAG_PROVIDER=cli
IEP_RAG_CLI_TOOL=claude          # o codex
IEP_RAG_CLI_MODEL=              # vacío = el modelo por defecto del CLI
IEP_RAG_CLI_TIMEOUT_SECONDS=120
```

La API se ejecuta en un contenedor y el CLI está instalado en el host, así que
**la API tiene que arrancarse en el host** para que esto funcione. Con el stack
ya levantado y sembrado:

```bash
IEP_DATABASE_URL=postgresql+psycopg://iep:iep@127.0.0.1:55432/iep IEP_STORAGE_ROOT=var/objects IEP_REPORT_ROOT=var/reports IEP_REGISTRY_API_BASE_URL=http://127.0.0.1:8080 IEP_RAG_PROVIDER=cli IEP_RAG_CLI_TOOL=claude python -m uvicorn iep.api.app:create_app --factory --host 127.0.0.1 --port 8010
```

El almacén de objetos vive en un volumen de Docker, así que cópialo una vez si
además quieres que funcione el visor de evidencias en este modo:

```bash
docker compose cp api:/var/lib/iep/objects var/
docker compose cp api:/var/lib/iep/reports var/
```

`GET /dossiers/{id}/questions` dice si el CLI configurado está en el `PATH` de
este proceso, y el panel lo repite, para que una mala configuración se lea como
una frase en lugar de como un fallo que hay que interpretar.

### Lo que cuesta un subproceso, y qué se hace al respecto

Un subproceso es una puerta más ancha que una llamada HTTP. Lo que el proveedor
mantiene:

- `argv` es una lista y nunca se usa el shell, así que ningún texto de un
  documento puede convertirse en un comando;
- el prompt viaja por **stdin**, así que nada de un documento llega a `argv`;
- cada llamada se ejecuta en un directorio temporal vacío que se borra después:
  un asistente arrancado dentro de un repositorio se lo lleva como contexto;
- las herramientas propias del CLI se desactivan por flag, y su sandbox se pone
  en sólo lectura cuando lo tiene;
- la llamada está acotada por reloj y el proceso se mata al expirar.

Lo que no tiene es lo que un endpoint alojado da gratis: una respuesta
restringida por esquema. Pidiéndole «la salida estructurada requerida», el CLI
respondió correctamente —en Markdown, con los campos escritos en prosa— porque
nada le había dicho cuál era la forma. Así que la forma se detalla en las
instrucciones que recibe el CLI, y sólo ahí: las *reglas* siguen en el único
prompt que comparten los dos proveedores, donde no pueden separarse. Prosa en
lugar de JSON es entonces un fallo y no una suposición, porque aceptarla
significaría inventar las citas que nunca dio.

### El presupuesto de llamadas

Un techo que la propia aplicación se impone, contado desde la auditoría sobre
una ventana móvil:

```text
IEP_RAG_CALL_BUDGET=40           # 0 = sin techo
IEP_RAG_BUDGET_WINDOW_DAYS=7
IEP_RAG_BUDGET_STOP_FRACTION=0.90
```

Al 90 % del techo la siguiente llamada se **rechaza**, no se avisa, y la
comprobación va antes de la recuperación y antes de la generación: rechazar
después sería gastar la llamada que se pretendía evitar.

No es el cupo restante de una suscripción. Ninguno de los dos CLI publica eso,
y un contador etiquetado como si lo fuera sería peor que ninguno, así que el
panel dice qué está midiendo: lo que ha gastado esta aplicación.

## Qué se demuestra y qué no

Se demuestra localmente: extensión y migración pgvector, vectores de 512
dimensiones, coseno exacto, aislamiento entre expedientes, fusión híbrida
determinista, reindexado sensible a configuración, contratos alojados contra
dobles, salida estricta, allowlist de citas, versión/hash de prompt y salida de
datos desactivada por defecto.

Eso no demuestra: calidad semántica de un modelo alojado, corrección en
expedientes reales, inmunidad general a prompt injection, términos de privacidad
de producción, throughput, beneficio de un índice aproximado ni impacto real.
