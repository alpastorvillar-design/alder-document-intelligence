**Español** · [English](../ai-safety.md)

# Seguridad de la IA

## Camino por defecto

El proveedor semántico por defecto es determinista y no hace ninguna llamada de
red. Es un proveedor de pruebas y demostración con forma de parser, no evidencia
de que se haya ejecutado un modelo de lenguaje alojado. El núcleo completo
—incluidos OCR, validación, revisión, informes y repetición— funciona sin modelo
alojado, sin motor de workflows y sin acceso a Internet.

## Adaptador alojado opcional

El adaptador opcional está aislado detrás de `SemanticExtractor`. Está
desactivado salvo que se seleccione explícitamente y se niega a arrancar sin
clave. El identificador de modelo, el timeout, el techo de intentos, el techo de
tokens, la versión del prompt y los parámetros de precio son configuración. Las
pruebas usan un doble en proceso del protocolo; el repositorio no contiene
ninguna credencial y la batería de pruebas no hace ninguna petición de pago.

El adaptador pide un esquema tipado y vuelve a validar la respuesta localmente.
JSON inválido, tipos erróneos, campos desconocidos y valores sin fundamento
fallan en cerrado o se descartan con un aviso. Un valor propuesto tiene que
haberse pedido, tiene que aparecer en la evidencia citada, y la cita tiene que
aparecer en el documento de origen.

El texto del documento va delimitado y etiquetado como dato no confiable. No
puede seleccionar una herramienta, ejecutar una acción, cambiar reglas de
validación ni aprobar un expediente. Importes, fechas, detección de duplicados,
comprobaciones entre fuentes, transiciones de estado y aprobación siguen siendo
deterministas.

## Coste y afirmaciones

El uso y el coste son estimaciones derivadas de los recuentos de tokens
registrados y de los precios unitarios publicados que se configuren. Están
etiquetados como estimaciones, no como coste incurrido. Un despliegue real
añadiría un proveedor aprobado, condiciones de tratamiento de datos, controles
regionales y de retención, redacción, presupuestos por cliente, telemetría de
producción y una evaluación representativa antes de habilitar este adaptador.

Este pipeline es primero extracción. Usa búsqueda de texto completo de
PostgreSQL para encontrar evidencia que ya está en un expediente; no afirma hacer
generación aumentada por recuperación. Un índice vectorial se justificaría sólo
después de que la recuperación léxica falle en consultas medidas donde la
similitud semántica importe.
