/*
    turbo-app.js — thin lifecycle layer over Hotwire Turbo Drive.

    Turbo swaps the <body> on navigation and re-runs its inline scripts,
    so anything a page sets up (setInterval pollers, document listeners,
    Chart instances) must be torn down before the next page or it leaks
    and, worse, multiplies server polling. Page scripts register through
    window.App; teardown runs automatically before every render.

    The app shell (sidebar + topbar) is marked data-turbo-permanent, so
    those elements and their inline scripts are preserved across
    navigations and run exactly once — this file only handles the two
    things permanence can't: page-level teardown, and re-applying the
    shell's per-page state (active nav item, collapsed sidebar) after the
    body is swapped.

    Everything degrades gracefully when Turbo is absent (kill-switch or
    load failure): App.ready falls back to DOMContentLoaded and the
    tracked primitives behave like plain setInterval/addEventListener.
*/
(function (window, document) {
    "use strict";

    var cleanups = [];

    var App = {
        // Arbitrary teardown, run before the next navigation.
        onCleanup: function (fn) {
            cleanups.push(fn);
            return fn;
        },

        // setInterval that is cleared automatically on navigation.
        setInterval: function (fn, ms) {
            var id = window.setInterval(fn, ms);
            cleanups.push(function () { window.clearInterval(id); });
            return id;
        },

        // document/window listener removed automatically on navigation.
        on: function (target, type, handler, opts) {
            target.addEventListener(type, handler, opts);
            cleanups.push(function () { target.removeEventListener(type, handler, opts); });
            return handler;
        },

        // A Chart.js instance destroyed automatically on navigation, so
        // the canvas is free when the page is revisited.
        trackChart: function (chart) {
            cleanups.push(function () { try { chart.destroy(); } catch (e) {} });
            return chart;
        },

        // Run once for this page view — on first load and on every Turbo
        // navigation. Falls back to DOMContentLoaded without Turbo.
        ready: function (fn) {
            if (window.Turbo) {
                document.addEventListener("turbo:load", fn, { once: true });
            } else if (document.readyState !== "loading") {
                fn();
            } else {
                document.addEventListener("DOMContentLoaded", fn, { once: true });
            }
        },

        // Keep keyboard focus inside an open modal/dialog `container`:
        // Tab / Shift+Tab cycle within it instead of escaping to the page
        // behind, and focus is returned to whatever was focused before it
        // opened once released. Returns a release() function the caller MUST
        // call when the modal closes. Escape is left to the caller unless an
        // onEscape option is given, so a modal with its own Escape handling
        // isn't double-fired.
        trapFocus: function (container, opts) {
            opts = opts || {};
            var FOCUSABLE = 'a[href], button:not([disabled]), ' +
                'textarea:not([disabled]), select:not([disabled]), ' +
                'input:not([disabled]):not([type="hidden"]), ' +
                '[tabindex]:not([tabindex="-1"])';
            var previouslyFocused = document.activeElement;

            function items() {
                return Array.prototype.slice
                    .call(container.querySelectorAll(FOCUSABLE))
                    .filter(function (el) {
                        // Skip hidden controls; keep the current one so an
                        // empty visible list never traps focus nowhere.
                        return el.offsetWidth > 0 || el.offsetHeight > 0 ||
                            el === document.activeElement;
                    });
            }

            function onKeydown(event) {
                if (event.key === "Escape" &&
                    typeof opts.onEscape === "function") {
                    opts.onEscape(event);
                    return;
                }
                if (event.key !== "Tab") return;
                var list = items();
                if (!list.length) { event.preventDefault(); return; }
                var first = list[0];
                var last = list[list.length - 1];
                var active = document.activeElement;
                if (event.shiftKey) {
                    if (active === first || !container.contains(active)) {
                        event.preventDefault();
                        last.focus();
                    }
                } else if (active === last || !container.contains(active)) {
                    event.preventDefault();
                    first.focus();
                }
            }

            // Capture phase so this runs before a modal's own bubble-phase
            // key handlers, and survives stopPropagation inside the modal.
            document.addEventListener("keydown", onKeydown, true);

            if (opts.initialFocus !== false) {
                var target = (opts.initialFocus && opts.initialFocus.focus)
                    ? opts.initialFocus
                    : (items()[0] || container);
                try { target.focus(); } catch (e) {}
            }

            return function release() {
                document.removeEventListener("keydown", onKeydown, true);
                if (opts.restoreFocus !== false && previouslyFocused &&
                    typeof previouslyFocused.focus === "function") {
                    try { previouslyFocused.focus(); } catch (e) {}
                }
            };
        }
    };

    function runCleanups() {
        var list = cleanups;
        cleanups = [];
        for (var i = 0; i < list.length; i++) {
            try { list[i](); } catch (e) { /* keep tearing down */ }
        }
    }

    // Both fire before Turbo leaves a page; running on each (the list is
    // emptied, so the second call is a no-op) makes teardown reliable
    // whether or not the page is being cached.
    document.addEventListener("turbo:before-cache", runCleanups);
    document.addEventListener("turbo:before-render", runCleanups);

    // --- Shell state that must survive a body swap -----------------------

    // The collapsed-sidebar flag lives on <body>, which Turbo re-renders
    // from the incoming page (without the class). Re-apply it to the new
    // body BEFORE it is shown, so there is no expand/collapse flash.
    document.addEventListener("turbo:before-render", function (event) {
        try {
            if (localStorage.getItem("sidebar_collapsed") === "yes" && event.detail && event.detail.newBody) {
                event.detail.newBody.classList.add("sidebar-collapsed");
            }
        } catch (e) {}
    });

    // The sidebar is a permanent element, so its server-rendered ".active"
    // highlight freezes on the first page. Recompute it after each
    // navigation by longest-prefix-matching the path, falling back to the
    // Dashboard link (its href is "/", which the role dashboards redirect
    // through and so never match directly).
    function updateActiveNav() {
        var links = document.querySelectorAll(".sidebar-nav a");
        if (!links.length) return;

        var path = window.location.pathname;
        var best = null;
        var bestLen = -1;
        var dashboardLink = null;

        links.forEach(function (a) {
            var linkPath;
            try { linkPath = new URL(a.href).pathname; } catch (e) { return; }

            if (linkPath === "/") { dashboardLink = a; return; }

            if (path === linkPath || path.indexOf(linkPath + "/") === 0) {
                if (linkPath.length > bestLen) { best = a; bestLen = linkPath.length; }
            }
        });

        var chosen = best || dashboardLink;
        links.forEach(function (a) { a.classList.toggle("active", a === chosen); });
    }

    document.addEventListener("turbo:load", updateActiveNav);

    // --- External-redirect guard (belt-and-suspenders) -------------------
    //
    // Some same-origin forms/links 302-redirect to a provider's consent
    // screen (OAuth connect: Zoho, Meta, Google). Turbo submits via fetch and
    // CANNOT follow a cross-origin redirect, so such a control appears dead.
    // Templates set data-turbo="false" explicitly; this auto-tags the known
    // external-redirect endpoints by URL pattern BEFORE any submit, so a
    // forgotten attribute never silently breaks an OAuth button again.
    //
    // Pattern-based on purpose: reading the cross-origin response to detect
    // the redirect is impossible (it is opaque), so we prevent Turbo from
    // ever fetching these instead. Add new external-redirect paths here.
    var EXTERNAL_REDIRECT = /(\/oauth\/|\/attendance\/connect)/;

    function tagExternalRedirects() {
        var nodes = document.querySelectorAll("form[action], a[href]");
        for (var i = 0; i < nodes.length; i++) {
            var el = nodes[i];
            var url = el.getAttribute("action") || el.getAttribute("href") || "";
            // Never override an explicit choice already on the element.
            if (EXTERNAL_REDIRECT.test(url) && !el.hasAttribute("data-turbo")) {
                el.setAttribute("data-turbo", "false");
            }
        }
    }

    document.addEventListener("turbo:load", tagExternalRedirects);
    if (!window.Turbo) {
        document.addEventListener("DOMContentLoaded", tagExternalRedirects);
    }

    window.App = App;
})(window, document);
