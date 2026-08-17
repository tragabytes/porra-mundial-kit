import test from "node:test";
import assert from "node:assert/strict";
import {
  fmtKickoffMadrid,
  madridDateHour,
  buildMispuntos,
  findKeyCI,
  downloadRelPath,
  isPublishAllowed,
} from "./lib_format.js";

test("isPublishAllowed: solo grupos mapeados; fail-open si el mapping está vacío", () => {
  const pg = { "34600000001-1111111111@g.us": "familia", "34600000002-2222222222@g.us": "amigos" };
  assert.equal(isPublishAllowed("34600000001-1111111111@g.us", pg), true);
  assert.equal(isPublishAllowed("34600000002-2222222222@g.us", pg), true);
  assert.equal(isPublishAllowed("999@g.us", pg), false);
  assert.equal(isPublishAllowed("", pg), false);
  // Fail-open: sin mapping no se bloquea (la auth Bearer sigue protegiendo)
  assert.equal(isPublishAllowed("999@g.us", {}), true);
  assert.equal(isPublishAllowed("999@g.us", null), true);
});

test("downloadRelPath: acepta la whitelist y rechaza traversal", () => {
  // Whitelist estática
  assert.equal(downloadRelPath("admin.xlsx"), "admin.xlsx");
  assert.equal(downloadRelPath("audit.json"), "audit.json");
  assert.equal(downloadRelPath("audit.md"), "audit.md");
  // Predicciones con nombre kebab válido
  assert.equal(downloadRelPath("predictions/rober.xlsx"), "predictions/rober.xlsx");
  assert.equal(downloadRelPath("predictions/eva-m.xlsx"), "predictions/eva-m.xlsx");
  // Rechazos: traversal, rutas raras, nombres no permitidos
  assert.equal(downloadRelPath("../state.json"), null);
  assert.equal(downloadRelPath("predictions/../../web_data.json"), null);
  assert.equal(downloadRelPath("predictions/rober.xlsx/../admin.xlsx"), null);
  assert.equal(downloadRelPath("predictions/ROBER.xlsx"), null); // mayúsculas fuera
  assert.equal(downloadRelPath("predictions/rober.exe"), null);
  assert.equal(downloadRelPath("state.json"), null);
  assert.equal(downloadRelPath(""), null);
  assert.equal(downloadRelPath(null), null);
});

test("fmtKickoffMadrid: UTC -> hora de Madrid con padding dd/mm HH:MM", () => {
  assert.equal(fmtKickoffMadrid("2026-06-13T01:00:00Z"), "13/06 03:00");
  assert.equal(fmtKickoffMadrid("2026-06-13T19:00:00Z"), "13/06 21:00");
  assert.equal(fmtKickoffMadrid("2026-06-15T16:00:00Z"), "15/06 18:00");
});

test("madridDateHour: fecha (YYYY-MM-DD) y hora (0-23) en Madrid", () => {
  assert.deepEqual(madridDateHour(Date.parse("2026-06-13T09:50:00Z")), {
    date: "2026-06-13",
    hour: 11,
  });
  // 23:30 UTC = 01:30 de Madrid del día siguiente (cambio de fecha)
  assert.deepEqual(madridDateHour(Date.parse("2026-06-13T23:30:00Z")), {
    date: "2026-06-14",
    hour: 1,
  });
});

const STATE = {
  leaderboard: [
    { name: "ROBER", points: 9, position: 1 },
    { name: "Nico", points: 5, position: 2 },
  ],
  all_predictions: [
    {
      label: "11/06 · México vs Sudáfrica",
      resultado: "1-0",
      predicciones: { ROBER: "1-0", Nico: "2-0" },
      puntos: { ROBER: 4, Nico: 1 },
    },
    {
      label: "12/06 · Brasil vs Marruecos",
      resultado: "1-1",
      predicciones: { ROBER: "2-0", Nico: "1-1" },
      puntos: { ROBER: 0, Nico: 4 },
    },
    {
      label: "20/06 · Foo vs Bar",
      resultado: null, // sin jugar: no debe aparecer
      predicciones: { ROBER: "1-0", Nico: "0-0" },
    },
  ],
};

test("findKeyCI encuentra la clave ignorando mayúsculas", () => {
  assert.equal(findKeyCI({ ROBER: 1 }, "rober"), "ROBER");
  assert.equal(findKeyCI({ ROBER: 1 }, "Nico"), null);
  assert.equal(findKeyCI(null, "ROBER"), null);
});

test("buildMispuntos: itemiza partidos jugados y reconcilia con el total", () => {
  const out = buildMispuntos(STATE, "ROBER", "familia");
  assert.match(out, /✅ 11\/06 · México vs Sudáfrica: pusiste 1-0, salió 1-0 → 4 pts/);
  assert.match(out, /❌ 12\/06 · Brasil vs Marruecos: pusiste 2-0, salió 1-1 → 0 pts/);
  assert.doesNotMatch(out, /Foo vs Bar/); // partido sin resultado se omite
  assert.match(out, /Puntos por partido \(signo\/exacto\): 4/);
  assert.match(out, /Otros \(grupos, eliminatorias y cuadro de honor\): 5/); // 9 - 4
  assert.match(out, /TOTAL: 9/);
});

test("buildMispuntos: nombre case-insensitive y sin línea 'Otros' si cuadra", () => {
  const out = buildMispuntos(STATE, "nico", "familia");
  assert.match(out, /🟨 11\/06 · México vs Sudáfrica: pusiste 2-0, salió 1-0 → 1 pts/);
  assert.match(out, /✅ 12\/06 · Brasil vs Marruecos: pusiste 1-1, salió 1-1 → 4 pts/);
  assert.match(out, /Puntos por partido \(signo\/exacto\): 5/);
  assert.doesNotMatch(out, /Otros/); // 5 - 5 = 0 -> sin línea de otros
  assert.match(out, /TOTAL: 5/);
});

test("buildMispuntos: jugador sin picks en partidos jugados", () => {
  const out = buildMispuntos(STATE, "Nadie", "familia");
  assert.match(out, /no tiene predicciones en los partidos ya jugados/);
});

test("buildMispuntos: recorta la lista pero suma sobre todos los partidos", () => {
  const preds = [];
  for (let i = 0; i < 35; i++) {
    preds.push({
      label: `p${i}`,
      resultado: "1-0",
      predicciones: { ROBER: "1-0" }, // exacto cada uno: 4 pts
      puntos: { ROBER: 4 },
    });
  }
  const state = { leaderboard: [{ name: "ROBER", points: 140 }], all_predictions: preds };
  const out = buildMispuntos(state, "ROBER", "familia", 30);
  assert.match(out, /\(últimos 30 de 35 partidos\)/);
  assert.match(out, /Puntos por partido \(signo\/exacto\): 140/); // 35 * 4, no 30 * 4
});

