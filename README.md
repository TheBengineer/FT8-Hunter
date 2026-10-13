# FT8 Hunter

Automated FT8 operating for WSJT-X / WS Digital Suite: **make as many contacts as
possible without being rude on the band.**

That means answering CQs strongest-first and stations calling you before anything
else, while limiting retries, holding off stations that don't answer, and pausing to
listen instead of transmitting non-stop.

## Run

```bash
python3 ft8_server.py --mycall YOURCALL --mygrid YOURGRID --port 2237 --http 8080   # stdlib only
# open http://localhost:8080/ (mobile-friendly UI)
```

In WS / WSJT-X, open Settings → Reporting, set the UDP server to `127.0.0.1:2237`
and tick **Accept UDP requests**. You can change retries, hold time and listen
cadence live from the Settings panel in the UI.

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
