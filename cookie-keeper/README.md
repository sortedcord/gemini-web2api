# Gemini cookie keeper

An isolated Playwright sidecar that keeps an existing Gemini Web browser session active and persists routine Google cookie rotation without putting Chromium in the inference path.

The keeper:

- starts one long-lived headless Chromium process;
- injects the existing cookie string from `/session/cookie.txt`;
- visits `https://gemini.google.com/app` periodically;
- reads the browser cookie jar;
- merges only `__Secure-1PSID`, `__Secure-1PSIDTS`, and `__Secure-1PSIDCC`;
- atomically replaces the shared cookie file with mode `0600`;
- exposes no HTTP port.

Both containers must mount the **directory**, not the individual file, so atomic replacement is visible to the bridge:

```yaml
services:
  gemini-web-bridge:
    volumes:
      - ./gemini-session:/session:ro

  gemini-cookie-keeper:
    build: ./cookie-keeper
    restart: unless-stopped
    init: true
    shm_size: "512mb"
    environment:
      COOKIE_FILE: /session/cookie.txt
      REFRESH_INTERVAL_MS: "300000"
      PAGE_SETTLE_MS: "5000"
    volumes:
      - ./gemini-session:/session
```

Configure the bridge's `cookie_file` as `/session/cookie.txt`.

This preserves routine rotation only. A Google logout, security challenge, revoked session, or expired primary session still requires a fresh browser export.
