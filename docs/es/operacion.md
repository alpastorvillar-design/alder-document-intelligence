**Español** · [English](../operations.md)

# Operación

## Topología local

Compose arranca PostgreSQL, un simulador local de fuentes, la API y el worker.
La API y el worker reutilizan `innovation-evidence-pipeline:local`; el simulador
es la misma imagen del proyecto con otro comando. El perfil opcional `n8n` añade
una imagen de terceros. Los volúmenes con nombre y el nombre de proyecto de
Compose aíslan el estado.

Sólo se publican puertos de loopback. La imagen de aplicación corre como uid
10001 y sólo tiene escritura en `/var/lib/iep` y en su home. Las comprobaciones
de salud cubren PostgreSQL, la disponibilidad de la API, la del simulador y que
el proceso del worker esté vivo.

## Arrancar, inspeccionar y parar

```bash
docker compose config --quiet
docker compose up -d --build --wait postgres devsources api worker
docker compose ps
docker compose logs --tail=100 api worker
docker compose --profile n8n down
```

Añade `--volumes` al último comando **sólo** cuando quieras borrar los datos
locales de la demostración. No uses nunca un comando global de limpieza de
Docker para este proyecto.

El proceso de la API ejecuta `alembic upgrade head` antes de arrancar. La
comprobación de disponibilidad mira tanto la conectividad con la base de datos
como la cabeza de migración. Además, una puerta de migración construye y destruye
el esquema en un esquema PostgreSQL aislado.

## Fallo y recuperación

Los workers reclaman trabajos con un lease y lo renuevan mientras procesan. Un
worker parado deja una fila `RUNNING` recuperable; otro worker devuelve el
trabajo expirado a `PENDING`, salvo que ya hubiera alcanzado su techo de
intentos. Las actualizaciones de finalización y de fallo son condicionales al
titular actual, así que un worker que llega tarde no puede sobrescribir el
resultado de su sustituto.

Los errores recuperables de conector, OCR, semántica, sistema operativo y
adyacentes a la base de datos usan intentos acotados y espera exponencial. La
entrada inválida falla sin reintentos inútiles. Los trabajos conservan un mensaje
de error truncado y su correlation id; los fallos públicos de la API contienen
ese mismo correlation id y nunca una traza.

## Observabilidad

Los logs son JSON estructurado con contexto de correlación, expediente, trabajo y
worker. Los contadores en proceso y las observaciones de duración sostienen la
demostración; un despliegue necesita métricas exportadas, redacción central de
logs, trazas, cuadros de mando, alertas y SLO explícitos. No se registran cuerpos
de documento, tokens portadores ni claves de API.
