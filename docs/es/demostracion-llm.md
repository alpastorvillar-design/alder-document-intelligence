**Español** · [English](../llm-demo.md)

# Ver el modelo de lenguaje funcionando

Hay dos formas de ejecutar el camino del proveedor alojado. La primera no cuesta
nada y no necesita clave. La segunda cuesta céntimos y usa un modelo real.

---

## 1. Qué es exactamente "el camino del LLM"

El pipeline nunca habla con un modelo directamente. Pide a un
`SemanticExtractor` una **clasificación** y unas **propuestas de campo
fundamentadas**. Hay dos implementaciones del mismo protocolo:

| Proveedor | Qué es | Se elige con |
| --- | --- | --- |
| `deterministic` | Reglas y expresiones regulares. Es el de por defecto | `--provider deterministic` |
| `llm` | Adaptador real contra la API de Anthropic, con salidas estructuradas | `--provider llm` |

Tres restricciones se cumplen sea cual sea el proveedor:

1. la respuesta tiene que validar contra el esquema declarado, o se descarta;
2. cada valor propuesto tiene que **citar texto que exista en el documento**, o
   se descarta;
3. **nada de lo que devuelve un proveedor se usa para aritmética.** Sus
   propuestas de campo ni siquiera se guardan como extracciones: sólo se usa la
   clasificación. Todo importe que compara una regla viene de un lector
   determinista con localizador.

Ese punto 3 es lo que hace inerte un documento que intente dar instrucciones al
sistema, y hay una prueba de integración que lo fija.

## 2. Opción A — gratis, sin clave, contra el simulador local

El servicio `devsources` responde a `/v1/messages` con el suficiente protocolo
para que el **SDK oficial** hable con él. No es un modelo: contesta leyendo el
documento con el proveedor determinista. Lo que sí ejerce es todo lo demás — el
formato de la petición con el esquema de salida estructurada, la validación de
la respuesta, la comprobación de fundamentación, los reintentos acotados y la
contabilidad de tokens y coste.

Además **falla a propósito una de cada tres llamadas** con una respuesta que el
esquema rechaza, para que el reintento se vea y no sólo se afirme.

```powershell
# La clave puede ser cualquier cosa: el simulador no la comprueba.
$env:IEP_LLM_API_KEY = "local-simulator"
docker compose up -d --force-recreate api worker devsources

docker compose exec -T api iep reset --reference INN-2025-042
docker compose exec -T api python -m corpus.generate --out /tmp/corpus
docker compose exec -T api iep seed --corpus /tmp/corpus `
  --call-page-url http://devsources:8080/public/convocatoria.html
