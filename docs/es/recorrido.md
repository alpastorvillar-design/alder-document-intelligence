**Español** · [English](../walkthrough.md)

# Recorrido guiado

Este documento asume que no sabes nada del proyecto. Explica qué hace, qué
piezas se ejecutan, qué es cada endpoint, cómo lanzar la demostración, cómo
mirar el workflow de n8n, y dónde encajan —y dónde no— el RAG y un modelo de
lenguaje.

---

## 1. El problema, en una frase

Una consultora que justifica ayudas a la innovación recibe, por expediente, una
memoria técnica en PDF, un puñado de facturas escaneadas, un Excel de horas, un
registro de personal en un sistema corporativo y una convocatoria publicada en
una web. Alguien tiene que decidir **si el gasto declarado está soportado**, y
si mañana lo cuestionan, **enseñar de dónde salió cada cifra**.

Este proyecto automatiza ese paso de revisión. No lo decide: lo prepara,
contradice lo que no cuadra, y deja la aprobación a una persona.

## 2. Qué se está ejecutando

Cinco contenedores. Cuatro por defecto, uno opcional.

| Servicio | Qué es | Por qué existe |
| --- | --- | --- |
| `postgres` | PostgreSQL 17.11 | Estado, cola de trabajos, auditoría y búsqueda de texto |
| `api` | FastAPI | Recibe documentos y responde preguntas. Es lo que abres en el navegador |
| `worker` | Bucle Python | Hace lo lento: abrir PDFs, llamar a Tesseract, leer Excel, consultar el registro |
| `devsources` | FastAPI (solo desarrollo) | **Finge** ser los sistemas corporativos: una API de personal y una página pública. Falla y limita a propósito para que se vea cómo reacciona el conector |
| `n8n` | Motor de workflows | Opcional. Orquesta el proceso llamando a la API. Sólo arranca con `--profile n8n` |

`api` y `worker` usan **la misma imagen**. Eso importa: garantiza que la versión
del extractor que mide la precisión es la misma que la que procesa.

**`devsources` es un simulador, no parte del producto.** Es una aplicación
distinta (`src/devsources/`) para que ningún endpoint de pruebas pueda existir
dentro de la API real.

## 3. El recorrido de un expediente

```
  1. crear expediente          POST /dossiers
            ↓
  2. subir documentos          POST /dossiers/{id}/documents   (uno por fichero)
            ↓
  3. encolar procesamiento     POST /dossiers/{id}/process
            ↓
  4. el worker lo coge         (cola en PostgreSQL, con lease)
            ↓
  5. captura fuentes externas  registro de personal + página de convocatoria
            ↓
  6. lee cada documento        PDF nativo → texto; escaneo → OCR; Excel → celdas
            ↓
  7. clasifica                 memoria / factura / partes horarios
            ↓
  8. agrega                    suma facturas, suma horas
            ↓
  9. valida                    11 funciones de regla (26 rule-ids posibles)
            ↓
 10. NEEDS_REVIEW              siempre. Nunca aprueba solo
            ↓
 11. una persona revisa        corrige, confirma, acepta, descarta
            ↓
 12. una persona decide        aprueba o rechaza, con motivo, en auditoría
```

**El punto 10 es la decisión de diseño central.** Un run correcto termina
siempre en `NEEDS_REVIEW`, haya encontrado algo o no. La máquina de estados ni
siquiera admite la transición `PROCESSING → APPROVED`; hay un test que lo fija.

## 4. Qué es cada endpoint

Cuando abras `http://127.0.0.1:8000/docs`, cada grupo y cada endpoint lleva ya su
propia explicación —para qué sirve, qué significa la respuesta y qué regla hay
detrás—, de modo que la página se lee sola (en inglés). Esto es lo mismo en una
pantalla:

### `system` — ¿está vivo?

