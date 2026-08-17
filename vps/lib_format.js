// Helpers de formato de fecha/hora en Europe/Madrid. Puros (sin efectos
// secundarios ni dependencias): importables desde server.js y desde los tests
// (node:test). server.js arranca el bot al importarse, así que estas funciones
// viven aquí para poder testearlas sin levantar el cliente de WhatsApp.

// "dd/mm HH:MM" en hora de Madrid a partir de un ISO 8601 UTC. Mismo formato que
// los labels de upcoming_predictions. Padding manual: no fiarse del "2-digit" de
// Intl, que en algún ICU devuelve el mes como "6" en vez de "06".
export function fmtKickoffMadrid(utcKickoff) {
  const parts = new Intl.DateTimeFormat("es-ES", {
    timeZone: "Europe/Madrid",
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(new Date(utcKickoff));
  const p = Object.fromEntries(parts.map((x) => [x.type, x.value]));
  const pad = (s) => String(s).padStart(2, "0");
  return `${pad(p.day)}/${pad(p.month)} ${pad(p.hour)}:${pad(p.minute)}`;
}

// Localiza, sin distinguir mayúsculas, la clave de `obj` igual a `name`. Los
// nombres en all_predictions/leaderboard van saneados por el cron; este match
// laxo absorbe diferencias de caja entre players.json y esos nombres.
export function findKeyCI(obj, name) {
  if (!obj || !name) return null;
  const low = String(name).toLowerCase();
  return Object.keys(obj).find((k) => k.toLowerCase() === low) || null;
}

// Mensaje de !mispuntos para un jugador YA resuelto (targetName). Puro: recibe
// el `state` sincronizado y devuelve el texto. Lista, por cada partido jugado
// donde el jugador predijo, su pick vs el resultado y los puntos del partido
// (signo/exacto); cierra con el resumen reconciliado contra el total del
// ranking ("otros" = total − puntos de partidos). Los totales se suman sobre
// TODOS los partidos aunque la lista mostrada se recorte a `maxLines`.
export function buildMispuntos(state, targetName, poolId, maxLines = 30) {
  const all = Array.isArray(state?.all_predictions) ? state.all_predictions : [];
  const played = all.filter((m) => m.resultado);

  const items = [];
  let sumPartidos = 0;
  for (const m of played) {
    const key = findKeyCI(m.predicciones, targetName);
    if (!key) continue;
    const pick = m.predicciones[key];
    const pts = m.puntos ? m.puntos[key] : undefined;
    let icon = "";
    let ptsTxt = "";
    if (typeof pts === "number") {
      sumPartidos += pts;
      icon = pick === m.resultado ? "✅" : pts > 0 ? "🟨" : "❌";
      ptsTxt = ` → ${pts} pts`;
    } else {
      // Partido jugado sin puntos itemizados (eliminatoria): solo señalar exacto.
      icon = pick === m.resultado ? "✅" : "▫️";
    }
    items.push(`${icon} ${m.label}: pusiste ${pick}, salió ${m.resultado}${ptsTxt}`);
  }
  if (!items.length) {
    return `🤷 «${targetName}» no tiene predicciones en los partidos ya jugados.`;
  }

  const lines = [`📊 Puntos de «${targetName}» — ${poolId}`, ""];
  if (items.length > maxLines) {
    lines.push(`(últimos ${maxLines} de ${items.length} partidos)`);
    lines.push(...items.slice(-maxLines));
  } else {
    lines.push(...items);
  }

  lines.push("", `🟰 Puntos por partido (signo/exacto): ${sumPartidos}`);
  const lb = Array.isArray(state?.leaderboard) ? state.leaderboard : [];
  const lbEntry = lb.find((r) => r.name.toLowerCase() === targetName.toLowerCase());
  if (lbEntry) {
    const otros = lbEntry.points - sumPartidos;
    if (otros !== 0) {
      lines.push(`➕ Otros (grupos, eliminatorias y cuadro de honor): ${otros}`);
    }
    lines.push(`🏆 TOTAL: ${lbEntry.points}`);
  }
  return lines.join("\n");
}

// Whitelist de descargas del panel (Fase 3b): valida un nombre y devuelve su ruta
// relativa dentro de download/, o null si no está permitido. Seguridad: solo deja
// pasar {admin.xlsx, audit.json, audit.md, predictions/<kebab>.xlsx}; cualquier
// '..', '/' extra o nombre raro cae a null → sin path traversal desde el nombre.
const DL_STATIC = new Set(["admin.xlsx", "audit.json", "audit.md"]);
export function downloadRelPath(name) {
  if (DL_STATIC.has(name)) return name;
  const m = /^predictions\/([a-z0-9-]+\.xlsx)$/.exec(name || "");
  return m ? `predictions/${m[1]}` : null;
}

// ¿Se permite publicar a este group_id? Defensa en profundidad sobre POST /publish:
// solo los grupos mapeados en POOL_GROUPS (los que el bot conoce) son destinos
// válidos. Fail-open si el mapping está vacío (misconfig) para no dejar al bot
// mudo por un error de configuración: la auth Bearer sigue siendo el control real.
export function isPublishAllowed(groupId, poolGroups) {
  const keys = Object.keys(poolGroups || {});
  if (keys.length === 0) return true;
  return keys.includes(groupId);
}

// Fecha (YYYY-MM-DD) y hora (0-23) en Europe/Madrid a partir de un epoch ms.
export function madridDateHour(ms) {
  const p = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: "Europe/Madrid",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      hourCycle: "h23",
    })
      .formatToParts(new Date(ms))
      .map((x) => [x.type, x.value]),
  );
  return { date: `${p.year}-${p.month}-${p.day}`, hour: Number(p.hour) };
}
