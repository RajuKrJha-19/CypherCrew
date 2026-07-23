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

    window.App = App;
})(window, document);
