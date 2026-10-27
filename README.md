# TradingEOD

Motor EOD de Alpaca para una cuenta dedicada de aproximadamente USD 4,000. Lee el equity real en cada ejecución, asigna 70% a Dual Momentum semanal y hasta 30% a dos posiciones RSI(2). Las credenciales, el capital y las señales no están incorporados en el código.

**Estado de entrega, 23 de octubre de 2026:** implementación con pruebas automatizadas en Python 3.12, recuperación acotada de cotizaciones y omisiones por activo. El 22 de octubre el bot evaluó a tiempo, confirmó Telegram y no operó porque ninguno de los doce swings cumplía RSI(2)<10; el core no correspondía ese jueves. La nueva versión añade medición automática de sesiones paper. Siguen pendientes diez sesiones observadas, dos evaluaciones semanales y comprobar compras y posteriores ventas reales en paper. No hay backtest ni evidencia de rentabilidad incluida.

## Archivos

| Archivo | Función |
|---|---|
| `requirements.txt`, `constraints.txt` | Versiones fijas de dependencias directas y transitivas |
| `src/bot.py` | Orquestación, cartera, ventas primero, compras después |
| `src/strategy.py` | Momentum, SMA, RSI y ATR Wilder; funciones sin efectos externos |
| `src/broker.py` | Alpaca, historial de fills, fracciones y conciliación de órdenes |
| `src/data.py` | Barras ajustadas y validación de cotizaciones |
| `src/quotes.py`, `src/execution.py` | Consultas acotadas, omisiones por activo y barrera ventas/compras |
| `src/observation.py` | Evidencia posterior de la sesión y comparación histórica con SIP |
| `src/safety.py` | Calendario, ventanas, efectivo y validaciones |
| `src/scheduling.py` | Selección de cron y espera acotada hasta la ventana válida |
| `src/diagnostics.py` | Conectividad Alpaca paper y Telegram sin enviar órdenes |
| `src/market_diagnostics.py`, `src/signal_diagnostics.py` | Reconstrucción de cotizaciones y señales históricas sin operar |
| `src/state.py` | Registro persistente por cuenta, fecha de Nueva York y modo |
| `src/notifier.py` | Webhook JSON y Telegram opcionales |
| `.github/workflows/daily_trading.yml` | Ejecución programada y manual |
| `.github/workflows/test_telegram.yml` | Prueba manual de Telegram sin acceso a Alpaca |
| `.github/workflows/check_connection.yml` | Diagnóstico paper de solo lectura y entrega Telegram confirmada |
| `.github/workflows/check_market_data.yml` | Reconstrucción de datos EOD para una fecha seleccionada |
| `.github/workflows/observe_paper.yml` | Observación automática tras las ejecuciones paper |
| `docs/DATOS_FUTUROS.md` | Criterios para evaluar un proveedor externo más económico |
| `.github/workflows/ci.yml` | Pruebas sin credenciales ni acceso al broker |
| `tests/` | Pruebas de indicadores, ejecución, estado, calendario y notificaciones |
| `setup_and_push.sh` | Instalador autocontenido y publicación Git sin force-push |

## Despliegue en GitHub

Requisitos operativos: Git, Bash y Python 3.12 o superior. En Windows sirve **Git Bash**; PowerShell no interpreta scripts `.sh`. Git debe estar autenticado en GitHub con permiso de escritura en el repositorio y de actualización de workflows. La identidad del commit debe estar configurada (`git config user.name` y `git config user.email`). No pegues tokens en el script.

Descarga `setup_and_push.sh` y, desde una carpeta donde puedas crear un checkout nuevo, ejecuta:

```bash
bash setup_and_push.sh TradingEOD
```

El único archivo contiene el proyecto completo: crea el directorio, inicializa Git, agrega `https://github.com/marcoabud/TradingEOD.git`, conserva la historia remota existente de `main`, escribe los archivos, crea un commit si hay cambios y hace `git push -u origin main`. Rechaza directorios locales no vacíos y no hace force-push. El script no instala credenciales ni ejecuta el bot. Si una regla de protección impide el push, conserva el checkout para revisión mediante PR.

Para extraer sin publicar: `bash setup_and_push.sh --extract-only TradingEOD-local`.

### Repository Secrets exactos

