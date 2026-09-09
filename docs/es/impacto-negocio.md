**Español** · [English](../business-impact.md)

# Impacto de negocio

El valor de negocio no es «usar más IA». Es reducir el esfuerzo manual de
montar la evidencia haciendo a la vez que cada decisión sea más fácil de
defender.

## Calculadora de escenarios

Para un periodo, define:

- `D`: expedientes recibidos;
- `M`: minutos manuales de referencia por expediente;
- `A`: minutos asistidos por expediente, incluida la revisión;
- `R`: proporción que puede usar el flujo asistido;
- `H`: coste cargado por hora de quien revisa;
- `I`: coste de implantación y operación del periodo.

Entonces:

```text
horas liberadas = D × R × max(M - A, 0) / 60
valor bruto de capacidad = horas liberadas × H
retorno del escenario = valor bruto de capacidad - I
```

Son salidas de un escenario, **no afirmaciones de ahorro**. Las entradas tienen
que venir de una medición de referencia muestreada y de un piloto controlado.
Hay que seguir además la completitud a la primera, la tasa de revisión, los
falsos positivos, los falsos negativos, el éxito en la recuperación de
evidencia, el retrabajo, el tiempo de ciclo, la adopción y las escaladas. Un
sistema más rápido que se deja bloqueantes, o en el que quien revisa no confía,
tiene valor negativo.

## Diseño para la adopción

Quien revisa ve el sitio del documento original, el valor extraído, la regla que
levantó la incidencia y el historial de correcciones. La herramienta no esconde
la incertidumbre ni fuerza una decisión automática. Un piloto debería empezar en
modo sombra, comparar resultados con revisores con experiencia, clasificar las
discrepancias, ajustar umbrales y sólo entonces cambiar los procedimientos
operativos.

La propiedad del soporte importa tanto como el modelo: responsable de proceso
nombrado, responsable de datos, responsable técnico, ruta de triaje, formación,
notas de versión y revisión del feedback forman parte del diseño operativo.
