/* ══════════════════════════════════════
   LAWA Scouts — WebSocket Client Manager
   ══════════════════════════════════════
   Single persistent connection for all real-time events:
   notifications, comments, reactions, presence.
*/

window.LawaWS = (function () {
    let ws = null;
    let connected = false;
    let reconnectTimer = null;
    let heartbeatTimer = null;
    let reconnectDelay = 1000;
    const MAX_RECONNECT_DELAY = 15000;
    const HEARTBEAT_INTERVAL = 30000;
    const listeners = {};

    function getWsUrl() {
        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        return `${proto}//${window.location.host}/ws`;
    }

    function connect() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

        try {
            ws = new WebSocket(getWsUrl());
        } catch (e) {
            scheduleReconnect();
            return;
        }

        ws.onopen = function () {
            connected = true;
            reconnectDelay = 1000;
            startHeartbeat();
            emit('connected', {});
        };

        ws.onmessage = function (event) {
            try {
                const msg = JSON.parse(event.data);
                if (msg.type === 'pong') return;
                emit(msg.type, msg.data || msg);
            } catch {}
        };

        ws.onclose = function () {
            connected = false;
            stopHeartbeat();
            scheduleReconnect();
        };

        ws.onerror = function () {
            connected = false;
        };
    }

    function scheduleReconnect() {
        if (reconnectTimer) return;
        reconnectTimer = setTimeout(() => {
            reconnectTimer = null;
            reconnectDelay = Math.min(reconnectDelay * 1.5, MAX_RECONNECT_DELAY);
            connect();
        }, reconnectDelay);
    }

    function startHeartbeat() {
        stopHeartbeat();
        heartbeatTimer = setInterval(() => {
            send({ action: 'ping' });
        }, HEARTBEAT_INTERVAL);
    }

    function stopHeartbeat() {
        if (heartbeatTimer) {
            clearInterval(heartbeatTimer);
            heartbeatTimer = null;
        }
    }

    function send(data) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(data));
        }
    }

    function on(event, callback) {
        if (!listeners[event]) listeners[event] = [];
        listeners[event].push(callback);
        return () => {
            listeners[event] = listeners[event].filter(cb => cb !== callback);
        };
    }

    function off(event, callback) {
        if (listeners[event]) {
            listeners[event] = callback
                ? listeners[event].filter(cb => cb !== callback)
                : [];
        }
    }

    function emit(event, data) {
        (listeners[event] || []).forEach(cb => {
            try { cb(data); } catch (e) { console.warn('WS listener error:', e); }
        });
    }

    // Auto-connect when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', connect);
    } else {
        connect();
    }

    // Reconnect after SPA navigation
    window.addEventListener('spa-navigated', function () {
        if (!connected) connect();
    });

    return {
        get connected() { return connected; },
        send,
        on,
        off,
        connect,
    };
})();
