from pathlib import Path


APP = Path(__file__).with_name("app.py")
text = APP.read_text(encoding="utf-8")

old_rev = 'RUNTIME_REV = "20261119-url-proxy-v1"'
new_rev = 'RUNTIME_REV = "20261119-url-proxy-v2"'
if old_rev not in text:
    raise RuntimeError("RUNTIME_REV not found")
text = text.replace(old_rev, new_rev, 1)

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
if old_register not in text:
    raise RuntimeError("Service Worker registration block not found")
text = text.replace(old_register, new_register, 1)

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
      let value = cookiePair.slice(equals + 1).trim();
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
if cookie_marker not in text:
    raise RuntimeError("RelayTransport marker not found")
text = text.replace(cookie_marker, cookie_code, 1)

old_body = '''        const bodyBytes = await serializeBody(body);
'''
new_body = '''        const bodyBytes = await serializeBody(body);
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
'''
if old_body not in text:
    raise RuntimeError("Relay body marker not found")
text = text.replace(old_body, new_body, 1)

old_meta = '''        const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(rawBuf, 4, metaLen)));
'''
new_meta = '''        const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(rawBuf, 4, metaLen)));
        storeSetCookies(urlStr, meta.headers);
'''
if old_meta not in text:
    raise RuntimeError("Relay meta marker not found")
text = text.replace(old_meta, new_meta, 1)

APP.write_text(text, encoding="utf-8")
print("Patched app.py for Service Worker control and per-origin cookie forwarding")
