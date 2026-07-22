// Generic live-stat poller. Any element with data-live-stat="<key>"
// gets its text patched from the JSON response of
// window.CYPHER_LIVE_STATS_API whenever that key's value changes -
// no page reload. Deliberately generic (keyed off the JSON payload,
// not hardcoded to any one dashboard) so the same file can back
// every dashboard variant without duplicating this logic per page.
(function () {

    const POLL_INTERVAL_MS = 10000;

    const nodes = document.querySelectorAll("[data-live-stat]");
    const endpoint = window.CYPHER_LIVE_STATS_API;

    if (!nodes.length || !endpoint) return;

    function flash(el) {
        el.classList.remove("stat-live-updated");
        // restart the animation even if it just played
        void el.offsetWidth;
        el.classList.add("stat-live-updated");
    }

    async function poll() {
        try {
            const response = await fetch(endpoint, { cache: "no-store" });

            if (!response.ok) return;

            const data = await response.json();

            if (!data || data.success === false) return;

            nodes.forEach(function (el) {
                const key = el.dataset.liveStat;

                if (!(key in data)) return;

                const newValue = String(data[key]);

                if (el.textContent.trim() !== newValue) {
                    el.textContent = newValue;
                    flash(el);
                }
            });

        } catch (error) {
            console.log("Live stats fetch error:", error);
        }
    }

    setInterval(poll, POLL_INTERVAL_MS);
})();
