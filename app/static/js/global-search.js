/*
    Global search dropdown.

    Progressive enhancement over the sidebar search form: the form works
    on its own (Enter -> full results page), and this layers a live,
    keyboard-navigable dropdown on top. Talks to /search/suggest, which
    is already permission-scoped server-side, so nothing here needs to
    know who may see what.

    Keys:
      /            focus the search (unless already typing somewhere)
      Down / Up    move through results
      Enter        open the highlighted result, else submit -> results page
      Esc          close the dropdown (second press blurs)
*/
(function () {
    "use strict";

    const form = document.querySelector(".global-search");
    if (!form) return;

    const input = form.querySelector("#globalSearchInput");
    const dropdown = form.querySelector("#globalSearchDropdown");
    if (!input || !dropdown) return;

    const DEBOUNCE_MS = 160;
    const MIN_CHARS = 2;

    let timer = null;
    let items = [];          // flat list of anchor elements, for arrow nav
    let activeIndex = -1;
    let lastQuery = "";
    let controller = null;

    const ICONS = {
        task: "fa-list-check",
        client: "fa-building",
        user: "fa-user",
        note: "fa-note-sticky",
    };

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function close() {
        dropdown.hidden = true;
        dropdown.innerHTML = "";
        items = [];
        activeIndex = -1;
    }

    function setActive(index) {
        if (!items.length) return;

        // wrap around
        activeIndex = (index + items.length) % items.length;

        items.forEach(function (el, i) {
            el.classList.toggle("is-active", i === activeIndex);
        });

        items[activeIndex].scrollIntoView({ block: "nearest" });
    }

    function render(data) {
        if (!data.groups || !data.groups.length) {
            dropdown.innerHTML =
                '<div class="gs-empty">No matches for “' +
                escapeHtml(data.query) +
                '”</div>';
            dropdown.hidden = false;
            items = [];
            activeIndex = -1;
            return;
        }

        let html = "";

        data.groups.forEach(function (group) {
            html += '<div class="gs-group-label">' + escapeHtml(group.label) + "</div>";

            group.items.forEach(function (item) {
                const icon = ICONS[item.type] || "fa-magnifying-glass";
                const code = item.code
                    ? '<span class="gs-code">' + escapeHtml(item.code) + "</span>"
                    : "";
                const badge = item.status
                    ? '<span class="gs-badge gs-badge-' +
                      item.type +
                      '">' + escapeHtml(item.status) + "</span>"
                    : "";

                html +=
                    '<a class="gs-item" href="' + item.url + '" role="option">' +
                        '<span class="gs-item-icon"><i class="fa-solid ' + icon + '"></i></span>' +
                        '<span class="gs-item-body">' +
                            '<span class="gs-item-title">' + code + escapeHtml(item.title) + "</span>" +
                            '<span class="gs-item-sub">' + escapeHtml(item.subtitle) + "</span>" +
                        "</span>" +
                        badge +
                    "</a>";
            });
        });

        html +=
            '<a class="gs-all" href="' + data.results_url + '">' +
            "See all results for “" + escapeHtml(data.query) + "”</a>";

        dropdown.innerHTML = html;
        dropdown.hidden = false;

        items = Array.prototype.slice.call(dropdown.querySelectorAll(".gs-item, .gs-all"));
        activeIndex = -1;
    }

    async function fetchSuggestions(query) {
        if (controller) controller.abort();
        controller = new AbortController();

        try {
            const response = await fetch(
                "/search/suggest?q=" + encodeURIComponent(query),
                { signal: controller.signal, cache: "no-store" }
            );
            if (!response.ok) return;

            const data = await response.json();
            // Ignore a response that arrived after the box moved on.
            if (data.query !== input.value.trim()) return;

            render(data);
        } catch (error) {
            if (error.name !== "AbortError") {
                console.log("Search error:", error);
            }
        }
    }

    input.addEventListener("input", function () {
        const query = input.value.trim();

        if (query === lastQuery) return;
        lastQuery = query;

        clearTimeout(timer);

        if (query.length < MIN_CHARS) {
            close();
            return;
        }

        timer = setTimeout(function () {
            fetchSuggestions(query);
        }, DEBOUNCE_MS);
    });

    input.addEventListener("keydown", function (event) {
        if (event.key === "ArrowDown") {
            event.preventDefault();
            setActive(activeIndex + 1);
        } else if (event.key === "ArrowUp") {
            event.preventDefault();
            setActive(activeIndex - 1);
        } else if (event.key === "Enter") {
            if (activeIndex >= 0 && items[activeIndex]) {
                event.preventDefault();
                window.location.href = items[activeIndex].getAttribute("href");
            }
            // otherwise let the form submit to the results page
        } else if (event.key === "Escape") {
            if (!dropdown.hidden) {
                close();
            } else {
                input.blur();
            }
        }
    });

    // Re-open on focus if there is still a query with results.
    input.addEventListener("focus", function () {
        if (input.value.trim().length >= MIN_CHARS && dropdown.innerHTML) {
            dropdown.hidden = false;
        }
    });

    document.addEventListener("click", function (event) {
        if (!form.contains(event.target)) close();
    });

    // "/" focuses search from anywhere, the way most tools bind it.
    document.addEventListener("keydown", function (event) {
        if (event.key !== "/") return;

        const tag = (event.target.tagName || "").toLowerCase();
        const typing =
            tag === "input" ||
            tag === "textarea" ||
            tag === "select" ||
            event.target.isContentEditable;

        if (typing) return;

        event.preventDefault();
        input.focus();
    });
})();
