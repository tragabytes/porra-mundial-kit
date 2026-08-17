# Bot WhatsApp — Porra Mundial 2026

Proceso Node.js que recibe webhooks de GitHub Actions y los reenvía al grupo
de WhatsApp del Mundial. Diseñado para correr 24/7 en un VPS modesto (Hetzner
CPX22 ~9.67€/mes; la línea CX22 original ya no existe en Hetzner).

## Arquitectura

- `server.js`: Express en puerto 8443 + cliente whatsapp-web.js en un único
  proceso. La sesión de WhatsApp se persiste en `./wwebjs_auth/` (gitignored)
  para no tener que escanear el QR en cada arranque.
- `ecosystem.config.cjs`: configuración de pm2 (autorestart, max_memory_restart 1G,
  timestamps en logs). Tiene que ser `.cjs` porque `package.json` declara
  `"type": "module"`.
- Endpoint `POST /publish` autenticado con `Authorization: Bearer <token>`.
- Endpoint `POST /sync` autenticado con el mismo token: el cron empuja
  `state.json` y `players.json` de cada porra tras cada ingest. Se persiste en
  `./data/<pool_id>/` para alimentar comandos del grupo (`!ranking`, etc.).
- Endpoint `GET /health` para monitorización.
- **Comandos en grupos mapeados** (vía `POOL_GROUPS` env var): `!ranking`,
  `!hoy`, `!proximo`, `!soy <nombre>`, `!miprediccion`, `!ayuda` (alias `!help`,
  `!comandos`). Mensajes sin prefijo `!` o desde grupos no listados se
  ignoran silenciosamente.
- **Comando con IA abierto a todo el grupo**: `!claudio <texto>` (alias
  `!bot`) llama a Claude Haiku 4.5 con contexto del pool (ranking completo,
  próximo partido, partidos de hoy y predicciones) y web search nativa de
  Anthropic restringida (`max_uses: 1`,
  solo para preguntas sobre el Mundial 2026 ausentes del contexto). Rate
  limit de 10 mensajes / 24 h por usuario (`bot_counters.json` en
  `data/<pool>/`, mutex en memoria para evitar races); el organizador
  (`ORGANIZER_JID`) queda exento del límite. El mensaje del usuario se
  envuelve en `<user_message>…</user_message>` y el system prompt incluye
  reglas anti-jailbreak; la respuesta se trunca a 800 chars antes de
  publicarse al grupo.
