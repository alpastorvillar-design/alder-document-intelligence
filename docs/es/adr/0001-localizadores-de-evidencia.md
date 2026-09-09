**Español** · [English](../../adr/0001-evidence-locators.md)

# ADR 0001: El localizador de evidencia forma parte de cada valor

**Estado:** aceptado

## Contexto

Una cifra sin su origen no se puede revisar, ni corregir, ni defender. Guardar
sólo los documentos crudos y los campos finales convierte cada discusión en una
búsqueda manual.

## Decisión

Cada extracción lleva un localizador tipado apropiado a su fuente: rango de
página, caja de palabra de OCR, celda de Excel, campo de API, selector HTML o ids
de las entradas derivadas. El hash del documento, las versiones de extractor y de
contrato, la confianza y el historial de correcciones viajan con él.

## Consecuencias

Los extractores tienen un contrato más estricto y los informes son más
verbosos. A cambio, la revisión, las pruebas, la comparación de repeticiones y la
auditoría pueden **señalar la evidencia** en lugar de limitarse a afirmar un
valor.
