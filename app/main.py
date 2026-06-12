import asyncio
import json
import os
import re
import ssl as ssl_mod
from pathlib import Path
from urllib.parse import urlparse

import httpx
import websockets
import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/config.yml"))

_HERE = Path(__file__).parent

app = FastAPI(title="Service Hub")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(_HERE / "templates"))

ICON_CDN = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg"
MDI_CDN  = "https://cdn.jsdelivr.net/npm/@mdi/svg@7.4.47/svg"


def _is_icon_slug(icon: str) -> bool:
    """True for Dashboard Icons slugs like 'proxmox' or 'pi-hole' (lowercase kebab, 2+ chars)."""
    return bool(re.match(r"^[a-z0-9][a-z0-9-]+$", icon or ""))


templates.env.globals["is_icon_slug"] = _is_icon_slug
templates.env.globals["icon_cdn"] = ICON_CDN
templates.env.globals["mdi_cdn"]  = MDI_CDN

# Headers that must be stripped so services can be framed
_STRIP_RESP_HEADERS = frozenset(
    {
        "x-frame-options",
        "content-security-policy",
        "content-security-policy-report-only",
        "content-encoding",    # httpx auto-decompresses; size may differ from original header
        "content-length",      # recomputed by Starlette from actual body size
        "transfer-encoding",
    }
)

# Headers that must not be forwarded to the upstream
_STRIP_REQ_HEADERS = frozenset({"host", "origin", "referer", "content-length"})


DEFAULT_CONFIG: dict = {
    "app": {"title": "Service Hub"},
    "services": [],
}


def _write_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as fh:
        yaml.dump(
            config,
            fh,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )


def load_config() -> dict:
    """Load the config, initializing it with defaults when missing or empty."""
    try:
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
        config = None

    if not config:
        config = dict(DEFAULT_CONFIG)
        try:
            _write_config(config)
        except OSError:
            # Read-only mount or similar — still serve sensible defaults in-memory.
            pass

    return config


def find_service(config: dict, service_id: str) -> dict:
    for svc in config.get("services", []):
        if svc.get("id") == service_id:
            return svc
    raise HTTPException(404, f"Service '{service_id}' not found in config")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    config = load_config()
    return templates.TemplateResponse(
        "index.html", {"request": request, "config": config}
    )


@app.get("/api/config")
async def get_config_raw():
    return load_config()


@app.put("/api/config")
async def put_config(request: Request):
    data = await request.json()

    if not isinstance(data.get("services"), list):
        raise HTTPException(400, "services must be a list")

    for svc in data["services"]:
        for field in ("id", "name", "url"):
            if not svc.get(field):
                raise HTTPException(400, f"each service needs '{field}'")

    try:
        _write_config(data)
    except OSError as exc:
        raise HTTPException(500, f"Cannot write config: {exc}")

    return {"ok": True}


@app.get("/api/services")
async def api_services():
    config = load_config()
    out = []
    for svc in config.get("services", []):
        out.append(
            {
                "id": svc["id"],
                "name": svc["name"],
                "icon": svc.get("icon", "🔗"),
                "color": svc.get("color", "#6366f1"),
                "frameUrl": (
                    f"/proxy/{svc['id']}/"
                    if svc.get("proxy")
                    else svc["url"]
                ),
            }
        )
    return {"app": config.get("app", {}), "services": out}


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------


