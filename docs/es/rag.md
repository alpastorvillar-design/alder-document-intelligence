**Español** · [English](../rag.md)

# Recuperación híbrida y RAG opcional

## Qué existe

Cada fila de `document_chunks` guarda texto, su localizador de evidencia, el
`tsvector` español de PostgreSQL y un `vector` anulable sin dimensión fija. Tres
modos comparten
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

### Un modelo de embeddings aprendido, en local

La línea base que se distribuye es una proyección determinista de trigramas de
caracteres. Demuestra la vía de pgvector y nada más, y la medición de abajo
explica por qué. `IEP_EMBEDDING_PROVIDER=ollama` la sustituye por un modelo
multilingüe real en la misma máquina: sin clave, sin salida de datos y sin
factura.

```text
IEP_EMBEDDING_PROVIDER=ollama
IEP_OLLAMA_EMBEDDING_MODEL=bge-m3
```

Después, `iep reindex --reference INN-2025-042` para cada expediente. El
reindexado se identifica con un hash de configuración que cubre proveedor,
modelo y anchura, así que las filas antiguas no se reutilizan ni se comparan
con las nuevas: simplemente dejan de seleccionarse.

La anchura la decide el modelo, no la configuración: `bge-m3` devuelve 1024
dimensiones y `qwen3-embedding` 2560. La columna era `vector(512)`, que era la
anchura de la línea base haciéndose pasar por el esquema y dejaba inservible
cualquier modelo aprendido; ahora no lleva modificador de dimensión, así que
conviven filas de anchuras distintas y `vector_dims()` informa de cada una. Lo
que se renuncia es a indexar —HNSW e IVFFlat necesitan anchura fija— y hoy no
se pierde nada, porque este proyecto hace búsqueda exacta con recorrido
secuencial por decisión deliberada.

### El suelo de relevancia: tres mediciones, dos equivocadas

La búsqueda vectorial devuelve `limit` filas para cualquier consulta, así que
una pregunta sobre algo que no está en el expediente volvía igual que una
sobre algo que sí. `IEP_RETRIEVAL_MIN_SIMILARITY` es el filtro para eso,
aplicado en SQL para que una consulta filtrada no gaste su límite en filas que
va a descartar.

Elegir su valor por defecto costó tres intentos, y la secuencia **es** el
hallazgo. Similitud coseno del primer resultado sobre `INN-2025-042`:

| Intento | Modelo | Consultas | Relevante más baja | Irrelevante más alta | Margen |
| --- | --- | ---: | ---: | ---: | ---: |
| 1 | `bge-m3` | 8 | 0,5365 | 0,4376 | **+0,099** |
| 2 | `qwen3-embedding:4b` | 13 | 0,5649 | 0,5671 | −0,002 |
| 3 | `qwen3-embedding:4b` | 17 | 0,4968 | 0,5671 | −0,070 |

El primero parecía un hueco limpio y de ahí salió un suelo de 0,45. El
segundo, tras añadir una pregunta relevante que exige contar personas dentro
de un texto y dos frases genéricas cortas en español, cerró el hueco por
completo: se debilitó el criterio a «nunca descartar un acierto relevante» y
el suelo pasó a 0,50. La tercera ampliación rompió también eso: «¿Cuántas
personas tienen dedicación al proyecto?» puntúa **0,4968**, por debajo de dos
consultas irrelevantes, así que 0,50 cortaba una pregunta legítima *y* dejaba
pasar ruido igualmente.

Cada ampliación bajó la relevante más baja. Un umbral ajustado a una muestra
falla con la misma pregunta formulada de otra manera, y así se ve en la
práctica — la misma pregunta, con suelo y sin él:

```text
suelo 0,50   -> 0 fragmentos, «la búsqueda no ha encontrado nada»
suelo 0      -> 1 cita, «No se puede determinar el número de personas
                 con dedicación al proyecto a partir de la evidencia»
```

La segunda es la mejor respuesta, y es la razón por la que el suelo se
distribuye **apagado**. La asimetría no está ni cerca: el ruido que llega al
generador se recupera, porque dice que la evidencia no sostiene la pregunta.
La evidencia retirada antes de que el generador la vea no la recupera nada,
porque nadie puede informar de una ausencia que nunca vio.

El ajuste se queda, para un despliegue que haya medido su propio corpus y sus
propias preguntas. Lo que se entrega aquí es el filtro y las mediciones, no un
número.

### El modelo de embeddings

Hay dos disponibles en local, y uno mide mejor:

| Modelo | Parámetros | Dims | Relevante más baja (13 consultas) |
| --- | ---: | ---: | ---: |
| `bge-m3` | 567M | 1024 | 0,4480 |
| `qwen3-embedding:4b` | 4B | 2560 | **0,5649** |

`qwen3-embedding:4b` puntúa más alto en todas las consultas relevantes y las
ordena mejor, a cambio de 2,5 GB y medio segundo más por lote, así que es el
predeterminado. `bge-m3` sigue siendo una buena opción más pequeña. Los dos
son modelos multilingües aprendidos y los dos son enormemente mejores que la
línea base hashing, cuya consulta relevante más baja puntúa 0,1626 — por
debajo de la mitad de las irrelevantes.

