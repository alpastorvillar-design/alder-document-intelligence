**Español** · [English](../workflow.md)

# Flujo y máquina de estados

## Estados

```
        ┌────────┐
        │ DRAFT  │  creado, aún sin documentos
        └───┬────┘
            │ primer documento aceptado
            ▼
    ┌───────────────┐  pueden seguir llegando documentos
    │   INGESTED    │◀─┐
    └───────┬───────┘  │
            │ process  │
            ▼          │
      ┌──────────┐     │
      │  QUEUED  │     │
      └────┬─────┘     │
           │ el worker lo reclama
           ▼           │
    ┌──────────────┐   │
    │  PROCESSING  │   │
    └──────┬───────┘   │
           │ siempre   │
           ▼           │
  ┌──────────────────┐ │  una corrección vuelve a validar
  │   NEEDS_REVIEW   │─┘
  └───┬───────────┬──┘
      │ humano    │ humano
      ▼           ▼
 ┌──────────┐ ┌──────────┐
 │ APPROVED │ │ REJECTED │──▶ INGESTED (reenviado con documentos nuevos)
 └──────────┘ └──────────┘
   terminal

  FAILED es alcanzable desde cualquier estado de trabajo, y se puede reencolar.
```

Declarada una sola vez en
[`src/iep/domain/states.py`](../../src/iep/domain/states.py) y comprobada en
`tests/unit/test_domain.py`.

## La transición que falta a propósito

`PROCESSING` no puede llegar a `APPROVED`. Un run correcto termina en
`NEEDS_REVIEW` haya encontrado algo o no, así que aprobar un expediente es
siempre una acción humana con un motivo registrado. El test que lo fija es
`test_processing_cannot_approve`.

`APPROVED` no tiene transiciones de salida. Una justificación aprobada es lo que
se presentó; cambiarla después convertiría el registro de auditoría en un relato
en lugar de un registro. Corregir un expediente aprobado significa un expediente
nuevo.

La aprobación se rechaza mientras haya una incidencia bloqueante abierta. Un
bloqueante hay que descartarlo antes con un motivo, y el descarte queda
registrado — una comprobación automática que se puede saltar en silencio no es
una comprobación.

## Cómo se aplica una transición

Dos guardas, y hacen falta las dos:

1. la máquina de estados rechaza un movimiento que no declara, antes de escribir
   nada;
2. la escritura es un `UPDATE ... WHERE id = ? AND status = ?` condicional con
   `RETURNING`. Si otro escritor concurrente ya movió la fila, no vuelve nada, y
   al llamante se le dice que el expediente cambió por debajo en lugar de
   sobrescribir al ganador.

`test_two_writers_cannot_both_win_a_transition` lo ejerce con dos sesiones.

## Ciclo de vida de un trabajo

```
PENDING ──reclama──▶ RUNNING ──▶ SUCCEEDED
   ▲                    │
   │                    ├─ fallo recuperable, quedan intentos  ─▶ PENDING (backoff)
   │                    ├─ fallo recuperable, intentos agotados ─▶ DEAD_LETTER
   │                    └─ fallo no recuperable ────────────────▶ FAILED
   │
   └── lease expirado: lo recupera cualquier worker
```

Un documento mal formado no se vuelve válido en un segundo intento, así que no
se reintenta; un parpadeo de base de datos o un conector con timeout sí.
`DEAD_LETTER` y `FAILED` son estados inspeccionables con el error adjunto, no un
descarte silencioso.

## Recuperación

Un worker que muere a mitad de un trabajo deja su fila en `RUNNING` con un lease
que nunca renovará. Cualquier worker barre los leases expirados de vuelta a
`PENDING` y registra un evento de auditoría `JOB_RECLAIMED`. Reiniciar el
trabajo es seguro porque el pipeline es idempotente: las extracciones hacen
upsert sobre una clave derivada de documento, campo y localizador; los segmentos
se reconstruyen por documento; las incidencias hacen upsert sobre su
`fingerprint`; y una corrección humana nunca la sobrescribe una nueva ejecución.

`tests/e2e/test_worker_and_migrations.py` cubre la recuperación, el evento de
auditoría y un run completo del worker.
