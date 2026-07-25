/*
    @-mention autocomplete for comment/reply boxes and the task
    description field.

    Typing "@" (at the start or after a space) opens a picker of active
    teammates (window.MENTION_USERS, set per page). Choosing one inserts
    "@Full Name " so the server can match and notify them. Bound once at
    document level via delegation, so it also covers reply boxes that are
    created on the fly, and survives Turbo navigations.
*/
(function () {
    if (window.__mentionAutocompleteBound) return;
    window.__mentionAutocompleteBound = true;

    var box = null;
    var activeField = null;
    var matches = [];
    var activeIndex = 0;
    var queryStart = -1;

    function ensureBox() {
        if (box) return box;
        box = document.createElement("div");
        box.className = "mention-suggest";
        box.hidden = true;
        document.body.appendChild(box);
        return box;
    }

    function hide() {
        if (box) box.hidden = true;
        activeField = null;
        queryStart = -1;
        matches = [];
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, function (c) {
            return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
        });
    }

    // The @query is the text from the last "@" (at start/after whitespace)
    // up to the caret, with no newline.
    function currentQuery(field) {
        var pos = field.selectionStart;
        var text = field.value.slice(0, pos);
        var at = text.lastIndexOf("@");
        if (at < 0) return null;
        if (at > 0 && !/\s/.test(text.charAt(at - 1))) return null;
        var q = text.slice(at + 1);
        if (q.indexOf("\n") !== -1 || q.length > 40) return null;
        return { start: at, query: q };
    }

    function paint() {
        if (!box) return;
        [].forEach.call(box.children, function (el, i) {
            el.classList.toggle("active", i === activeIndex);
        });
    }

    function render(field) {
        var info = currentQuery(field);
        if (!info) { hide(); return; }

        var users = window.MENTION_USERS || [];
        var ql = info.query.toLowerCase();
        matches = users.filter(function (n) {
            return n.toLowerCase().indexOf(ql) !== -1;
        }).slice(0, 6);

        if (!matches.length) { hide(); return; }

        activeField = field;
        queryStart = info.start;
        activeIndex = 0;

        var b = ensureBox();
        b.innerHTML = matches.map(function (n, i) {
            return '<div class="mention-suggest-item' + (i === 0 ? " active" : "") +
                '" data-i="' + i + '">' + escapeHtml(n) + "</div>";
        }).join("");

        var r = field.getBoundingClientRect();
        b.style.left = (window.scrollX + r.left) + "px";
        b.style.top = (window.scrollY + r.bottom + 4) + "px";
        b.style.minWidth = Math.min(Math.max(r.width, 180), 280) + "px";
        b.hidden = false;
    }

    function choose(i) {
        if (!activeField || i < 0 || i >= matches.length) return;
        var name = matches[i];
        var field = activeField;
        var pos = field.selectionStart;
        var before = field.value.slice(0, queryStart);
        var after = field.value.slice(pos);
        var insert = "@" + name + " ";
        field.value = before + insert + after;
        var caret = (before + insert).length;
        field.setSelectionRange(caret, caret);
        field.focus();
        // Let listeners (auto-grow, etc.) react to the change.
        field.dispatchEvent(new Event("input", { bubbles: true }));
        hide();
    }

    // "message" covers comments/replies; "description" covers the task
    // description on the add/edit forms - both are plain textareas, so
    // the same picker and highlighting work for either untouched.
    document.addEventListener("input", function (e) {
        var t = e.target;
        if (
            t &&
            t.tagName === "TEXTAREA" &&
            (t.name === "message" || t.name === "description")
        ) render(t);
    });

    document.addEventListener("keydown", function (e) {
        if (!box || box.hidden || !activeField) return;
        if (e.key === "ArrowDown") {
            e.preventDefault();
            activeIndex = (activeIndex + 1) % matches.length;
            paint();
        } else if (e.key === "ArrowUp") {
            e.preventDefault();
            activeIndex = (activeIndex - 1 + matches.length) % matches.length;
            paint();
        } else if ((e.key === "Enter" && !e.ctrlKey && !e.metaKey) || e.key === "Tab") {
            // Plain Enter picks the mention; Ctrl/Cmd+Enter still posts.
            e.preventDefault();
            choose(activeIndex);
        } else if (e.key === "Escape") {
            hide();
        }
    });

    document.addEventListener("mousedown", function (e) {
        var item = e.target.closest(".mention-suggest-item");
        if (item) {
            e.preventDefault();
            choose(parseInt(item.getAttribute("data-i"), 10));
        } else if (box && !box.hidden && e.target !== activeField) {
            hide();
        }
    });

    document.addEventListener("scroll", function () { if (box && !box.hidden) hide(); }, true);

    /*
        The bound-once guard above stops the delegated listeners from
        stacking up on every task page visit - correct, since delegation
        itself needs no re-binding. But `box` is a plain <div> this
        script appends to document.body on first use, and a real Turbo
        navigation replaces <body> outright, taking that div with it.
        The guard means this script never runs again to notice, so
        `box` keeps pointing at the now-detached node forever after:
        ensureBox() sees it's still truthy, skips rebuilding, and every
        later "@" render writes into a div nothing can see - the picker
        silently stops appearing on every task after the first one you
        opened, until a hard refresh clears this script's state
        entirely. Dropping the cached references on navigation forces
        the next render() to rebuild into the (live) body Turbo just
        installed - the same fix task-panel.js needed for its drawer.
    */
    function resetForNavigation() {
        box = null;
        activeField = null;
        matches = [];
        activeIndex = 0;
        queryStart = -1;
    }

    document.addEventListener("turbo:before-render", resetForNavigation);
    document.addEventListener("turbo:before-cache", resetForNavigation);
})();
