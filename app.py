from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import re
import socket
import struct
import time
from urllib.parse import quote, unquote, urlparse, urlunparse

for _np_var in ("NO_PROXY", "no_proxy", "GLOBAL_AGENT_NO_PROXY"):
    _np_val = os.environ.get(_np_var, "")
    if _np_val:
        os.environ[_np_var] = ",".join(
            p.strip() for p in _np_val.split(",") if p.strip() and not ("::" in p or "[" in p or "]" in p)
        )

import httpx
from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response

try:
    from curl_cffi.const import CurlOpt
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
except Exception:
    CurlOpt = None
    CurlAsyncSession = None


def _scrub_curl_cookies(curl) -> None:
    if curl is None or CurlOpt is None:
        return
    try:
        curl.setopt(CurlOpt.COOKIELIST, "ALL")
        curl.setopt(CurlOpt.COOKIELIST, "SESS")
        if not (os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")):
            ipresolve = getattr(CurlOpt, "IPRESOLVE", None)
            if ipresolve is not None:
                curl.setopt(ipresolve, 1)
    except Exception:
        pass


if CurlAsyncSession is not None:

    class StatelessCurlSession(CurlAsyncSession):
        async def pop_curl(self):
            curl = await super().pop_curl()
            _scrub_curl_cookies(curl)
            return curl

        def push_curl(self, curl) -> None:
            _scrub_curl_cookies(curl)
            super().push_curl(curl)

        async def request(self, method: str, url: str, *args, **kwargs):
            kwargs["discard_cookies"] = True
            try:
                self._cookies.clear()
            except Exception:
                pass
            try:
                return await super().request(method, url, *args, **kwargs)
            finally:
                try:
                    self._cookies.clear()
                except Exception:
                    pass

else:
    StatelessCurlSession = None


SCRAMJET_VERSION = "2.0.67-alpha.2"
CONTROLLER_VERSION = "0.0.14"
UTILS_VERSION = "0.0.3"
LIBCURL_VERSION = "2.0.5"
EPOXY_VERSION = "3.0.1"
RUNTIME_REV = "20261119-url-proxy-v1"

SCRAMJET_WISP_URL = os.getenv("SCRAMJET_WISP_URL", "").strip()
DEFAULT_URL = os.getenv("DEFAULT_PROXY_URL", "https://example.com").strip()

router = APIRouter()

ASSET_URLS = {
    "scramjet.js": [
        f"https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet@{SCRAMJET_VERSION}/dist/scramjet.js",
        f"https://unpkg.com/@mercuryworkshop/scramjet@{SCRAMJET_VERSION}/dist/scramjet.js",
    ],
    "scramjet.wasm": [
        f"https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet@{SCRAMJET_VERSION}/dist/scramjet.wasm",
        f"https://unpkg.com/@mercuryworkshop/scramjet@{SCRAMJET_VERSION}/dist/scramjet.wasm",
    ],
    "scramjet-utils.js": [
        f"https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet-utils@{UTILS_VERSION}/dist/scramjet-utils.js",
        f"https://unpkg.com/@mercuryworkshop/scramjet-utils@{UTILS_VERSION}/dist/scramjet-utils.js",
    ],
    "controller.api.js": [
        f"https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet-controller@{CONTROLLER_VERSION}/dist/controller.api.js",
        f"https://unpkg.com/@mercuryworkshop/scramjet-controller@{CONTROLLER_VERSION}/dist/controller.api.js",
    ],
    "controller.inject.js": [
        f"https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet-controller@{CONTROLLER_VERSION}/dist/controller.inject.js",
        f"https://unpkg.com/@mercuryworkshop/scramjet-controller@{CONTROLLER_VERSION}/dist/controller.inject.js",
    ],
    "controller.sw.js": [
        f"https://cdn.jsdelivr.net/npm/@mercuryworkshop/scramjet-controller@{CONTROLLER_VERSION}/dist/controller.sw.js",
        f"https://unpkg.com/@mercuryworkshop/scramjet-controller@{CONTROLLER_VERSION}/dist/controller.sw.js",
    ],
    "epoxy-client.js": [
        f"https://cdn.jsdelivr.net/npm/@mercuryworkshop/epoxy-transport@{EPOXY_VERSION}/dist/index.js",
        f"https://unpkg.com/@mercuryworkshop/epoxy-transport@{EPOXY_VERSION}/dist/index.js",
    ],
}

TRANSPORT_ASSET_URLS = {
    "libcurl": f"https://cdn.jsdelivr.net/npm/@mercuryworkshop/libcurl-transport@{LIBCURL_VERSION}/dist/",
    "epoxy": f"https://cdn.jsdelivr.net/npm/@mercuryworkshop/epoxy-transport@{EPOXY_VERSION}/dist/",
}

_ASSET_CACHE: dict[str, tuple[bytes, str]] = {}
_ASSET_LOCKS = {name: asyncio.Lock() for name in ASSET_URLS}
_TRANSPORT_CACHE: dict[str, tuple[bytes, str]] = {}
_TRANSPORT_LOCKS: dict[str, asyncio.Lock] = {}
_HTTP_CLIENT: httpx.AsyncClient | None = None
_HTTP_CLIENT_LOCK = asyncio.Lock()
_CURL_SESSION: StatelessCurlSession | None = None
_CURL_SESSION_LOCK = asyncio.Lock()
_WASM_JS_CACHE: bytes = b""


def _transport_media(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".mjs") or lower.endswith(".js"):
        return "application/javascript"
    if lower.endswith(".wasm"):
        return "application/wasm"
    if lower.endswith(".map"):
        return "application/json"
    return "application/octet-stream"


async def _get_http_client() -> httpx.AsyncClient:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is not None and not _HTTP_CLIENT.is_closed:
        return _HTTP_CLIENT
    async with _HTTP_CLIENT_LOCK:
        if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
            limits = httpx.Limits(max_connections=200, max_keepalive_connections=100, keepalive_expiry=60.0)
            timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=10.0)
            _HTTP_CLIENT = httpx.AsyncClient(follow_redirects=False, limits=limits, timeout=timeout)
    return _HTTP_CLIENT


