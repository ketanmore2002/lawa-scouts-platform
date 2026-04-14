/* ══════════════════════════════════════
   LAWA Scouts — SPA Client-Side Router
   ══════════════════════════════════════
   Intercepts internal link clicks, fetches pages via AJAX,
   swaps only <main> content. Navbar, WebSocket, Alpine globals stay alive.
*/

(function () {
    const MAIN_SELECTOR = 'main.main-content';
    const PROGRESS_ID = 'spa-progress';
    let progressEl = null;
    let prefetchCache = new Map();

    function getProgress() {
        if (!progressEl) {
            progressEl = document.getElementById(PROGRESS_ID);
            if (!progressEl) {
                progressEl = document.createElement('div');
                progressEl.id = PROGRESS_ID;
                progressEl.style.width = '0%';
                document.body.prepend(progressEl);
            }
        }
        return progressEl;
    }

    function showProgress() {
        const p = getProgress();
        p.style.width = '0%';
        p.style.opacity = '1';
        requestAnimationFrame(() => { p.style.width = '30%'; });
    }

    function advanceProgress() {
        const p = getProgress();
        p.style.width = '70%';
    }

    function finishProgress() {
        const p = getProgress();
        p.style.width = '100%';
        setTimeout(() => { p.style.opacity = '0'; p.style.width = '0%'; }, 300);
    }

    function isInternalLink(url) {
        try {
            const u = new URL(url, window.location.origin);
            if (u.origin !== window.location.origin) return false;
            // Don't SPA-navigate auth, API, static, or WS routes
            const skip = ['/login', '/api/', '/static/', '/ws', '/shared/'];
            return !skip.some(s => u.pathname.startsWith(s));
        } catch {
            return false;
        }
    }

    async function fetchPage(url) {
        const cached = prefetchCache.get(url);
        if (cached && Date.now() - cached.ts < 15000) {
            return cached.html;
        }
        const res = await fetch(url, {
            headers: { 'X-Requested-With': 'SPA' },
            credentials: 'same-origin',
        });
        if (!res.ok || res.redirected) {
            // If redirected to login, do full navigation
            if (res.url && res.url.includes('/login')) {
                window.location.href = res.url;
                return null;
            }
            return null;
        }
        return await res.text();
    }

    function extractMain(html) {
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const main = doc.querySelector(MAIN_SELECTOR);
        // Also extract any page-specific scripts
        const scripts = doc.querySelectorAll('script:not([src])');
        const pageScripts = [];
        scripts.forEach(s => {
            // Only include scripts that are inside or after main (page-specific)
            if (s.textContent && !s.textContent.includes('function showToast') && !s.textContent.includes('function apiCall')) {
                pageScripts.push(s.textContent);
            }
        });
        return main ? { html: main.innerHTML, scripts: pageScripts, title: doc.title } : null;
    }

    async function navigate(url, pushState = true) {
        if (!isInternalLink(url)) {
            window.location.href = url;
            return;
        }

        showProgress();

        try {
            const html = await fetchPage(url);
            advanceProgress();

            if (!html) {
                window.location.href = url;
                return;
            }

            const content = extractMain(html);
            if (!content) {
                window.location.href = url;
                return;
            }

            // Swap content
            const main = document.querySelector(MAIN_SELECTOR);
            if (!main) {
                window.location.href = url;
                return;
            }

            // Clear existing Alpine components in main
            main.innerHTML = content.html;

            // Add transition class
            main.classList.remove('spa-entering');
            void main.offsetWidth; // force reflow
            main.classList.add('spa-entering');

            // Update title
            if (content.title) document.title = content.title;

            // Update URL
            if (pushState) {
                history.pushState({ spaUrl: url }, content.title || '', url);
            }

            // Execute page-specific scripts
            content.scripts.forEach(scriptText => {
                const script = document.createElement('script');
                script.textContent = scriptText;
                document.body.appendChild(script);
                script.remove();
            });

            // Re-initialize Alpine on the new content
            if (window.Alpine) {
                window.Alpine.initTree(main);
            }

            // Scroll to top
            window.scrollTo(0, 0);

            // Invalidate API cache on navigation
            if (typeof invalidateCache === 'function') invalidateCache();

            finishProgress();

            // Dispatch custom event for page scripts that need to know about navigation
            window.dispatchEvent(new CustomEvent('spa-navigated', { detail: { url } }));

        } catch (err) {
            console.warn('SPA navigation failed, falling back:', err);
            window.location.href = url;
        }
    }

    // Global navigateTo function
    window.navigateTo = function (url) {
        navigate(url, true);
    };

    // Intercept link clicks
    document.addEventListener('click', function (e) {
        const link = e.target.closest('a[href]');
        if (!link) return;

        const href = link.getAttribute('href');
        if (!href || href.startsWith('#') || href.startsWith('javascript:')) return;
        if (link.target === '_blank' || link.hasAttribute('download')) return;
        if (e.ctrlKey || e.metaKey || e.shiftKey) return;
        // Skip links with data-no-spa attribute
        if (link.hasAttribute('data-no-spa')) return;

        const url = new URL(href, window.location.origin).href;
        if (isInternalLink(url)) {
            e.preventDefault();
            navigate(url, true);
        }
    });

    // Handle back/forward
    window.addEventListener('popstate', function (e) {
        if (e.state && e.state.spaUrl) {
            navigate(e.state.spaUrl, false);
        } else {
            navigate(window.location.href, false);
        }
    });

    // Prefetch on hover (100ms debounce)
    let prefetchTimer = null;
    document.addEventListener('mouseover', function (e) {
        const link = e.target.closest('a[href]');
        if (!link) return;
        const href = link.getAttribute('href');
        if (!href) return;
        const url = new URL(href, window.location.origin).href;
        if (!isInternalLink(url)) return;
        if (prefetchCache.has(url)) return;

        clearTimeout(prefetchTimer);
        prefetchTimer = setTimeout(async () => {
            try {
                const html = await fetchPage(url);
                if (html) prefetchCache.set(url, { html, ts: Date.now() });
            } catch {}
        }, 100);
    });

    document.addEventListener('mouseout', function () {
        clearTimeout(prefetchTimer);
    });

    // Set initial state
    history.replaceState({ spaUrl: window.location.href }, document.title, window.location.href);
})();
