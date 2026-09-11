**Español** · [English](README.md)

# Innovation Evidence Pipeline

Revisar una justificación de ayudas a la innovación es un problema documental
antes que un problema de datos. Un expediente llega como una memoria técnica en
PDF, un montón de justificantes de gasto escaneados, un libro de partes
horarios, un registro en un sistema corporativo y una convocatoria publicada en
una página web. Alguien tiene que decidir si el gasto declarado está realmente
soportado y, si la justificación se cuestiona más adelante, mostrar de dónde
salió cada cifra.

Este repositorio es una **implementación de referencia orientada a producción**
de ese paso de revisión: ingesta documentación heterogénea, extrae campos
conservando un localizador a la página, celda o caja exacta de la que
proceden, cruza las fuentes entre sí con reglas deterministas, envía a una
persona lo que no puede resolver y produce un informe auditable.

Es una implementación de referencia, no un sistema desplegado. En
[docs/es/brecha-produccion.md](docs/es/brecha-produccion.md) está lo que habría
que cambiar antes de ejecutarlo contra expedientes reales.

## La decisión de diseño que importa

Un modelo de lenguaje es genuinamente útil aquí: clasificar documentos, sacar el
título de un proyecto de un texto en prosa, detectar que dos apartados se
contradicen. También es la herramienta equivocada para decidir si 184.320 € de
coste de personal declarado cuadran con el parte horario.

Por eso el pipeline reparte el trabajo:

| Responsabilidad | Quién la asume |
| --- | --- |
| Localizar texto, celdas y palabras en un escaneo | Extractores deterministas (PyMuPDF, openpyxl, Tesseract) |
| Interpretar prosa, clasificar, proponer campos candidatos | Un proveedor semántico intercambiable |
| Encontrar fragmentos de soporte | Recuperación léxica, pgvector exacta o híbrida |
| Redactar desde fragmentos recuperados | Proveedor RAG opcional de solo lectura con ids de cita verificados |
| Aritmética, elegibilidad, duplicados, cruce entre fuentes | Reglas deterministas y versionadas |
| Cualquier cosa ambigua, de baja confianza o contradictoria | Una persona revisora, con la evidencia delante |
| Aprobar o rechazar | Una persona, registrado en una auditoría append-only |

Cada campo extraído lleva su documento de origen, su localizador, el extractor
que lo produjo, la versión de ese extractor, la versión del contrato, una
confianza y cualquier corrección humana. Nada dentro del pipeline puede aprobar
un expediente.

## Estado

La vertical está implementada y ejercitada por pruebas unitarias, de integración
contra PostgreSQL, de migración, de recuperación y de humo en contenedor. Las
mediciones publicadas salen del arnés de evaluación en lugar de copiarse a esta
página; están en [resultados medidos](docs/es/resultados-medidos.md).

## Puesta en marcha

Los requisitos son Docker Engine con Compose v2 y espacio libre suficiente para
las imágenes base fijadas. No hace falta ningún servicio externo ni credencial
de modelo.

```bash
cp .env.example .env
docker compose up -d --build --wait postgres devsources api worker
docker compose exec -T api python -m corpus.generate --out /tmp/corpus
docker compose exec -T api iep seed --corpus /tmp/corpus \
  --call-page-url http://devsources:8080/public/convocatoria.html
docker compose exec -T api iep process --reference INN-2025-042
docker compose exec -T api iep report --reference INN-2025-042
```

En Windows, [`scripts/demo.ps1`](scripts/demo.ps1) ejecuta esos pasos para el
expediente consistente y para el que lleva defectos sembrados a propósito; añade
`-Fresh` para borrarlos antes, porque un expediente en revisión rechaza
documentos nuevos por diseño. Para parar únicamente este stack:
`docker compose --profile n8n down`; añade `--volumes` cuando sus datos locales
ya no hagan falta.

La API, la pantalla de revisión y el simulador local de fuentes escuchan sólo en
loopback: `http://127.0.0.1:8000/docs`, `http://127.0.0.1:8000/ui/dossiers` y
`http://127.0.0.1:8080`. La interfaz opcional de workflows se describe en
[`automation/n8n/README.es.md`](automation/n8n/README.es.md).

## La pantalla de revisión

Lo que produce el pipeline es una decisión que alguien tiene que tomar y
defender, así que tiene pantalla y no sólo API. Cinco, en el orden en que un
expediente las recorre: la bandeja, el alta, el progreso, la revisión y la
evidencia detrás de un valor. Jinja renderizado en servidor contra la misma
API JSON que llamaría una integración: sin paso de compilación, sin una segunda
implementación de las reglas y sin nada que se cargue de la red, así que
funciona sin conexión a Internet.

La interfaz está en español. El dominio, los documentos y las personas que la
usarían lo son; las rutas, los `field_path` y los identificadores de regla
siguen en inglés porque son claves, no prosa.

**La bandeja** — los expedientes que esperan una decisión, con lo que los
bloquea.

![La bandeja de revisión](docs/img/01-queue.png)

**El alta** — se arrastran los ficheros del expediente. Cada uno se comprueba
por tamaño, por su firma real y abriéndolo con su parser *antes* de guardarse,
y un rechazo dice qué fichero es y por qué. Nada que no se pueda abrir llega al
almacén, pero el rechazo queda registrado, así que se ve qué falta en lugar de
tener que adivinarlo.

![Alta de un expediente y subida de sus documentos](docs/img/02-intake.png)