async def _get_curl_session():
    global _CURL_SESSION
    if StatelessCurlSession is None:
        return None
    if _CURL_SESSION is not None:
        return _CURL_SESSION
    async with _CURL_SESSION_LOCK:
        if _CURL_SESSION is None:
            _CURL_SESSION = StatelessCurlSession(
                impersonate="chrome131",
                allow_redirects=False,
                timeout=20,
                max_clients=32,
            )
    return _CURL_SESSION


async def _fetch_scramjet_asset(asset_name: str) -> Response:
    if asset_name not in ASSET_URLS:
        return Response(status_code=404)
    if asset_name in _ASSET_CACHE:
        body, media = _ASSET_CACHE[asset_name]
        return Response(body, media_type=media, headers={"Cache-Control": "public, max-age=86400, immutable"})
    async with _ASSET_LOCKS[asset_name]:
        if asset_name in _ASSET_CACHE:
            body, media = _ASSET_CACHE[asset_name]
            return Response(body, media_type=media, headers={"Cache-Control": "public, max-age=86400, immutable"})
        media = "application/wasm" if asset_name.endswith(".wasm") else "application/javascript"
        client = await _get_http_client()
        for source in ASSET_URLS[asset_name]:
            try:
                response = await client.get(source, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
                response.raise_for_status()
                body = response.content
                if not body:
                    continue
                if asset_name.endswith(".js") and b"<!doctype html" in body[:1024].lower():
                    continue
                _ASSET_CACHE[asset_name] = (body, media)
                return Response(body, media_type=media, headers={"Cache-Control": "public, max-age=86400, immutable"})
            except Exception:
                continue
        return Response(status_code=502, content=f"asset unavailable: {asset_name}")


@router.get("/scramjet/{asset_name}", include_in_schema=False)
async def scramjet_asset(asset_name: str):
    return await _fetch_scramjet_asset(asset_name)


@router.get("/scramjet/{transport}/{asset_path:path}", include_in_schema=False)
async def scramjet_transport_asset(transport: str, asset_path: str):
    base = TRANSPORT_ASSET_URLS.get(transport)
    if not base or not asset_path or asset_path.startswith(".") or ".." in asset_path.split("/"):
        return Response(status_code=404)
    cache_key = f"{transport}/{asset_path}"
    if cache_key in _TRANSPORT_CACHE:
        body, media = _TRANSPORT_CACHE[cache_key]
        return Response(body, media_type=media, headers={"Cache-Control": "public, max-age=86400, immutable"})
    lock = _TRANSPORT_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        if cache_key in _TRANSPORT_CACHE:
            body, media = _TRANSPORT_CACHE[cache_key]
            return Response(body, media_type=media, headers={"Cache-Control": "public, max-age=86400, immutable"})
        try:
            client = await _get_http_client()
            upstream = await client.get(
                base + quote(asset_path, safe="/._-"),
                headers={"User-Agent": "Mozilla/5.0"},
                follow_redirects=True,
            )
            upstream.raise_for_status()
            body = upstream.content
            if not body:
                raise RuntimeError("empty transport asset")
            media = _transport_media(asset_path)
            _TRANSPORT_CACHE[cache_key] = (body, media)
            return Response(body, media_type=media, headers={"Cache-Control": "public, max-age=86400, immutable"})
        except Exception:
            return Response(status_code=502, content=f"transport asset unavailable: {cache_key}")


def _patch_controller_api(source: bytes) -> bytes:
    text = source.decode("utf-8")
    if "__sj_buffered_body" in text:
        return source

    old_remote = (
        "let s=await this.transport.request(new URL(e),t,r,o,void 0);return[s,[s.body]]"
    )
    new_remote = (
        "let s=await this.transport.request(new URL(e),t,r,o,void 0);"
        "if(s.body instanceof ReadableStream){s.body=await new Response(s.body).arrayBuffer()}"
        "return[s,s.body instanceof ArrayBuffer?[s.body]:[]]"
    )
    text = text.replace(old_remote, new_remote)

    transfer_re = re.compile(
        r"(?P<var>[A-Za-z_$][\w$]*)\.body\s*instanceof\s*ReadableStream\s*"
        r"\|\|\s*(?P=var)\.body\s*instanceof\s*ArrayBuffer\s*"
        r"\?\s*\[\s*(?P=var)\.body\s*\]\s*:\s*\[\s*\]"
    )
    m = transfer_re.search(text)
    if not m:
        return text.encode("utf-8")
    var_name = m.group("var")
    returns = list(re.finditer(r"return\s*\[", text[: m.start()]))
    if not returns:
        return text.encode("utf-8")
    return_pos = returns[-1].start()
    body_match = re.search(rf"body\s*:\s*{re.escape(var_name)}\.body", text[return_pos : m.start()])
    if not body_match:
        return text.encode("utf-8")
    body_start = return_pos + body_match.start()
    body_end = return_pos + body_match.end()
    body_name = "__sj_buffered_body"

    inject = (
        f"const {body_name} = {var_name}.body instanceof ReadableStream ? "
        f"await new Response({var_name}.body).arrayBuffer() : {var_name}.body;\n"
    )
    s2 = text[:return_pos] + inject + text[return_pos:]
    shift = len(inject)
    s2 = s2[: body_start + shift] + "body: " + body_name + s2[body_end + shift :]
    tm = re.compile(
        rf"{re.escape(var_name)}\.body\s*instanceof\s*ReadableStream\s*"
        rf"\|\|\s*{re.escape(var_name)}\.body\s*instanceof\s*ArrayBuffer\s*"
        rf"\?\s*\[\s*{re.escape(var_name)}\.body\s*\]\s*:\s*\[\s*\]"
    ).search(s2, body_start + shift)
    if not tm:
        return text.encode("utf-8")
    replacement = f"{body_name} instanceof ArrayBuffer ? [{body_name}] : []"
    s2 = s2[: tm.start()] + replacement + s2[tm.end() :]
    return s2.encode("utf-8")


def _patch_controller_sw(source: bytes) -> bytes:
    text = source.decode("utf-8")
    if "_sjBodyVal" in text:
        return source
    old_call = "s=[...e.request.headers],n=await r.rpc.call("
    new_call = (
        "s=[...e.request.headers],"
        '_sjBody=(e.request.method!=="GET"&&e.request.method!=="HEAD")'
        "?await e.request.arrayBuffer().catch(()=>null):null,"
        "_sjBodyVal=(_sjBody&&_sjBody.byteLength>0)?_sjBody:null,"
        "n=await r.rpc.call("
    )
    if old_call in text:
        text = text.replace(old_call, new_call, 1)
        text = text.replace("body:e.request.body,", "body:_sjBodyVal,", 1)
        text = text.replace(
            "e.request.body instanceof ReadableStream||e.request.body instanceof ArrayBuffer?[e.request.body]:void 0",
            "_sjBodyVal instanceof ArrayBuffer?[_sjBodyVal]:void 0",
            1,
        )
    return text.encode("utf-8")


def _patch_controller_inject(source: bytes) -> bytes:
    text = source.decode("utf-8")
    old_req = (
        'async request(e,t,o,r,i){return await this.rpc.call("request",{remote:e.href,method:t,body:o,headers:r})}'
    )
    new_req = (
        "async request(e,t,o,r,i){"
        "if(o instanceof ReadableStream||o instanceof Blob){o=await new Response(o).arrayBuffer()}"
        'else if("undefined"!=typeof URLSearchParams&&o instanceof URLSearchParams){o=o.toString()}'
        'return await this.rpc.call("request",{remote:e.href,method:t,body:o,headers:r})}'
    )
    text = text.replace(old_req, new_req, 1)
    return text.encode("utf-8")


def _patch_scramjet_js(source: bytes) -> bytes:
    text = source.decode("utf-8")
    text = text.replace("(?i:url)", "(?:[uU][rR][lL])")
    old_code = "let r=e.box.histories.get(t.this),s=(0,n.Qf)(t.args[2]);"
    new_code = "let r=e.box.histories.get(t.this),s=null!=t.args[2]?(0,n.Qf)(t.args[2]):null;"
    if old_code in text:
        text = text.replace(old_code, new_code, 1)
    return text.encode("utf-8")


@router.get("/proxy/runtime/scramjet.js", include_in_schema=False)
async def runtime_scramjet_js():
    resp = await _fetch_scramjet_asset("scramjet.js")
    if getattr(resp, "status_code", 500) != 200 or not getattr(resp, "body", b""):
        return resp
    return Response(_patch_scramjet_js(bytes(resp.body)), media_type="application/javascript")


@router.get("/proxy/runtime/controller.api.js", include_in_schema=False)
async def runtime_controller_api():
    resp = await _fetch_scramjet_asset("controller.api.js")
    if getattr(resp, "status_code", 500) != 200 or not getattr(resp, "body", b""):
        return resp
    return Response(_patch_controller_api(bytes(resp.body)), media_type="application/javascript")


@router.get("/proxy/runtime/controller.sw.js", include_in_schema=False)
async def runtime_controller_sw():
    resp = await _fetch_scramjet_asset("controller.sw.js")
    if getattr(resp, "status_code", 500) != 200 or not getattr(resp, "body", b""):
        return resp
    return Response(_patch_controller_sw(bytes(resp.body)), media_type="application/javascript")


@router.get("/proxy/runtime/controller.inject.js", include_in_schema=False)
async def runtime_controller_inject():
    resp = await _fetch_scramjet_asset("controller.inject.js")
    if getattr(resp, "status_code", 500) != 200 or not getattr(resp, "body", b""):
        return resp
    return Response(_patch_controller_inject(bytes(resp.body)), media_type="application/javascript")


@router.api_route("/proxy/sw.js", methods=["GET", "HEAD"], include_in_schema=False)
async def proxy_sw(request: Request) -> Response:
    code = f"""
importScripts('/proxy/runtime/controller.sw.js?v={RUNTIME_REV}');
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {{
  try {{
    if (typeof $scramjetController !== 'undefined' && $scramjetController.shouldRoute(event)) {{
      event.respondWith($scramjetController.route(event));
    }}
  }} catch (error) {{
    console.error('[Proxy SW]', error);
  }}
}});
self.addEventListener('message', event => {{
  if (event.data === 'claim') {{
    try {{ event.waitUntil(self.clients.claim()); }} catch {{}}
  }}
}});
"""
    return Response(
        code,
        media_type="application/javascript",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Service-Worker-Allowed": "/proxy/",
        },
    )


