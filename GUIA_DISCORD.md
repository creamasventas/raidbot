# 🤖 Guía para integrar el bot a Discord

Esta guía crea la "identidad" del bot en Discord y lo mete a tu servidor.
Se hace **una sola vez**. Son todos clics, no hay que programar.

> 💡 Recuerda: estos pasos crean el bot, pero el bot solo **responde** cuando
> está prendido en Railway (ver GUIA_DESPLIEGUE.md). Son dos cosas separadas.

---

## Paso 1 — Crear la aplicación

1. Entra a https://discord.com/developers/applications
2. Clic en **"New Application"** (arriba a la derecha).
3. Ponle un nombre (el que verá la gente en el servidor) → **"Create"**.

---

## Paso 2 — Crear el bot y copiar el token

1. Menú de la izquierda → **"Bot"**.
2. Clic en **"Reset Token"** → **"Yes, do it!"**.
3. Copia el token que aparece. **Guárdalo** — es el `DISCORD_TOKEN` de Railway.

> ⚠️ El token es como la contraseña del bot. Si alguien lo tiene, controla tu bot.
> Si crees que se filtró, vuelve aquí y haz "Reset Token" otra vez.

---

## Paso 3 — Activar los Intents (importante)

En la misma página de **"Bot"**, baja hasta **"Privileged Gateway Intents"**
y activa estos dos interruptores:

- ✅ **Server Members Intent**
- ✅ **Message Content Intent**

Clic en **"Save Changes"**.

(Si no los activas, el bot no puede dar puntos por mensajes ni reconocer miembros.)

---

## Paso 4 — Invitar el bot al servidor (la forma fácil)

1. Menú de la izquierda → **"General Information"**.
2. Copia el **"Application ID"** (botón "Copy").
3. Toma este link y reemplaza `TU_APPLICATION_ID` por el número que copiaste:

```
https://discord.com/oauth2/authorize?client_id=TU_APPLICATION_ID&permissions=84992&scope=bot+applications.commands
```

4. Pega el link completo en tu navegador y presiona Enter.
5. Elige tu servidor en el menú → **"Autorizar"** → completa el captcha.

El bot ya aparece en la lista de miembros de tu servidor (gris/offline hasta
que lo prendas en Railway).

> El número `84992` ya incluye los permisos exactos que necesita:
> ver canales, enviar mensajes, poner embeds y leer el historial.

---

## Paso 5 — Comprobar que todo conecta

Una vez que el bot esté prendido en Railway:

1. En cualquier canal de tu servidor, escribe `/`
2. Deberían aparecer los comandos: `/ping`, `/create-task`, `/points`, `/tasks`, etc.
3. Prueba con `/ping` — el bot debe responder con su latencia.

Si los comandos no aparecen, espera unos minutos (Discord a veces tarda en
mostrarlos la primera vez) o reinicia el bot en Railway.

---

## ✅ Resumen de qué dato va dónde

| Dato que sacaste aquí | Dónde se usa |
|---|---|
| **Token** (Paso 2) | Variable `DISCORD_TOKEN` en Railway |
| **Application ID** (Paso 4) | Solo para el link de invitación |
| **ID del servidor** | Variable `GUILD_ID` en Railway (clic derecho en el servidor → Copiar ID) |
