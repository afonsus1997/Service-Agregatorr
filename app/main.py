import os
import re
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
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


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


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
        with open(CONFIG_PATH, "w") as fh:
            yaml.dump(
                data,
                fh,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )
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


def _rewrite_paths(text: str, proxy_base: str) -> str:
    """Rewrite absolute-path references to go through the proxy. Skip already-proxied paths."""
    already = re.escape(proxy_base)

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


def _rewrite_html(content: bytes, service_id: str, proxy_dir: str | None = None) -> bytes:
    text = content.decode("utf-8", errors="replace")
    proxy_base = f"/proxy/{service_id}"
    text = _rewrite_paths(text, proxy_base)
    if proxy_dir and proxy_dir != proxy_base + "/":
        text = _inject_base(text, proxy_dir)
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

    body = await request.body()

    try:
        async with httpx.AsyncClient(
            verify=not svc.get("ignore_ssl", False),
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        ) as client:
            upstream = await client.request(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                content=body,
            )
    except httpx.ConnectError as exc:
        raise HTTPException(502, f"Cannot connect to {svc['name']}: {exc}")
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Upstream error for {svc['name']}: {exc}")

    resp_headers = {
        k: v
        for k, v in upstream.headers.multi_items()
        if k.lower() not in _STRIP_RESP_HEADERS
    }

    content = upstream.content
    content_type = resp_headers.get("content-type", "")

    if "text/html" in content_type:
        # Compute the directory of the final upstream URL for base-tag injection.
        # Strip trailing slash first so "login.php/" is treated the same as "login.php".
        _fp = urlparse(str(upstream.url)).path.rstrip("/") or "/"
        _last = _fp.split("/")[-1]
        if "." in _last:  # last segment is a file (e.g. login.php) — use its parent dir
            final_dir = _fp.rsplit("/", 1)[0] + "/"
        else:             # path is already a directory
            final_dir = _fp + "/"
        proxy_dir = f"/proxy/{service_id}{final_dir}"
        content = _rewrite_html(content, service_id, proxy_dir)
    elif "text/css" in content_type:
        content = _rewrite_css(content, service_id)

    # content-length is stripped from resp_headers; Starlette sets it from len(content)
    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=resp_headers,
    )
