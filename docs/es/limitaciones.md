**Español** · [English](../limitations.md)

# Limitaciones

- El corpus es pequeño, sintético, en español y deliberadamente regular. Prueba
  caminos y controles, no generalización al mundo real.
- El OCR usa un solo motor y un solo paquete de idioma. La diversidad de
  maquetación, la escritura a mano, las tablas de varias páginas, las rotaciones
  y las fotos malas de móvil necesitan un banco de pruebas más amplio.
- Los patrones de extracción apuntan a este contrato de fixture. No se afirma
  comprensión genérica de documentos.
- Existen recuperación léxica, vectorial exacta e híbrida, pero el proveedor
  determinista por hashing no es un modelo semántico aprendido. Todavía no se
  ha publicado un benchmark representativo de recuperación semántica.
- El adaptador RAG y sus controles de citas y salida de datos se prueban contra
  dobles. No se afirma haber evaluado calidad con un modelo alojado ni ejecutado
  una llamada de producción.
- El proveedor semántico determinista no es la ejecución de un modelo alojado. El
  adaptador alojado opcional sólo tiene pruebas de contrato locales.
- La clave de API es una guarda de demostración por secreto compartido; no hay
  modelo de autorización por usuario, rol, cliente ni expediente.
- El almacenamiento y los informes son ficheros locales. No están implementados
  el borrado selectivo, el cifrado, la copia de seguridad, la inmutabilidad, la
  retención ni la retención legal.
- Faltan el aislamiento de parsers, el análisis de malware, la imposición de
  salida de red en el host y los límites de recursos de producción.
- Las métricas se quedan en proceso; no hay backend de telemetría duradero ni
  alertado.
- Al workflow opcional le falta un techo duro de antigüedad total de sondeo y
  autenticación de producción.
- Los bloqueos de dependencias mejoran la reproducibilidad, pero no sustituyen a
  la gobernanza de vulnerabilidades, licencias, imágenes y procedencia.
- La demostración no establece ninguna afirmación de rendimiento, techo de
  concurrencia, disponibilidad, tiempo de recuperación ni coste.