| Endpoint | Para qué sirve |
| --- | --- |
| `GET /healthz` | ¿El proceso responde? No toca nada más. Es lo que mira Docker |
| `GET /readyz` | ¿Puede *trabajar*? Comprueba base de datos y almacén de objetos |
| `GET /metrics` | Contadores en formato Prometheus: trabajos, incidencias, errores |

Están separados a propósito: si confundes "vivo" con "listo", un orquestador
reinicia un proceso sano porque la base de datos parpadeó un segundo.

### `dossiers` — el expediente y sus documentos

| Endpoint | Para qué sirve |
| --- | --- |
| `POST /dossiers` | Crea el expediente: referencia, periodo, importe reclamado |
| `GET /dossiers` | Lista. Acepta `?reference=INN-2025-042` para buscar por referencia de negocio |
| `GET /dossiers/{id}` | Un expediente y su estado |
| `POST /dossiers/{id}/documents` | Sube **un** fichero. Comprueba tamaño, firma y parser antes de guardar nada |
| `GET /dossiers/{id}/documents` | Qué se ha entregado, incluido lo que se rechazó y por qué |
| `POST /dossiers/{id}/process` | Encola el procesamiento. Devuelve el trabajo |

### `jobs` — ¿ya está?

| Endpoint | Para qué sirve |
| --- | --- |
| `GET /jobs` | Trabajos, filtrables por estado |
| `GET /jobs/{id}` | Uno concreto: estado, intentos, último error |

Un trabajo fallido **se ve**, con su error y su número de intentos. `FAILED` y
`DEAD_LETTER` son estados inspeccionables, no un descarte silencioso.

### `review` — lo que hace una persona

| Endpoint | Para qué sirve |
| --- | --- |
| `GET /dossiers/{id}/extractions` | Cada campo extraído, con su localizador y su confianza |
| `GET /dossiers/{id}/findings` | Las incidencias que las reglas han levantado |
| `GET /dossiers/{id}/decisions` | Qué ha decidido cada persona y por qué |
| `POST /extractions/{id}/correct` | "Esto está mal leído, el valor correcto es X" |
| `POST /extractions/{id}/confirm` | "Lo he mirado contra el documento, está bien" |
| `POST /findings/{id}/resolve` | Acepta o descarta una incidencia, con motivo |
| `POST /dossiers/{id}/approve` | Aprueba. **Se niega** si queda un bloqueante abierto |
| `POST /dossiers/{id}/reject` | Rechaza, con motivo |

Una corrección **nunca borra** lo que leyó la máquina: guarda el valor original
al lado, con quién lo cambió, cuándo y por qué.

### `artifacts` — lo que te llevas

| Endpoint | Para qué sirve |
| --- | --- |
| `POST /dossiers/{id}/reports` | Genera el informe HTML |
| `GET /dossiers/{id}/reports/latest.html` | El último informe, para leerlo |
| `GET /dossiers/{id}/export.json` | Todo en JSON, para otro sistema |
| `GET /dossiers/{id}/export.csv` | Todo en CSV, con protección contra fórmulas |
| `GET /dossiers/{id}/audit` | El registro append-only: todo lo que ha pasado |
| `GET /dossiers/{id}/evidence?q=...` | Busca una frase dentro de los documentos del expediente |

### `ui` — la pantalla de revisión

| Endpoint | Para qué sirve |
| --- | --- |
| `GET /ui/dossiers` | La lista. **Es la página por la que empezar** |
| `GET /ui/dossiers/{id}` | La revisión de un expediente: incidencias, campos, botones |

## 5. Cómo lanzar la demostración