- Frente: **Caddy** como reverse proxy con HTTPS automático (Let's Encrypt)
  resolviendo desde un subdominio gratis de **DuckDNS**.

## Despliegue completo en Hetzner CPX22 (Ubuntu 24.04)

### 1. Provisión del VPS

- Crear cuenta en Hetzner Cloud, añadir método de pago.
- New Project → New Server: Falkenstein, Ubuntu 24.04, **Shared Resources →
  Regular Performance → CPX22** (4GB RAM, AMD, ~9.67€/mes), añadir SSH key
  pública, name `porra-bot`.
- Anotar la IP pública IPv4.

### 2. Setup base del sistema

```bash
ssh root@<ip-del-vps>

# Actualizar lista de paquetes
apt update

# Node.js 22 + pm2
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt install -y nodejs
npm install -g pm2

# Dependencias del sistema para Chromium (lo arrastra whatsapp-web.js)
apt install -y libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
  libgbm1 libpango-1.0-0 libcairo2 libasound2t64
```

### 3. Copiar el bot al VPS

El repo es **privado**, así que `git clone` por HTTPS falla en el VPS (no
tiene PAT configurado). Como el bot solo necesita 3 archivos pequeños y no
hace falta `git pull` futuro, lo más limpio es scp directo desde tu máquina:

```bash
# Desde tu PC, en la raíz del repo
ssh root@<ip-del-vps> "mkdir -p /root/porra-bot"
scp vps/server.js vps/lib_format.js vps/package.json vps/ecosystem.config.cjs \
    root@<ip-del-vps>:/root/porra-bot/

# De vuelta al VPS
ssh root@<ip-del-vps>
cd /root/porra-bot
npm install     # ~22s, instala 277 paquetes incluido Chromium de Puppeteer
```

### 4. Token Bearer + primer arranque (QR)

```bash
# En el VPS, dentro de /root/porra-bot
TOKEN=$(openssl rand -hex 32)
echo "VPS_WEBHOOK_TOKEN=$TOKEN" > .env
echo ">>> Copia este token a GitHub Secrets como VPS_WEBHOOK_TOKEN:"
echo "    $TOKEN"

# Arranque interactivo para escanear QR
set -a && source .env && set +a && node server.js
# - Aparece QR ASCII en la consola. Escanéalo con el móvil de la línea dedicada del bot
#   (NO el WhatsApp personal del organizador):
#   WhatsApp → menú (⋮) → Dispositivos vinculados → Vincular un dispositivo.
# - Tras escanear, espera a "WhatsApp client listo." y a la lista de grupos
#   disponibles. Anota el ID del grupo de TEST.
# - Ctrl+C cuando lo tengas.
```

### 5. Levantar como servicio con pm2

```bash
# Cargar .env en la shell y arrancar (pm2 7.x ignora env_file silenciosamente
# en ecosystem.config.cjs, hay que pasarle el env desde la shell).
set -a && source .env && set +a && pm2 start ecosystem.config.cjs --update-env
pm2 save                                            # guarda el dump con env
pm2 startup systemd -u root --hp /root              # configura systemd

# Verificación
pm2 status
pm2 logs porra-bot --lines 20 --nostream
curl http://localhost:8443/health   # → {"status":"up","whatsapp":"ready"}
```

Tras un reboot, systemd → `pm2-root.service` → `pm2 resurrect` arranca el bot
automáticamente con el entorno guardado.

### 6. HTTPS público con DuckDNS + Caddy

GitHub Actions requiere una URL HTTPS válida. Esta combo es 100% CLI, gratis
y persistente.

**6.1. Subdominio DuckDNS** (1 minuto en web)

1. https://www.duckdns.org → sign in con Google/GitHub/Reddit.
2. Crear subdominio (p.ej. `mi-porra.duckdns.org`), "update ip" con la IP del
   VPS.
3. Copiar el `token` que aparece arriba (UUID).

**6.2. Cron de auto-update en el VPS** (por si la IP cambiase)

```bash
(crontab -l 2>/dev/null; echo '*/5 * * * * curl -ksS "https://www.duckdns.org/update?domains=<subdominio>&token=<token-duckdns>&ip=" > /var/log/duckdns.log 2>&1') | crontab -
```

**6.3. Caddy con cert Let's Encrypt automático**

```bash
# Instalar Caddy del repo oficial
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | tee /etc/apt/sources.list.d/caddy-stable.list
apt update
apt install -y caddy

# Configurar Caddyfile (reemplaza <subdominio> por el tuyo)
cat > /etc/caddy/Caddyfile <<EOF
<subdominio>.duckdns.org {
    reverse_proxy localhost:8443
}
EOF

systemctl restart caddy
# Caddy obtiene el cert en ~10s vía ACME http-01 challenge.

# Test desde tu PC (HTTPS público)
curl https://<subdominio>.duckdns.org/health
# → {"status":"up","whatsapp":"ready"}
```

## Operación diaria

```bash
pm2 status              # ver si está arriba
pm2 logs porra-bot      # ver logs en tiempo real
pm2 restart porra-bot   # reiniciar
pm2 stop porra-bot      # parar
systemctl status caddy  # estado del reverse proxy
journalctl -u caddy -f  # logs de Caddy en tiempo real
```

## Variables de entorno

| Var | Obligatoria | Descripción |
|---|---|---|
| `VPS_WEBHOOK_TOKEN` | Sí | Token Bearer que el cliente (GHA cron) debe enviar |
| `POOL_GROUPS` | No | JSON inline `{"<chatId>":"<pool_id>"}` que mapea cada grupo WhatsApp a su porra. Sin esto, los comandos del grupo no funcionan. **Importante**: en el `.env` envolver el valor entre comillas simples para que `source .env` no se coma las dobles. Ej: `POOL_GROUPS='{"120363xxx@g.us":"familia"}'` |
| `ANTHROPIC_API_KEY` | No | Habilita `!bot`/`!claudio` para todos los miembros del grupo, con rate limit 10/24h por persona y web search restringida (1 uso/mensaje). Sin ella el comando responde "IA aún no configurada". |
| `ORGANIZER_JID` | No | JID del organizador, **exento** del rate limit del `!bot`. Si no se define, nadie está exento (comportamiento conservador). En grupos modernos suele venir como `<id>@lid`. |
| `GH_DISPATCH_TOKEN` | Sí (producción) | PAT fine-grained de GitHub (solo el repo de la porra, permiso **Actions: Read and write**; apunta su fecha de caducidad) con el que el bot dispara el workflow `cron-matches.yml`: una vez ~18 min antes de cada kickoff (preview) y desde kickoff+110 min cada 30 min hasta que el partido queda procesado en ambos pools. El calendario llega en `state.upcoming_kickoffs` vía `/sync` y se recuerda en `data/kickoff_dispatcher.json`. **El workflow ya no tiene `schedule:` propio: sin este token no corre nada.** Estado visible en `GET /health` (bloque `dispatcher`). |
| `NTFY_TOPIC` | No | Canal de [ntfy.sh](https://ntfy.sh) para avisos al móvil del organizador. El bot hace push cuando WhatsApp se desconecta (`disconnected`/`auth_failure`) y `cron-health.yml` cuando el VPS no responde o la sesión está caducada. Mismo valor en el secret `NTFY_TOPIC` de GitHub. Sin él no se envían pushes (el resto funciona igual). Suscribe la app ntfy a ese canal. |
| `PORT` | No | Puerto HTTP, default 8443 |

### Gotcha: JID `@lid` vs `@c.us`

En grupos WhatsApp modernos, el remitente puede aparecer como `<id>@lid`
(Linked Identifier) en vez de `<numero>@c.us`. Es opaco y NO contiene el
número de teléfono. La identificación del bot (merge de `whatsapp_jid` en
`!soy`, futura comprobación del organizador) se hace por igualdad de string
y funciona con ambos formatos — pero ten en cuenta al configurar
`ORGANIZER_JID` (PR-C) que probablemente sea `@lid`, no `@c.us`.

## Testing del bot aislado

Desde tu máquina, con todo configurado:

```bash
curl -X POST https://<subdominio>.duckdns.org/publish \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Hola mundo desde curl",
    "group_id": "<id-del-grupo-anotado>"
  }'
# → {"status":"ok"}
```

> **Aviso UTF-8:** PowerShell 5.1 de Windows corrompe los caracteres acentuados
> al pasarlos a `curl` (recodifica a cp1252). Si pruebas desde Windows y ves
> "p�blico" en el mensaje recibido, no es bug del bot — es PowerShell. El cron
> real corre en Ubuntu UTF-8 y no tiene este problema.

### Testing de comandos de grupo

Desde un cliente WhatsApp dentro de un grupo mapeado en `POOL_GROUPS`:

| Comando | Respuesta |
|---|---|
| `!ranking` | Tabla del ranking actual de la porra del grupo (top jugadores con puntos). |
| `!hoy` | Ranking de la jornada de hoy (puntos del día). Hasta el primer partido del día responde que aún no hay datos. |
| `!proximo` | Próximo partido programado (equipos, fecha en zona Madrid, fase). |
| `!soy <nombre>` | Asocia tu número de WhatsApp con un jugador de `players.json`. El nombre debe coincidir EXACTO (case-insensitive). |
| `!miprediccion` | Si ya hiciste `!soy`, confirma tu identidad. |
| `!ayuda` (alias `!help`, `!comandos`) | Lista de comandos disponibles. |
| `!claudio <texto>` (alias `!bot`) | Llama a Claude Haiku 4.5 con contexto del pool y web search restringida (1 uso por mensaje, solo Mundial 2026). Rate limit 10 mensajes/24h por persona (el organizador queda exento). Si superas el cupo, reacción 🚫 y el bot no responde. |

Mensajes sin `!` o desde grupos no listados en `POOL_GROUPS` se ignoran.

### Testing del endpoint /sync

```bash
curl -X POST https://<subdominio>.duckdns.org/sync \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "pool_id": "familia",
    "state": {"announced_match_ids": [], "next_match": null},
    "players": [{"slot": 1, "name": "Lucía"}]
  }'
# → {"status":"ok","pool_id":"familia","players":1}
# Tras la llamada: ssh vps "ls /root/porra-bot/data/familia/"
# → players.json  state.json
```

## Actualizar el bot

Como el repo es privado y el VPS no tiene credenciales, los updates son
manuales:

```bash
# Desde tu PC, tras hacer cambios en vps/
scp vps/server.js vps/lib_format.js vps/package.json vps/ecosystem.config.cjs \
    root@<ip-del-vps>:/root/porra-bot/

ssh root@<ip-del-vps> "cd /root/porra-bot && npm install && pm2 restart porra-bot"
```

> **Si cambiaste `ecosystem.config.cjs`** (p.ej. `restart_delay`, `max_restarts`,
> `min_uptime`), `pm2 restart` NO aplica esos campos: hay que **recrear** el
> proceso:
> ```bash
> ssh root@<ip-del-vps> "cd /root/porra-bot && pm2 delete porra-bot && \
>   set -a && . ./.env && set +a && pm2 start ecosystem.config.cjs --update-env && pm2 save"
> ```

## Si baneas el número

Pasa con baja probabilidad (~2-5% durante el Mundial según estimación), pero
si ocurre:
1. Confirma el baneo en el móvil de la línea dedicada (mensaje de WhatsApp al abrir).
2. La sesión `wwebjs_auth/` queda inservible: `rm -rf wwebjs_auth/`.
3. Da de alta una eSIM nueva (otro número), repite paso 4 (QR) con ese
   número.
4. Actualiza `WHATSAPP_GROUP_ID` en GitHub Secrets si has tenido que crear un
   grupo nuevo (no debería ser el caso si solo cambias el número del bot).
