/*
    Command palette (Cmd/Ctrl-K).

    A keyboard-first launcher over the whole app. Empty, it lists create
    actions and every section you can reach. As you type it filters those
    AND queries /search/suggest, so tasks, clients and people show up
    inline - arrow to one, Enter to open it. No mouse needed.

    The command list is rendered server-side (permission-gated) into
    #cmdkData; this script only presents and routes it.
*/
(function () {

    let root, input, results;
    let commands = [];
    let rows = [];          // current visible rows [{label, sub, url, icon, type}]
    let active = -1;        // highlighted row index
    let searchSeq = 0;      // guards out-of-order /search/suggest replies
    let debounceId = null;

    function load() {
        const el = document.getElementById("cmdkData");
        if (!el) return false;
        try {
            commands = (JSON.parse(el.textContent) || {}).commands || [];
        } catch (e) {
            commands = [];
        }
        root = document.getElementById("cmdk");
        input = document.getElementById("cmdkInput");
        results = document.getElementById("cmdkResults");
        return !!(root && input && results);
    }

    function isOpen() {
        return root && !root.hidden;
    }

    function open() {
        if (!root) return;
        root.hidden = false;
        document.body.classList.add("cmdk-open");
        input.value = "";
        render(commands.map(toRow), "");
        // Focus after paint so the caret lands and the open animation runs.
        setTimeout(function () { input.focus(); }, 0);
    }

    function close() {
        if (!root) return;
        root.hidden = true;
        document.body.classList.remove("cmdk-open");
        active = -1;
        searchSeq++;               // abandon any in-flight search
    }

    function toRow(cmd) {
        return {
            label: cmd.label,
            sub: cmd.group,
            url: cmd.url,
            icon: cmd.icon || "fa-arrow-right",
            type: "command",
            keywords: (cmd.label + " " + (cmd.keywords || "") + " " + cmd.group).toLowerCase()
        };
    }

    function iconFor(type) {
        if (type === "task") return "fa-list-check";
        if (type === "client") return "fa-building";
        if (type === "user") return "fa-user";
        if (type === "note") return "fa-note-sticky";
        return "fa-arrow-right";
    }

    function escapeHtml(s) {
        return String(s == null ? "" : s)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function render(list, term) {
        rows = list;
        active = list.length ? 0 : -1;

        if (!list.length) {
            results.innerHTML =
                '<div class="cmdk-empty">No matches' +
                (term ? ' for "' + escapeHtml(term) + '"' : "") + "</div>";
            return;
        }

        let html = "";
        let lastGroup = null;
        list.forEach(function (r, i) {
            if (r.sub !== lastGroup) {
                html += '<div class="cmdk-group">' + escapeHtml(r.sub) + "</div>";
                lastGroup = r.sub;
            }
            html +=
                '<button type="button" class="cmdk-row' + (i === active ? " active" : "") +
                '" data-i="' + i + '" role="option">' +
                '<i class="fa-solid ' + r.icon + ' cmdk-row-icon"></i>' +
                '<span class="cmdk-row-label">' + escapeHtml(r.label) + "</span>" +
                (r.badge ? '<span class="cmdk-row-badge">' + escapeHtml(r.badge) + "</span>" : "") +
                "</button>";
        });
        results.innerHTML = html;
        scrollActiveIntoView();
    }

    function scrollActiveIntoView() {
        const el = results.querySelector(".cmdk-row.active");
        if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
    }

    function setActive(next) {
        if (!rows.length) return;
        active = (next + rows.length) % rows.length;
        const nodes = results.querySelectorAll(".cmdk-row");
        nodes.forEach(function (n, i) { n.classList.toggle("active", i === active); });
        scrollActiveIntoView();
    }

    function choose(i) {
        const r = rows[i];
        if (!r || !r.url) return;
        const url = r.url;
        close();
        // Navigate on the next tick, outside the key/click handler. Doing
        // it synchronously inside an Enter keydown was getting swallowed in
        // some engines; deferring makes it fire reliably either way.
        setTimeout(function () { window.location.assign(url); }, 0);
    }

    function localMatches(term) {
        const t = term.toLowerCase();
        return commands
            .map(toRow)
            .filter(function (r) { return r.keywords.indexOf(t) !== -1; });
    }

    async function runSearch(term) {
        const mine = ++searchSeq;
        let remote = [];
        try {
            const res = await fetch("/search/suggest?q=" + encodeURIComponent(term));
            const data = await res.json();
            (data.groups || []).forEach(function (g) {
                (g.items || []).forEach(function (it) {
                    remote.push({
                        label: (it.code ? "#" + it.code + " " : "") + (it.title || "Untitled"),
                        sub: g.label || it.type,
                        url: it.url,
                        icon: iconFor(it.type),
                        badge: it.subtitle || "",
                        type: it.type
                    });
                });
            });
        } catch (e) {
            remote = [];
        }
        if (mine !== searchSeq || !isOpen()) return;  // stale or closed
        render(localMatches(term).concat(remote), term);
    }

    function onType() {
        const term = input.value.trim();
        if (debounceId) clearTimeout(debounceId);

        if (!term) {
            searchSeq++;
            render(commands.map(toRow), "");
            return;
        }

        // Show local (instant) matches immediately, then fold in search.
        render(localMatches(term), term);

        if (term.length >= 2) {
            debounceId = setTimeout(function () { runSearch(term); }, 160);
        }
    }

    function bind() {
        input.addEventListener("input", onType);

        results.addEventListener("click", function (e) {
            const row = e.target.closest(".cmdk-row");
            if (row) choose(parseInt(row.dataset.i, 10));
        });

        results.addEventListener("mousemove", function (e) {
            const row = e.target.closest(".cmdk-row");
            if (!row) return;
            const i = parseInt(row.dataset.i, 10);
            if (i !== active) setActive(i);
        });

        root.addEventListener("click", function (e) {
            if (e.target.closest("[data-cmdk-close]")) close();
        });

        input.addEventListener("keydown", function (e) {
            if (e.key === "ArrowDown") { e.preventDefault(); setActive(active + 1); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setActive(active - 1); }
            else if (e.key === "Enter") { e.preventDefault(); if (active >= 0) choose(active); }
            else if (e.key === "Escape") { e.preventDefault(); close(); }
        });
    }

    // Turbo swaps the body on navigation, so the palette markup is a fresh
    // element on every visit. Re-acquire references each time and bind the
    // new element once (tracked by a flag ON the element, so old detached
    // nodes don't block re-binding).
    function init() {
        if (!load()) return;
        if (root.dataset.cmdkBound) return;
        bind();
        root.dataset.cmdkBound = "1";
    }

    // Global open shortcut: Cmd/Ctrl-K from anywhere. Also closes on repeat.
    document.addEventListener("keydown", function (e) {
        if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
            e.preventDefault();
            init();
            if (!root) return;
            isOpen() ? close() : open();
        }
    });

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
    document.addEventListener("turbo:load", init);
})();
