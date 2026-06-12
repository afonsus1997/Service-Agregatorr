'use strict';

// ── Icon helpers ────────────────────────────────────────────────

const ICON_CDN = 'https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg';
const MDI_CDN  = 'https://cdn.jsdelivr.net/npm/@mdi/svg@7.4.47/svg';

// Curated list of Dashboard Icons slugs for popular homelab services
const DASHBOARD_ICONS = [
  'adguard-home','adguard-home-sync','alertmanager','ansible','authentik',
  'autobrr','bazarr','bitwarden','caddy','calibre','calibre-web',
  'chronograf','cockpit','code-server','deluge','docker','dozzle',
  'duplicati','emby','esphome','filebrowser','flaresolverr','flood',
  'forgejo','freshrss','freenas','gitea','gitlab','grafana','grocy',
  'guacamole','haproxy','headscale','heimdall','homarr','home-assistant',
  'homer','immich','influxdb','jackett','jellyfin','jellyseerr',
  'joplin','keycloak','kodi','komga','kubernetes','lidarr','loki',
  'mealie','mosquitto','navidrome','netdata','nginx','nginx-proxy-manager',
  'nextcloud','node-red','nzbget','ombi','opnsense','overseerr',
  'pfsense','photoprism','pi-hole','plex','portainer','prometheus',
  'prowlarr','proxmox','qbittorrent','radarr','rancher','readarr',
  'redis','restic','rustdesk','sabnzbd','seafile','sonarr','syncthing',
  'synology','tailscale','tautulli','traefik','transmission','trilium',
  'truenas','ubiquiti','unifi','unraid','uptime-kuma','vaultwarden',
  'vscode','webmin','whisparr','wireguard','wordpress','xen-orchestra',
  'zabbix','zigbee2mqtt','zoneminder'
];

// Curated Material Design Icons (pictogrammers.com/library/mdi/) for homelab
const MDI_ICONS = [
  // Network / Infrastructure
  'server','server-network','server-outline',
  'router','router-wireless','router-network',
  'switch','switch-outline','lan','ethernet','wifi','wifi-strength-4',
  'network-outline','ip-network','ip-network-outline',
  // Security
  'shield','shield-lock','shield-check','shield-home','shield-outline',
  'lock','lock-open','key','vpn',
  // Storage & Backup
  'database','database-outline','database-cog','harddisk','harddisk-plus',
  'backup-restore','folder','folder-network','folder-open',
  'cloud','cloud-sync','cloud-upload','cloud-download',
  // Monitoring & Alerts
  'chart-line','chart-bar','chart-areaspline','gauge','speedometer',
  'bell','bell-ring','bell-outline','alert','alert-circle','alert-outline',
  'check-circle','check-circle-outline','heart-pulse',
  // Hardware
  'chip','memory','cpu-64-bit','raspberry-pi',
  'monitor','monitor-multiple','laptop','desktop-classic','television',
  // Containers & Dev
  'docker','kubernetes','code-braces','git','github','gitlab',
  'terminal','console','bash','code-json',
  // Home Automation
  'home-automation','home-lightning-bolt','robot','thermometer',
  'lightbulb','lightbulb-on','power','power-plug',
  // Communication & Scheduling
  'email','email-outline','chat','chat-outline','forum',
  'calendar','clock','timer','calendar-clock',
  // Media
  'music-note','video','camera','camera-outline','television-play',
  // General UI
  'cog','cog-outline','cogs','wrench','tools',
  'web','link','earth','web-box',
  'account','account-group','certificate',
  'sync','refresh','download','upload','file',
  'view-dashboard','view-dashboard-outline','puzzle','puzzle-outline',
  'dns',
];

function isIconSlug(icon) {
  return /^[a-z0-9][a-z0-9-]+$/.test(icon || '');
}

function iconPreviewHTML(icon) {
  icon = icon || '🔗';
  if (icon.startsWith('mdi:')) {
    const name = icon.slice(4);
    return `<img class="svc-preview-img mdi-icon" src="${MDI_CDN}/${name}.svg" alt="${name}" onerror="this.style.opacity='.2'">`;
  }
  if (isIconSlug(icon)) {
    return `<img class="svc-preview-img" src="${ICON_CDN}/${icon}.svg" alt="${icon}" onerror="this.style.opacity='.2'">`;
  }
  if (icon.startsWith('http') || icon.startsWith('/')) {
    return `<img class="svc-preview-img" src="${escHtml(icon)}" alt="icon">`;
  }
  return escHtml(icon);
}

function updatePreview(input) {
  const card = input.closest('.svc-card');
  const preview = card.querySelector('.svc-preview');
  preview.innerHTML = iconPreviewHTML(input.value.trim());
}

// ── Service switching ───────────────────────────────────────────

let activeId = null;
let configOpen = false;
const loaded = new Set();

