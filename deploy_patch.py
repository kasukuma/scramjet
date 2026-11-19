from pathlib import Path


APP = Path(__file__).with_name("app.py")
text = APP.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str, already_applied: str | None = None) -> None:
    global text
    if old in text:
        text = text.replace(old, new, 1)
    elif already_applied is None or already_applied not in text:
        raise RuntimeError(f"{label} patch point not found")


# Bump cached shell/service-worker URLs after each deployment patch update.
replace_once(
    'RUNTIME_REV = "20261119-url-proxy-v1"',
    'RUNTIME_REV = "20261119-url-proxy-v3"',
    "runtime revision",
    'RUNTIME_REV = "20261119-url-proxy-v3"',
)
if 'RUNTIME_REV = "20261119-url-proxy-v2"' in text:
    text = text.replace(
        'RUNTIME_REV = "20261119-url-proxy-v2"',
        'RUNTIME_REV = "20261119-url-proxy-v3"',
        1,
    )

old_register = '''    await navigator.serviceWorker.register(SW_URL, {{ scope: SW_SCOPE, updateViaCache: "none" }});
    if (!navigator.serviceWorker.controller) {{
      await new Promise((resolve) => {{
        const onChange = () => {{
          navigator.serviceWorker.removeEventListener("controllerchange", onChange);
          resolve();
        }};
        navigator.serviceWorker.addEventListener("controllerchange", onChange);
      }});
    }}
'''
new_register = '''    await navigator.serviceWorker.register(SW_URL, {{ scope: SW_SCOPE, updateViaCache: "none" }});
    await navigator.serviceWorker.ready;

    if (!navigator.serviceWorker.controller) {{
      await new Promise((resolve, reject) => {{
        const timeout = setTimeout(
          () => reject(new Error("Service Workerがページを制御できません")),
          10000
        );
        const onChange = () => {{
          clearTimeout(timeout);
          navigator.serviceWorker.removeEventListener("controllerchange", onChange);
          resolve();
        }};
        navigator.serviceWorker.addEventListener("controllerchange", onChange);
        if (navigator.serviceWorker.controller) onChange();
      }});
    }}
'''
replace_once(
    old_register,
    new_register,
    "Service Worker control",
    "Service Workerがページを制御できません",
)

cookie_marker = '''  class RelayTransport {{
'''
cookie_code = '''  const COOKIE_JAR_KEY = "scramjet:url-proxy-cookies:v1";

  function loadCookieJar() {{
    try {{
      return JSON.parse(sessionStorage.getItem(COOKIE_JAR_KEY) || "{{}}");
    }} catch (_) {{
      return {{}};
    }}
  }}

  function saveCookieJar(jar) {{
    try {{ sessionStorage.setItem(COOKIE_JAR_KEY, JSON.stringify(jar)); }} catch (_) {{}}
  }}

  function cookieOrigin(urlStr) {{
    try {{ return new URL(urlStr).origin; }} catch (_) {{ return ""; }}
  }}

  function getCookieHeader(urlStr) {{
    const origin = cookieOrigin(urlStr);
    const jar = loadCookieJar();
    const now = Date.now();
    const entries = jar[origin] || [];
    const active = [];
    for (const entry of entries) {{
      if (!entry.expires || entry.expires > now) active.push(entry);
    }}
    jar[origin] = active;
    saveCookieJar(jar);
    return active.map((entry) => entry.name + "=" + entry.value).join("; ");
  }}

  function storeSetCookies(urlStr, headers) {{
    const origin = cookieOrigin(urlStr);
    if (!origin || !Array.isArray(headers)) return;
    const jar = loadCookieJar();
    const entries = jar[origin] || [];

    for (const pair of headers) {{
      if (!Array.isArray(pair) || String(pair[0]).toLowerCase() !== "set-cookie") continue;
      const parts = String(pair[1]).split(";");
      const cookiePair = (parts.shift() || "").trim();
      const equals = cookiePair.indexOf("=");
      if (equals <= 0) continue;
      const name = cookiePair.slice(0, equals).trim();
      const value = cookiePair.slice(equals + 1).trim();
      if (value === "") continue;

      let expires = 0;
      let cookiePath = "/";
      for (const attr of parts) {{
        const trimmed = attr.trim();
        const lower = trimmed.toLowerCase();
        if (lower.startsWith("expires=")) {{
          const ms = Date.parse(trimmed.slice(trimmed.indexOf("=") + 1));
          if (!Number.isNaN(ms)) expires = ms;
        }} else if (lower.startsWith("max-age=")) {{
          const seconds = Number(trimmed.slice(trimmed.indexOf("=") + 1));
          if (Number.isFinite(seconds)) expires = seconds <= 0 ? 1 : Date.now() + seconds * 1000;
        }} else if (lower.startsWith("path=")) {{
          cookiePath = trimmed.slice(trimmed.indexOf("=") + 1).trim() || "/";
        }}
      }}

      const index = entries.findIndex((entry) => entry.name === name);
      const entry = {{ name, value, expires, path: cookiePath }};
      if (index >= 0) entries[index] = entry;
      else entries.push(entry);
    }}

    jar[origin] = entries;
    saveCookieJar(jar);
  }}

  class RelayTransport {{
'''
if cookie_marker in text:
    if "const COOKIE_JAR_KEY" not in text:
        text = text.replace(cookie_marker, cookie_code, 1)
