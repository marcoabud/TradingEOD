# Datos externos: evaluación pendiente

Marco pidió conservar para una siguiente fase la búsqueda de un proveedor de datos en tiempo real más económico. No hay contratación ni cambio de fuente automático en esta entrega.

Primero se usarán los registros de diez sesiones paper para distinguir fallos transitorios de falta persistente de cobertura. La comparación con SIP se hará después, al instante exacto registrado. Que una salida se omita repetidamente por datos es motivo para revisar la fuente antes de considerar dinero real.

## Requisitos para comparar proveedores

- Cotizaciones bid/ask en tiempo real con tamaños y timestamp de origen; preferencia por NBBO consolidado estadounidense. Un precio de último trade no basta.
- Cobertura de los 17 símbolos del universo, incluidos SGOV y BRK.B, y equivalencia documentada de símbolos, zonas horarias, condiciones de cotización y unidades de tamaños.
- Precio total para uso personal y automatizado, derechos de uso, tasas de bolsas y límites de consultas. Confirmar explícitamente si ofrece datos retrasados, de una sola bolsa o consolidados.
- API con autenticación y límites de tiempo, errores claros y disponibilidad medible cerca del cierre. Integración mediante un adaptador de cotizaciones, sin alterar el broker ni anti-PDT.
- Prueba paralela sin órdenes: antigüedad, spread, cobertura, latencia y discrepancias frente a IEX/SIP en los instantes de decisión. Repetir en cierres anticipados.
- Cambiar la fuente requiere decisión explícita, pruebas y observación paper. El cambio de históricos se evalúa por separado porque puede cambiar RSI y momentum.

## Referencia de coste, verificada el 23 de octubre de 2026

Alpaca publica Basic gratuito con datos en tiempo real de IEX y Algo Trader Plus a USD 99/mes con cobertura de todas las bolsas estadounidenses. Son USD 1,188 al año, equivalentes al 29.7% del capital inicial de USD 4,000, sin componer. Este coste justifica estudiar alternativas; no demuestra que exista una opción equivalente más barata. [Planes oficiales de Alpaca](https://docs.alpaca.markets/us/docs/about-market-data-api).

Las consultas históricas SIP con `end` al menos quince minutos antiguo pueden servir para la comparación sin una suscripción de datos recientes. Latest SIP y los últimos quince minutos requieren el acceso correspondiente. Revalidar precios y permisos cuando se tome la decisión. [Condiciones oficiales de datos](https://docs.alpaca.markets/us/docs/market-data-faq).

Pendiente: elaborar una lista corta con precios actuales y verificar su licencia/cobertura real después de recoger la evidencia. No se sustituirán las cotizaciones por barras, snapshots sin bid/ask válido ni SIP retrasado para autorizar órdenes.