// Append a unique cache-buster to the iframe's entry URL. The proxied HTML is rewritten
// (base href, injected shims) and changes across proxy updates; without this, browsers
// serve a stale cached document in the iframe — and a hard reload doesn't evict iframe
// caches. Only the entry document carries it; the app's own assets/API calls don't.
function frameSrc(frame) {
  const url = frame.dataset.src;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}_cb=${Date.now()}`;
}

function selectService(id) {
  if (configOpen) closeConfig();

  if (activeId) {
    document.getElementById(`frame-${activeId}`)?.classList.remove('active');
    document.getElementById(`nav-${activeId}`)?.classList.remove('active');
  }

  const frame = document.getElementById(`frame-${id}`);
  const navItem = document.getElementById(`nav-${id}`);
  if (!frame) return;

  if (!loaded.has(id)) {
    frame.src = frameSrc(frame);
    loaded.add(id);
  }

  frame.classList.add('active');
  navItem?.classList.add('active');
  document.getElementById('welcome').style.display = 'none';

  activeId = id;
  try { localStorage.setItem('hub:activeService', id); } catch (_) {}

  document.getElementById('btn-refresh').disabled = false;
}

(function restore() {
  let last;
  try { last = localStorage.getItem('hub:activeService'); } catch (_) {}
  if (last && document.getElementById(`frame-${last}`)) selectService(last);
})();

function refreshActive() {
  if (!activeId) return;
  const frame = document.getElementById(`frame-${activeId}`);
  if (frame) frame.src = frameSrc(frame);
}

function loadAll() {
  const btn = document.getElementById('btn-load-all');
  document.querySelectorAll('.service-frame').forEach(frame => {
    const id = frame.id.replace('frame-', '');
    if (!loaded.has(id)) {
      frame.src = frameSrc(frame);
      loaded.add(id);
    }
  });
  if (btn) { btn.disabled = true; btn.title = 'All tabs loaded'; }
}

// ── Config panel ────────────────────────────────────────────────

async function openConfig() {
  configOpen = true;

  if (activeId) {
    document.getElementById(`frame-${activeId}`)?.classList.remove('active');
    document.getElementById(`nav-${activeId}`)?.classList.remove('active');
  }
  document.getElementById('welcome').style.display = 'none';
  document.getElementById('nav-settings').classList.add('active');

  const panel = document.getElementById('config-panel');
  panel.classList.add('open');
  panel.innerHTML = '<div class="cfg-loading">Loading…</div>';

  try {
    const resp = await fetch('/api/config');
    if (!resp.ok) throw new Error(resp.statusText);
    renderConfigPanel(await resp.json());
  } catch (e) {
    panel.innerHTML = `<div class="cfg-loading" style="color:#ef4444">Failed to load config: ${e.message}</div>`;
  }
}

function closeConfig() {
  configOpen = false;
  document.getElementById('config-panel').classList.remove('open');
  document.getElementById('nav-settings').classList.remove('active');

  if (activeId) {
    document.getElementById(`frame-${activeId}`)?.classList.add('active');
    document.getElementById(`nav-${activeId}`)?.classList.add('active');
  } else {
    document.getElementById('welcome').style.display = '';
  }
}

function renderConfigPanel(config) {
  const app = config.app || {};
  const services = config.services || [];

  document.getElementById('config-panel').innerHTML = `
    <div class="cfg-header">
      <button class="btn-ghost" onclick="closeConfig()">← Back</button>
      <h2>Configuration</h2>
      <div class="cfg-header-actions">
        <button class="btn-ghost" onclick="closeConfig()">Cancel</button>
        <button class="btn-primary" id="cfg-save-btn" onclick="saveConfig()">Save</button>
      </div>
    </div>
    <div class="cfg-body">
      <div class="cfg-section">
        <div class="cfg-section-title">Application</div>
        <div class="cfg-row">
          <div class="cfg-field">
            <label class="cfg-label">Title</label>
            <input id="cfg-title" class="cfg-input" type="text"
                   value="${escHtml(app.title || '')}" placeholder="My Home Lab">
          </div>
          <div class="cfg-field">
            <label class="cfg-label">Theme</label>
            <select id="cfg-theme" class="cfg-input cfg-select">
              <option value="dark"  ${app.theme !== 'light' ? 'selected' : ''}>Dark</option>
              <option value="light" ${app.theme === 'light' ? 'selected' : ''}>Light</option>
            </select>
          </div>
        </div>
      </div>

      <div class="cfg-section">
        <div class="cfg-section-header">
          <div class="cfg-section-title">Services</div>
          <button class="btn-add" onclick="addServiceCard()">+ Add</button>
        </div>
        <div id="services-list">
          ${services.map(serviceCardHTML).join('')}
        </div>
      </div>
    </div>
  `;
}

function serviceCardHTML(svc) {
  svc = svc || {};
  const id        = escHtml(svc.id    || '');
  const name      = escHtml(svc.name  || '');
  const url       = escHtml(svc.url   || '');
  const icon      = escHtml(svc.icon  || '🔗');
  const color     = svc.color      || '#6366f1';
  const proxy     = svc.proxy      ? 'checked' : '';
  const ignoreSsl = svc.ignore_ssl ? 'checked' : '';
  const newTab    = svc.new_tab    ? 'checked' : '';

  return `
    <div class="svc-card" data-orig-id="${id}">
      <div class="svc-card-top">
        <span class="svc-preview">${iconPreviewHTML(svc.icon || '🔗')}</span>
        <input class="f-icon cfg-input inp-icon" type="text"
               value="${icon}" placeholder="🔗 or slug"
               oninput="updatePreview(this)">
        <button class="btn-icon" type="button"
                onclick="openIconPicker(this.closest('.svc-card-top').querySelector('.f-icon'))"
                title="Browse icons">⋯</button>
        <input class="f-name cfg-input inp-grow" type="text"
               value="${name}" placeholder="Service name" required>
        <input class="f-color" type="color" value="${color}" title="Accent color">
        <button class="btn-icon" onclick="moveCard(this,-1)" title="Move up">▲</button>
        <button class="btn-icon" onclick="moveCard(this,1)"  title="Move down">▼</button>
        <button class="btn-icon btn-del" onclick="deleteCard(this)" title="Remove">✕</button>
      </div>
      <div class="svc-card-mid">
        <input class="f-url cfg-input inp-full" type="text"
               value="${url}" placeholder="https://192.168.1.1" required>
      </div>
      <div class="svc-card-bot">
        <label class="toggle-label">
          <input class="f-proxy" type="checkbox" ${proxy}>
          Proxy
        </label>
        <label class="toggle-label">
          <input class="f-ssl" type="checkbox" ${ignoreSsl}>
          Ignore SSL
        </label>
        <label class="toggle-label" title="Open directly in a new browser tab instead of embedding (for apps that resist proxying)">
          <input class="f-newtab" type="checkbox" ${newTab}>
          New tab
        </label>
        ${id ? `<span class="svc-id-hint">id: ${id}</span>` : ''}
      </div>
    </div>`;
}

function addServiceCard() {
  const list = document.getElementById('services-list');
  list.insertAdjacentHTML('beforeend', serviceCardHTML({}));
  const newCard = list.lastElementChild;
  newCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  newCard.querySelector('.f-name')?.focus();
}

function deleteCard(btn) {
  btn.closest('.svc-card').remove();
}

function moveCard(btn, dir) {
  const card = btn.closest('.svc-card');
  const list = card.parentElement;
  if (dir === -1) {
    const prev = card.previousElementSibling;
    if (prev) list.insertBefore(card, prev);
  } else {
    const next = card.nextElementSibling;
    if (next) list.insertBefore(next, card);
  }
}

async function saveConfig() {
  const title = document.getElementById('cfg-title').value.trim();
  const theme = document.getElementById('cfg-theme').value;

  const services = [];
  document.querySelectorAll('#services-list .svc-card').forEach(card => {
    const origId = card.dataset.origId;
    const name   = card.querySelector('.f-name').value.trim();
    const url    = card.querySelector('.f-url').value.trim();
    if (!name || !url) return;

    const id = origId || slugify(name) || `svc-${Date.now()}`;
    services.push({
      id,
      name,
      url,
      icon:       card.querySelector('.f-icon').value.trim() || '🔗',
      color:      card.querySelector('.f-color').value,
      proxy:      card.querySelector('.f-proxy').checked,
      ignore_ssl: card.querySelector('.f-ssl').checked,
      new_tab:    card.querySelector('.f-newtab').checked,
    });
  });

  if (services.length === 0) {
    alert('Add at least one service before saving.');
    return;
  }

  const btn = document.getElementById('cfg-save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving…';

  try {
    const resp = await fetch('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ app: { title, theme }, services }),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(`Save failed: ${err.detail || resp.statusText}`);
      btn.disabled = false;
      btn.textContent = 'Save';
      return;
    }

    location.reload();
  } catch (e) {
    alert(`Save failed: ${e.message}`);
    btn.disabled = false;
    btn.textContent = 'Save';
  }
}

// ── Icon picker ─────────────────────────────────────────────────

let _pickerTarget = null;
let _pickerTab    = 'dash'; // 'dash' | 'mdi'

function openIconPicker(inputEl) {
  _pickerTarget = inputEl;
  _pickerTab    = 'dash';

  let picker = document.getElementById('icon-picker');
  if (!picker) {
    picker = document.createElement('div');
    picker.id = 'icon-picker';
    picker.className = 'icon-picker';
    document.body.appendChild(picker);
  }

  picker.innerHTML = `
    <div class="ip-backdrop" onclick="closeIconPicker()"></div>
    <div class="ip-modal">
      <div class="ip-tabs">
        <button class="ip-tab active" type="button" onclick="switchPickerTab('dash',this)">App Icons</button>
        <button class="ip-tab" type="button" onclick="switchPickerTab('mdi',this)">MDI</button>
      </div>
      <div class="ip-header">
        <input class="ip-search cfg-input" type="text"
               placeholder="Search icons…"
               oninput="_ipFilter(this.value)"
               onkeydown="_ipKey(event)">
        <span class="ip-count" id="ip-count"></span>
      </div>
      <div class="ip-grid" id="ip-grid"></div>
    </div>`;

  const current  = inputEl.value.trim();
  const searchEl = picker.querySelector('.ip-search');

  if (current.startsWith('mdi:')) {
    _pickerTab = 'mdi';
    picker.querySelectorAll('.ip-tab')[0].classList.remove('active');
    picker.querySelectorAll('.ip-tab')[1].classList.add('active');
    searchEl.value = current.slice(4);
  } else {
    searchEl.value = isIconSlug(current) ? current : '';
  }

  picker.classList.add('open');
  renderIconGrid(searchEl.value);
  setTimeout(() => { searchEl.focus(); searchEl.select(); }, 40);
}

function switchPickerTab(tab, btnEl) {
  _pickerTab = tab;
  document.querySelectorAll('.ip-tab').forEach(b => b.classList.remove('active'));
  btnEl.classList.add('active');
  const query = document.querySelector('.ip-search')?.value.trim().toLowerCase() || '';
  renderIconGrid(query);
}

function closeIconPicker() {
  document.getElementById('icon-picker')?.classList.remove('open');
  _pickerTarget = null;
}

function _ipFilter(raw) {
  renderIconGrid(raw.trim().toLowerCase());
}

function _ipKey(e) {
  if (e.key === 'Escape') { closeIconPicker(); return; }
  if (e.key === 'Enter') {
    const first = document.querySelector('#ip-grid .ip-item');
    if (first) first.click();
  }
}

function _normalizeQuery(q) {
  // Strip dashes and spaces so "pihole" matches "pi-hole", "adguard home" matches "adguard-home"
  return q.replace(/[-\s]/g, '');
}

function renderIconGrid(query) {
  const grid   = document.getElementById('ip-grid');
  const count  = document.getElementById('ip-count');
  const isMdi  = _pickerTab === 'mdi';
  const icons  = isMdi ? MDI_ICONS : DASHBOARD_ICONS;
  const cdn    = isMdi ? MDI_CDN : ICON_CDN;
  const prefix = isMdi ? 'mdi:' : '';
  const imgCls = isMdi ? 'mdi-icon' : '';

  let hits;
  if (!query) {
    hits = icons;
  } else {
    const norm = _normalizeQuery(query);
    hits = icons.filter(s => s.includes(query) || _normalizeQuery(s).includes(norm));
  }

  if (count) count.textContent = query ? `${hits.length} result${hits.length !== 1 ? 's' : ''}` : '';

  if (!hits.length) {
    if (isMdi && query) {
      // Try the typed name directly from CDN — any valid MDI name works
      const name = query.replace(/^mdi:/, '');
      grid.innerHTML = `
        <button class="ip-custom-try" type="button" onclick="selectIcon('mdi:${escHtml(name)}')" title="mdi:${escHtml(name)}">
          <img src="${MDI_CDN}/${escHtml(name)}.svg" alt="${escHtml(name)}" class="mdi-icon"
               onerror="this.closest('.ip-custom-try').classList.add('ip-custom-invalid')">
          <span class="ip-custom-label">
            <strong>mdi:${escHtml(name)}</strong>
            <em>not in common list — click to use if icon loads</em>
          </span>
        </button>`;
    } else {
      grid.innerHTML = `<div class="ip-empty">No icons match "<strong>${escHtml(query)}</strong>"</div>`;
    }
    return;
  }

  grid.innerHTML = hits.map(slug => `
    <button class="ip-item" type="button" onclick="selectIcon('${prefix}${slug}')" title="${prefix}${slug}">
      <img src="${cdn}/${slug}.svg" alt="${slug}" class="${imgCls}" onerror="this.style.opacity='.15'">
      <span>${slug}</span>
    </button>`).join('');
}

function selectIcon(slug) {
  if (_pickerTarget) {
    _pickerTarget.value = slug;
    updatePreview(_pickerTarget);
  }
  closeIconPicker();
}

// ── Helpers ─────────────────────────────────────────────────────

function escHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function slugify(str) {
  return str.toLowerCase().trim()
    .replace(/[^\w\s-]/g, '')
    .replace(/[\s_]+/g, '-')
    .replace(/^-+|-+$/g, '');
}