### Una pregunta no es una frase

La recuperación léxica no devolvía nada para las preguntas que sugiere el
propio copiloto. `plainto_tsquery` une sus términos con AND, que es lo correcto
para una frase copiada de un documento y lo incorrecto para una pregunta: ante
*¿Qué periodo de ejecución declara la memoria?* exigía `periodo & ejecución &
declara & memoria` en un mismo fragmento, y `declara` y `memoria` son palabras
de la pregunta, no del documento que la responde. Medido sobre `INN-2025-042`,
64 fragmentos:

| Forma de la consulta | Fragmentos que coinciden |
| --- | ---: |
| todos los términos (`plainto_tsquery`) | 0 |
| cualquier término (`websearch_to_tsquery`, OR) | 2 |

El expediente dice `Periodo de ejecución: 01/03/2024 - 30/11/2024` en su
primera página, y el copiloto informaba de que esas palabras no aparecían. En
modo híbrido la mitad vectorial seguía encontrando el fragmento, así que el
síntoma visible dependía del modo elegido: justo el tipo de defecto que una
demo esconde hasta que alguien pulsa la sugerencia.

Ahora la recuperación prueba primero con todos los términos y, si nada
coincide, con cualquiera de ellos, ordenados por `ts_rank_cd`. El orden
importa: un fragmento que contiene la consulta entera es mejor coincidencia que
uno que contiene una parte, y ninguna función de ranking puede recuperar esa
distinción cuando los dos están ya en el mismo conjunto de resultados. La
densidad de cobertura es el ranking adecuado para el respaldo porque premia al
fragmento que cubre más de la pregunta con las coincidencias más cerca.

Un resultado léxico vacío sigue afirmando algo preciso, y más fuerte que antes:
ni una sola palabra de la pregunta aparece en este expediente.

Las tres sondas de recuperación publicadas no se mueven - citan frases de los
documentos, que la pasada estricta ya encontraba - y eso se comprobó en vez de
suponerlo: de dieciocho pares sonda/expediente, diecisiete devuelven la misma
lista de fragmentos y el único que cambia pasó de cero aciertos al documento
correcto.

### Una configuración que no puede coincidir

La búsqueda vectorial exige que la consulta y las filas lleven el mismo hash de
configuración de embeddings, que es lo que impide comparar un vector base de
512 dimensiones con uno aprendido de 2560. El precio es que apuntar un proceso
a otro modelo de embeddings convierte cualquier consulta vectorial en un
resultado vacío: correcto, e indistinguible de una pregunta que el expediente
no responde.

Aquí pasó exactamente eso. El corpus se reindexó con `qwen3-embedding:4b`
mientras seguía sirviendo un proceso configurado con el modelo anterior, y su
copiloto informaba de que un periodo impreso en la primera página no estaba.
El silencio es la única respuesta que nadie puede depurar, así que ya no se da:
`stored_embeddings()` informa de con qué configuraciones está indexado un
expediente y, cuando ninguna coincide con este proceso, tanto la respuesta
vacía como el estado que pinta la pantalla dicen qué modelo hay almacenado,
cuál está configurado y que `iep reindex` es el arreglo.

### Por qué una respuesta local tardaba dos minutos

No se enviaba `num_ctx`, así que Ollama reservaba el contexto máximo del
modelo. En `qwen3.5:9b` son 262144 tokens, y la caché KV resultante no cabe en
16 GB de VRAM junto a los pesos: se desborda, y cada token generado se paga
entonces a velocidad de memoria del host. Misma petición, misma respuesta de
68 tokens:

| `num_ctx` | Generación | VRAM ocupada |
| --- | ---: | ---: |
| sin fijar (262144) | ~35 s | 14,0 GB |
| 8192 | ~1,2 s | 5,7 GB |

De extremo a extremo, una respuesta citada a una pregunta sugerida pasó de
97-151 s a unos 4-14 s. La ventana se dimensiona a partir del prompt que este
sistema construye de verdad, no por redondez: `rag_max_context_chars` son 12000
caracteres, el español gasta unos 3,5 caracteres por token, así que la
evidencia son ~3400 tokens, el prompt de sistema ~500 y la respuesta hasta
`rag_max_output_tokens`. Una prueba comprueba que la ventana configurada supera
esa suma, porque una ventana demasiado pequeña para el prompt es peor que una
lenta: el modelo respondería a partir de un fragmento truncado de la evidencia
y lo citaría como si estuviera completo.

El tamaño de los pesos también es el número que predice cómo se va a sentir una
respuesta, así que el selector lo muestra (`qwen3.5:9b · 6,6 GB`) y
preselecciona el modelo con el que está configurado el proceso. Elegir otro
modelo local desaloja al que está cargado, lo que cuesta cerca de un minuto de
carga antes del primer token.

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
**la API tiene que arrancarse en el host** para que esto funcione. Lo hace un
solo comando, y sirve la misma dirección que el modo contenedor:

```powershell
.\scripts\dev.ps1 -Mode host
```

