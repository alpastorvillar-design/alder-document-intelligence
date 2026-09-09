**Español** · [English](../architecture.md)

# Arquitectura

## Forma

Un monolito modular. Una base de código, una imagen de contenedor, dos procesos:

```
                         ┌──────────────┐
   cliente HTTP ────────▶│     api      │──┐
   (portal, n8n, curl)   │  (FastAPI)   │  │
                         └──────────────┘  │
                                           │   misma imagen,
                         ┌──────────────┐  │   mismos extractores,
                         │    worker    │◀─┘   distinto comando
                         │ (bucle poll) │
                         └──────┬───────┘
                                │
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
 ┌─────────────┐        ┌──────────────┐        ┌──────────────┐
 │ PostgreSQL  │        │  almacén de  │        │   fuentes    │
 │ estado,cola │        │   objetos    │        │   externas   │
 │ auditoría,  │        │ direccionado │        │ API registro │
 │ búsqueda    │        │ por contenido│        │ página web   │
 └─────────────┘        └──────────────┘        └──────────────┘
```

La API acepta trabajo y responde preguntas. El worker hace lo lento: abrir PDFs,
invocar Tesseract, leer libros de Excel, llamar al registro. Comparten imagen
para que una versión de extractor sea idéntica en ambos, que es lo que hace que
una cifra de precisión medida signifique algo.

### Por qué un monolito

Las piezas de este sistema cambian juntas. Un campo nuevo implica un extractor
nuevo, un contrato nuevo, una regla nueva y una columna nueva en el informe;
repartir eso entre servicios convertiría un commit en cuatro despliegues y una
ventana de compatibilidad, y no compraría nada. El argumento para separar sería
el escalado independiente, y el único componente que lo necesitaría es el
worker — que ya es un proceso aparte que se puede lanzar N veces contra la misma
cola.

## Módulos

| Módulo | Responsabilidad | Depende de |
| --- | --- | --- |
| `iep.domain` | contratos, enums, máquina de estados | nada |
| `iep.db` | tablas, sesión, ida y vuelta de enums | domain |
| `iep.storage` | interfaz de almacén de objetos y backend local | nada |
| `iep.ingestion` | comprobaciones de tamaño, firma y parser; procedencia | domain, db, storage |
| `iep.extraction` | texto de PDF, OCR, libros de Excel, parsing, lectores de campo | domain |
| `iep.semantic` | protocolo de proveedor, proveedor determinista, adaptador alojado | domain |
| `iep.connectors` | cliente del registro, captura controlada de páginas | domain |
| `iep.validation` | catálogo de reglas y persistencia de incidencias | domain, db, connectors |
| `iep.pipeline` | la orquestación por expediente | todo lo anterior |
| `iep.review` | decisiones humanas | domain, db |
| `iep.reporting` | informe HTML, exportación JSON y CSV | domain, db |
| `iep.retrieval` | búsqueda léxica de evidencia | db |
| `iep.worker` | cola y bucle del worker | pipeline |
| `iep.api` | superficie HTTP, errores, idempotencia, vista mínima de revisión | todo |

Las dependencias van en una sola dirección. `iep.domain` no importa nada del
proyecto, así que un cambio de contrato se ve en todos los sitios donde importa
y en ninguno donde no.

## El camino de un expediente

1. **Crear.** `POST /dossiers` con referencia, periodo y total reclamado.
2. **Ingerir.** Cada subida se comprueba en tamaño, en firma y abriéndola con su
   parser antes de guardar nada. Los bytes aceptados van al almacén de objetos
   bajo su SHA-256; la fila registra tamaño, tipo, hash, origen y número de
   páginas. Un fichero rechazado también se registra, con su motivo, sin guardar
   sus bytes.
3. **Encolar.** `POST /dossiers/{id}/process` inserta un trabajo cuya clave de
   idempotencia es por defecto el expediente más el conjunto de digests de sus
   documentos, así que pulsar el botón dos veces sin cambiar nada no hace nada.
4. **Reclamar.** Un worker coge el trabajo con `SELECT ... FOR UPDATE SKIP
   LOCKED` y un lease.
5. **Capturar.** Se lee el registro de personal por HTTP y se captura la página
   de la convocatoria publicada. Ambos se guardan como documentos con sus hashes,
   de modo que una cifra que viene de un sistema corporativo es tan trazable como
   una leída en una página.
6. **Leer.** Por documento: texto nativo del PDF si hay capa de texto utilizable,
   si no OCR; libros de Excel por openpyxl. Cada valor sale con un localizador —
   página y rango de caracteres, hoja y celda, o caja delimitadora y confianza de
   palabra.
7. **Clasificar.** El proveedor semántico dice qué es el documento. Sólo se usa
   la clasificación; las propuestas de campo del proveedor no se persisten como
   extracciones.
8. **Agregar.** Las sumas se calculan desde extracciones guardadas y se registran
   con un localizador derivado que nombra la regla y las entradas.
9. **Validar.** Reglas deterministas comparan la memoria, el Excel, los
   justificantes, el registro y la convocatoria entre sí.
10. **Revisar.** El expediente pasa a `NEEDS_REVIEW` — siempre, se haya
    encontrado algo o no. Una persona corrige, confirma, acepta, descarta,
    aprueba o rechaza, cada acción con motivo y registrada.
11. **Informar.** HTML para una persona, JSON y CSV para un sistema, ambos
    mostrando la evidencia y cualquier corrección junto a la lectura original.

## Decisiones que merece la pena discutir

Registradas como ADR en [`adr/`](adr/):

- [0001](adr/0001-localizadores-de-evidencia.md) — cada valor lleva de dónde salió
- [0002](adr/0002-cola-en-postgres.md) — la cola es una tabla, no un broker
- [0003](adr/0003-reglas-deterministas-no-un-modelo.md) — el modelo no hace aritmética
- [0004](adr/0004-recuperacion-lexica-no-rag.md) — búsqueda léxica, y qué lo cambiaría
- [0005](adr/0005-sin-agente-en-la-aprobacion.md) — ningún agente entre un documento y una aprobación

## Lo que no hay

Ni broker de mensajes, ni base de datos vectorial, ni motor de orquestación en
el camino crítico, ni microservicios, ni nube. Cada una de esas piezas se
consideró y se descartó en los ADR o en [limitaciones.md](limitaciones.md), y
añadir una sin una necesidad medida haría el sistema más difícil de explicar sin
ganar nada.
