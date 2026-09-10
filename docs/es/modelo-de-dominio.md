**Español** · [English](../domain-model.md)

# Modelo de dominio

Cada objeto de abajo es un contrato Pydantic en
[`src/iep/domain/contracts.py`](../../src/iep/domain/contracts.py) y una tabla en
[`src/iep/db/models.py`](../../src/iep/db/models.py). `CONTRACT_VERSION` se
estampa en cada extracción guardada, así que una fila siempre puede decir bajo
qué forma se escribió.

## Entidades

### Expediente (`Dossier`)

Una justificación en revisión. Lleva la referencia de negocio (`INN-YYYY-NNN`),
el periodo del proyecto, el total reclamado, una página de convocatoria opcional
y su estado. La referencia es única: reenviarla es una repetición de un
expediente existente, nunca un segundo expediente.

### Documento

Cualquier cosa a la que se puedan trazar las conclusiones, sea cual sea su
origen: una subida, una instantánea del registro o una página capturada.
Registra el nombre de fichero del cliente (sólo para mostrar), el tipo de medio
declarado (registrado, no confiado), el tipo de medio decidido por firma y
parser, el tipo clasificado, tamaño, SHA-256, clave de almacenamiento, número de
páginas y —cuando se rechazó— por qué.

Un documento rechazado no tiene bytes en el almacén de objetos. Se registra para
que quien revisa vea qué se entregó, en lugar de preguntarse qué falta.

### Extracción

Un campo, un valor, un sitio de donde salió:

| Campo | Por qué existe |
| --- | --- |
| `field_path` | qué se leyó, p. ej. `invoice.total_eur`, `timesheet.rows[3].hours` |
| `value_text` / `value_number` / `value_date` | el valor tipado; los importes son `Decimal` |
| `locator` | el sitio exacto — ver abajo |
| `method` | texto de PDF, OCR, celda de Excel, HTTP, HTML, agregado, humano |
| `extractor_version` | qué lector lo produjo |
| `contract_version` | bajo qué forma se escribió |
| `confidence` | lo que lo deriva a una persona |
| `status` | extraído, necesita revisión, confirmado, corregido, rechazado |
| `original_value_text` | lo que leyó la máquina, conservado cuando una persona discrepa |
| `corrected_by` / `corrected_at` / `correction_reason` | quién, cuándo, por qué |
| `dedup_key` | hace que reprocesar sea una actualización y no un duplicado |

### Localizador de evidencia

Una unión discriminada, para que no se pueda construir una combinación
imposible:

| Tipo | Lleva |
| --- | --- |
| `PDF_PAGE` | página, rango de caracteres, fragmento |
| `OCR_WORD_BOX` | página, caja, confianza de palabra del motor, fragmento |
| `EXCEL_CELL` | hoja, referencia A1, fila, columna |
| `API_FIELD` | endpoint, id del registro, ruta JSON, versión de contrato |
| `HTML_SELECTOR` | URL, selector CSS, momento de captura, fragmento |
| `DERIVED` | los ids de extracción que se combinaron, y la regla |

### Incidencia de validación (`ValidationFinding`)

Lo que una regla concluyó sobre el expediente: id de regla, versión de regla,
severidad (`BLOCKER` / `WARNING` / `INFO`), mensaje, detalle estructurado, las
extracciones y documentos a los que apunta, estado, y cómo lo resolvió quien
revisó. Su `fingerprint` es un hash de regla más sujeto, así que volver a
validar refresca la misma fila en vez de añadir un casi-duplicado.

### Decisión de revisión

Un registro append-only de una acción humana: corregir, confirmar, aceptar,
descartar, aprobar, rechazar — con actor, motivo y marca de tiempo.

### Trabajo de procesamiento

Una fila de cola: expediente, tipo, estado, intentos, techo, payload, clave de
idempotencia, momento de disponibilidad, lease y titular, último error.

### Evento de auditoría

Append-only. Acción, actor, correlation id, payload estructurado, marca de
tiempo. Se escribe dentro de la transacción del cambio que describe, así que un
rollback no puede dejar un registro afirmando que pasó algo.

### Informe

Un informe HTML renderizado con su hash de contenido, el estado del expediente
en ese momento, y los recuentos con los que se generó.

### Fragmento de documento

Un segmento de texto acotado y enlazado a expediente y documento, con su
localizador original, vector de búsqueda textual en español y un embedding
`vector` opcional sin dimensión fija. Proveedor, modelo, hash de configuración y fecha hacen
inspeccionable el reindexado y evitan mezclar espacios vectoriales incompatibles.

### Error de API

La única forma de fallo que ve un cliente: `error`, `message`,
`correlation_id` y un objeto `detail` opcional. Nunca una traza de pila.

## Rutas de campo en este dominio

| Prefijo | Fuente | Ejemplos |
| --- | --- | --- |
| `report.*` | memoria técnica, texto de PDF | `project_code`, `period_start`, `declared_total_eur` |
| `invoice.*` | justificante escaneado, OCR | `number`, `issue_date`, `base_eur`, `vat_eur`, `total_eur` |
| `timesheet.rows[n].*` | celdas de Excel | `employee_id`, `month`, `hours`, `hourly_rate_eur`, `amount_eur` |
| `invoices.*` | derivado | `total_eur`, `count` |
| `timesheet.*` | derivado | `total_amount_eur`, `row_count` |
| `call.*` | página publicada | `eligible_from`, `eligible_to`, `max_funding_eur` |

## Omisiones deliberadas

No hay `user`, ni `organisation`, ni `tenant`. La identidad de quien revisa es
una cadena que aporta el cliente, lo cual es honesto respecto a que no hay
autenticación: ver [modelo-de-amenazas.md](modelo-de-amenazas.md) y
[brecha-produccion.md](brecha-produccion.md). Una tabla `user` sin autenticación
parecería control de acceso sin serlo.
