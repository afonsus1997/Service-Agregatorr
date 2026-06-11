# service-agregatorr



A self-hosted homelab dashboard that aggregates web UIs into a single sidebar. Services load in persistent iframes — switching tabs keeps them alive. Built with FastAPI + Docker.

![Docker Image](https://img.shields.io/docker/v/afonsus1997/service-agregatorr?label=Docker%20Hub)

> [!WARNING]
> **This project was completely hallucinated by Claude. Use at your own risk.**
> Instructions are provided but no support is given. The proxy strips security headers by design — **never expose this to the internet.** LAN use only.

---

## Features

- Sidebar with per-service icons, names and accent colours
- Persistent iframes — switching services does not reload them
- **Proxy mode** — strips `X-Frame-Options` / CSP so services that block embedding still work; rewrites internal paths and base URLs transparently
- Configurable entirely from the UI — changes saved to YAML, no restart needed
- **Dashboard Icons** — 100+ homelab service icons (Pi-hole, Proxmox, Portainer, …)
- **MDI icons** — search the full [Material Design Icons](https://pictogrammers.com/library/mdi/) library (`mdi:server`, `mdi:router`, …) or type any name directly
- Custom URL or emoji icons also supported
- Dark / light theme

---

## Quick start

### Docker Compose (recommended)

1. Create a `config.yml` (see [Configuration](#configuration)) in the same directory as `docker-compose.yml`, or let the app create a minimal one on first run.

2. Create `docker-compose.yml`:

```yaml
services:
  service-agregatorr:
    image: afonsus1997/service-agregatorr:latest
    ports:
      - "8080:8080"
    volumes:
      - ./config.yml:/config/config.yml
    restart: unless-stopped
    # Required on Linux to reach LAN IPs from inside the container
    network_mode: host
```

3. Start:

```bash
docker compose up -d
```

4. Open `http://localhost:8080` (or your server IP if using `network_mode: host`).

> **Note:** `network_mode: host` is needed on Linux so the container can reach LAN addresses like `192.168.x.x`. On macOS/Windows you can remove it and use `ports: ["8080:8080"]` instead, but LAN services must be reachable from the Docker network.

---

## Configuration

The config file is mounted at `/config/config.yml` inside the container. Edit it directly or use the **Settings** panel in the UI (⚙️ in the sidebar).


### Service fields

| Field | Required | Description |
|-------|----------|-------------|
| `id` | yes | Unique slug (auto-generated from name if omitted in UI) |
| `name` | yes | Display name in the sidebar |
| `url` | yes | Full URL of the service |
| `icon` | no | Dashboard Icons slug, `mdi:name`, URL, or emoji (default `🔗`) |
| `color` | no | Hex accent colour for the active indicator (default `#6366f1`) |
| `proxy` | no | Route through the built-in reverse proxy (default `false`) |
| `ignore_ssl` | no | Disable TLS verification for self-signed certs (default `false`) |

---

## Icons

Three sources are supported:

| Type | Example | How to use |
|------|---------|------------|
| **Dashboard Icons** | `proxmox`, `pi-hole`, `portainer` | Type the slug or browse via ⋯ picker → *App Icons* tab |
| **MDI** | `mdi:server`, `mdi:router-wireless` | Type `mdi:name` directly, or ⋯ picker → *MDI* tab; search or type any name from [pictogrammers.com](https://pictogrammers.com/library/mdi/) |
| **Emoji** | `🔗`, `🖥️`, `🔀` | Type the emoji directly in the icon field |
| **URL** | `https://host/logo.png` | Paste a full image URL |

---

## Proxy mode

Enable `proxy: true` for services that refuse to load in iframes (most management UIs). The proxy:

- Strips `X-Frame-Options` and `Content-Security-Policy` headers
- Rewrites absolute paths and `<base href>` so assets load correctly through the proxy
- Follows redirects transparently (e.g. `/admin/` → `/admin/login.php`)
- Supports self-signed SSL (`ignore_ssl: true`)

> **Limitations:** Services that detect iframe embedding via JavaScript (`window.parent !== window`) — such as YouTube or Google — will still refuse to render. This tool is designed for LAN management UIs, not public sites.

---

## Running locally (development)

```bash
# Install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run with auto-reload
CONFIG_PATH=./config.yml uvicorn main:app \
  --host 0.0.0.0 --port 8080 --reload --app-dir app
```