async def _serve_virtual_wasm_js() -> Response:
    global _WASM_JS_CACHE
    if not _WASM_JS_CACHE:
        try:
            resp = await _fetch_scramjet_asset("scramjet.wasm")
            raw_wasm = bytes(getattr(resp, "body", b"") or b"")
            if raw_wasm:
                b64 = base64.b64encode(raw_wasm).decode("ascii")
                _WASM_JS_CACHE = f"self.WASM = '{b64}';".encode("ascii")
        except Exception:
            pass
    return Response(_WASM_JS_CACHE or b"", media_type="application/javascript")


@router.api_route("/proxy/~/sj/{rest:path}", methods=["GET", "HEAD", "POST", "OPTIONS"], include_in_schema=False)
async def sj_virtual_fallback(rest: str, request: Request):
    if request.method == "OPTIONS":
        return Response(status_code=204, headers={"Access-Control-Allow-Origin": "*"})
    if rest.endswith("scramjet.wasm.js"):
        return await _serve_virtual_wasm_js()
    return Response(b"Service Worker route required", status_code=404)


_PUBLIC_HOST_CACHE: dict[tuple[str, int], float] = {}


async def _ensure_public_host(hostname: str, port: int) -> None:
    host = (hostname or "").strip().lower().rstrip(".")
    if not host or host in ("localhost", "localhost.localdomain") or host.endswith((".local", ".internal", ".localhost")):
        raise PermissionError(f"blocked host {host}")
    try:
        ip_lit = ipaddress.ip_address(host)
        if ip_lit.is_private or ip_lit.is_loopback or ip_lit.is_link_local or ip_lit.is_reserved or ip_lit.is_multicast:
            raise PermissionError(f"non-public literal address {host}")
        return
    except ValueError:
        pass
    if "." not in host:
        raise PermissionError(f"blocked single-label host {host}")

    key = (host, port)
    now = time.time()
    cached = _PUBLIC_HOST_CACHE.get(key)
    if cached and (now - cached) < 300.0:
        return

    loop = asyncio.get_running_loop()
    infos = await loop.run_in_executor(None, lambda: socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM))
    for _, _, _, _, sockaddr in infos:
        ip_text = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise PermissionError(f"non-public address {ip_text}")
    _PUBLIC_HOST_CACHE[key] = now