Eso para la API y el worker del contenedor, arranca los dos aquí contra el
mismo PostgreSQL y abre `http://127.0.0.1:8000/ui/dossiers`.

Antes había dos puertos, y se rompían el uno al otro en silencio. Dos procesos
de API pueden compartir esta base de datos y no compartir sistema de ficheros,
así que un documento subido por uno queda registrado en una fila que el otro
puede leer mientras sus bytes están donde sólo el primero los ve —y
`POST /process` encola el trabajo para que lo ejecute un worker, así que lo que
fallaba entre los dos era subir y procesar, no sólo descargar el informe—. Un
proceso a la vez en un solo puerto elimina la clase entera.

Lo que pone el script, y por qué importa cada cosa:

```text
IEP_DATABASE_URL          el mismo PostgreSQL, por el puerto publicado
IEP_STORAGE_ROOT          var/objects, que se llena del volumen al entrar
IEP_REPORT_ROOT           var/reports, igual
IEP_RAG_PROVIDER=cli      el sentido de este modo
IEP_EMBEDDING_PROVIDER    ollama — el que es fácil olvidar
IEP_PDF_RENDERER          Chrome, que aquí no está en el PATH
PATH                      Tesseract delante, o los escaneos no se leen
```

`IEP_EMBEDDING_PROVIDER` importa porque la búsqueda vectorial sólo compara
vectores hechos con la misma configuración: un proceso en host que se quede con
la línea base por defecto no encontrará nada en un corpus indexado con un
modelo aprendido. La pantalla lo dice, en vez de devolver una respuesta vacía.

`IEP_PDF_RENDERER` sólo hace falta fuera de la imagen, que instala `chromium`
en el PATH. En Windows Chrome no está en el PATH, así que sin esto
`reports/latest.pdf` responde 503 justo en la máquina donde es más probable
que alguien lo pruebe.

El almacén de objetos y los informes se copian del volumen al entrar, por lo
mismo: la base de datos es compartida, así que un documento sembrado en modo
contenedor está en una fila que este proceso puede leer y que nombra un fichero
que no puede leer, y el visor de evidencias respondería 404 en la URL que
acababa de funcionar. `docker compose cp` fusiona con el directorio que ya
existe en lugar de anidarse dentro, así que no se pierde nada de lo que hubiera:

```bash
docker compose cp api:/var/lib/iep/objects/. var/objects
docker compose cp api:/var/lib/iep/reports/. var/reports
```

`.\scripts\dev.ps1` sin argumentos vuelve al modo contenedor; `-Stop` lo para
todo y conserva los volúmenes.

`GET /dossiers/{id}/questions` dice si el CLI configurado está en el `PATH` de
este proceso, y el panel lo repite, para que una mala configuración se lea como
una frase en lugar de como un fallo que hay que interpretar.

### Qué modelos ofrece el selector

Tres fuentes, y no son simétricas — que es lo interesante.

A **Ollama** se le pregunta por HTTP (`/api/tags`) y contesta con lo que haya
descargado, así que la lista es la de esta máquina. Los modelos que solo hacen
embeddings se filtran: ofrecer uno haría el selector más rico y la primera
pregunta fallaría.

A **Codex** también se le pregunta. El CLI trae un app-server que habla
JSON-RPC por stdio, y `model/list` devuelve el mismo catálogo que pinta su
selector interactivo `/model`: ids, nombres, las descripciones del propio
proveedor y el esfuerzo de razonamiento por defecto. Medido en frío en esta
máquina, 1,8 s. La respuesta se guarda diez minutos porque la pantalla
consulta el estado con frecuencia y cada pregunta cuesta un proceso. Los
modelos marcados `hidden` se descartan, porque esa marca existe precisamente
para mantenerlos fuera de un selector. Hay dos cosas que a propósito no se
leen: `~/.codex/` —la configuración daría un solo modelo y los históricos de
sesión son el registro del trabajo de alguien— y `account/read`, que responde
con la dirección de correo con la que se inició sesión y que ninguna pantalla
de aquí necesita.

**Claude** no tiene equivalente, así que sus tres entradas son una elección
editorial escrita a mano: rápido y barato, capaz, el más capaz. Quien elige
entre once nombres no está eligiendo.

Cuando un descubrimiento falla, el backend sigue en la pantalla con una única
entrada que significa «lo que tenga configurado el CLI», y que no envía
`--model` en absoluto. Perder un backend que funciona porque un protocolo
experimental se ha movido sería peor que no conocer su inventario.

Venga de donde venga, un id de modelo que llega de un cliente se resuelve
contra este catálogo antes de usarse, porque en los backends de CLI acaba en
`argv`. Un id que nadie ofreció se rechaza, no se sustituye por otro.

El cupo restante de la suscripción **no** está disponible: en codex llega como
notificación `account/rateLimitsUpdated` después de ejecutar un turno —por eso
el `/status` interactivo muestra lo que aprendió la última llamada y no una
cifra en vivo— y `claude auth status --json` informa del plan sin campo de
cupo. Por eso el medidor informa de lo que ha gastado esta aplicación, que es
el único número del que puede responder.

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
