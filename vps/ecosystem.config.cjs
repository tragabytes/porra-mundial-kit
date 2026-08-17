// pm2 config para el bot.
//
// IMPORTANTE: las variables sensibles (VPS_WEBHOOK_TOKEN) NO se leen aquí.
// pm2 7.x ignora el campo `env_file`, así que hay que cargar el .env en la
// shell ANTES de arrancar pm2. Pm2 captura el entorno actual y lo persiste
// con `pm2 save` para los reboots.
//
// Despliegue (en el VPS, desde /root/porra-bot):
//   set -a && source .env && set +a && pm2 start ecosystem.config.cjs --update-env
//   pm2 save
//   pm2 startup systemd -u root --hp /root
//
// Tras un reboot, systemd → pm2-root.service → pm2 resurrect rearranca el bot
// con el entorno guardado en el dump.
module.exports = {
  apps: [{
    name: 'porra-bot',
    script: 'server.js',
    autorestart: true,
    max_memory_restart: '1G',
    time: true,
    // Acota el bucle de reinicios ante un fallo permanente (p.ej. sesión de
    // WhatsApp caducada que no se arregla sola). Un bot sano vive horas/días
    // (uptime >> min_uptime) y resetea el contador; solo las caídas rápidas de
    // arranque acumulan, y tras max_restarts pm2 lo deja 'errored' (lo recoge
    // cron-health por ntfy).
    restart_delay: 5000,
    min_uptime: '10m',
    max_restarts: 5,
  }],
};