_RELAY_STRIP_REQ_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "accept-encoding",
}

_RELAY_STRIP_RESP_HEADERS = {
    "content-encoding",
    "transfer-encoding",
    "content-length",
    "connection",
    "keep-alive",
    "alt-svc",
    "strict-transport-security",
    "content-security-policy",
    "content-security-policy-report-only",
    "x-frame-options",
    "cross-origin-opener-policy",
    "cross-origin-embedder-policy",
    "cross-origin-resource-policy",
}


def _pack_relay_response(status: int, status_text: str, headers_list: list[list[str]], body: bytes) -> Response:
    meta_bytes = json.dumps(
        {"status": status, "statusText": status_text, "headers": headers_list},
        ensure_ascii=False,
    ).encode("utf-8")
    payload = struct.pack(">I", len(meta_bytes)) + meta_bytes + body
    return Response(
        payload,
        media_type="application/octet-stream",
        headers={"Cache-Control": "private, no-store, no-cache, must-revalidate"},
    )


@router.post("/proxy/runtime/http-relay", include_in_schema=False)
async def proxy_http_relay(request: Request):
    raw = await request.body()
    if len(raw) < 4:
        return Response(b"invalid frame", status_code=400)
    meta_len = struct.unpack(">I", raw[:4])[0]
    if len(raw) < 4 + meta_len:
        return Response(b"truncated frame", status_code=400)
    try:
        meta = json.loads(raw[4 : 4 + meta_len].decode("utf-8"))
    except Exception:
        return Response(b"bad json", status_code=400)

    req_body = raw[4 + meta_len :]
    url = str(meta.get("url") or "").strip()
    method = str(meta.get("method") or "GET").upper().strip()
    raw_headers = meta.get("headers") or []

    try:
        parsed = urlparse(url)
    except Exception:
        return Response(b"bad url", status_code=400)

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return Response(b"unsupported scheme", status_code=400)

    host = parsed.hostname.lower().rstrip(".")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        await _ensure_public_host(host, port)
    except Exception:
        return Response(b"blocked host", status_code=403)

    header_dict: dict[str, str] = {}
    cookie_parts: list[str] = []
    items = raw_headers if isinstance(raw_headers, list) else (list(raw_headers.items()) if isinstance(raw_headers, dict) else [])
    for item in items:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        k = str(item[0]).strip()
        v = str(item[1]).strip()
        kl = k.lower()
        if not k or kl in _RELAY_STRIP_REQ_HEADERS or kl.startswith(":"):
            continue
        if kl == "cookie":
            cookie_parts.append(v)
        else:
            header_dict[k] = v
    if cookie_parts:
        header_dict["Cookie"] = "; ".join(cookie_parts)
    header_dict.setdefault(
        "User-Agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    )

    try:
        curl_session = await _get_curl_session()
        if curl_session is not None:
            upstream = await curl_session.request(
                method=method,
                url=url,
                headers=header_dict,
                data=req_body if req_body else None,
                allow_redirects=False,
                timeout=20,
            )
            status = int(upstream.status_code)
            status_text = str(getattr(upstream, "reason", "") or "OK")
            resp_body = bytes(upstream.content or b"")
            raw_multi = upstream.headers.multi_items()
        else:
            client = await _get_http_client()
            upstream = await client.request(
                method=method,
                url=url,
                headers=header_dict,
                content=req_body if req_body else None,
                follow_redirects=False,
            )
            status = int(upstream.status_code)
            status_text = str(upstream.reason_phrase or "OK")
            resp_body = bytes(upstream.content or b"")
            raw_multi = list(upstream.headers.multi_items())
    except Exception as exc:
        err_msg = f"upstream error: {exc}".encode("utf-8", "ignore")
        return _pack_relay_response(502, "Bad Gateway", [["content-type", "text/plain; charset=utf-8"]], err_msg)

    out_headers: list[list[str]] = []
    for k, v in raw_multi:
        kl = k.lower()
        if kl in _RELAY_STRIP_RESP_HEADERS or kl.startswith(":"):
            continue
        out_headers.append([k, v])

    return _pack_relay_response(status, status_text, out_headers, resp_body)


WISP_BUFFER = 2048
WISP_REPLENISH = 1024
WISP_MAX_STREAMS = 128
WISP_CONNECT_TIMEOUT = 12.0
WISP_IDLE_TIMEOUT = 900.0


def _wisp_packet(kind: int, stream_id: int, payload: bytes = b"") -> bytes:
    return struct.pack("<BI", kind, stream_id) + payload


def _wisp_info() -> bytes:
    return _wisp_packet(0x05, 0, bytes((2, 1)))


def _wisp_continue(stream_id: int, remaining_packets: int) -> bytes:
    return _wisp_packet(0x03, stream_id, struct.pack("<I", max(1, remaining_packets)))


def _wisp_close(stream_id: int, reason: int) -> bytes:
    return _wisp_packet(0x04, stream_id, bytes((reason & 0xFF,)))


def _wisp_extensions(payload: bytes) -> set[int]:
    extensions: set[int] = set()
    if len(payload) < 2:
        return extensions
    pos = 2
    while pos + 5 <= len(payload):
        ext_id = payload[pos]
        size = struct.unpack_from("<I", payload, pos + 1)[0]
        pos += 5
        if size > len(payload) - pos:
            break
        extensions.add(ext_id)
        pos += size
    return extensions


async def _public_tcp(hostname: str, port: int):
    loop = asyncio.get_running_loop()
    infos = await loop.run_in_executor(None, lambda: socket.getaddrinfo(hostname, port, 0, socket.SOCK_STREAM))
    for family, _, proto, _, sockaddr in infos:
        ip_text = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_text)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                continue
        except ValueError:
            continue
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip_text, sockaddr[1], family=family, proto=proto),
            WISP_CONNECT_TIMEOUT,
        )
        return reader, writer
    raise OSError("no public destination")