**La revisión** — primero el veredicto, y después cada incidencia como una
ficha: cómo se llama en lenguaje llano, las cifras que la provocan, los
documentos a los que afecta, por qué salta la regla y el requisito que aplica.
Eso último es lo que convierte una incidencia en algo discutible en lugar de
una opinión. La aprobación se niega mientras haya un bloqueante abierto o un
campo sin confirmar; descartar una incidencia como falso positivo exige motivo
y queda registrado.

![La pantalla de revisión de un expediente con once incidencias bloqueantes](docs/img/03-review.png)

**La evidencia** — para cualquier valor, el documento del que se leyó con el
sitio exacto recuadrado. El recuadro se dibuja con las coordenadas que se
guardaron durante la extracción, no recalculadas al mostrarlo. Desde aquí se
abre el original: un PDF como PDF, un escaneo como imagen, un libro como
descarga que abre Excel.

![Un justificante escaneado con el total recuadrado donde se leyó](docs/img/04-evidence.png)

**El informe** — el documento que sale de la casa, y lo único de aquí escrito
para alguien que no estaba delante. Empieza por la comprobación sobre la que se
sostiene la justificación: para cada concepto de gasto, lo que declara la
memoria frente a lo que suman los documentos que la soportan, la diferencia y
si cuadra. Una cifra que no se llegó a leer sigue faltando en lugar de
convertirse en un cero. «Descargar PDF» se lo pide al servidor:
`reports/latest.pdf` renderiza el HTML almacenado con Chromium, dentro de la
imagen, así que el documento que se archiva es el mismo para todo el mundo.
Sustituyó al diálogo del navegador después de que un PDF hecho por esa vía
llegara como 26 mapas de bits, sin fuentes incrustadas y sin texto
seleccionable —«imprimir como imagen», que deja un documento archivado sin
poder buscarse—. El renderizado son 0,6 MB con diez fuentes incrustadas y
11.484 operadores de texto. La hoja de estilos de impresión sigue mandando en
la maquetación, y «Imprimir» sigue abriendo el diálogo para papel.

![El informe de justificación, con la conciliación de importes primero](docs/img/05-report.png)

**Preguntar a las evidencias** — la pantalla de revisión y la de evidencia
llevan el mismo copiloto de sólo lectura, en un cajón que se abre al lado del
expediente y no encima, porque la tabla es justo lo que quien revisa necesita
seguir leyendo mientras pregunta. Recupera los fragmentos más cercanos a la
pregunta y pide a un modelo que redacte una respuesta *citándolos*; cada cita
se comprueba contra lo que realmente se envió, y un identificador que el modelo
no recibió tumba la respuesta entera en lugar de aparecer como nota al pie. No
puede aprobar, rechazar ni cambiar un campo, y preguntar queda en la auditoría.

El modelo se elige en la pantalla, entre los que esta máquina alcanza de
verdad: los locales que descubre en Ollama, con el tamaño de sus pesos a la
vista porque es lo que decide si una respuesta tarda segundos o minutos, y el
CLI de `claude` o `codex` cuando la API corre en el host. Un identificador de
modelo que llega de un cliente se resuelve contra ese catálogo en vez de
confiar en él, porque en los backends de CLI acabaría en `argv`. El medidor
informa de los tokens gastados hoy y separa las llamadas medidas de las
locales, que no cuestan nada y no consumen presupuesto.

![El buzón de preguntas, con el modelo y el presupuesto de llamadas](docs/img/06-ask.png)

La generación viene apagada. `IEP_RAG_PROVIDER=ollama` responde con un modelo
de esta máquina, sin clave y sin que nada salga de ella; `IEP_RAG_PROVIDER=cli`
responde con `claude` o `codex` en esta misma máquina —un proveedor sólo para
desarrollo, para poder mostrar el punto de integración sin clave de API— y
`IEP_RAG_PROVIDER=openai` es la vía alojada que usaría un despliegue. En
cualquier caso el copiloto dice qué interruptor falta cuando está apagado, y
cuántas llamadas ha gastado la aplicación frente al techo que se impone a sí
misma. Véase [recuperación
híbrida y RAG opcional](docs/es/rag.md).

## Documentación

Toda la documentación existe en español y en inglés, con un selector de idioma en
la primera línea de cada documento. El índice completo está en
[docs/es/README.md](docs/es/README.md).

¿Primera vez aquí? Empieza por el **[recorrido guiado](docs/es/recorrido.md)**:
qué se está ejecutando, qué hace cada endpoint, cómo lanzar la demostración, cómo
abrir n8n, y dónde encajan —y dónde no— el RAG y un modelo de lenguaje.

- [Recorrido guiado](docs/es/recorrido.md) y
  [demostración del LLM](docs/es/demostracion-llm.md), además de
  [recuperación híbrida y RAG opcional](docs/es/rag.md)
- [Arquitectura](docs/es/arquitectura.md),
  [modelo de dominio](docs/es/modelo-de-dominio.md) y
  [flujo de trabajo](docs/es/workflow.md)
- [Ingesta y procedencia](docs/es/ingesta-y-procedencia.md),
  [validación](docs/es/estrategia-de-validacion.md) y
  [seguridad de la IA](docs/es/seguridad-ia.md)
- [Modelo de amenazas](docs/es/modelo-de-amenazas.md),
  [operación](docs/es/operacion.md) y
  [brecha con producción](docs/es/brecha-produccion.md)
- [Mediciones](docs/es/resultados-medidos.md),
  [impacto de negocio](docs/es/impacto-negocio.md),
  [limitaciones](docs/es/limitaciones.md) y
  [guion de demostración](docs/es/demostracion.md)

## Licencia

MIT. Véase [LICENSE](LICENSE).