Desde el repositorio, en PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/demo.ps1 -Fresh
```

`-Fresh` borra los dos expedientes de demostración antes de empezar. Sin él, la
segunda vez te dirá que ya están en revisión y no volverá a cargarlos — un
expediente en revisión rechaza documentos nuevos por diseño.

El script hace, en orden:

1. valida el modelo de Compose;
2. construye la imagen (omitible con `-SkipBuild`);
3. levanta `postgres`, `devsources`, `api` y `worker` y espera a que estén sanos;
4. genera el corpus sintético dentro del contenedor;
5. crea los dos expedientes y sube sus documentos;
6. procesa los dos;
7. genera los dos informes;
8. imprime el inventario y los enlaces.

### Qué deberías ver

```
INN-2025-041: 4 accepted, 0 duplicate, 0 rejected
  rejected justificante-danado.pdf: PDF has no pages
  rejected notas-internas.txt: unrecognised file signature
INN-2025-042: 8 accepted, 1 duplicate, 2 rejected
```

- `INN-2025-041` es el **camino limpio**: 0 incidencias.
- `INN-2025-042` lleva defectos sembrados a propósito: 17 incidencias, 11 bloqueantes.
- Los dos rechazos y el duplicado **son parte del guion**: un PDF truncado, un
  `.txt` que no es formato admitido, y una copia byte a byte de la memoria.

### Dónde mirar después

1. `http://127.0.0.1:8000/ui/dossiers` — la lista. Pulsa *review* en `INN-2025-042`.
2. En esa pantalla verás arriba las incidencias y abajo cada campo con su
   **evidencia**: "página 1, caracteres 120-141" o "hoja 'Partes horarios',
   celda E7" o "página 1, caja (243,801) 512x28, confianza OCR 64 %".
3. Escribe tu nombre en el recuadro, escribe un motivo en una incidencia y pulsa
   *Dismiss*. Recarga: verás quién y por qué.
4. Pulsa *Generate report*: se abre el informe HTML.
5. Intenta *Approve* con bloqueantes abiertos: **te lo va a rechazar**. Ese es el
   comportamiento correcto, y es lo que conviene enseñar.

## 6. Cómo abrir n8n y ver el workflow

n8n **no arranca por defecto**. Se levanta con su perfil:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/demo.ps1 -SkipBuild -WithN8n
```

O a mano, en PowerShell:

```powershell
docker compose --profile n8n run --rm --no-deps n8n import:workflow --input=/workflows/dossier-review.json
docker compose --profile n8n run --rm --no-deps n8n update:workflow --id=iep-dossier-review --active=true
docker compose --profile n8n up -d --wait n8n
```

Para estos tres usa PowerShell y no Git Bash: Git Bash reescribe la ruta de
contenedor `/workflows/...` como ruta de Windows y la importación falla con
`ENOENT`.

Se importa **antes** de arrancar el servidor a propósito: n8n guarda su estado
en SQLite y dos procesos escribiendo a la vez dan `SQLITE_BUSY`.

Después abre `http://127.0.0.1:5678`. La primera vez te pedirá crear una cuenta
local — es sólo de tu instancia, no sale a ningún sitio. Dentro verás el
workflow *Dossier review orchestration*. Pulsa cualquier nodo para ver qué hace.

Para dispararlo:

```bash
curl -X POST http://127.0.0.1:5678/webhook/dossier-review -H "content-type: application/json" -d "{\"reference\":\"INN-2025-042\"}"
```

Responde con la notificación simulada: asunto, número de incidencias abiertas,
bloqueantes y enlace a la pantalla de revisión.

**Qué demuestra y qué no.** Demuestra que el pipeline se puede orquestar desde
fuera por HTTP, con clave de idempotencia y correlation id. No contiene lógica
de negocio: qué es una incidencia y si un expediente puede aprobarse se deciden
en Python, donde están versionados y probados.

## 7. Tesseract en local (opcional)

Sin Tesseract instalado, 13 pruebas se **omiten** en tu máquina. CI instala
Tesseract y las ejecuta. La imagen de producción también incluye Tesseract para
que la aplicación haga OCR real, pero deliberadamente no incluye pytest ni las
demás herramientas de desarrollo. Si quieres ejecutar esas pruebas en el host:

```powershell
winget install --id UB-Mannheim.TesseractOCR
```

