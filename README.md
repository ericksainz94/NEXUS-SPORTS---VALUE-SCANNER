# Nexus Sports Value Scanner — despliegue gratis

Esto corre en GitHub, sin servidor propio y sin costo. Pasos:

## 1. Crea el repositorio
1. Ve a github.com, crea una cuenta si no tienes, y crea un repositorio
   nuevo. Puede ser **privado** (recomendado, ya que ahí vivirá tu lógica
   del bot) — GitHub Actions y Pages funcionan igual en repos privados.
2. Sube estos archivos manteniendo la misma estructura de carpetas:
   ```
   nexus_sports_value.py
   .github/workflows/scan.yml
   docs/index.html
   docs/picks.json
   ```

## 2. Configura tus credenciales como Secrets (NO las pongas en el código)
En tu repo: **Settings → Secrets and variables → Actions → New repository secret**
Crea estos tres secrets:
- `TOKEN_TG` → tu token de Telegram
- `API_KEY_ODDS` → tu API key de The Odds API
- `CHAT_ID` → tu chat id de Telegram

## 3. Activa GitHub Pages
**Settings → Pages → Source → selecciona la rama `main` y la carpeta `/docs`**
Guarda. En unos minutos tu sitio va a estar disponible en:
`https://TU-USUARIO.github.io/TU-REPOSITORIO/`

## 4. Dale permiso de escritura al workflow (para que pueda guardar el historial)
**Settings → Actions → General → Workflow permissions → marca
"Read and write permissions"** y guarda. Esto es necesario para que el
bot pueda hacer commit del CSV/JSON actualizado después de cada escaneo.

## 5. Prueba manual
Ve a la pestaña **Actions** de tu repo → selecciona "Nexus Sports Value
Scanner" → **Run workflow** (botón a la derecha). Esto corre el bot ahora
mismo sin esperar al cron, para que confirmes que Telegram y el sitio
funcionan.

## Cómo mantener tu registro de resultados
Después de que un pick se resuelve, edita `picks_log.csv` directo en
GitHub (botón de lápiz en la interfaz web) y llena las columnas
`resultado_real` (GANO / PERDIO) y `ganancia_perdida`. Al hacer commit,
la próxima corrida del workflow actualiza automáticamente `docs/picks.json`
y tu sitio reflejará el nuevo winrate real.

## Nota sobre los horarios
GitHub Actions no garantiza el minuto exacto en horas pico — puede
atrasarse unos minutos. Si necesitas precisión al segundo (por ejemplo,
picks que dependen de que un partido no haya empezado), agrega margen de
tiempo en tu lógica o revisa manualmente antes de actuar sobre un pick.

## Sobre monetizar el sitio
Antes de agregar cualquier red de anuncios, revisa las políticas de
contenido de apuestas/predicciones deportivas de esa red para tu país —
varias las restringen o prohíben. Evita redes de "pop ads"/popunders:
son conocidas por servir malware y contenido engañoso a tus visitantes,
y la mayoría de redes serias (como Google AdSense) prohíben combinarlas.