@router.websocket("/wisp/")
async def wisp_endpoint(websocket: WebSocket):
    requested = list(websocket.scope.get("subprotocols", []) or [])
    v2 = bool(requested)
    try:
        await websocket.accept(subprotocol=requested[0] if requested else None)
    except Exception:
        return

    streams: dict[int, asyncio.StreamWriter] = {}
    tasks: dict[int, asyncio.Task] = {}
    pending_queues: dict[int, list[bytes]] = {}
    extensions: set[int] = set()
    alive = True

    async def send_packet(data: bytes) -> bool:
        nonlocal alive
        if not alive:
            return False
        try:
            await websocket.send_bytes(data)
            return True
        except Exception:
            alive = False
            return False

    async def close_stream(stream_id: int, reason: int = 2, notify: bool = True):
        pending_queues.pop(stream_id, None)
        writer = streams.pop(stream_id, None)
        task = tasks.pop(stream_id, None)
        if task and task is not asyncio.current_task():
            task.cancel()
        if writer:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        if notify:
            await send_packet(_wisp_close(stream_id, reason))

    async def pump(stream_id: int, reader: asyncio.StreamReader):
        try:
            while alive:
                data = await asyncio.wait_for(reader.read(64 * 1024), WISP_IDLE_TIMEOUT)
                if not data:
                    await send_packet(_wisp_close(stream_id, 2))
                    break
                if not await send_packet(_wisp_packet(0x02, stream_id, data)):
                    break
        except Exception:
            await send_packet(_wisp_close(stream_id, 0x03))
        finally:
            pending_queues.pop(stream_id, None)
            writer = streams.pop(stream_id, None)
            tasks.pop(stream_id, None)
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    try:
        if v2:
            await send_packet(_wisp_info())
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                raw = message.get("bytes") or b""
                if len(raw) < 5:
                    continue
                kind = raw[0]
                stream_id = struct.unpack_from("<I", raw, 1)[0]
                payload = raw[5:]
                if stream_id != 0:
                    await send_packet(_wisp_close(stream_id, 0x41))
                    continue
                if kind == 0x05:
                    if len(payload) < 2 or payload[0] != 2:
                        await send_packet(_wisp_close(0, 0x04))
                        return
                    extensions = _wisp_extensions(payload)
                    break
                if kind == 0x03:
                    v2 = False
                    break
                if kind == 0x04:
                    return
                await send_packet(_wisp_close(0, 0x04))
                return

        await send_packet(_wisp_continue(0, WISP_BUFFER))
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            raw = message.get("bytes")
            if not raw or len(raw) < 5:
                continue
            kind = raw[0]
            stream_id = struct.unpack_from("<I", raw, 1)[0]
            payload = raw[5:]
            if kind == 0x01:
                if (
                    stream_id == 0
                    or stream_id in streams
                    or stream_id in pending_queues
                    or len(streams) + len(pending_queues) >= WISP_MAX_STREAMS
                    or len(payload) < 3
                ):
                    await send_packet(_wisp_close(stream_id, 0x41))
                    continue
                stream_type = payload[0]
                port = struct.unpack_from("<H", payload, 1)[0]
                try:
                    host = payload[3:].decode("utf-8", "strict").strip().rstrip(".").lower()
                except UnicodeDecodeError:
                    await send_packet(_wisp_close(stream_id, 0x41))
                    continue
                if stream_type != 1 or not host or len(host) > 253 or not (1 <= port <= 65535):
                    await send_packet(_wisp_close(stream_id, 0x41))
                    continue

                pending_queues[stream_id] = []

                async def open_one(sid=stream_id, hostname=host, dst_port=port):
                    try:
                        reader, writer = await _public_tcp(hostname, dst_port)
                        buf = pending_queues.pop(sid, None)
                        if buf is None:
                            writer.close()
                            await writer.wait_closed()
                            return
                        streams[sid] = writer
                        if v2 and 0x05 in extensions:
                            await send_packet(_wisp_continue(sid, WISP_BUFFER))
                        if buf:
                            for chunk in buf:
                                writer.write(chunk)
                            await writer.drain()
                            await send_packet(_wisp_continue(sid, WISP_REPLENISH))
                        tasks[sid] = asyncio.create_task(pump(sid, reader))
                    except Exception:
                        pending_queues.pop(sid, None)
                        await send_packet(_wisp_close(sid, 0x03))

                tasks[stream_id] = asyncio.create_task(open_one())
            elif kind == 0x02:
                writer = streams.get(stream_id)
                if writer is not None:
                    try:
                        if payload:
                            writer.write(payload)
                            await writer.drain()
                            await send_packet(_wisp_continue(stream_id, WISP_REPLENISH))
                    except Exception:
                        await close_stream(stream_id, 0x03)
                elif stream_id in pending_queues:
                    if payload:
                        pending_queues[stream_id].append(payload)
            elif kind == 0x04:
                await close_stream(stream_id, 0x02)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        pending_queues.clear()
        for task in list(tasks.values()):
            if task is not asyncio.current_task():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        for writer in list(streams.values()):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass


def _normalize_target_url(raw: str) -> str:
    val = (raw or "").strip()
    if len(val) > 2048:
        val = val[:2048]
    if not val:
        return DEFAULT_URL
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", val):
        val = "https://" + val
    try:
        p = urlparse(val)
        if p.scheme in ("http", "https") and p.hostname:
            return urlunparse((p.scheme.lower(), p.netloc, p.path or "/", p.params, p.query, p.fragment))
    except Exception:
        pass
    return DEFAULT_URL


def _js_literal(value: str) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _shell_html(initial_url: str) -> str:
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Scramjet URL Proxy</title>
<style>
*{{box-sizing:border-box}}
html,body{{margin:0;width:100%;height:100%;display:flex;flex-direction:column;background:#111;color:#eee;font-family:system-ui,-apple-system,sans-serif}}
.bar{{display:flex;gap:8px;padding:10px;background:#1a1a1a;border-bottom:1px solid #2e2e2e}}
.bar input{{flex:1;padding:9px 12px;border:1px solid #3c3c3c;border-radius:6px;background:#0d0d0d;color:#fff;font-size:14px}}
.bar button{{padding:9px 16px;border:0;border-radius:6px;background:#2b6cb0;color:#fff;font-size:14px;cursor:pointer}}
.bar button:hover{{background:#3182ce}}
.status{{padding:4px 12px;font-size:12px;color:#a0aec0;background:#151515;border-bottom:1px solid #242424}}
#frame{{flex:1;width:100%;height:100%;border:0;background:#fff}}
</style>
</head>
<body>
<form class="bar" id="url-form">
  <input id="url-input" type="text" value="{_normalize_target_url(initial_url)}" placeholder="https://example.com" required />
  <button type="submit">プロキシ表示</button>
</form>
<div class="status" id="status">初期化中...</div>
<iframe id="frame" allow="autoplay; clipboard-read; clipboard-write; encrypted-media; fullscreen"></iframe>

<script src="/proxy/runtime/scramjet.js?v={RUNTIME_REV}"></script>
<script src="/proxy/runtime/controller.api.js?v={RUNTIME_REV}"></script>
<script src="/scramjet/scramjet-utils.js?v={UTILS_VERSION}"></script>
<script>
(() => {{
  "use strict";
  const INITIAL_URL = {_js_literal(_normalize_target_url(initial_url))};
  const WISP_OVERRIDE = {_js_literal(SCRAMJET_WISP_URL)};
  const SW_URL = "/proxy/sw.js?v={RUNTIME_REV}";
  const SW_SCOPE = "/proxy/";
  const PREFIX = "/proxy/~/sj/";
  const RELAY_ENDPOINT = "/proxy/runtime/http-relay";

  const formEl = document.getElementById("url-form");
  const inputEl = document.getElementById("url-input");
  const statusEl = document.getElementById("status");
  const frameEl = document.getElementById("frame");

  let sjFrame = null;

  function setStatus(msg) {{
    statusEl.textContent = msg;
  }}

  function normalizeUrl(raw) {{
    const s = String(raw || "").trim();
    if (!s) return INITIAL_URL;
    const withScheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(s) ? s : "https://" + s;
    try {{
      const u = new URL(withScheme);
      if (u.protocol === "http:" || u.protocol === "https:") return u.href;
    }} catch (_) {{}}
    return INITIAL_URL;
  }}

  function wispUrl() {{
    if (WISP_OVERRIDE) return WISP_OVERRIDE;
    return (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/wisp/";
  }}

  async function serializeBody(body) {{
    if (!body) return new Uint8Array(0);
    if (body instanceof Uint8Array) return body;
    if (body instanceof ArrayBuffer) return new Uint8Array(body);
    if (ArrayBuffer.isView(body)) return new Uint8Array(body.buffer, body.byteOffset, body.byteLength);
    if (typeof body === "string") return new TextEncoder().encode(body);
    if (body instanceof Blob) return new Uint8Array(await body.arrayBuffer());
    if (body instanceof ReadableStream) return new Uint8Array(await new Response(body).arrayBuffer());
    return new TextEncoder().encode(String(body));
  }}

  class RelayTransport {{
    constructor() {{
      this.wispClient = null;
    }}
    async init() {{}}
    async ensureWisp() {{
      if (this.wispClient) return this.wispClient;
      const mod = await import("/scramjet/libcurl/index.mjs?v={LIBCURL_VERSION}");
      const Ctor = mod.default || mod.LibcurlClient || mod.LibcurlTransport?.LibcurlClient;
      const client = new Ctor({{ wisp: wispUrl() }});
      if (typeof client.init === "function") await client.init();
      this.wispClient = client;
      return client;
    }}
    async request(remote, method, body, headers, signal) {{
      const urlStr = remote instanceof URL ? remote.href : String(remote || "");
      const m = String(method || "GET").toUpperCase();
      const hdrList = Array.isArray(headers)
        ? headers.map(([k, v]) => [String(k), String(v)])
        : (headers && typeof headers === "object" ? Object.entries(headers).map(([k, v]) => [String(k), String(v)]) : []);
      try {{
        const bodyBytes = await serializeBody(body);
        const metaBytes = new TextEncoder().encode(JSON.stringify({{ url: urlStr, method: m, headers: hdrList }}));
        const buf = new Uint8Array(4 + metaBytes.byteLength + bodyBytes.byteLength);
        new DataView(buf.buffer).setUint32(0, metaBytes.byteLength, false);
        buf.set(metaBytes, 4);
        buf.set(bodyBytes, 4 + metaBytes.byteLength);

        const resp = await fetch(RELAY_ENDPOINT, {{
          method: "POST",
          credentials: "omit",
          cache: "no-store",
          headers: {{ "content-type": "application/octet-stream" }},
          body: buf,
          signal,
        }});
        if (!resp.ok) throw new Error("relay status " + resp.status);
        const rawBuf = await resp.arrayBuffer();
        const metaLen = new DataView(rawBuf).getUint32(0, false);
        const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(rawBuf, 4, metaLen)));
        const payload = new Uint8Array(rawBuf, 4 + metaLen);
        const respBody = (m === "HEAD" || meta.status === 204 || meta.status === 304)
          ? new ArrayBuffer(0)
          : payload.buffer.slice(payload.byteOffset, payload.byteOffset + payload.byteLength);
        return {{
          body: respBody,
          headers: Array.isArray(meta.headers) ? meta.headers : [],
          status: Number(meta.status || 200),
          statusText: String(meta.statusText || "OK"),
        }};
      }} catch (_) {{
        const wisp = await this.ensureWisp();
        return await wisp.request(remote, method, body, hdrList, signal);
      }}
    }}
    connect(url, protocols, requestHeaders, onopen, onmessage, onclose, onerror) {{
      this.ensureWisp()
        .then((w) => w.connect(url, protocols, requestHeaders, onopen, onmessage, onclose, onerror))
        .catch((err) => onerror?.(err));
    }}
  }}

  async function init() {{
    if (!("serviceWorker" in navigator)) {{
      setStatus("このブラウザは Service Worker に非対応です");
      return;
    }}
    await navigator.serviceWorker.register(SW_URL, {{ scope: SW_SCOPE, updateViaCache: "none" }});
    if (!navigator.serviceWorker.controller) {{
      await new Promise((resolve) => {{
        const onChange = () => {{
          navigator.serviceWorker.removeEventListener("controllerchange", onChange);
          resolve();
        }};
        navigator.serviceWorker.addEventListener("controllerchange", onChange);
      }});
    }}

    const Controller = globalThis.$scramjetController?.Controller;
    const transport = new RelayTransport();
    await transport.init();

    const controller = new Controller({{
      serviceworker: navigator.serviceWorker.controller,
      transport,
      config: {{
        prefix: PREFIX,
        scramjetPath: "/proxy/runtime/scramjet.js?v={RUNTIME_REV}",
        injectPath: "/proxy/runtime/controller.inject.js?v={RUNTIME_REV}",
        wasmPath: "/scramjet/scramjet.wasm",
        virtualWasmPath: "scramjet.wasm.js",
        codec: {{
          encode: (v) => (v ? encodeURIComponent(v) : v),
          decode: (v) => (v ? decodeURIComponent(v) : v),
        }},
      }},
    }});
    await controller.wait();
    sjFrame = controller.createFrame(frameEl);

    const params = new URLSearchParams(location.search);
    const qUrl = params.get("url");
    const target = normalizeUrl(qUrl || INITIAL_URL);
    inputEl.value = target;
    setStatus("読込中: " + target);
    sjFrame.go(target);
  }}

  formEl.addEventListener("submit", (e) => {{
    e.preventDefault();
    const target = normalizeUrl(inputEl.value);
    inputEl.value = target;
    if (!sjFrame) {{
      setStatus("まだ初期化中です。少し待ってから再試行してください。");
      return;
    }}
    setStatus("読込中: " + target);
    sjFrame.go(target);
  }});

  frameEl.addEventListener("load", () => {{
    setStatus("表示完了: " + inputEl.value);
  }});

  init().catch((err) => {{
    console.error(err);
    setStatus("初期化エラー: " + err.message);
  }});
}})();
</script>
</body>
</html>"""


@router.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/proxy/", status_code=307)


@router.get("/proxy", include_in_schema=False)
async def proxy_redirect():
    return RedirectResponse("/proxy/", status_code=307)


@router.get("/proxy/", response_class=HTMLResponse, include_in_schema=False)
async def proxy_home(url: str = ""):
    target = _normalize_target_url(url) if url else DEFAULT_URL
    resp = HTMLResponse(_shell_html(target))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self' data: blob:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval' data: blob:; "
        "style-src 'self' 'unsafe-inline' data: blob:; "
        "img-src 'self' data: blob: https: http:; "
        "font-src 'self' data: blob: https: http:; "
        "connect-src 'self' data: blob: ws: wss: https: http:; "
        "frame-src 'self' data: blob: https: http:; "
        "worker-src 'self' data: blob:; "
        "media-src 'self' data: blob: https: http:; "
        "frame-ancestors 'self'"
    )
    return resp


app = FastAPI()
app.include_router(router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