docker compose exec -T api iep process --reference INN-2025-042 --provider llm
```

En la salida verás:

```json
{
  "reference": "INN-2025-042",
  "status": "NEEDS_REVIEW",
  "documents_processed": 8,
  "findings_open": 17,
  "semantic_provider": "anthropic",
  "semantic_config_hash": "fe743784a4441e399d998fe351adf562"
}
```

`semantic_provider: "anthropic"` confirma que ha ido por el adaptador. El
`semantic_config_hash` identifica la combinación exacta de modelo, prompts y
esquema con la que se produjo ese resultado — si cambias un prompt, cambia el
hash, y una extracción vieja sigue diciendo bajo qué configuración se escribió.

Entre las líneas de log del propio comando verás el reintento:

```
llm_schema_violation   attempt=1  errors=3
llm_call_completed     attempts=2 input_tokens=93 output_tokens=127 0.014s
llm_call_completed     attempts=1 input_tokens=92 output_tokens=127 0.003s
```

Ese primer par es exactamente el comportamiento que interesa enseñar: el modelo
devolvió algo que no encaja en el esquema, el adaptador **no lo aceptó**, le
devolvió el error de validación y reintentó. Si se agotan los intentos, no se
inventa nada: el documento se queda sin clasificar y sigue su camino a revisión.

### Qué demuestra y qué no

| Demuestra | No demuestra |
| --- | --- |
| Que el SDK oficial está bien usado | El juicio de un modelo real sobre prosa |
| Salidas estructuradas y validación estricta | Latencia real de red |
| El rechazo de una respuesta mal formada y el reintento | Coste real facturado |
| La comprobación de fundamentación | Tasas de error de un modelo concreto |
| La contabilidad de tokens y la estimación de coste | |

Merece la pena decirlo así al presentarlo. Es la diferencia entre "he integrado
un LLM" y "sé qué parte de mi integración está demostrada".

## 3. Opción B — con un modelo real

### Coste

Medido sobre el corpus completo (24 documentos, ~55.000 tokens de entrada
estimados, ~800 de salida por documento):

| Modelo | Precio entrada/salida por millón | Coste estimado del corpus entero |
| --- | --- | --- |
| `claude-haiku-4-5` | 1 $ / 5 $ | **~0,13 €** |
| `claude-sonnet-5` | 2 $ / 10 $ | ~0,27 € |
| `claude-opus-5` | 5 $ / 25 $ | ~0,67 € |

Son céntimos. Aun así, **es dinero real**: la estimación es parametrizada y
puede quedarse corta si repites el corpus muchas veces.

### Procedimiento

1. Consigue una clave en la consola de Anthropic. Empieza por `sk-ant-`.
2. **No la pongas en ningún fichero del repositorio.** `.env` está en
   `.gitignore`, pero la vía limpia es la variable de entorno de la sesión.

```powershell
$env:IEP_LLM_API_KEY = "sk-ant-..."
$env:IEP_LLM_BASE_URL = "https://api.anthropic.com"
$env:IEP_LLM_MODEL = "claude-haiku-4-5"

docker compose up -d --force-recreate api worker
docker compose exec -T api iep reset --reference INN-2025-042
docker compose exec -T api iep seed --corpus /tmp/corpus `
  --call-page-url http://devsources:8080/public/convocatoria.html
docker compose exec -T api iep process --reference INN-2025-042 --provider llm
```

3. Cuando termines, quita la variable para no dejarla en la sesión:

```powershell
Remove-Item Env:IEP_LLM_API_KEY
```

### Qué mirar

- Los `llm_call_completed` traerán `input_tokens` y `output_tokens` **reales**,
  no estimados.
- `estimated_cost_eur` en la contabilidad de uso se calcula con esos tokens
  reales y los precios de lista. Sigue siendo una **estimación**: el precio
  facturado lo fija el proveedor, no este repositorio.
- La clasificación puede diferir de la del proveedor determinista. Eso es
  interesante: compara `document_kind` en `GET /dossiers/{id}/documents` entre
  una ejecución y otra.

### Lo que no cambia

Los 17 hallazgos de `INN-2025-042` **no dependen del proveedor**. Las reglas
comparan importes leídos por extractores deterministas. Si cambias de proveedor
y el número de hallazgos se mueve, es porque la clasificación de un documento
cambió y por tanto se le aplicaron otros lectores — no porque un modelo haya
decidido una cifra.

## 4. Estado honesto de esta integración

- El adaptador está escrito contra la documentación oficial vigente y usa
  `messages.parse` con un modelo Pydantic como esquema de salida.
- Está **probado contra dobles inyectados** y **ejecutado contra el simulador
  local**. En este repositorio no consta ninguna ejecución contra la API real.
- El proveedor se niega a construirse sin `IEP_LLM_API_KEY`, para que no se
  pueda seleccionar por accidente.
- Los prompts son ficheros versionados dentro del paquete
  (`src/iep/semantic/prompts/`), con su propia cadena de versión, y entran en el
  hash de configuración.

A la pregunta "¿lo has ejecutado contra el modelo real?", la respuesta honesta
es: el camino está construido y ejecutado extremo a extremo contra un simulador
que habla el protocolo; contra la API real cuesta unos céntimos y está a una
variable de entorno de distancia.