Después, añade la carpeta de instalación al `PATH` (normalmente
`C:\Program Files\Tesseract-OCR`) y **abre una terminal nueva**. Comprueba:

```powershell
tesseract --version
tesseract --list-langs
```

Necesitas el idioma `spa`. El instalador de UB-Mannheim lo ofrece en
*Additional language data*; si no lo marcaste, reinstala marcándolo. Con eso, las
13 pruebas dejan de omitirse.

**No es necesario para la demostración**: la aplicación corre dentro del
contenedor, que ya incluye el motor OCR y los datos del idioma español.

## 8. Recuperación y frontera RAG opcional

**RAG** = *Retrieval-Augmented Generation*: recuperas fragmentos relevantes y se
los das a un modelo **para que genere** una respuesta apoyada en ellos.

El índice de evidencia ofrece tres modos:

- Trocea cada documento en segmentos, **cada uno con su localizador**.
- texto completo de PostgreSQL en español para términos exactos;
- coseno exacto sobre un `vector(512)` almacenado con pgvector;
- fusión por rango recíproco de las listas lexical y vectorial.

`GET /dossiers/{id}/evidence?q=periodo de ejecucion&mode=hybrid` devuelve los
top-k con sus localizadores. Eso es solo recuperación.

`POST /dossiers/{id}/questions` sí es RAG: recupera evidencia, entrega fragmentos
acotados a un generador alojado y devuelve un borrador con ids de cita. Está
desactivado por defecto, es de solo lectura, no tiene herramientas y no puede
aprobar ni cambiar un expediente.

Los vectores por hashing predeterminados prueban offline la base y el ranking,
pero no son un modelo semántico aprendido. El proveedor alojado de embeddings se
activa aparte y debe evaluarse con paráfrasis representativas antes de afirmar
calidad. Se usa búsqueda exacta porque este corpus no justifica un índice
aproximado.

Los comandos, controles y límites están en
[Recuperación híbrida y RAG opcional](rag.md); el cambio se registra en la
[ADR 0006](adr/0006-recuperacion-hibrida-y-rag-opcional.md).

## 9. Ver el modelo de lenguaje funcionando

Hoy hay **dos proveedores semánticos** detrás del mismo protocolo:

| Proveedor | Qué hace | Coste |
| --- | --- | --- |
| `deterministic` (por defecto) | Clasifica y localiza campos con reglas y expresiones regulares | 0 € |
| `llm` | Adaptador real contra la API de Anthropic con salidas estructuradas | Céntimos |

El adaptador `llm` **nunca ha hecho una llamada facturable**: está probado
contra dobles inyectados. Se puede activar, y en
[`docs/es/demostracion-llm.md`](demostracion-llm.md) está el procedimiento
exacto, con lo que cuesta y con qué mirar.

El modelo de extracción semántica solo clasifica; sus campos propuestos no se
persisten y ninguna regla compara cifras producidas por él. El modelo RAG
separado puede redactar un borrador con citas, pero no tiene herramientas ni
ruta de escritura. Ninguno puede mover cifras ni aprobar un expediente.

## 10. Si algo va mal

| Síntoma | Qué mirar |
| --- | --- |
| La lista sale vacía | ¿Ejecutaste `seed`? `docker compose exec -T api iep status` |
| `iep seed` dice "already NEEDS_REVIEW" | Correcto. Usa `-Fresh` o `iep reset --reference <ref>` |
| El webhook de n8n da 404 | n8n aún no ha registrado el webhook; espera unos segundos y reintenta |
| Un contenedor no arranca | `docker compose logs --tail=100 api worker` |
| Quiero empezar de cero del todo | `docker compose --profile n8n down --volumes` y vuelve a lanzar la demo |

Cada respuesta de error de la API lleva un `correlation_id`. Ese identificador
aparece en los logs de `api` y de `worker`, así que se puede seguir una petición
concreta de punta a punta.
