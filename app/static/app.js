/* ══════════════════════════════════════
   LAWA Scouts — Shared JS
   ══════════════════════════════════════ */

function showToast(message, type = 'success') {
    const c = document.getElementById('toast-container');
    const t = document.createElement('div');
    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    t.style.cssText = 'padding:14px 20px;border-radius:12px;font-size:14px;font-family:Inter,sans-serif;backdrop-filter:blur(12px);transition:all .3s;animation:fadeInUp .3s ease;border-left:3px solid;box-shadow:0 4px 12px rgba(0,0,0,.1);';
    if (type === 'success') {
        t.style.background = isLight ? 'rgba(16,185,129,.12)' : 'rgba(16,185,129,.15)';
        t.style.borderLeftColor = '#10b981';
        t.style.color = isLight ? '#059669' : '#34d399';
    } else {
        t.style.background = isLight ? 'rgba(239,68,68,.1)' : 'rgba(239,68,68,.15)';
        t.style.borderLeftColor = '#ef4444';
        t.style.color = isLight ? '#dc2626' : '#f87171';
    }
    t.textContent = message;
    c.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; t.style.transform = 'translateY(-8px)'; setTimeout(() => t.remove(), 300); }, 3000);
}

/* ── API helper with in-memory GET cache ── */
const _apiCache = new Map();
const API_CACHE_TTL = 60000; // 60s

async function apiCall(url, method = 'GET', body = null, opts = {}) {
    const cacheKey = method === 'GET' && !opts.noCache ? url : null;
    if (cacheKey) {
        const cached = _apiCache.get(cacheKey);
        if (cached && Date.now() - cached.ts < API_CACHE_TTL) {
            return cached.data;
        }
    }

    const fetchOpts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body) fetchOpts.body = JSON.stringify(body);
    const res = await fetch(url, fetchOpts);
    if (res.status === 401) {
        document.cookie = 'access_token=; Max-Age=0; path=/';
        if (window.location.pathname !== '/login') {
            window.location.href = '/login';
        }
        throw new Error('Session expired');
    }
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Request failed' }));
        throw new Error(err.detail || 'Request failed');
    }
    const data = await res.json();

    if (cacheKey) {
        _apiCache.set(cacheKey, { data, ts: Date.now() });
    }

    // Any write (POST/PUT/PATCH/DELETE) implicitly invalidates the cached GETs
    // for the same URL family, so lists reflect the change on next fetch.
    if (method !== 'GET' && !opts.skipAutoInvalidate) {
        const parent = url.split('?')[0].replace(/\/$/, '').replace(/\/[^/]+$/, '');
        invalidateCache(parent);
    }
    return data;
}

function invalidateCache(pattern) {
    if (!pattern) { _apiCache.clear(); return; }
    for (const key of _apiCache.keys()) {
        if (key.includes(pattern)) _apiCache.delete(key);
    }
}

