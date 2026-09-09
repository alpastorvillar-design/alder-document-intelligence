**Español** · [English](../production-gap.md)

# Brecha con producción

Este repositorio es una implementación de referencia orientada a producción, no
un servicio desplegado. Demuestra el flujo de control de negocio y expone el
trabajo que aún falta, en lugar de esconderlo detrás de vocabulario
arquitectónico.

## Hay que cerrarlo antes de tocar datos reales

- Añadir identidad de organización y de usuario, autorización a nivel de
  expediente, TLS, límites de tasa, gestión de secretos y un destino de auditoría
  externo e inmutable.
- Establecer roles de privacidad, base jurídica, condiciones de encargado de
  tratamiento, restricciones regionales, retención, borrado selectivo, retención
  legal y respuesta a incidentes.
- Aislar los parsers no confiables en workers restringidos con límites de CPU,
  memoria, ficheros, procesos y red; añadir análisis de malware y política de
  imágenes parcheadas.
- Imponer la salida de red de los conectores fuera del código de aplicación y
  eliminar el riesgo de DNS entre el momento de comprobación y el de uso mediante
  un proxy controlado o una conexión fijada.
- Sustituir el almacén de objetos local por almacenamiento cifrado y versionado
  con publicación atómica de informes; añadir pruebas de copia, restauración y
  recuperación ante desastre.
- Definir SLO, objetivos de capacidad, alertado, runbooks, propiedad del soporte
  y una evaluación representativa de rendimiento y adversarial.

## Escala y disponibilidad

La API y el worker comparten una imagen pero son procesos separados. Los workers
pueden escalar horizontalmente porque las reclamaciones usan `SKIP LOCKED` de
PostgreSQL; los leases y el vallado por titular recuperan el trabajo abandonado.
Ningún banco de pruebas de aquí establece una concurrencia segura de producción,
una distribución de tamaños de documento, un SLO de latencia ni un techo de base
de datos.

La cola en tabla es adecuada mientras la consistencia transaccional y un volumen
de trabajos moderado importen más que el rendimiento de un broker. Un broker se
justifica si la contención medida, los requisitos de aislamiento o unas
semánticas de entrega independientes superan a PostgreSQL. Esa decisión necesita
evidencia de carga, no moda.

## Semántica alojada y orquestación

El adaptador semántico alojado sólo está probado por contrato contra un doble
local del protocolo. Habilitarlo exige una evaluación con datos reales,
aprobación de privacidad, presupuestos, redacción, observabilidad del proveedor y
política de conmutación por fallo. El modelo tiene que quedarse fuera de la
aritmética y de la aprobación.

El motor de workflows opcional usa un volumen SQLite local y no tiene login en la
demostración. En producción necesita su propia base de datos, autenticación,
retención de ejecuciones, sondeo acotado, enrutado de errores a un canal con
dueño e importación con control de cambios. Está deliberadamente fuera del camino
de procesamiento del núcleo.

## Seguimientos de ingeniería conocidos

- Vincular las decisiones de revisión a ids de actor autenticados e inmutables en
  lugar de a cadenas.
- Añadir concurrencia optimista a nivel de incidencia, no sólo revisiones a nivel
  de campo.
- Añadir vallado por token de titular y una vía de recuperación para el operador
  cuando una reserva de idempotencia de petición queda en vuelo por una caída de
  proceso.
- Hacer transaccional la publicación del fichero de informe con el estado de la
  base de datos.
- Añadir límites duros de antigüedad e intentos extremo a extremo en el workflow.
- Ejecutar puertas de vulnerabilidades de dependencias, licencias, imágenes y
  lista de materiales de software bajo una política de remediación acordada.
- Ejercitar cargas grandes en paralelo y terminación controlada de procesos en un
  runtime de contenedores parecido a producción.