elif "const COOKIE_JAR_KEY" not in text:
    raise RuntimeError("Cookie jar patch point not found")

replace_once(
    '''        const bodyBytes = await serializeBody(body);
''',
    '''        const bodyBytes = await serializeBody(body);
        const cookieHeader = getCookieHeader(urlStr);
        if (cookieHeader) {{
          for (let i = 0; i < hdrList.length; i++) {{
            if (String(hdrList[i][0]).toLowerCase() === "cookie") {{
              hdrList.splice(i, 1);
              i--;
            }}
          }}
          hdrList.push(["Cookie", cookieHeader]);
        }}
''',
    "Cookie request forwarding",
    "const cookieHeader = getCookieHeader(urlStr);",
)
replace_once(
    '''        const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(rawBuf, 4, metaLen)));
''',
    '''        const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(rawBuf, 4, metaLen)));
        storeSetCookies(urlStr, meta.headers);
''',
    "Set-Cookie response handling",
    "storeSetCookies(urlStr, meta.headers);",
)

old_wisp_url = '''  function wispUrl() {{
    if (WISP_OVERRIDE) return WISP_OVERRIDE;
    return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/wisp/";
  }}
'''
new_wisp_url = '''  function wispUrls() {{
    const localUrl = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/wisp/";
    const preferredUrl = WISP_OVERRIDE || "wss://wisp.mercurywork.shop/";
    return [...new Set([preferredUrl, localUrl])];
  }}
'''
replace_once(old_wisp_url, new_wisp_url, "Wisp URL list", "function wispUrls()")

replace_once(
    '''    constructor() {{
      this.wispClient = null;
    }}
''',
    '''    constructor() {{
      this.wispClients = new Map();
    }}
''',
    "Wisp client cache",
    "this.wispClients = new Map();",
)
replace_once(
    '''    async ensureWisp() {{
      if (this.wispClient) return this.wispClient;
      const mod = await import("/scramjet/libcurl/index.mjs?v={LIBCURL_VERSION}");
      const Ctor = mod.default || mod.LibcurlClient || mod.LibcurlTransport?.LibcurlClient;
      const client = new Ctor({{ wisp: wispUrl() }});
      if (typeof client.init === "function") await client.init();
      this.wispClient = client;
      return client;
    }}
''',
    '''    async ensureWisp(wispUrl) {{
      if (this.wispClients.has(wispUrl)) return this.wispClients.get(wispUrl);
      const mod = await import("/scramjet/libcurl/index.mjs?v={LIBCURL_VERSION}");
      const Ctor = mod.default || mod.LibcurlClient || mod.LibcurlTransport?.LibcurlClient;
      const client = new Ctor({{ wisp: wispUrl }});
      if (typeof client.init === "function") await client.init();
      this.wispClients.set(wispUrl, client);
      return client;
    }}
''',
    "Wisp client initialization",
    "async ensureWisp(wispUrl)",
)
replace_once(
    '''      }} catch (_) {{
        const wisp = await this.ensureWisp();
        return await wisp.request(remote, method, body, hdrList, signal);
      }}
''',
    '''      }} catch (_) {{
        let lastError;
        for (const wispUrl of wispUrls()) {{
          try {{
            const wisp = await this.ensureWisp(wispUrl);
            return await wisp.request(remote, method, body, hdrList, signal);
          }} catch (error) {{
            lastError = error;
            this.wispClients.delete(wispUrl);
          }}
        }}
        throw lastError || new Error("すべてのWisp接続に失敗しました");
      }}
''',
    "Wisp HTTP fallback",
    "for (const wispUrl of wispUrls())",
)
replace_once(
    '''    connect(url, protocols, requestHeaders, onopen, onmessage, onclose, onerror) {{
      this.ensureWisp()
        .then((w) => w.connect(url, protocols, requestHeaders, onopen, onmessage, onclose, onerror))
        .catch((err) => onerror?.(err));
    }}
''',
    '''    connect(url, protocols, requestHeaders, onopen, onmessage, onclose, onerror) {{
      const candidates = wispUrls();
      const attempt = (index) => {{
        if (index >= candidates.length) {{
          onerror?.(new Error("すべてのWisp接続に失敗しました"));
          return;
        }}
        const wispUrl = candidates[index];
        let settled = false;
        const timeout = setTimeout(() => fail(new Error("Wisp接続タイムアウト")), 8000);
        const fail = (error) => {{
          if (settled) return;
          settled = true;
          clearTimeout(timeout);
          this.wispClients.delete(wispUrl);
          attempt(index + 1);
        }};
        this.ensureWisp(wispUrl).then((w) => {{
          try {{
            w.connect(url, protocols, requestHeaders, (...args) => {{
              if (settled) return;
              settled = true;
              clearTimeout(timeout);
              onopen?.(...args);
            }}, onmessage, onclose, fail);
          }} catch (error) {{
            fail(error);
          }}
        }}).catch(fail);
      }};
      attempt(0);
    }}
''',
    "Wisp WebSocket fallback",
    "const candidates = wispUrls();",
)

APP.write_text(text, encoding="utf-8")
print("Patched app.py for Service Worker control, cookie forwarding, and external/local Wisp failover")