def _rewrite_paths(text: str, proxy_base: str, host_root: str | None = None) -> str:
    """Rewrite absolute-path references to go through the proxy. Skip already-proxied paths."""
    already = re.escape(proxy_base)

    # Absolute self-references to the upstream's own origin. A switch that redirects to
    # http://192.168.20.2/login.cgi (or links to it) would otherwise escape the proxy
    # and get X-Frame-blocked / blanked. Rewriting scheme+host to the proxy base turns
    # those into root-relative paths that stay inside the proxy.
    if host_root:
        text = text.replace(host_root, proxy_base)
        netloc = re.escape(host_root.split("://", 1)[-1])
        # protocol-relative //192.168.20.2/foo (not already part of a longer host)
        text = re.sub(rf'(?<![\w.])//{netloc}(?=[/"\'?\s]|$)', proxy_base, text)

    # href="/foo", src="/foo", action="/foo" — skip if already starts with proxy_base
    text = re.sub(
        rf'((?:href|src|action|data-src)\s*=\s*["\'])(?!{already})(/(?!/))',
        lambda m: f"{m.group(1)}{proxy_base}/",
        text,
    )

    # CSS url('/foo') and url("/foo")
    text = re.sub(
        rf'(url\(["\']?)(?!{already})(/(?!/))',
        lambda m: f"{m.group(1)}{proxy_base}/",
        text,
    )

    # Best-effort JS string paths: url: "/api/...", location = "/foo"
    text = re.sub(
        rf'((?:url|URL|href|location)\s*[=:]\s*["\'])(?!{already})(/(?!/))',
        lambda m: f"{m.group(1)}{proxy_base}/",
        text,
    )

    # JS redirects via dotted forms: location.href="/x", .assign("/x"), .replace("/x").
    # These are the common ways a switch login page forwards to /login.cgi, and the
    # bare-"location" pattern above misses them.
    text = re.sub(
        rf'(\.(?:href|assign|replace)\s*[=(]\s*["\'])(?!{already})(/(?!/))',
        lambda m: f"{m.group(1)}{proxy_base}/",
        text,
    )

    # <meta http-equiv="refresh" content="0; url=/login.cgi"> with a root-relative URL.
    text = re.sub(
        rf'(<meta\b[^>]*?\brefresh\b[^>]*?\burl\s*=\s*)(?!{already})(/(?!/))',
        lambda m: f"{m.group(1)}{proxy_base}/",
        text,
        flags=re.IGNORECASE,
    )

    # Route top/parent NAVIGATION through __hubNav (defined in the injected shim). The
    # helper no-ops a redirect to the already-loaded page — a frame-detection guard like
    # IPMI's `if (window != top) top.location.href = "/"`, which would otherwise reload
    # forever — and navigates the iframe otherwise (a switch forwarding to /login.cgi).
    # The target URLs are already proxy-prefixed by the rewrites above. Reads and
    # comparisons (`if (window != top)`, `x = top.location.href`) are NOT navigation, so
    # they don't match and are left intact.
    text = re.sub(
        r"\b(?:window\.)?(?:top|parent)\.location\.(?:replace|assign)\s*\(\s*([^)]*?)\s*\)",
        lambda m: f"__hubNav({m.group(1)})",
        text,
    )
    text = re.sub(
        r"\b(?:window\.)?(?:top|parent)\.location(?:\.href)?\s*=(?!=)\s*([^;\n}]+)",
        lambda m: f"__hubNav({m.group(1).rstrip()})",
        text,
    )

    return text


