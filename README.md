# RNT P2P Bridge

Puente de lectura **solo lectura** para el mercado RNT P2P. Consulta el GraphQL público de `backend.reentalp2p.com`, valida que la fotografía del mercado esté completa y publica un JSON estático mediante GitHub Pages.

## Qué resuelve

- Evita que el monitor de ChatGPT dependa directamente de la resolución DNS del backend de Reental.
- Contrasta `getOrdersData.numOrders` con todas las órdenes individuales de `getOrders`.
- Si la lectura no es completa, publica `complete: false` y **no** presenta órdenes como actuales.
- Normaliza `amount` (18 decimales) y `price` (6 decimales para USDT Polygon observado).
- Conserva eventos `NEW`, `CHANGED`, `INACTIVE`, `ERROR` y `RECOVERED` durante 7 días, de modo que una orden muy breve no se pierda entre dos revisiones horarias posteriores.
- No compra, firma, conecta una wallet ni necesita credenciales.

## Instalación rápida

1. Crea un repositorio **público** nuevo en GitHub, por ejemplo `rnt-p2p-bridge`.
2. Sube todo el contenido de esta carpeta a la rama `main`.
3. En el repositorio: **Settings → Pages → Build and deployment → Source → GitHub Actions**.
4. Ve a **Actions → Refresh RNT P2P bridge → Run workflow** y ejecútalo una vez manualmente.
5. Espera a que el workflow finalice. En **Settings → Pages** aparecerá la URL pública, normalmente:
   `https://TU_USUARIO.github.io/rnt-p2p-bridge/`
6. Abre:
   - `https://TU_USUARIO.github.io/rnt-p2p-bridge/status.json`
   - `https://TU_USUARIO.github.io/rnt-p2p-bridge/orders.json`
7. Comprueba que `status.json` indique `"complete": true` y que `integrity.consistent` sea `true`.
8. Pásame la URL exacta de `orders.json`. Con ella se cambia el monitor de ChatGPT para usar este puente como fuente redundante.

## Frecuencia

El workflow se ejecuta cada 10 minutos (`03, 13, 23, 33, 43, 53` de cada hora UTC) y también permite ejecución manual. GitHub Actions admite cron programado; la ejecución puede sufrir retrasos ocasionales, por lo que el JSON incluye `generatedAt` y `lastCompleteAt` para poder detectar datos antiguos.

## Regla de consumo crítica

**Nunca usar `orders` para generar una alerta si `complete != true`.**

Cuando hay un error:

- `complete` = `false`
- `orders` = `[]`
- `lastKnownOrders` conserva la última fotografía buena únicamente como contexto histórico
- `error` explica el fallo

## Qué contiene `orders.json`

Campos principales:

- `generatedAt`
- `complete`
- `lastCompleteAt`
- `integrity.expectedOrders`
- `integrity.recoveredOrders`
- `integrity.activeOrders`
- `integrity.consistent`
- `orders[]` con `_id`, `hash`, proyecto, maker, cantidad normalizada, precio P2P, listing/expiration, exchange y target
- `events[]` con eventos de los últimos 7 días

## Cuándo NO usarlo / cuándo detenerlo

No confíes en este puente para alertas si ocurre cualquiera de estas situaciones:

1. `complete` no es `true`.
2. `integrity.consistent` no es `true`.
3. `generatedAt` tiene una antigüedad superior a 20–30 minutos con la programación actual.
4. Reental cambia el esquema GraphQL y aparecen errores de campos/tipos.
5. El endpoint empieza a exigir autenticación, cookies, wallet, firma o credenciales. **No añadas secretos de wallet al repositorio.**
6. Reental publica una limitación contractual/técnica contra este tipo de consulta automatizada; en ese caso hay que revisar el método antes de continuar.
7. El payment token deja de ser el USDT Polygon conocido y el script marca la advertencia correspondiente; no interpretes automáticamente `price` como USDT.
8. El número agregado de órdenes no coincide con las órdenes individuales recuperadas.
9. GitHub Actions deja de ejecutarse. En repositorios públicos, GitHub puede desactivar workflows programados tras periodos prolongados de inactividad; revisa la pestaña Actions si `generatedAt` deja de actualizarse.
10. Necesitas ejecución de órdenes. Este proyecto es deliberadamente **solo lectura** y no debe ampliarse con claves privadas, seed phrases ni firmas automáticas.

## Seguridad

No contiene wallet, cookies, API keys ni tokens. El repositorio puede ser público porque los datos consultados ya son públicos. Si en el futuro el endpoint requiere credenciales, no los incrustes en el código ni en el JSON publicado.
