**Español** · [English](../threat-model.md)

# Modelo de amenazas

## Alcance y activos

La implementación de referencia protege los bytes de los documentos, los datos
de negocio extraídos, las decisiones humanas, los informes, las credenciales de
los conectores y la integridad del registro de auditoría. Sus actores son un
cliente de la API, quien revisa, quien opera, el worker, el simulador local de
registro y página, y un proveedor semántico opcional.

La frontera de la demostración es **un operador local de confianza y datos
sintéticos**. Esto no es un diseño de autorización ni multi-cliente.

## Amenazas principales y controles presentes

| Amenaza | Control presente | Trabajo de producción pendiente |
| --- | --- | --- |
| Subida desmedida o disfrazada | techo de petición en streaming, sniffing de firma, apertura con parser, límites de ZIP y de píxeles de imagen | análisis de malware y conversión en sandbox |
| Path traversal o colisión de nombres | los nombres de fichero son sólo para mostrar; las claves de almacenamiento son digests SHA-256 validados | servicio de objetos endurecido y cuotas |
| Abuso del parser de PDF, imagen o Excel | formatos acotados, rechazo de PDF cifrados o corruptos, rechazo de macros, timeout de OCR | workers aislados, límites del SO, proceso de parcheo de parsers |
| Ejecución de fórmulas de hoja de cálculo | las fórmulas no se usan como evidencia; las celdas CSV se neutralizan | política de visor seguro aguas abajo |
| Falsificación de petición desde el servidor (SSRF) | lista de permitidos de esquema, puerto y host más comprobación de la dirección resuelta | cortafuegos de salida, fijación de DNS o proxy |
| Deriva de la estructura HTML | los selectores obligatorios fallan de forma visible; la captura cruda se hashea | propiedad del contrato monitorizada y alertas de cambio |
| Inyección de prompt o campos inventados | delimitadores de dato no confiable, esquema tipado, fundamentación, reglas deterministas, aprobación humana | gobernanza del proveedor, redacción, evaluación adversarial |
| Acciones duplicadas o concurrentes | claves de idempotencia con ámbito, unicidad en base de datos, transiciones condicionales, bloqueos de fila, leases y vallado | pruebas de carga distribuida y SLO |
| Mezcla de datos entre expedientes | cada consulta y cada frontera de unicidad llevan el id de expediente; pruebas dedicadas | políticas de base de datos a nivel de cliente |
| Fuga de secretos o datos personales | sin secretos en el repositorio, errores estructurados, puertos sólo locales, corpus sintético, escaneo del historial | gestor de secretos, redacción/DLP, registro de accesos |
| Manipulación de la auditoría | flujo de aplicación append-only y escrituras transaccionales | destino de auditoría externo e inmutable con bloqueos de retención |

## Privacidad y retención

Los expedientes reales pueden contener datos personales de empleados y
proveedores. Un despliegue necesita base jurídica documentada, limitación de
finalidad, minimización de datos, acceso por roles, contratos de encargado,
plazos de retención, flujos de borrado y retención legal, atención a los derechos
de las personas interesadas, respuesta ante brechas y evaluación de
transferencias. La demostración no implementa ninguno de esos controles
organizativos y usa únicamente identidades sintéticas.

Los ficheros de objetos y de informes son volúmenes locales, y los ficheros
temporales del corpus son artefactos locales de ejecución. Quien opera puede
borrar los volúmenes del proyecto, pero no hay una API de borrado selectivo. La
retención de auditoría frente a las obligaciones de supresión exige una decisión
de política antes de admitir datos reales.

## Frontera de autenticación

La clave de API compartida opcional es una guarda de demostración, no identidad
de producción. No hay usuarios, organizaciones, roles, sesiones ni reclamaciones
de cliente. Producción exige identidades autenticadas de carga de trabajo y de
quien revisa, mínimo privilegio, autorización en cada expediente, rotación de
claves, límites de tasa y TLS.
