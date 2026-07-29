/*
    teams-meeting.js — boots the Jitsi embed on the call page.

    The one thing here that is not obvious and matters most:

        api.dispose() MUST run when leaving the page.

    Turbo swaps the <body> without a reload, so the iframe and the media
    tracks it holds survive a navigation unless they are explicitly torn
    down. The visible symptom is the camera light staying on after someone
    has left the call — which is the single worst bug a meeting feature can
    ship with, because it looks like spying.
*/
(function () {
    "use strict";

    function init() {
        var root = document.querySelector("[data-tm-call]");
        if (!root) return;
        if (root.dataset.tmReady === "1") return;   // idempotent per DOM
        root.dataset.tmReady = "1";

        var stage = document.getElementById("teamsCallStage");
        var fallback = root.querySelector("[data-tm-call-fallback]");
        var configNode = document.getElementById("teamsCallConfig");
        if (!stage || !configNode) return;

        function showFallback() {
            stage.hidden = true;
            if (fallback) fallback.hidden = false;
        }

        // No SDK means the Jitsi host is unreachable or blocked. Say so and
        // offer the direct link rather than leaving an empty grey box.
        if (typeof window.JitsiMeetExternalAPI !== "function") {
            showFallback();
            return;
        }

        var config;
        try {
            config = JSON.parse(configNode.textContent);
        } catch (e) {
            showFallback();
            return;
        }

        var api;
        try {
            api = new window.JitsiMeetExternalAPI(config.domain, {
                roomName: config.roomName,
                parentNode: stage,
                jwt: config.jwt || undefined,
                userInfo: config.userInfo || {},
                configOverwrite: config.configOverwrite || {},
                interfaceConfigOverwrite: config.interfaceConfigOverwrite || {}
            });
        } catch (e) {
            showFallback();
            return;
        }

        function dispose() {
            if (!api) return;
            try { api.dispose(); } catch (e) { /* already gone */ }
            api = null;
        }

        // Hanging up returns to the meeting rather than leaving a dead
        // frame on screen.
        api.addEventListener("readyToClose", function () {
            dispose();
            var back = root.dataset.leaveUrl;
            if (back) {
                if (window.Turbo) window.Turbo.visit(back);
                else window.location.href = back;
            }
        });

        // Three teardown paths, because each covers a case the others miss:
        // App.onCleanup for a Turbo navigation inside the shell, pagehide
        // for a full page load or a closed tab, and readyToClose above for
        // hanging up. Calling dispose twice is harmless; not calling it is
        // a camera that stays on.
        if (window.App && window.App.onCleanup) window.App.onCleanup(dispose);
        window.addEventListener("pagehide", dispose, { once: true });
    }

    // Loaded from <head> and never re-executed by Turbo, so this registers
    // a persistent listener and also runs for the current document - the
    // same reasoning as teams-chat.js.
    document.addEventListener("turbo:load", init);

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init, { once: true });
    } else {
        init();
    }
})();
