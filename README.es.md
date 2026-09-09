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
[docs/production-gap.md](docs/production-gap.md) está lo que habría que cambiar
antes de ejecutarlo contra expedientes reales.

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
página; están en [resultados medidos](docs/measured-results.md).

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
[`automation/n8n/README.md`](automation/n8n/README.md).

## Documentación

La documentación técnica está en inglés, que es la convención del repositorio.

- [Arquitectura](docs/architecture.md), [modelo de dominio](docs/domain-model.md)
  y [flujo de trabajo](docs/workflow.md)
- [Ingesta y procedencia](docs/ingestion-and-provenance.md),
  [validación](docs/validation-strategy.md) y [seguridad de IA](docs/ai-safety.md)
- [Modelo de amenazas](docs/threat-model.md), [operación](docs/operations.md) y
  [brecha hasta producción](docs/production-gap.md)
- [Mediciones](docs/measured-results.md), [impacto de negocio](docs/business-impact.md),
  [limitaciones](docs/limitations.md) y [guion de demostración](docs/demo.md)

## Licencia

MIT. Véase [LICENSE](LICENSE).
