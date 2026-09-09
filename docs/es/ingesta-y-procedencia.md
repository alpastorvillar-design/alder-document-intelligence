**Español** · [English](../ingestion-and-provenance.md)

# Ingesta y procedencia

## Frontera de confianza

Toda subida es no confiable. El tipo de medio y el nombre de fichero que declara
el cliente se conservan como procedencia, pero nunca eligen un parser ni una
ruta de almacenamiento. La secuencia de entrada es:

1. limitar los bytes que se leen de la petición;
2. identificar la firma;
3. aplicar límites estructurales y de expansión propios del formato;
4. abrir el contenido con su parser real;
5. calcular el SHA-256 de los bytes aceptados;
6. almacenar por digest de contenido;
7. escribir las filas de documento y auditoría en la transacción de base de
   datos.

Los PDF los abre PyMuPDF, las imágenes las verifica Pillow con un techo de
píxeles, y los `.xlsx` se inspeccionan como contenedores ZIP acotados antes de
que openpyxl los lea. Los libros con macros se rechazan. Las fórmulas se
registran como aviso y sus valores en caché no son evidencia. Una entrega
rechazada sigue siendo visible como metadatos, pero sus bytes no se guardan.

## Caminos de evidencia

El texto nativo del PDF y el OCR sobre ráster son caminos separados. Una capa de
texto utilizable produce localizadores de página y rango de caracteres. Un
escaneo se rasteriza a los DPI configurados y se envía a Tesseract con un timeout
duro; las palabras aceptadas conservan página, caja delimitadora y confianza del
motor. Las palabras de baja confianza derivan el valor a revisión.

Los valores de Excel conservan la hoja y la celda A1. Los valores del registro
conservan endpoint, id de registro, ruta JSON y versión de contrato. Los valores
de HTML local conservan la URL, el selector CSS, el momento de captura y el
fragmento de origen. Los agregados conservan los ids de las extracciones de
entrada y el nombre de la regla. La respuesta externa o la página también se
guardan como documento direccionado por contenido.

El lector de páginas se demuestra **únicamente** contra el fixture local
sintético. Usarlo en otro sitio exige un responsable identificado, una lista de
permitidos explícita, revisión de robots.txt y de las condiciones del sitio, una
finalidad lícita, minimización de datos, una tasa de petición documentada y un
contrato de cambios. Las redirecciones están desactivadas, las peticiones van
espaciadas, y un selector obligatorio que falte produce un error visible en
lugar de un dato silencioso.

## Aislamiento y repetición

La unicidad de documento es `(dossier_id, content_sha256)`: bytes duplicados
dentro de un expediente son un solo documento, mientras que dos expedientes
siguen aislados entre sí. Los nombres visibles pueden colisionar sin convertirse
en rutas. Las extracciones hacen upsert por una clave de deduplicación estable,
los segmentos por documento y ordinal, y las incidencias por huella de regla. Los
valores confirmados o corregidos por una persona quedan protegidos frente a
sobrescritura automática.

El corpus sintético escribe la verdad de referencia junto a las entradas
generadas, fuera del árbol del repositorio. Sus PDF y libros de Excel están
normalizados para que dos construcciones independientes sean idénticas byte a
byte; la batería unitaria lo comprueba en lugar de confiar en la afirmación.
