# Comparación histórica sin activar cambios de trading

`python -m src.research` solo lee archivos locales y produce una comparación. No importa el broker, no consulta APIs, no utiliza claves, no envía mensajes y no modifica el bot. Los resultados no autorizan activar ninguna variante.

## Variantes predefinidas

Todas conservan el swing RSI(2), hasta dos slots del 15%, el core del 70%, la banda de tres puntos porcentuales, ventas antes de compras, efectivo sin apalancamiento, fracciones de nueve decimales, exclusión de reentrada el mismo día y prohibición de vender un activo comprado ese día.

1. `weekly`: régimen y rebalanceo core únicamente en la última sesión semanal.
2. `daily_defensive`: además de lo anterior, si hay posiciones ofensivas y el régimen diario pasa a RISK-OFF, permite rotarlas a SGOV. No vuelve a entrar en ofensivos antes de la siguiente revisión semanal.
3. `trailing_core_5pct`: las reglas semanales más una salida a SGOV al retroceder 5% desde el máximo de un índice del bloque ofensivo core.
4. `trailing_core_10pct`: la misma comparación con 10%.

Los umbrales 5% y 10% son escenarios fijos para sensibilidad, no resultados de optimizar los cuatro snapshots de octubre. El índice del trailing se actualiza con el retorno de las posiciones ofensivas existentes antes de cada operación; los aportes/retiros del bloque no crean máximos artificiales. Se reinicia al iniciar una nueva etapa ofensiva después de no tener posiciones ofensivas. Es un trailing de la cartera ofensiva core, no un stop intradía ni un trailing de todo el equity. Las variantes trailing no añaden el filtro defensivo diario: se comparan como alternativas separadas.

## Archivos de entrada

Directorio con `manifest.json`, `daily.csv` y `calendar.csv`. Se exige el universo completo: SPY, QQQ, SMH, GLD, SGOV, AAPL, MSFT, NVDA, AMZN, META, GOOGL, AVGO, LLY, COST, JPM, BRK.B y V.

`daily.csv`:

```text
date,symbol,open,high,low,close,volume
```

Cada símbolo debe tener exactamente una barra por sesión de calendario, en orden ascendente y con precios ajustados por splits y dividendos (`adjustment=all`). No se rellenan días o símbolos faltantes ni se aceptan precios raw/split-only. Esto evita distorsiones por splits y el sesgo de omitir distribuciones de SGOV. Los valores ajustados se interpretan como unidades sintéticas de retorno total, no acciones históricas literales. La metodología de ajustes del proveedor sigue siendo una limitación.

`calendar.csv`:

```text
date,open,close
```

Las fechas son `YYYY-MM-DD`; `open` y `close` son ISO-8601 con zona horaria. Para certificar la última sesión de una semana incompleta debe incluirse el calendario de la siguiente sesión; un viernes terminal es inequívoco. El periodo inicial recomendado va desde la disponibilidad de SGOV en 2020; la simulación exige 250 sesiones previas completas antes de operar.

Manifest mínimo:

```json
{
  "schema_version": 1,
  "source": "Alpaca StockHistoricalDataClient",
  "feed": "sip",
  "adjustment": "all",
  "retrieved_at": "2026-11-01T15:00:00+00:00",
  "symbols": ["SPY", "QQQ", "SMH", "GLD", "SGOV", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "LLY", "COST", "JPM", "BRK.B", "V"],
  "price_basis": "total_return_adjusted",
  "execution_mode": "adjusted_close_proxy",
  "files": {"daily.csv": "SHA256_HEXADECIMAL", "calendar.csv": "SHA256_HEXADECIMAL"}
}
```

Los SHA-256 se verifican antes del cálculo. El informe conserva el manifest y su propio digest para identificar exactamente la muestra. Los hashes aseguran integridad, no certifican la calidad o veracidad económica del proveedor.

## Precios de ejecución y causalidad

En `adjusted_close_proxy`, cada fecha se evalúa usando indicadores calculados con las **250 barras anteriores**, excluyendo por completo el OHLC diario de esa misma fecha. El cierre ajustado del día se usa como precio actual para las reglas de swing/trailing y como precio hipotético de ejecución. Observar y ejecutar al mismo cierre es una aproximación optimista: no reproduce las 15:45 ni demuestra retornos realizables. Sus resultados se etiquetan exploratorios.

El modo opcional `window_marks` exige `execution.csv` con columnas `timestamp,symbol,price`, una marca por símbolo y sesión exactamente en `Calendar.close−15 minutos`. Su manifest debe añadir el hash del archivo y `execution_price_basis=total_return_adjusted`. Las marcas deben compartir la base de ajuste de las barras diarias; no se pueden mezclar barras ajustadas con cotizaciones raw. No hay sustitución por cierres cuando falta una marca. Incluso en este modo son precios de referencia: no se modelan libro de órdenes, bid/ask, latencia ni fills individuales. El equity de fin de sesión se valora al cierre ajustado.

## Costes y comparación

```sh
python -m src.research --input DATA_DIR --output NEW_OUTPUT_DIR --initial-equity 4000 --cost-bps 1 5 10
```

El destino debe ser nuevo; no se sobrescriben investigaciones anteriores. Los costes predeterminados son 1, 5 y 10 puntos básicos por lado, aplicados tanto a ventas como a compras. Se descuentan del efectivo y el tamaño de compra se limita al efectivo disponible incluyendo costes. No son estimaciones de costes observados: son escenarios de sensibilidad.

El 60% inicial de sesiones evaluables forma el tramo de desarrollo y el 40% final el tramo fuera de muestra. Se requieren al menos 60 sesiones por tramo. No se ajustan parámetros ni se elige un ganador automáticamente. Las posiciones se mantienen al cruzar la separación y los rendimientos OOS se calculan desde el equity inmediatamente anterior al tramo.

Los resultados incluyen:

- Retorno neto y anualizado, caída máxima diaria, equity, costes y número de operaciones.
- Rotación bruta en USD y dividida por equity medio; exposición ofensiva media.
- Diferencia de retorno frente a la base semanal para el mismo escenario de costes.
- Diferencia de P&L en los días negativos de la base, normalizando cada tramo a USD 4,000. Es una comparación emparejada, no una atribución causal de pérdidas evitadas.
- Salidas defensivas cuyo bloque vendido sube en las cinco sesiones posteriores y retorno ponderado de ese bloque. Las cinco sesiones deben estar dentro del mismo tramo para contabilizar el seguimiento. Una subida posterior mide coste de oportunidad; no prueba que la salida fuese incorrecta.

`comparison.json` contiene métricas de periodo completo, desarrollo y OOS. `REPORT.md` resume OOS. Un archivo por variante/coste guarda posiciones, equity, órdenes simuladas y salidas defensivas. `RESULTS.sha256.json` permite comprobar que no se alteraron esos resultados.

## Límites que impiden activar reglas automáticamente

El universo elegido hoy introduce sesgo de selección/supervivencia; no incluye activos desaparecidos ni una composición histórica reconstruida. Los datos ajustados pueden revisarse. Las distribuciones se incorporan mediante unidades de retorno total, no pagos de dividendos separados. No se simulan interrupciones, rechazos, fills parciales, mala cotización, filtros de spread, restricciones de liquidación o latencia. El retorno core puede conservar residuos menores al 3% por la banda heredada de la estrategia. La caída máxima es entre observaciones diarias, no intradía. Una muestra que favorezca una variante no demuestra rentabilidad futura ni elimina la necesidad de validación paper y conciliación operativa.
