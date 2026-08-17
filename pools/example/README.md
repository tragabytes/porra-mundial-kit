# pools/example — layout de una porra

Este directorio es un **ejemplo** de cómo se organiza cada porra. Para crear la
tuya, copia esta estructura a `pools/<id-de-tu-porra>/`:

```
pools/<id>/
├── ADMIN.xlsx      # ← AQUÍ va tu Excel ADMIN comprado en https://matejero.es
├── players.json    # tus jugadores (ver players.json de ejemplo, datos ficticios)
└── state.json      # lo crea el cron automáticamente; no lo edites
```

Notas:

- El `ADMIN.xlsx` es el producto de pago de matejero.es. **No está incluido en
  el kit** y no debe subirse a un repo público: añade `pools/**/ADMIN.xlsx` al
  `.gitignore` si tu repo es público.
- Si tienes varias porras, cada una lleva su **propia copia** del ADMIN.
- El schema de `players.json` está documentado en `docs/montaje.md` (paso 3).
- Sin el `ADMIN.xlsx` en su sitio, cualquier script del cron termina con
  `FileNotFoundError: ... pools/<id>/ADMIN.xlsx` — es el recordatorio de que
  primero hay que comprar y colocar el Excel.
