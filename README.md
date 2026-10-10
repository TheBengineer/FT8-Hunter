# FT8 Hunter

Auto-calling FT8 hunter: watches live decodes from WSJT-X / WS Digital Suite over UDP,
answers CQs strongest-first from a mobile-friendly web UI, and rebroadcasts traffic so
**WRL Desktop can log alongside it**.

Station: tested with a Xiegu G90 + FLRig on Ubuntu 24.04, callsign KR4MZM.

## Files

- `ft8_server.py` — hunter daemon: UDP watcher + targeting engine + JSON API + serves the webapp. Zero dependencies (stdlib only).
- `ft8_app.html` — single-page mobile portrait web UI.

## Quick start

```bash
python3 ft8_server.py --mycall YOURCALL --mygrid YOURGRID --port 2237 --http 8080
# open http://localhost:8080/ on your phone
```

In WSJT-X / WS Suite: Settings → Reporting → UDP Server `127.0.0.1:2237`, tick
**Accept UDP requests**. Press **Smoke test** in the UI to verify the loop
(WS answers with its decode backlog + Status).

## How it works

- **Decode feed**: passive `SO_REUSEPORT` listener on 2237. WSJT-X never binds 2237
  itself — it sends *from* an ephemeral port, so Replies must go back to that port
  (learned automatically from WS's own packets).
- **Reply = click**: a Type 4 Reply is only honored if every field (time, SNR, DT,
  DF, `~` mode marker, message) exactly matches a prior decode — same as clicking a
  line in the decode window. Params come from live Type 2 packets, never guessed.
- **TX control**: WS auto-sequences once armed. The hunter sends Halt Tx (type 8)
  when a target times out or is skipped.
- **WRL passthrough**: every WS packet is rebroadcast to `127.0.0.1:2238` — point
  WRL Desktop there. (Two apps can't share 2237: Linux REUSEPORT *load-balances*,
  so they'd each miss half the traffic.)

## Targeting engine

Priority per 15s cycle: stations calling you (first-come-first-serve, preempts
everything) → CQ callers strongest-first → strongest overheard traffic (tail-end).

- CQ: N tries (default 3), then 10-minute hold. Tail-end: 2 tries, same hold.
- In-progress QSOs extend on reply; logged QSOs (Type 5) clear the target and mark
  the call worked immediately.
- Only **today's** ADIF contacts count as worked (auto-reloads when WS logs).
- Every few idle cycles it rests a full minute to listen for the off sequence;
  even/odd decode parity is tracked and shown in the UI.

All numbers (tries, hold minutes, listen cadence) are adjustable in Settings at the
bottom of the web UI — no restart needed.
