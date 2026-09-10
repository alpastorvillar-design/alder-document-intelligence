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

## Recuperación y generación fundamentada

El pipeline sigue siendo primero extracción. Los fragmentos permiten búsqueda
léxica, distancia coseno exacta con pgvector y fusión híbrida por posiciones. El
proveedor offline por hashing no se presenta como modelo semántico. Un modelo de
embeddings alojado requiere clave y `IEP_ALLOW_EXTERNAL_AI=true`; al reprocesar,
el hash de configuración evita mezclar espacios vectoriales antiguos y nuevos.

`POST /dossiers/{id}/questions` es la única frontera RAG. Está desactivada por
defecto, es de solo lectura, limita top-k y contexto y no entrega herramientas.
El texto recuperado se codifica como JSON no confiable. El prompt versionado pide
abstención cuando la evidencia no basta y la aplicación rechaza ids de cita no
recuperados después de validar el esquema estricto. Esto reduce prompt injection
y alucinaciones; no demuestra que una respuesta sea cierta. Siguen haciendo
falta evaluación representativa y revisión humana de las citas.