Configúralos en [TradingEOD → Settings → Secrets and variables → Actions](https://github.com/marcoabud/TradingEOD/settings/secrets/actions):

| Nombre | Valor |
|---|---|
| `ALPACA_API_KEY` | API key del entorno elegido |
| `ALPACA_SECRET_KEY` | Secret key del mismo entorno |
| `ALPACA_PAPER` | `true` para paper; `false` para live. Vacío usa `true`; otro texto se rechaza |
| `WEBHOOK_URL` | URL HTTPS de n8n u otro receptor JSON; omitir si no quieres webhook |

Telegram directo es opcional: guarda el token del bot en **`TELEGRAM_TRADINGEOD`** y el destino en **`TELEGRAM_CHAT_ID`**, ambos como Repository Secrets. Los workflows aceptan también **`TELEGRAM_BOT_TOKEN`** como nombre alternativo del secret; si existen ambos tokens, se usa `TELEGRAM_TRADINGEOD`. Dentro del proceso Python el nombre del entorno sigue siendo `TELEGRAM_BOT_TOKEN`; para una ejecución local utiliza ese nombre. No pongas una URL de Telegram en `WEBHOOK_URL`: cada canal tiene su formato propio y Telegram directo no requiere webhook. No incluyas secretos en `.env.example`, commits, logs o mensajes.

Variables de repositorio (pestaña **Variables**, no Secrets):

| Nombre | Valor / efecto |
|---|---|
| `TRADING_ENABLED` | Ausente o distinto de `true`: las ejecuciones programadas solo simulan. `true`: permite enviar órdenes al entorno indicado por `ALPACA_PAPER` |
| `ALPACA_DATA_FEED` | `sip` por defecto; requiere acceso a datos SIP recientes. `iex` permite el feed IEX si está habilitado en tu cuenta |

El acceso a SIP es independiente de usar una cuenta paper. No hay cambio silencioso a datos IEX: son una cobertura de mercado distinta y pueden cambiar las señales. Un error de permisos o un fallo general del proveedor detiene la secuencia; una cotización individual inválida activa la política de omisiones descrita abajo.

IEX representa una sola bolsa. En la reconstrucción del 21 de octubre de 2026, SGOV superó los 90 segundos de antigüedad, y LLY, JPM y BRK.B superaron el spread permitido. El acceso a latest SIP devolvió HTTP 403. El bot exige una cotización válida cuando el activo interviene en una decisión o está en cartera; no relaja estas reglas para compensar un feed insuficiente. Tras agotar la recuperación de un símbolo, omite las órdenes dependientes y conserva las decisiones independientes que todavía pueden justificarse.

**Check EOD Market Data** permite seleccionar `session_date` (`YYYY-MM-DD`; vacío usa la fecha actual de Nueva York). Reconstruye aproximadamente el cierre−15 minutos+3 segundos mediante consultas históricas de solo lectura. Después carga las 250 sesiones completas y evalúa señales bajo el supuesto explícito de una cuenta sin posiciones. Los candidatos son hipotéticos: no lee la cartera, no envía órdenes, no registra una sesión de trading ni notifica Telegram. El resultado de calidad de datos se distingue del éxito de recopilar evidencia; no es un backtest ni prueba de fills. SIP histórico con al menos 15 minutos de antigüedad se consulta solo como comparación; nunca se usa como precio en vivo.

### Prueba manual de Telegram

Con el token y `TELEGRAM_CHAT_ID` configurados, abre **Actions → Test Telegram → Run workflow**, selecciona `main` y ejecuta la prueba. Puede hacerse a cualquier hora, incluidos fines de semana y feriados: no consulta el calendario de mercado, no utiliza credenciales Alpaca y no ejecuta el motor de trading.

El mensaje indica **PRUEBA MANUAL DE TELEGRAM**, estado `telegram_test` y modo `notification_test`. Equity y P&L aparecen como «No consultado». El workflow solo termina correctamente si Telegram confirma la entrega; falla si falta alguno de los dos secretos o el envío no se confirma. No envía un webhook ni activa las ejecuciones de trading. Esta prueba comprueba el canal de avisos; no valida señales ni órdenes.

### Puesta en servicio

1. Agrega las claves **paper**, `ALPACA_PAPER=true` y el feed que realmente tengas contratado/habilitado. Mantén `TRADING_ENABLED` sin definir.
2. Abre **Actions → Tests** y verifica que las pruebas pasen en GitHub.
3. Abre **Actions → Check Paper Connection → Run workflow** para comprobar Alpaca y Telegram a cualquier hora. Consulta Clock, Calendar, cuenta, posiciones y órdenes abiertas; no evalúa señales ni envía/cancela órdenes. Solo acepta paper y falla si Telegram no confirma. También se ejecuta al publicar cambios en su módulo o su propio workflow. El artifact `connectivity-summary-…` conserva el resultado.
4. Abre **Actions → Daily EOD Trading → Run workflow**, rama `main`, `dry_run=true`. Con `wait_for_window=true` (predeterminado) puede arrancar hasta 330 minutos antes y esperar. Una ejecución manual posterior a la ventana termina con `skipped`; no recupera una sesión perdida. Revisa `eod-summary-…/run_summary.json`.
5. Para probar órdenes paper, mantén `ALPACA_PAPER=true` y ejecuta manualmente con `dry_run=false`, o define `TRADING_ENABLED=true` para el calendario. La ejecución manual con `dry_run=false` es una activación explícita y no depende de `TRADING_ENABLED`.
6. Revisa los fills, saldos, alertas y registro diario durante varias sesiones, incluyendo un rebalanceo semanal. El paso a live requiere que tú cambies a credenciales live y `ALPACA_PAPER=false`; no existe promoción automática.

El workflow usa `GITHUB_TOKEN` automático con `contents: write` únicamente en el job de trading. La política del repositorio/organización debe permitir crear y escribir en la rama `eod-state`. Si lo impide, no se envían órdenes. No hace falta un secret personal de GitHub.

## Decisiones cuantitativas explícitas

- **Señales disponibles a la hora de operar:** a las 15:45 de Nueva York aún no existe el cierre definitivo de hoy. Se usan exactamente las **250 sesiones completadas anteriores**, ajustadas por splits y distribuciones (`Adjustment.ALL`), para SMA, retornos, volatilidad, RSI y ATR. El filtro core usa el cierre completado de SPY; las comparaciones de precio del swing usan el punto medio de una cotización reciente. No se trata una barra parcial como cierre final.
- **Core, solo última sesión de la semana:** el calendario hasta el viernes determina si hoy toca rebalancear; un jueves previo a viernes feriado cuenta. Score = `(0.60 * R60 + 0.40 * R120) / sigma60`, con 60 rendimientos diarios simples y desviación estándar muestral anualizada por `sqrt(252)`. Un retorno de 60 períodos necesita 61 cierres.
- **RISK-OFF:** SPY cerrado a/bajo SMA200, o R60 del activo con mejor score menor que R60 de SGOV → SGOV 70%. **RISK-ON:** SPY sobre SMA200 y prueba relativa aprobada → dos primeros, 35% cada uno. La igualdad de SPY con SMA200 se resuelve conservadoramente en refugio; igualdad del R60 con SGOV pasa. Empates de score siguen el orden SPY, QQQ, SMH, GLD.
- **Tolerancia:** menos de **3 puntos porcentuales del equity total**, no 3% relativo del objetivo. También puede quedar una posición residual menor al 3% con objetivo cero. Los residuos consumen presupuesto core; no se ignoran para nuevas compras.
- **Swing:** universo solicitado, incluido `BRK.B`; hasta dos posiciones. Entradas nuevas hasta 15% por posición y 30% agregado, priorizando RSI menor. RSI Wilder(2), ATR Wilder(14). El ATR del stop se recalcula con las barras completadas de cada sesión; la referencia de entrada es `avg_entry_price` de Alpaca. No se aumenta una posición swing abierta ni se rebalancea diariamente su deriva de peso.
- **Salidas primero:** precio > SMA5, RSI2 >70, precio < entrada−2.5×ATR14 o precio < SMA200. Se vende toda la posición si no está bloqueada por compra del mismo día. Una posición bloqueada sigue ocupando su slot.
- **Efectivo:** los slots sin señal permanecen en efectivo; no se suman a SGOV, evitando mezclar atribución entre bloques y rotaciones adicionales. Las compras usan `notional`, las ventas cantidad fraccionaria de hasta nueve decimales. Se conserva USD 1 de margen operativo y puede quedar polvo por redondeo/bandas: invertir exactamente el 100% no es una garantía compatible con costos variables y estas reglas. No se gastan importes por debajo del mínimo de compra de USD 1.

## Horario y mercado

Los cron solicitados son `45 19 * * 1-5` y `45 20 * * 1-5` (UTC). Se agregan `45 16 * * 1-5` y `45 17 * * 1-5` para las sesiones que cierran a las 13:00 de Nueva York.

Para reducir el efecto de los retrasos de GitHub se agregan arranques anticipados: `17 14 * * 1-5` / `17 15 * * 1-5` para cierre normal EDT/EST y `17 11 * * 1-5` / `17 12 * * 1-5` para cierre anticipado. Estas son sus familias nominales: cualquiera de los ocho cron conocidos que llegue dentro de los 330 minutos anteriores al objetivo, o dentro de la ventana de inicio válida, puede servir de respaldo. Calendar y Clock determinan el horario real; la etiqueta del cron no autoriza operar antes o después. Un cron desconocido, una fecha sin sesión o un disparo programado demasiado temprano sale sin operar. La familia principal arranca nominalmente 328 minutos antes; el proceso consulta Clock a intervalos de hasta un minuto y revalida Calendar antes de continuar. El límite de espera sigue siendo 330 minutos y el job tiene un máximo de 360 minutos. Esto mantiene un runner ocupado varias horas por sesión; hay que revisar los minutos/costos de Actions antes de cambiar a un repositorio privado o un runner de pago.

Python consulta Clock y Calendar antes de la lógica de cartera. Solo admite inicio entre **cierre−15 y cierre−10 minutos**, y nuevas órdenes antes de **cierre−5 minutos**. En un día normal: inicio 15:45–15:49:59 ET; en cierre anticipado: 12:45–12:49:59 ET. La tolerancia de cinco minutos reconoce retrasos del programador; nunca convierte una ejecución tardía en una orden fuera del horario permitido.

Nueva York alterna EST/EDT. Ciudad de México permanece en UTC−6: un cierre normal equivale a **13:45 CDMX en EDT** y **14:45 CDMX en EST**. No se codifica un horario fijo de México.

GitHub advierte que los eventos `schedule` pueden retrasarse o perderse y, en repositorios públicos inactivos, desactivarse. Esta plataforma no garantiza una orden exactamente a las 15:45 ni una ejecución todos los días. El arranque anticipado mitiga los retrasos; no ofrece una garantía horaria. Si se requiere esa garantía operativa, debe evaluarse un ejecutor externo y su supervisión.

Si un cron de la familia principal llega después del límite y no existe registro de evaluación ni de sesión de órdenes, registra `missed_window`, intenta avisar y termina con error visible en Actions. Un respaldo que fue admitido a esperar pero pierde la ventana también informa ese resultado. La evaluación se registra después de verificar la ventana y obtener el identificador de cuenta, antes de validar cartera y datos. Así, un bloqueo de cotizaciones no provoca después una falsa alerta de ausencia de evaluación. Ese registro no autoriza órdenes ni impide otro intento válido; `claim_session` conserva su barrera independiente antes de operar. Las simulaciones no crean registros de evaluación. Un marcador separado evita avisos repetidos del mismo día. Una familia alternativa que ya llega tarde, los feriados y las ejecuciones manuales fuera de horario permanecen en `skipped` sin aviso. Si GitHub nunca entrega ningún evento, este workflow no puede detectar por sí mismo su propia ausencia.

La reserva del aviso ocurre antes de enviarlo y no se borra si Telegram falla: hay hasta tres intentos de entrega dentro de esa ejecución, pero no una entrega garantizada ni reintento en otros cron. Los timeouts de red pueden duplicar un mensaje. El marcador del aviso nunca consume la autorización diaria de trading.

Las simulaciones tardías tampoco consumen marcadores de alerta de trading. Si la ventana vence entre la comprobación previa y la segunda comprobación del motor, la ejecución se bloquea y notifica el error. La cola de concurrencia conserva hasta 100 pendientes (`queue: max`) para que los cron auxiliares no reemplacen los respaldos que ya esperan; solo una ejecución del bot corre a la vez.

## Controles de ejecución

1. **Cuenta dedicada:** no mezclar operaciones manuales, otros bots, otra copia del workflow ni otro repositorio con la misma cuenta. Se rechazan posiciones fuera del universo, shorts, más de dos swings y órdenes abiertas. Ninguna consulta previa puede impedir una operación externa concurrente después de la comprobación.
2. **Sin venta de compra del mismo día:** se consulta el ledger `FILL`, paginado, incluyendo fills parciales de órdenes antiguas, y se cruza con órdenes del día. El día es el calendario de Nueva York. Se repite inmediatamente antes de cada venta; errores, historial saturado o datos ambiguos bloquean. No se recompra un activo vendido hoy. La regla también bloquea un stop del día de entrada.
3. **Capital:** equity real; gasto acotado por el mínimo de efectivo, buying power y non-marginable buying power, con presupuestos por bloque. No se usa poder de compra apalancado para exceder efectivo. Se requiere cuenta margin activa y sin restricciones, `multiplier >=2`; cuentas 1x/cash se bloquean porque estos campos no certifican por sí solos fondos liquidados T+1. El sistema no pretende cubrir una cuenta cash sin una implementación específica de liquidación.
4. **Una sesión real:** primero se crea atómicamente un marcador en `eod-state`, por cuenta (hash), fecha y paper/live. Abarca también sesiones sin órdenes. Se mantiene tras errores: un relanzamiento no intenta completar a ciegas un plan parcial. Dry-run no consume marcador. Los IDs de órdenes incorporan fecha, lado y símbolo como protección adicional.
5. **Barrera entre fases:** todas las ventas deben quedar `filled` y reflejarse en posiciones antes de comprar. Timeout, fill parcial, rechazo o cancelación interrumpen la ejecución. Se cancela únicamente la orden propia pendiente; si no se confirma, el resumen la identifica para conciliación. Una respuesta perdida al enviar se busca por `client_order_id`; jamás se repite el POST de la orden.
6. **Datos:** exige 250 sesiones completas por símbolo y OHLCV válidos. Calcula indicadores históricos y pide cotizaciones de posiciones y destinos core; las de nuevas entradas se piden después de resolver salidas y slots. Precios no requeridos quedan en `null`. Mantiene antigüedad ≤90 segundos, tamaños positivos y spread ≤0.5%, también en una última comprobación local justo antes del POST. No sustituye un precio actual por cierre previo, trade, barra o `market_value`. Los históricos de SGOV siguen siendo obligatorios aunque no necesite cotización en vivo.
7. **Stops EOD:** son evaluaciones al final de la sesión, no stops alojados en el broker ni protección intradía. Gaps, suspensiones, ausencia de señal horaria y prioridad anti-PDT pueden producir pérdidas superiores a 2.5 ATR. Market orders ejecutan al precio disponible; la tolerancia de pesos reduce rotación, no garantiza un precio de ejecución.

La prohibición de ida y vuelta intradía se conserva como regla operativa solicitada. No se presenta como certificación de cumplimiento normativo universal; las condiciones regulatorias y de cuenta del broker deben verificarse por separado.

## Recuperación de cotizaciones y omisiones

Cada lote puede hacer hasta cuatro consultas, con esperas nominales de 3, 5 y 10 segundos. Todas las fases comparten un presupuesto de 30 segundos de trabajo activo de cotizaciones: consultas, comprobaciones Clock y esperas. El tiempo de fills no consume ese presupuesto. No se reinicia por activo o fase, no hay reintentos ocultos del SDK y un `Retry-After` debe caber en lo que resta. El plazo de órdenes siempre prevalece. Un timeout HTTP no es un límite absoluto del proceso ante DNS o un servidor anómalo; las respuestas que excedan el presupuesto se descartan y la validación final impide usarlas.

Si el runner ya espera, unos cinco minutos antes realiza un preflight de solo lectura, con presupuesto separado de ocho segundos y un intento. Ese diagnóstico no consume el permiso diario ni presta sus precios a la ejecución. Se consultan de nuevo al operar.

| Fallo | Resultado |
|---|---|
| Cotización de un candidato swing | Reserva su posible slot y presupuesto; no asciende otro candidato para sustituirlo. Un segundo candidato independiente puede operar. |
| Precio válido demuestra que un candidato pierde SMA200 | Lo descarta y puede evaluar el siguiente del ranking RSI. |
| Cotización de una posición existente | Puede salir otra posición válida; bloquea compras nuevas durante esa ejecución. |
| Cotización de una posición o destino core | Congela el grupo antes de su primera venta; un swing independiente puede continuar si la cartera es valorable. |
| Fallo tras comenzar órdenes core | Informa core incompleto; las órdenes no son atómicas y puede quedar efectivo. |
| Permisos, transporte agotado, cuenta, calendario o historial anti-PDT no verificables | Detiene la secuencia. |
| Orden parcial, rechazada o incierta | Detiene y exige conciliación; no se trata como una omisión de cotizaciones. |

El ranking RSI se fija antes de las órdenes. Tras una omisión no se introduce un reemplazo ni se cambia de fuente. Todas las ventas deben confirmarse y conciliarse antes de comprar. La reserva diaria cubre también una sesión sin órdenes, y no se libera para reintentar un plan parcial.

## Observación paper

**Observe Paper Quotes** se inicia después de **Daily EOD Trading** y puede lanzarse manualmente con un `run_id`. Lee únicamente artifacts JSON de ejecuciones verificadas de este repositorio y datos históricos de Alpaca paper. No crea órdenes, marcadores de trading ni mensajes Telegram.

Conserva la hora real de cada comprobación, tamaños, bid/ask y reintentos. Consulta SIP histórico del mismo instante cuando ya tiene más de quince minutos, aplicando la misma validación de edad, spread y tamaños; el precio retrasado se usa solo para comparar. Si el proveedor rechaza la consulta, se registra la falta de evidencia.

Los artifacts de observación se conservan 90 días y alimentan el acumulado de campaña. La meta inicial es diez fechas distintas y dos evaluaciones semanales. Las sesiones sin precios requeridos cuentan como observación operativa cuando el resumen nuevo lo demuestra, pero no aportan evidencia de cotizaciones. Los resúmenes antiguos sin tiempos comprobables no se inventan ni se completan con una hora aproximada. Compras y ventas posteriores confirmadas se contabilizan por separado: diez sesiones sin fills no validan la ejecución ni la rentabilidad. La campaña ayuda a revisar; no autoriza pasar a dinero real.

## Resumen, fallos y recuperación

`run_summary.json` contiene estado (`skipped`, `missed_window`, `dry_run`, `completed`, `degraded`, `blocked`, `error`), horario observado/objetivo, régimen, fecha de señal, equity, P&L diario, plan, fills y posiciones protegidas. Las incidencias incluyen símbolo, fase, intentos y tiempos exactos, reservas y estado del core. `degraded` significa evaluación terminada con omisiones por datos; no implica ejecución completa del objetivo. Telegram distingue omisiones, falta de operaciones requeridas y órdenes confirmadas. `P&L = equity − last_equity`: no se ajusta por depósitos o retiros. Un dry-run separa órdenes hipotéticas de fills reales. Webhook envía JSON; Telegram envía texto. Ambos usan HTTPS, límites de tiempo y mensajes de error sin exponer URLs/tokens. La entrega queda registrada en `notification_delivery`; un fallo produce error en Actions, sin revertir órdenes ni provocar nuevas compras.

Si hay `unknown_submission`, `cancel_unconfirmed` o ejecución incompleta: revisa órdenes, fills y posiciones directamente en Alpaca. No elimines el marcador diario para forzar otro intento y no vuelvas a ejecutar en otra máquina. El diseño prefiere omitir operaciones a duplicarlas. El marcador se renueva automáticamente para la siguiente fecha; una orden que siga abierta bloqueará la siguiente sesión.

## Pruebas y ejecución local

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

En Windows activa `.venv/Scripts/activate` desde Git Bash. Exporta las variables del entorno de forma privada; `.env.example` es una referencia y el bot no carga automáticamente archivos `.env`. Después: `python -m src.bot`.

Por defecto `DRY_RUN=true` y `ALPACA_PAPER=true`. Solo para ejecución local efectiva hace falta además `EOD_STATE_DIR` con ruta absoluta y persistente; no se debe operar localmente y en GitHub sobre la misma cuenta. El estado local solo coordina procesos que comparten ese directorio. La prueba manual del workflow conserva los mismos controles de calendario que el calendario automático.

## Referencias verificadas

- [Alpaca-py: solicitudes de órdenes](https://alpaca.markets/sdks/python/api_reference/trading/requests.html)
- [Alpaca: órdenes y filtros de envío](https://docs.alpaca.markets/us/reference/getallorders-1)
- [Alpaca: actividades y fills](https://docs.alpaca.markets/us/v1.1/docs/account-activities)
- [Alpaca: fractional trading](https://docs.alpaca.markets/us/v1.1/docs/fractional-trading)
- [Alpaca: barras históricas](https://docs.alpaca.markets/us/reference/stockbars)
- [GitHub: eventos programados y limitaciones](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
- [Fidelity: RSI](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/RSI), [ATR](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/atr)

Versiones de paquetes consultadas en PyPI y fijadas al preparar esta entrega, 2026-10-16. Los pins de Actions apuntan a commits de releases oficiales. Actualizar dependencias requiere repetir las pruebas y la validación paper.
