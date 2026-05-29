# 🚀 Guía para poner el bot en línea (24/7)

Esta guía está escrita para que la siga **cualquier persona**, aunque no programe.
La parte de "instalar" se hace **una sola vez**. Después, todo el manejo diario
del bot se hace desde Discord con comandos.

---

## 📋 Antes de empezar — ten esto a la mano

Junta estos datos en un bloc de notas. Los vas a pegar más adelante:

1. **El token del bot de Discord** (del Portal de Desarrolladores de Discord).
2. **El ID de tu servidor de Discord** (clic derecho en el nombre del servidor → "Copiar ID de servidor"; necesitas activar el Modo Desarrollador en Ajustes → Avanzado).
3. **El correo, usuario y contraseña** de la cuenta de X/Twitter que el bot usará
   para verificar (usa una cuenta dedicada, NO tu cuenta personal).

---

## Paso 1 — Subir el código a GitHub

GitHub es como un "Google Drive para código". Railway lee el bot desde ahí.

1. Crea una cuenta gratis en https://github.com
2. Crea un repositorio nuevo (botón verde "New"), ponle un nombre como `mi-bot-discord`.
3. Sube todos los archivos del bot. (Si no sabes cómo, cualquier persona con algo
   de técnica lo hace en 5 minutos, o puedes arrastrar los archivos en la web de GitHub.)

> ⚠️ **MUY IMPORTANTE:** NO subas el archivo `.env` (tiene tus contraseñas).
> El archivo `.gitignore` ya está configurado para evitarlo automáticamente.

---

## Paso 2 — Crear la cuenta en Railway

1. Entra a https://railway.com y regístrate (puedes usar tu cuenta de GitHub).
2. El plan **Hobby** cuesta **$5 al mes** e incluye $5 de crédito de uso.
   Para un bot con verificación de Twitter, calcula entre **$5 y $10 al mes**.

---

## Paso 3 — Conectar el bot

1. En Railway, clic en **"New Project"** → **"Deploy from GitHub repo"**.
2. Elige el repositorio que creaste en el Paso 1.
3. Railway detecta el `Dockerfile` solo y empieza a construir el bot. Espera unos minutos.

---

## Paso 4 — Poner las variables (las contraseñas)

Aquí NO editas archivos — todo es en la página web de Railway.

1. Abre tu proyecto → pestaña **"Variables"**.
2. Agrega una por una (botón "New Variable"):

| Nombre | Valor |
|---|---|
| `DISCORD_TOKEN` | (tu token de Discord) |
| `GUILD_ID` | (el ID de tu servidor) |
| `TWITTER_EMAIL` | (correo de la cuenta de X) |
| `TWITTER_USERNAME` | (usuario de X, sin @) |
| `TWITTER_PASSWORD` | (contraseña de X) |
| `TWITTER_HEADLESS` | `true` |
| `VERIFY_ENABLED` | `true` |

3. Railway reinicia el bot solo cada vez que guardas una variable.

---

## Paso 5 — Agregar almacenamiento que no se borre

Para que el bot no pierda los puntos ni la sesión de X cuando se reinicia:

1. En tu proyecto → clic en el servicio del bot → pestaña **"Settings"**.
2. Busca **"Volumes"** → **"Add Volume"**.
3. En "Mount Path" escribe exactamente: `/data`
4. Guarda.

---

## Paso 6 — ¡Listo! Revisa que esté vivo

1. Ve a la pestaña **"Deployments"** → **"View Logs"**.
2. Si ves algo como `Logged in as TuBot#1234`, ¡ya está funcionando!
3. La primera vez, el bot inicia sesión en X automáticamente (puede tardar 1 minuto).

---

## ✅ De ahora en adelante: todo desde Discord

Ya no necesitas volver a Railway para el día a día. Todo se maneja con comandos:

| Quieres... | Comando |
|---|---|
| Crear una tarea/campaña | `/create-task` |
| Ver tareas activas | `/tasks` |
| Ver puntos de alguien | `/points check` |
| Dar puntos manualmente | `/points add` |
| Ver el ranking | `/points leaderboard` |
| Desactivar una tarea | `/task-admin deactivate` |

Los usuarios usan `/set-twitter` para vincular su cuenta y `/verify` para reclamar puntos.

---

## 🆘 Si algo se rompe

**El bot está offline (gris en Discord):**
Entra a Railway → Deployments → "Restart". Casi siempre se arregla así.

**El `/verify` no encuentra a nadie / da error:**
Probablemente X cerró la sesión del bot. Esto pasa de vez en cuando porque la
verificación usa un navegador automatizado. Si pasa seguido, considera cambiar
a un método más simple (que el usuario pegue el link de su comentario) — pregúntale
a quien te ayudó con el setup.

**Quieres cambiar una contraseña o ajuste:**
Railway → pestaña "Variables" → edita el valor → se reinicia solo.
