**Español** · [English](README.md)

# Orquestación con n8n

Este directorio contiene tres workflows importables. Son opcionales:
`docker compose up` no arranca n8n, y el núcleo, las pruebas y el arnés de
evaluación funcionan sin él.

| Fichero | Patrón que demuestra | Resultado |
| --- | --- | --- |
| `dossier-review.json` | webhook, idempotencia, trabajo asíncrono, sondeo y error común | procesa y dirige el expediente a revisión |
| `review-queue-digest.json` | programación, lectura de una colección, agregación y salida simulada | resumen diario de la bandeja pendiente |
| `approved-dossier-handoff.json` | webhook, guardia de estado y mapeo de contrato | prepara la entrega de un expediente aprobado |

## 1. Procesamiento y enrutado

```
webhook (o disparador manual)
  -> localiza el expediente por su referencia de negocio
  -> POST /dossiers/{id}/process        (con Idempotency-Key)
  -> espera y consulta GET /jobs/{id} hasta que el trabajo sale de PENDING/RUNNING
  -> GET /dossiers/{id}
  -> si NEEDS_REVIEW: lista las incidencias abiertas y emite una notificación
     en caso contrario: informa de que no hace falta acción del revisor
  -> cualquier fallo HTTP en cualquier paso cae en una única rama de error que
     responde a quien llamó con el correlation id
```

La lógica de negocio no está aquí. El workflow decide *a quién avisar*; qué
cuenta como incidencia, qué dice un documento y si un expediente puede
aprobarse se deciden en Python, detrás de la API, donde están versionados y
probados. Esa separación es justamente el motivo de usar una herramienta de
workflows: mover una regla de validación a un nodo la dejaría en un sitio sin
pruebas y sin historial.

## 2. Resumen diario de revisión

`review-queue-digest.json` se puede lanzar a mano y lleva un disparador diario a
las 08:00. Consulta únicamente los expedientes en `NEEDS_REVIEW`, suma el importe
reclamado y construye una lista ordenada con enlaces a la pantalla de revisión.
La salida imita el cuerpo de un correo o mensaje, pero no llama a ningún servicio
externo ni lleva credenciales. En un despliegue real ese último nodo se sustituye
por el conector corporativo autorizado.

## 3. Entrega de un expediente aprobado

`approved-dossier-handoff.json` recibe una referencia, consulta el estado y sólo
recupera `export.json` si el expediente está en `APPROVED`. Un expediente en
revisión o rechazado queda en la rama bloqueada. La última transformación produce
un contrato `innovation.dossier.approved.v1`, pero la entrega a un ERP o gestor
documental es simulada: este repositorio no afirma disponer de esa integración.

Los dos workflows nuevos son deliberadamente de sólo lectura. Muestran
orquestación, priorización y una barrera de seguridad sin duplicar en n8n las
reglas o las transiciones de estado del backend.

## Por qué cada pieza es como es

- **Idempotency-Key en la llamada de proceso.** n8n reintenta. Sin la clave, un
  reintento tras un timeout encolaría el expediente dos veces.
- **Correlation id propagado.** Cada petición lleva
  `X-Correlation-ID: n8n-<id de ejecución>`, de modo que una ejecución del
  workflow se puede localizar en los logs de la API y del worker.
- **Sondeo en lugar de callback.** Un webhook de vuelta hacia n8n sería menos
  código aquí y más acoplamiento: el pipeline tendría que conocer n8n. El
  endpoint de trabajos ya expone lo que un orquestador necesita.
- **Una sola rama de error.** Cada nodo HTTP continúa por su salida de error
  hacia un único nodo que reporta el fallo con el correlation id, en lugar de
  que la ejecución se detenga en silencio.
- **Sin credenciales.** El único host con el que habla es la API en la red de
  Compose. No hay nada externo configurado, y el paso de notificación es un nodo
  Set que formatea el mensaje que *enviaría*.

## Cómo importarlos y ejecutarlos

El script de desarrollo selecciona la dirección correcta de la API para n8n:

```powershell
# API y worker en contenedores
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 -Mode container -WithN8n

# API y worker en Windows (necesario para los adaptadores de CLI locales)
powershell -ExecutionPolicy Bypass -File scripts/dev.ps1 -Mode host -WithN8n
```

Importa y activa antes de arrancar el servidor, para que el fichero SQLite de la
demostración tenga un único escritor. Después abre http://localhost:5678 o llama
al webhook:

```bash
docker compose --profile n8n stop n8n
docker compose --profile n8n run --rm --no-deps n8n \
  import:workflow --input=/workflows/dossier-review.json
docker compose --profile n8n run --rm --no-deps n8n \
  import:workflow --input=/workflows/review-queue-digest.json
docker compose --profile n8n run --rm --no-deps n8n \
  import:workflow --input=/workflows/approved-dossier-handoff.json
docker compose --profile n8n run --rm --no-deps n8n \
  update:workflow --id=iep-dossier-review --active=true
docker compose --profile n8n run --rm --no-deps n8n \
  update:workflow --id=iep-approved-dossier-handoff --active=true
docker compose --profile n8n up -d --wait n8n
curl -X POST http://localhost:5678/webhook/dossier-review \
     -H 'content-type: application/json' \
     -d '{"reference":"INN-2025-042"}'
curl -X POST http://localhost:5678/webhook/approved-dossier-handoff \
     -H 'content-type: application/json' \
     -d '{"reference":"INN-2025-041"}'
```

Cada workflow lleva un `id` estable, así que reimportarlo lo actualiza en el
sitio en lugar de dejar copias. La activación no está incorporada a los ficheros
a propósito: un workflow que llega escuchando o ejecutando su horario es una
sorpresa, no una funcionalidad. El resumen diario se ejecuta manualmente desde
el editor durante la demostración y sólo se activa cuando su horario haya sido
aceptado por quien opera el proceso.

## Verificación

La suite de Python valida que los identificadores de nodo sean únicos y
estables, que no haya credenciales ni rutas locales incrustadas, y que los
trabajos fallidos salgan del bucle de sondeo. El job de contenedor de CI importa
el workflow en la imagen fijada de n8n y lo ejecuta contra un stack local recién
sembrado. Sólo la ejecución de CI correspondiente vale como prueba de
comportamiento en tiempo de ejecución; los recuentos de incidencias pertenecen a
la salida generada por la evaluación, no a este documento.

## Límites

- La espera previa al sondeo del primer workflow es de cuatro segundos fijos y el bucle no tiene
  techo de intentos. En un despliegue real eso se convierte en un reintento
  acotado con antigüedad máxima, y el endpoint de trabajos gana un contrato de
  "abandonar después de".
- El resumen y la entrega terminan en nodos simulados. Añadir correo, Teams, ERP
  o gestor documental exige credenciales, autorización y un contrato real.
- n8n guarda su propio estado en un volumen SQLite. Un despliegue real lo
  apuntaría a PostgreSQL y lo pondría detrás de autenticación.
- Las pruebas estructurales no sustituyen a una importación y ejecución reales.
  CI realiza esa comprobación de humo en tiempo de ejecución contra la imagen
  fijada.
