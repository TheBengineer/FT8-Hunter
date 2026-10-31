# Changelog

What changed in each release of FT8 Hunter, newest first.

## 1.1.13 (2026-10-31)

- The "Someone else is on frequency — no new calls for 5m" warning has an
  **It's just interference** button. It ends the pause so the hunter calls
  again straight away, and turns the busy-frequency check off for 30 minutes,
  so the same interference doesn't start another pause. After that the check
  is back on its own.
- The hunter's window now looks up the computer's network address every
  minute, as the page does. Before, it kept the address it had at startup, so
  after the network handed out a new one the window's link and QR code pointed
  phones at the old address, and differed from the page's.

## 1.1.12 (2026-10-31)

- The Linux download now updates itself, as the Windows installer already
  does. When a new version is out, **Install and restart** in the page (or
  **⬆ Update to …** in the window) stops hunting, turns WS's TX off, downloads
  the new release and checks it against the release's SHA-256. It then puts the new
  FT8-Hunter in place of the old one, and starts it again with the same
  options. A `ws-hunter.AppImage` kept beside the hunter is replaced too, but
  only when the release carries a different WS build. Before this, Linux only
  offered a link to the release page. The hunter's folder must be one you can
  write to.
- Run as a systemd service, the Linux hunter exits with code 75 after an update
  and lets systemd start the new version, so give the unit `Restart=on-failure`.

## 1.1.11 (2026-10-30)

- The hunter deletes the .wav files WS keeps when **File → Save all** or **Save
  decoded** is on, once they are an hour old. It deletes only WS's own files,
  named by date and time, from WS's save folder, and leaves the samples folder
  alone. It's on by default; turn it off in Settings with **Delete WS's saved
  .wav files after an hour**.

## 1.1.10 (2026-10-30)

- Q65 support. The hunter takes Q65's period (15, 30, 60, 120 or 300 s) and
  sub-mode (A–E) from WS. Its timing and Tx offset picks follow them;
  before this, Q65 was treated as FT8, with 15 s periods. On Q65 it decides what
  to send 1 s after the period starts, since WS decodes Q65 only just before the
  period ends.
- On Q65, a quiet band no longer stops the hunter with "nothing decoded for 5
  min".
- Decodes that end in a marker, such as an AP decode's `a1` or Q65's `q3`, are
  read correctly. Before, a sign-off decoded with AP help, such as `RR73 a2`,
  wasn't recognised as one.
- The band scan still covers FT8, FT4 and FT2 only.

## 1.1.9 (2026-10-30)

- Bundles WS build v3.2.0-hunter.2. A rig control error now makes WS retry
  quietly, up to three times over 7 s. The **Rig Control Error** box only
  appears if the radio doesn't answer at all. Before, every second error asked
  you to click Retry. This helps when RF in the shack garbles the CAT link now
  and then.

## 1.1.8 (2026-10-29)

- With WS's Split Operation set to **Fake It**, the hunter now also puts the
  dial back in two cases. One is when the radio reports its Tx frequency late,
  for example a slow FLRig. The other is when the slip shows up while a new
  message is starting to transmit. In 1.1.7 the dial could still end up 500 Hz
  or 1 kHz high. Setting Split Operation to **Rig** avoids the slip altogether.

## 1.1.7 (2026-10-28)

- With WS's Split Operation set to **Fake It**, WS sometimes took the radio's Tx
  frequency as its dial after transmitting, leaving the dial 500 Hz or 1 kHz
  high. The hunter now puts it back and says so in its log.
- Updating from the hunter on Windows no longer fails to start the new hunter
  with "Failed to load Python DLL".
- An update no longer stops partway when the old hunter is slow to quit: the
  installer waits up to 15 s for it, then closes it.