/* ── Notification bell (now uses WebSocket when available) ── */
function notifBell() {
    return {
        notifications: [],
        unreadCount: 0,
        showPanel: false,
        _loaded: false,
        _loading: false,
        _interval: null,
        _wsBound: false,
        _bindWS() {
            if (this._wsBound || !(window.LawaWS && window.LawaWS.connected)) return;
            this._wsBound = true;
            window.LawaWS.on('notification', (data) => {
                if (typeof data.unread_count === 'number') {
                    this.unreadCount = data.unread_count;
                } else {
                    this.unreadCount += 1;
                }
                // Prepend the notification if a full payload was sent
                if (data && data.id) {
                    // Avoid duplicates
                    if (!this.notifications.some(x => x.id === data.id)) {
                        this.notifications = [data, ...this.notifications].slice(0, 50);
                    }
                }
            });
        },
        async startPolling() {
            // Prefetch first page so opening the panel is instant
            this._prefetch();
            this._bindWS();
            if (!this._wsBound) {
                this._interval = setInterval(() => this.fetchCount(), 30000);
                const checkWS = setInterval(() => {
                    if (window.LawaWS && window.LawaWS.connected) {
                        clearInterval(this._interval);
                        clearInterval(checkWS);
                        this._bindWS();
                    }
                }, 2000);
            }
        },
        async _prefetch() {
            if (this._loading) return;
            this._loading = true;
            try {
                const res = await apiCall('/api/notifications?per_page=20', 'GET', null, { noCache: true });
                this.notifications = res.notifications || [];
                this.unreadCount = res.unread_count || 0;
                this._loaded = true;
            } catch (e) {}
            finally { this._loading = false; }
        },
        async fetchCount() {
            try {
                const res = await apiCall('/api/notifications/unread-count', 'GET', null, { noCache: true });
                this.unreadCount = res.count;
            } catch (e) {}
        },
        togglePanel() {
            this.showPanel = !this.showPanel;
            if (this.showPanel) {
                // Refresh in background; don't block panel opening
                this._prefetch();
            }
        },
        markAllRead() {
            // Optimistic clear
            const prevNotifs = this.notifications;
            const prevCount = this.unreadCount;
            this.notifications = [];
            this.unreadCount = 0;
            apiCall('/api/notifications/read-all', 'PUT').catch(() => {
                this.notifications = prevNotifs;
                this.unreadCount = prevCount;
            });
        },
        clickNotif(n) {
            // Optimistic remove + navigate immediately
            const wasUnread = !n.is_read;
            this.notifications = this.notifications.filter(x => x.id !== n.id);
            if (wasUnread) {
                this.unreadCount = Math.max(0, this.unreadCount - 1);
                apiCall(`/api/notifications/${n.id}/read`, 'PUT').catch(() => {});
            }
            this.showPanel = false;
            if (n.link) navigateTo(n.link);
        },
        notifIcon(type) {
            const icons = {
                mention: '<svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 12a4 4 0 10-8 0 4 4 0 008 0zm0 0v1.5a2.5 2.5 0 005 0V12a9 9 0 10-9 9"/></svg>',
                comment_reply: '<svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg>',
                workspace_invite: '<svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M3 8l7.89 5.26a2 2 0 002.22 0L21 8M5 19h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z"/></svg>',
                new_report: '<svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>',
            };
            return icons[type] || '<svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6 6 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"/></svg>';
        },
        notifIconStyle(type) {
            const styles = {
                mention: 'background:rgba(99,102,241,.15);color:#818cf8;',
                comment_reply: 'background:rgba(16,185,129,.15);color:#34d399;',
                workspace_invite: 'background:rgba(168,85,247,.15);color:#a78bfa;',
                new_report: 'background:rgba(34,211,238,.15);color:#22d3ee;',
            };
            return styles[type] || 'background:var(--surface);color:var(--text-muted);';
        },
        timeAgo(iso) {
            if (!iso) return '';
            const diff = Date.now() - new Date(iso).getTime();
            const mins = Math.floor(diff / 60000);
            if (mins < 1) return 'just now';
            if (mins < 60) return mins + 'm ago';
            const hrs = Math.floor(mins / 60);
            if (hrs < 24) return hrs + 'h ago';
            const days = Math.floor(hrs / 24);
            if (days < 7) return days + 'd ago';
            return new Date(iso).toLocaleDateString();
        },
    }
}

/* ── User nav ── */
function userNav() {
    return {
        user: null,
        open: false,
        async init() {
            try {
                const res = await fetch('/api/auth/me');
                if (res.ok) this.user = await res.json();
            } catch (e) { /* not logged in */ }
        },
        async logout() {
            await fetch('/api/auth/logout', { method: 'POST' });
            window.location.href = '/login';
        }
    }
}

/* ── Theme toggle ── */
function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    const darkIcon = document.querySelector('.theme-icon-dark');
    const lightIcon = document.querySelector('.theme-icon-light');
    if (darkIcon) darkIcon.style.display = theme === 'light' ? 'block' : 'none';
    if (lightIcon) lightIcon.style.display = theme === 'dark' ? 'block' : 'none';
}
function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    localStorage.setItem('lawa-theme', next);
    applyTheme(next);
    window.dispatchEvent(new CustomEvent('theme-changed', { detail: { theme: next } }));
}
// Apply saved theme immediately
(function() {
    const saved = localStorage.getItem('lawa-theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
    applyTheme(saved);
})();