def _inject_base(text: str, proxy_dir: str) -> str:
    """Set <base href> to proxy_dir so relative URLs resolve correctly.
    Replaces an existing <base href=...> if present; otherwise injects one after <head>.
    Pi-hole and similar apps set <base href="/admin/login.php/"> (the PHP self-path with
    trailing slash), which makes relative asset paths resolve to wrong proxy sub-paths."""
    replaced, n = re.subn(
        r'(<base\b[^>]*?\bhref\s*=\s*)["\'][^"\']*["\']',
        lambda m: f'{m.group(1)}"{proxy_dir}"',
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    if n:
        return replaced
    return re.sub(
        r'(<head\b[^>]*>)',
        lambda m: f'{m.group(1)}<base href="{proxy_dir}">',
        text,
        count=1,
        flags=re.IGNORECASE,
    )


# Injected before any of the page's own scripts. Does two jobs:
#
# 1. Compat: legacy appliance UIs (managed switches, routers) assign `document.domain`
#    or call history.pushState/replaceState with their original absolute URL — both
#    throw "SecurityError: The operation is insecure" once the page is reframed and
#    served from the proxy origin, halting the rest of their startup script.
#
# 2. URL rewriting: SPAs (e.g. TrueNAS) build API and WebSocket URLs dynamically from
#    window.location — `ws://{host}/api/current`, `fetch('/api/...')` — with no proxy
#    prefix, so they escape the static HTML rewrite and hit the hub root. We wrap
#    WebSocket/fetch/XMLHttpRequest to re-add the `/proxy/<id>` prefix to same-host
#    and root-relative URLs at call time.
#
# The whole thing is wrapped so it can never itself throw and break a page that
# doesn't need it. `__PB__` is replaced with the JSON-encoded proxy base.
_SHIM_TEMPLATE = (
    "<script>(function(){try{"
    # --- document.domain / history compat ---
    "try{Object.defineProperty(document,'domain',{configurable:true,"
    "get:function(){return location.hostname},set:function(){}});}catch(e){}"
    # --- strip Secure from client-side document.cookie writes ---
    # Apps like Proxmox set their auth cookie in JS with the `secure` flag hardcoded
    # (PVE.Utils.setAuthData). On an http-served hub the browser silently rejects a
    # Secure cookie, so login never sticks -> 401. Drop `secure` (and SameSite=None,
    # which requires Secure) from cookie writes so they're storable over http.
    "try{var _cd=Object.getOwnPropertyDescriptor(Document.prototype,'cookie')"
    "||Object.getOwnPropertyDescriptor(HTMLDocument.prototype,'cookie');"
    "if(_cd&&_cd.set&&_cd.get){Object.defineProperty(document,'cookie',{configurable:true,"
    "get:function(){return _cd.get.call(document);},"
    "set:function(v){try{v=String(v).replace(/;\\s*secure\\b/ig,'')"
    ".replace(/;\\s*samesite\\s*=\\s*none/ig,'; SameSite=Lax');}catch(e){}"
    "return _cd.set.call(document,v);}});}}catch(e){}"
    "['pushState','replaceState'].forEach(function(m){var o=history[m];"
    "if(typeof o!=='function')return;history[m]=function(s,t,u){"
    "try{return o.call(this,s,t,u);}catch(e){"
    "try{return o.call(this,s,t);}catch(_){return undefined;}}};});"
    # --- frame-bust handler ---
    # Rewritten top/parent navigation is routed here. If the target is the page already
    # loaded (a frame-detection guard redirecting itself, e.g. IPMI's `top.location='/'`),
    # do nothing so the page renders in-frame instead of reloading forever. Otherwise
    # navigate the iframe (e.g. a switch forwarding to /login.cgi).
    "window.__hubNav=function(u){try{if(u==null)return;"
    "var t=new URL(String(u),location.href).href;"
    "if(t===location.href)return;location.replace(t);}"
    "catch(e){try{location.replace(u);}catch(_){}}};"
    # --- absolute-path rewriting ---
    "var PB=__PB__;"
    "function rw(u){try{if(u==null)return u;u=String(u);"
    "var m=u.match(/^(https?:|wss?:)?\\/\\/([^\\/?#]+)([\\/?#].*)?$/i);"
    "if(m){if(m[2]!==location.host)return u;var r=m[3]||'/';"
    "if(r.indexOf(PB+'/')===0||r===PB)return u;return (m[1]||'')+'//'+m[2]+PB+r;}"
    "if(u.charAt(0)==='/'&&u.charAt(1)!=='/'){"
    "if(u.indexOf(PB+'/')===0||u===PB)return u;return PB+u;}"
    "return u;}catch(e){return u;}}"
    "var _WS=window.WebSocket;if(_WS){var WS=function(url,protocols){"
    "return new _WS(rw(url),protocols);};WS.prototype=_WS.prototype;"
    "try{WS.CONNECTING=_WS.CONNECTING;WS.OPEN=_WS.OPEN;WS.CLOSING=_WS.CLOSING;"
    "WS.CLOSED=_WS.CLOSED;}catch(e){}window.WebSocket=WS;}"
    "var _ES=window.EventSource;if(_ES){var ES=function(url,cfg){"
    "return new _ES(rw(url),cfg);};ES.prototype=_ES.prototype;"
    "try{ES.CONNECTING=_ES.CONNECTING;ES.OPEN=_ES.OPEN;ES.CLOSED=_ES.CLOSED;}"
    "catch(e){}window.EventSource=ES;}"
    "var _f=window.fetch;if(_f){window.fetch=function(input,init){try{"
    "if(typeof input==='string')input=rw(input);"
    "else if(input&&input.url)input=new Request(rw(input.url),input);"
    "}catch(e){}return _f.call(this,input,init);};}"
    "var _xo=window.XMLHttpRequest&&XMLHttpRequest.prototype.open;if(_xo){"
    "XMLHttpRequest.prototype.open=function(method,url){try{url=rw(url);}catch(e){}"
    "return _xo.apply(this,[method,url].concat([].slice.call(arguments,2)));};}"
    "}catch(e){}})();</script>"
)


def _build_shim(proxy_base: str) -> str:
    return _SHIM_TEMPLATE.replace("__PB__", json.dumps(proxy_base))


def _inject_shim(text: str, proxy_base: str) -> str:
    """Insert the compatibility shim as the first script the page runs.
    Prefers just inside <head>; falls back to after <html ...>, then to the very top
    (covers quirks-mode / frameset pages that may omit <head>)."""
    shim = _build_shim(proxy_base)
    for pattern in (r"(<head\b[^>]*>)", r"(<html\b[^>]*>)"):
        new_text, n = re.subn(
            pattern,
            lambda m: f"{m.group(1)}{shim}",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        if n:
            return new_text
    return shim + text


def _inject_importmap(text: str, proxy_base: str) -> str:
    """Remap absolute-path ES module imports through the proxy.

    SPAs like OPNsense load widgets via dynamic `import("/ui/js/widgets/X.js")`. The
    fetch/XHR shim can't intercept `import()`, and an absolute path ignores <base href>,
    so the request hits the hub root and 404s (wrong MIME -> module blocked). An import
    map rewrites the specifier before resolution. The identity entry for the proxy base
    means already-prefixed specifiers (longest-prefix-wins) are left as-is, so nothing
    double-prefixes. Skipped if the page already ships its own import map (only one is
    allowed per document)."""
    if re.search(r'<script\b[^>]*type\s*=\s*["\']importmap["\']', text, re.IGNORECASE):
        return text
    importmap = (
        '<script type="importmap">'
        '{"imports":{'
        f'"/":"{proxy_base}/",'
        f'"{proxy_base}/":"{proxy_base}/"'
        "}}"
        "</script>"
    )
    for pattern in (r"(<head\b[^>]*>)", r"(<html\b[^>]*>)"):
        new_text, n = re.subn(
            pattern,
            lambda m: f"{m.group(1)}{importmap}",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        if n:
            return new_text
    return importmap + text


def _rewrite_html(
    content: bytes,
    service_id: str,
    proxy_dir: str | None = None,
    host_root: str | None = None,
) -> bytes:
    text = content.decode("utf-8", errors="replace")
    # Some appliances' embedded servers send TWO concatenated HTTP responses on a
    # Connection: close socket with no Content-Length, so httpx reads both as one body.
    # The browser then parses a duplicated DOM (two <form name="login"> makes
    # document.login a collection, so document.login.username is undefined). If a second
    # HTTP response is glued on after the first document, cut at the first </html>.
    if re.search(r"</html\s*>\s*HTTP/\d", text, re.IGNORECASE):
        end = re.search(r"</html\s*>", text, re.IGNORECASE)
        if end:
            text = text[: end.end()]
    proxy_base = f"/proxy/{service_id}"
    text = _rewrite_paths(text, proxy_base, host_root)
    if proxy_dir and proxy_dir != proxy_base + "/":
        text = _inject_base(text, proxy_dir)
    text = _inject_shim(text, proxy_base)
    # Injected last so it lands first in <head> — an import map must precede module scripts.
    text = _inject_importmap(text, proxy_base)
    return text.encode("utf-8")


def _rewrite_css(content: bytes, service_id: str) -> bytes:
    text = content.decode("utf-8", errors="replace")
    proxy_base = f"/proxy/{service_id}"
    already = re.escape(proxy_base)
    text = re.sub(
        rf'(url\(["\']?)(?!{already})(/(?!/))',
        lambda m: f"{m.group(1)}{proxy_base}/",
        text,
    )
    return text.encode("utf-8")


def _rewrite_set_cookie(value: str, proxy_base: str) -> str:
    """Make an upstream Set-Cookie storable on the hub origin so logins persist.

    - Drop `Secure`: the hub is often served over http, and browsers silently drop
      Secure cookies on insecure pages — which is why https appliances (IPMI) "log in"
      but never advance: the session cookie is rejected and the next request is anonymous.
    - Drop `Domain=...`: the upstream scopes the cookie to its own IP/host, which the
      browser rejects since the page is served from the hub host. Removing it defaults
      the cookie to the hub host.
    - Force `Path=<proxy_base>/`: scopes the cookie to this service (so two appliances
      that both set e.g. `SID` don't collide on the shared origin) while still matching
      every proxied request for it.
    - `SameSite=None` requires Secure, which we just dropped; downgrade to Lax.
    """
    parts = [p.strip() for p in value.split(";") if p.strip()]
    if not parts:
        return value
    out = [parts[0]]  # name=value
    for attr in parts[1:]:
        low = attr.lower()
        if low == "secure" or low.startswith("domain=") or low.startswith("path="):
            continue
        if low.startswith("samesite=") and low.partition("=")[2].strip() == "none":
            out.append("SameSite=Lax")
            continue
        out.append(attr)
    out.append(f"Path={proxy_base}/")
    return "; ".join(out)


# Canonical Paul-Johnston md5.js (defines the global hex_md5). Some cheap managed
# switches reference <script src="md5.js"> to hash the login password client-side but
# their embedded web server never actually serves the file, so hex_md5 is undefined and
# the login form can't submit. Served as a fallback when the upstream md5.js is missing.
_MD5_JS = rb"""
var hexcase=0,chrsz=8;
function hex_md5(s){return binl2hex(core_md5(str2binl(s),s.length*chrsz));}
function core_md5(x,len){
 x[len>>5]|=0x80<<((len)%32);x[(((len+64)>>>9)<<4)+14]=len;
 var a=1732584193,b=-271733879,c=-1732584194,d=271733878;
 for(var i=0;i<x.length;i+=16){
  var olda=a,oldb=b,oldc=c,oldd=d;
  a=md5_ff(a,b,c,d,x[i+0],7,-680876936);d=md5_ff(d,a,b,c,x[i+1],12,-389564586);
  c=md5_ff(c,d,a,b,x[i+2],17,606105819);b=md5_ff(b,c,d,a,x[i+3],22,-1044525330);
  a=md5_ff(a,b,c,d,x[i+4],7,-176418897);d=md5_ff(d,a,b,c,x[i+5],12,1200080426);
  c=md5_ff(c,d,a,b,x[i+6],17,-1473231341);b=md5_ff(b,c,d,a,x[i+7],22,-45705983);
  a=md5_ff(a,b,c,d,x[i+8],7,1770035416);d=md5_ff(d,a,b,c,x[i+9],12,-1958414417);
  c=md5_ff(c,d,a,b,x[i+10],17,-42063);b=md5_ff(b,c,d,a,x[i+11],22,-1990404162);
  a=md5_ff(a,b,c,d,x[i+12],7,1804603682);d=md5_ff(d,a,b,c,x[i+13],12,-40341101);
  c=md5_ff(c,d,a,b,x[i+14],17,-1502002290);b=md5_ff(b,c,d,a,x[i+15],22,1236535329);
  a=md5_gg(a,b,c,d,x[i+1],5,-165796510);d=md5_gg(d,a,b,c,x[i+6],9,-1069501632);
  c=md5_gg(c,d,a,b,x[i+11],14,643717713);b=md5_gg(b,c,d,a,x[i+0],20,-373897302);
  a=md5_gg(a,b,c,d,x[i+5],5,-701558691);d=md5_gg(d,a,b,c,x[i+10],9,38016083);
  c=md5_gg(c,d,a,b,x[i+15],14,-660478335);b=md5_gg(b,c,d,a,x[i+4],20,-405537848);
  a=md5_gg(a,b,c,d,x[i+9],5,568446438);d=md5_gg(d,a,b,c,x[i+14],9,-1019803690);
  c=md5_gg(c,d,a,b,x[i+3],14,-187363961);b=md5_gg(b,c,d,a,x[i+8],20,1163531501);
  a=md5_gg(a,b,c,d,x[i+13],5,-1444681467);d=md5_gg(d,a,b,c,x[i+2],9,-51403784);
  c=md5_gg(c,d,a,b,x[i+7],14,1735328473);b=md5_gg(b,c,d,a,x[i+12],20,-1926607734);
  a=md5_hh(a,b,c,d,x[i+5],4,-378558);d=md5_hh(d,a,b,c,x[i+8],11,-2022574463);
  c=md5_hh(c,d,a,b,x[i+11],16,1839030562);b=md5_hh(b,c,d,a,x[i+14],23,-35309556);
  a=md5_hh(a,b,c,d,x[i+1],4,-1530992060);d=md5_hh(d,a,b,c,x[i+4],11,1272893353);
  c=md5_hh(c,d,a,b,x[i+7],16,-155497632);b=md5_hh(b,c,d,a,x[i+10],23,-1094730640);
  a=md5_hh(a,b,c,d,x[i+13],4,681279174);d=md5_hh(d,a,b,c,x[i+0],11,-358537222);
  c=md5_hh(c,d,a,b,x[i+3],16,-722521979);b=md5_hh(b,c,d,a,x[i+6],23,76029189);
  a=md5_hh(a,b,c,d,x[i+9],4,-640364487);d=md5_hh(d,a,b,c,x[i+12],11,-421815835);
  c=md5_hh(c,d,a,b,x[i+15],16,530742520);b=md5_hh(b,c,d,a,x[i+2],23,-995338651);
  a=md5_ii(a,b,c,d,x[i+0],6,-198630844);d=md5_ii(d,a,b,c,x[i+7],10,1126891415);
  c=md5_ii(c,d,a,b,x[i+14],15,-1416354905);b=md5_ii(b,c,d,a,x[i+5],21,-57434055);
  a=md5_ii(a,b,c,d,x[i+12],6,1700485571);d=md5_ii(d,a,b,c,x[i+3],10,-1894986606);
  c=md5_ii(c,d,a,b,x[i+10],15,-1051523);b=md5_ii(b,c,d,a,x[i+1],21,-2054922799);
  a=md5_ii(a,b,c,d,x[i+8],6,1873313359);d=md5_ii(d,a,b,c,x[i+15],10,-30611744);
  c=md5_ii(c,d,a,b,x[i+6],15,-1560198380);b=md5_ii(b,c,d,a,x[i+13],21,1309151649);
  a=md5_ii(a,b,c,d,x[i+4],6,-145523070);d=md5_ii(d,a,b,c,x[i+11],10,-1120210379);
  c=md5_ii(c,d,a,b,x[i+2],15,718787259);b=md5_ii(b,c,d,a,x[i+9],21,-343485551);
  a=safe_add(a,olda);b=safe_add(b,oldb);c=safe_add(c,oldc);d=safe_add(d,oldd);
 }
 return Array(a,b,c,d);
}
function md5_cmn(q,a,b,x,s,t){return safe_add(bit_rol(safe_add(safe_add(a,q),safe_add(x,t)),s),b);}
function md5_ff(a,b,c,d,x,s,t){return md5_cmn((b&c)|((~b)&d),a,b,x,s,t);}
function md5_gg(a,b,c,d,x,s,t){return md5_cmn((b&d)|(c&(~d)),a,b,x,s,t);}
function md5_hh(a,b,c,d,x,s,t){return md5_cmn(b^c^d,a,b,x,s,t);}
function md5_ii(a,b,c,d,x,s,t){return md5_cmn(c^(b|(~d)),a,b,x,s,t);}
function safe_add(x,y){var lsw=(x&0xFFFF)+(y&0xFFFF);var msw=(x>>16)+(y>>16)+(lsw>>16);return(msw<<16)|(lsw&0xFFFF);}
function bit_rol(num,cnt){return(num<<cnt)|(num>>>(32-cnt));}
function str2binl(str){var bin=Array();var mask=(1<<chrsz)-1;for(var i=0;i<str.length*chrsz;i+=chrsz)bin[i>>5]|=(str.charCodeAt(i/chrsz)&mask)<<(i%32);return bin;}
function binl2hex(binarray){var hex_tab=hexcase?"0123456789ABCDEF":"0123456789abcdef";var str="";for(var i=0;i<binarray.length*4;i++){str+=hex_tab.charAt((binarray[i>>2]>>((i%4)*8+4))&0xF)+hex_tab.charAt((binarray[i>>2]>>((i%4)*8))&0xF);}return str;}
"""


@app.api_route(
    "/proxy/{service_id}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy(service_id: str, path: str, request: Request):
    config = load_config()
    svc = find_service(config, service_id)

    base_url = svc["url"].rstrip("/")
    _parsed = urlparse(svc["url"])
    host_root = f"{_parsed.scheme}://{_parsed.netloc}"
    # Detect if the configured URL ends with a file (has an extension in the last
    # path segment, e.g. /admin/login.php).  Adding a trailing slash to a PHP file
    # activates PATH_INFO mode — the file then handles all sub-paths and returns HTML
    # for every CSS/JS request, causing MIME-type blocks in the browser.
    _svc_last_seg = _parsed.path.rstrip("/").split("/")[-1]
    _svc_is_file = "." in _svc_last_seg and bool(_svc_last_seg)

    # Collapse a leading slash on the captured path. SPAs (Portainer) append "/api"
    # to a base href that already ends in "/", producing "/proxy/portainer//api/...";
    # the doubled slash is captured here as a leading "/" and would forward upstream
    # as "host//api/...", which 404s. The route already owns the slash after the id.
    path = path.lstrip("/")

    if path:
        # Always resolve resource requests from the host root so absolute paths like
        # /admin/style.css don't double-prefix to /admin/admin/style.css.
        target_url = f"{host_root}/{path}"
    elif _svc_is_file:
        target_url = base_url          # no trailing slash — avoids PHP PATH_INFO
    else:
        target_url = f"{base_url}/"    # directory URL — keep trailing slash

    qs = str(request.url.query)
    if qs:
        target_url = f"{target_url}?{qs}"

    forward_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _STRIP_REQ_HEADERS
    }
    # Force identity encoding so upstream sends uncompressed bytes.
    # httpx adds its own Accept-Encoding otherwise and auto-decompresses,
    # leaving Content-Length stale and causing a size mismatch crash.
    forward_headers["accept-encoding"] = "identity"

    # Rewrite Origin/Referer to the upstream's own origin (instead of dropping them) so the
    # device sees first-party requests. Embedded servers commonly gate asset delivery and
    # login on a matching Referer/Origin — stripping them makes requests look foreign, which
    # blocks logins and can make resources (e.g. md5.js) 404.
    proxy_prefix = f"/proxy/{service_id}"
    if any(k.lower() == "origin" for k in request.headers):
        forward_headers["origin"] = host_root
    referer = request.headers.get("referer")
    if referer:
        _ref = urlparse(referer)
        _ref_path = _ref.path
        if _ref_path.startswith(proxy_prefix):
            _ref_path = _ref_path[len(proxy_prefix):] or "/"
        forward_headers["referer"] = f"{host_root}{_ref_path}" + (
            f"?{_ref.query}" if _ref.query else ""
        )

    body = await request.body()

    # read=None so long-lived streams (Server-Sent Events, e.g. OPNsense's live widgets)
    # aren't killed by a read timeout; connect stays bounded so dead hosts still fail fast.
    client = httpx.AsyncClient(
        verify=not svc.get("ignore_ssl", False),
        follow_redirects=True,
        timeout=httpx.Timeout(30.0, read=None),
    )
    req = client.build_request(
        method=request.method,
        url=target_url,
        headers=forward_headers,
        content=body,
    )
    try:
        upstream = await client.send(req, stream=True)
    except httpx.ConnectError as exc:
        await client.aclose()
        raise HTTPException(502, f"Cannot connect to {svc['name']}: {exc}")
    except httpx.RequestError as exc:
        await client.aclose()
        raise HTTPException(502, f"Upstream error for {svc['name']}: {exc}")

    # Set-Cookie is handled separately: a dict collapses its (legitimately repeated)
    # values into one, and each needs rewriting so the cookie survives on the hub origin.
    resp_headers = {
        k: v
        for k, v in upstream.headers.multi_items()
        if k.lower() not in _STRIP_RESP_HEADERS and k.lower() != "set-cookie"
    }
    set_cookies = upstream.headers.get_list("set-cookie")
    proxy_base = f"/proxy/{service_id}"
    content_type = resp_headers.get("content-type", "")

    # md5.js fallback: if the device references md5.js to hash the login password but its
    # server doesn't serve it (404/empty), hand back a working hex_md5 so login can submit.
    # A device that does serve a real md5.js is passed through untouched.
    if path.rsplit("/", 1)[-1].lower() == "md5.js":
        try:
            raw = await upstream.aread()
        finally:
            await upstream.aclose()
            await client.aclose()
        if upstream.status_code >= 400 or not raw.strip():
            return Response(content=_MD5_JS, media_type="application/javascript")
        response = Response(
            content=raw,
            status_code=upstream.status_code,
            headers=resp_headers,
        )
        for cookie in set_cookies:
            response.raw_headers.append(
                (b"set-cookie", _rewrite_set_cookie(cookie, proxy_base).encode("latin-1"))
            )
        return response

    # Only HTML/CSS is rewritten, and only those need the whole body in memory. Everything
    # else (JS, images, JSON, and crucially never-ending SSE streams) is streamed straight
    # through — buffering an SSE stream with .content would hang the request forever.
    if "text/html" in content_type or "text/css" in content_type:
        try:
            raw = await upstream.aread()
        finally:
            await upstream.aclose()
            await client.aclose()
        if "text/html" in content_type:
            # Compute the directory of the final upstream URL for base-tag injection.
            # Strip trailing slash so "login.php/" is treated the same as "login.php".
            _fp = urlparse(str(upstream.url)).path.rstrip("/") or "/"
            _last = _fp.split("/")[-1]
            if "." in _last:  # last segment is a file (e.g. login.php) — use its parent dir
                final_dir = _fp.rsplit("/", 1)[0] + "/"
            else:             # path is already a directory
                final_dir = _fp + "/"
            proxy_dir = f"/proxy/{service_id}{final_dir}"
            raw = _rewrite_html(raw, service_id, proxy_dir, host_root)
        else:
            raw = _rewrite_css(raw, service_id)
        response = Response(
            content=raw,
            status_code=upstream.status_code,
            headers=resp_headers,
        )
    else:
        async def body_iter():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()

        response = StreamingResponse(
            body_iter(),
            status_code=upstream.status_code,
            headers=resp_headers,
        )

    for cookie in set_cookies:
        rewritten = _rewrite_set_cookie(cookie, proxy_base)
        response.raw_headers.append((b"set-cookie", rewritten.encode("latin-1")))
    return response


# ---------------------------------------------------------------------------
# WebSocket proxy
# ---------------------------------------------------------------------------
# SPAs like TrueNAS run entirely over a WebSocket (e.g. /api/current). The client
# shim rewrites those URLs to /proxy/<id>/..., and this endpoint bridges the browser
# socket to the upstream device socket, relaying frames in both directions.


@app.websocket("/proxy/{service_id}/{path:path}")
async def proxy_ws(client_ws: WebSocket, service_id: str, path: str):
    config = load_config()
    svc = next(
        (s for s in config.get("services", []) if s.get("id") == service_id),
        None,
    )
    if svc is None:
        await client_ws.close(code=1008)  # policy violation
        return

    parsed = urlparse(svc["url"])
    scheme = "wss" if parsed.scheme == "https" else "ws"
    target = f"{scheme}://{parsed.netloc}/{path.lstrip('/')}"
    if client_ws.url.query:
        target = f"{target}?{client_ws.url.query}"

    # Forward the client's offered subprotocols so upstream can pick one.
    offered = client_ws.headers.get("sec-websocket-protocol")
    subprotocols = [p.strip() for p in offered.split(",")] if offered else None

    ssl_ctx = None
    if scheme == "wss":
        ssl_ctx = ssl_mod.create_default_context()
        if svc.get("ignore_ssl"):
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl_mod.CERT_NONE

    try:
        upstream = await websockets.connect(
            target,
            subprotocols=subprotocols,
            ssl=ssl_ctx,
            open_timeout=15,
            max_size=None,  # device payloads can exceed the 1 MiB default
        )
    except Exception:
        # Accept then immediately close so the browser sees a clean failure
        # rather than a handshake reject it can't introspect.
        await client_ws.accept()
        await client_ws.close(code=1011)  # internal error
        return

    await client_ws.accept(subprotocol=upstream.subprotocol)

    async def client_to_upstream():
        try:
            while True:
                msg = await client_ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except Exception:
            pass
        finally:
            await upstream.close()

    async def upstream_to_client():
        try:
            async for message in upstream:
                if isinstance(message, (bytes, bytearray)):
                    await client_ws.send_bytes(message)
                else:
                    await client_ws.send_text(message)
        except Exception:
            pass
        finally:
            try:
                await client_ws.close()
            except Exception:
                pass

    await asyncio.gather(client_to_upstream(), upstream_to_client())
