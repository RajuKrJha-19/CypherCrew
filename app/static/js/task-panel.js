/*
    Asana-style task side drawer.

    Opening a task used to mean a full page navigation away from the
    board, losing scroll position, filters and the view you were working
    in. Now any link to /tasks/<id> - from the board, the list, the
    dashboard, the calendar, reports or the review queue - slides the
    task in from the right and leaves the page behind it intact.

    The drawer renders the real task page (?panel=1, which drops the app
    shell) inside an iframe. tasks/detail.html is 2000 lines carrying
    timers, uploads, comments, approve/reject and status forms; re-hosting
    it verbatim is what guarantees all of that keeps behaving exactly as
    it does on the full page, instead of being re-implemented against a
    second, drifting copy.

    Full page navigation still works everywhere: middle-click, ctrl/cmd
    click and "open in new tab" are left alone, direct URLs render the
    normal page, and if this script fails to load every link is still a
    plain <a href>.
*/

(function () {

    "use strict";

    const WIDTH_KEY = "cypher_task_drawer_width";
    const MIN_WIDTH = 460;
    const DEFAULT_WIDTH = 720;

    // Matches /tasks/12 but deliberately not /tasks/add, /tasks/12/edit
    // or any of the file/action sub-routes.
    const TASK_URL = /^\/tasks\/(\d+)\/?$/;

    let root = null;
    let drawerBody = null;
    let frame = null;
    let loader = null;
    let openFullLink = null;
    let positionLabel = null;
    let prevBtn = null;
    let nextBtn = null;

    let currentId = null;
    let siblings = [];
    let dirty = false;
    let ignoreNextLoad = false;
    let pushedHistory = false;
    let pendingReload = false;
    let lastFocused = null;

    function onFrameLoad() {

        let href = null;
        let path = null;

        try {
            href = frame.contentWindow.location.href;
            path = frame.contentWindow.location.pathname;
        } catch (err) {
            // Same-origin throughout, so this shouldn't happen; if it
            // ever does, fall through to the plain stale-board handling.
        }

        // A newly appended iframe fires a load for its initial about:blank
        // document before our navigation lands. That is not the panel.
        if (!href || href === "about:blank") return;

        loader.classList.remove("show");

        const expected = ignoreNextLoad;
        ignoreNextLoad = false;

        if (!root.classList.contains("show")) return;

        // The panel left the task it was showing. Deleting a task
        // redirects to the task list, and rendering a full task list
        // inside the drawer would be nonsense - close and refresh
        // instead. Checked here rather than via postMessage because the
        // page redirected *to* is a normal app page that knows nothing
        // about the drawer.
        if (path && currentId && taskIdFromHref(path) !== currentId) {
            dirty = true;
            close();
            return;
        }

        if (expected) return;

        // A load we didn't trigger means a form inside the panel posted
        // and redirected - the board behind is now stale.
        dirty = true;
    }

    /*
        The frame is created on open and destroyed on close rather than
        being kept around blank. Closing then genuinely stops whatever the
        task page was doing (live timers, media, polling), and it sidesteps
        the browser restoring the old frame document - and re-requesting
        the task - when history.back() steps off the drawer's entry.
    */
    function ensureFrame() {

        if (frame && frame.isConnected) return;

        frame = document.createElement("iframe");
        frame.className = "task-drawer-frame";
        frame.title = "Task details";
        frame.setAttribute("allowfullscreen", "");
        frame.addEventListener("load", onFrameLoad);

        drawerBody.appendChild(frame);
    }

    function destroyFrame() {

        if (!frame) return;

        frame.remove();
        frame = null;
    }

    /*
        Assigning iframe.src performs a normal navigation, and an iframe
        navigation adds an entry to the *joint* session history. Stepping
        through three tasks would then bury the page we came from three
        entries deep, so closing with history.back() would land on a
        previous task instead of the board. location.replace() swaps the
        frame's document without touching history.
    */
    function navigateFrame(url) {

        ensureFrame();
        ignoreNextLoad = true;

        try {
            frame.contentWindow.location.replace(url);
        } catch (err) {
            // Only reachable if the frame is cross-origin, which it
            // never is here - fall back rather than dropping the click.
            frame.src = url;
        }
    }

    function taskIdFromHref(href) {

        if (!href) return null;

        let url;

        try {
            url = new URL(href, window.location.origin);
        } catch (err) {
            return null;
        }

        if (url.origin !== window.location.origin) return null;

        const match = TASK_URL.exec(url.pathname);

        return match ? match[1] : null;
    }

    function storedWidth() {

        const saved = parseInt(localStorage.getItem(WIDTH_KEY), 10);

        if (!saved || isNaN(saved)) return DEFAULT_WIDTH;

        return clampWidth(saved);
    }

    function clampWidth(width) {
        const max = Math.max(MIN_WIDTH, window.innerWidth - 220);
        return Math.min(Math.max(width, MIN_WIDTH), max);
    }

    function build() {

        root = document.createElement("div");
        root.className = "task-drawer-root";

        root.innerHTML = `
            <div class="task-drawer-backdrop" data-drawer="close"></div>

            <aside class="task-drawer" role="dialog" aria-modal="true" aria-label="Task details">

                <div class="task-drawer-resize" role="separator" aria-orientation="vertical"
                    tabindex="0" aria-label="Resize task panel" title="Drag to resize"></div>

                <header class="task-drawer-bar">

                    <div class="task-drawer-stepper">
                        <button type="button" class="task-drawer-tool" data-drawer="prev"
                            aria-label="Previous task" title="Previous task (K)">
                            <i class="fa-solid fa-chevron-up"></i>
                        </button>

                        <span class="task-drawer-position" id="taskDrawerPosition"></span>

                        <button type="button" class="task-drawer-tool" data-drawer="next"
                            aria-label="Next task" title="Next task (J)">
                            <i class="fa-solid fa-chevron-down"></i>
                        </button>
                    </div>

                    <div class="task-drawer-bar-spacer"></div>

                    <a class="task-drawer-tool" id="taskDrawerFull" href="#"
                        aria-label="Open as full page" title="Open as full page">
                        <i class="fa-solid fa-up-right-and-down-left-from-center"></i>
                    </a>

                    <button type="button" class="task-drawer-tool" data-drawer="close"
                        aria-label="Close" title="Close (Esc)">
                        <i class="fa-solid fa-xmark"></i>
                    </button>

                </header>

                <div class="task-drawer-body">

                    <div class="task-drawer-loader" id="taskDrawerLoader">
                        <div class="task-drawer-skeleton skeleton-title"></div>
                        <div class="task-drawer-skeleton skeleton-line"></div>
                        <div class="task-drawer-skeleton skeleton-line short"></div>
                        <div class="task-drawer-skeleton skeleton-block"></div>
                        <div class="task-drawer-skeleton skeleton-line"></div>
                        <div class="task-drawer-skeleton skeleton-line short"></div>
                    </div>

                </div>

            </aside>
        `;

        document.body.appendChild(root);

        drawerBody = root.querySelector(".task-drawer-body");
        loader = root.querySelector("#taskDrawerLoader");
        openFullLink = root.querySelector("#taskDrawerFull");
        positionLabel = root.querySelector("#taskDrawerPosition");
        prevBtn = root.querySelector('[data-drawer="prev"]');
        nextBtn = root.querySelector('[data-drawer="next"]');

        root.addEventListener("click", function (event) {

            const action = event.target.closest("[data-drawer]");

            if (!action) return;

            const name = action.dataset.drawer;

            if (name === "close") close();
            if (name === "prev") step(-1);
            if (name === "next") step(1);
        });

        setupResize();
        applyWidth(storedWidth());
    }

    function setupResize() {

        const handle = root.querySelector(".task-drawer-resize");
        const drawer = root.querySelector(".task-drawer");

        let dragging = false;

        handle.addEventListener("mousedown", function (event) {
            event.preventDefault();
            dragging = true;
            document.body.classList.add("task-drawer-resizing");
        });

        document.addEventListener("mousemove", function (event) {
            if (!dragging) return;
            applyWidth(window.innerWidth - event.clientX);
        });

        document.addEventListener("mouseup", function () {

            if (!dragging) return;

            dragging = false;
            document.body.classList.remove("task-drawer-resizing");
            localStorage.setItem(WIDTH_KEY, parseInt(drawer.style.width, 10));
        });

        // Keyboard resizing, so the panel width isn't mouse-only.
        handle.addEventListener("keydown", function (event) {

            const step = event.shiftKey ? 100 : 20;
            let width = parseInt(drawer.style.width, 10);

            if (event.key === "ArrowLeft") width += step;
            else if (event.key === "ArrowRight") width -= step;
            else return;

            event.preventDefault();
            applyWidth(width);
            localStorage.setItem(WIDTH_KEY, parseInt(drawer.style.width, 10));
        });
    }

    function applyWidth(width) {
        root.querySelector(".task-drawer").style.width = clampWidth(width) + "px";
    }

    /*
        Prev/next walk the tasks visible on the page the drawer was opened
        from, in the order they appear, so stepping through a board column
        or a filtered list follows the order that's actually on screen.
    */
    function collectSiblings() {

        const ids = [];

        document.querySelectorAll("a[href]").forEach(function (link) {

            if (link.closest(".task-drawer-root")) return;

            const id = taskIdFromHref(link.getAttribute("href"));

            if (id && ids.indexOf(id) === -1) ids.push(id);
        });

        return ids;
    }

    function syncStepper() {

        const index = siblings.indexOf(currentId);

        if (index === -1 || siblings.length < 2) {
            positionLabel.textContent = "";
            prevBtn.disabled = true;
            nextBtn.disabled = true;
            return;
        }

        positionLabel.textContent = (index + 1) + " / " + siblings.length;
        prevBtn.disabled = index === 0;
        nextBtn.disabled = index === siblings.length - 1;
    }

    function step(delta) {

        const index = siblings.indexOf(currentId);

        if (index === -1) return;

        const next = siblings[index + delta];

        if (!next) return;

        show(next, { replace: true });
    }

    function show(taskId, options) {

        options = options || {};

        currentId = String(taskId);

        loader.classList.add("show");
        navigateFrame("/tasks/" + currentId + "?panel=1");

        openFullLink.href = "/tasks/" + currentId;

        const url = "/tasks/" + currentId;
        const state = { cypherTaskDrawer: currentId };

        if (options.fromHistory) {
            // Already where we need to be - don't touch the stack.
        } else if (options.replace || pushedHistory) {
            history.replaceState(state, "", url);
            pushedHistory = true;
        } else {
            history.pushState(state, "", url);
            pushedHistory = true;
        }

        syncStepper();
    }

    function open(taskId, options) {

        if (!root) build();

        options = options || {};

        if (!root.classList.contains("show")) {
            lastFocused = document.activeElement;
            siblings = collectSiblings();
            dirty = false;
            // Reopening via Forward lands on an entry that already exists,
            // so closing should still step back off it.
            pushedHistory = Boolean(options.fromHistory);
            root.classList.add("show");
            document.body.classList.add("task-drawer-open");
        }

        show(taskId, options);

        // Focus the panel so Esc and the toolbar are reachable before the
        // iframe has loaded and taken focus.
        root.querySelector('[data-drawer="close"]').focus({ preventScroll: true });
    }

    function close(options) {

        options = options || {};

        if (!root || !root.classList.contains("show")) return;

        root.classList.remove("show");
        document.body.classList.remove("task-drawer-open");

        destroyFrame();
        currentId = null;

        if (lastFocused && typeof lastFocused.focus === "function") {
            lastFocused.focus({ preventScroll: true });
        }

        // Something in the panel changed the task, so the board behind it
        // is showing stale status/columns - reload it on the way out.
        const shouldReload = dirty;
        dirty = false;

        if (pushedHistory && !options.fromHistory) {
            // Step off the drawer's history entry first, so a reload lands
            // on the page we came from rather than on /tasks/<id>. The
            // reload is deferred to the popstate handler so it can't race
            // the navigation.
            pushedHistory = false;
            pendingReload = shouldReload;
            history.back();
            return;
        }

        if (shouldReload) {
            window.location.reload();
        }
    }

    document.addEventListener("click", function (event) {

        // Leave modified clicks to the browser so "open in new tab",
        // "open in new window" and middle-click all still work.
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        if (event.button !== 0) return;

        const link = event.target.closest("a[href]");

        if (!link) return;
        if (link.target && link.target !== "_self") return;
        if (link.hasAttribute("download")) return;
        if (link.dataset.noDrawer !== undefined) return;
        if (link.closest(".task-drawer-root")) return;

        const taskId = taskIdFromHref(link.getAttribute("href"));

        if (!taskId) return;

        event.preventDefault();
        open(taskId);
    });

    document.addEventListener("keydown", function (event) {

        if (!root || !root.classList.contains("show")) return;

        if (event.key === "Escape") {
            close();
            return;
        }

        // Only when focus is outside the iframe - inside it, the panel's
        // own comment box and fields must keep these keys.
        if (event.target.closest("input, textarea, select, [contenteditable]")) return;

        if (event.key === "j") step(1);
        if (event.key === "k") step(-1);
    });

    window.addEventListener("popstate", function (event) {

        const id = event.state && event.state.cypherTaskDrawer;

        if (id) {
            open(String(id), { fromHistory: true });
            return;
        }

        if (root && root.classList.contains("show")) {
            pushedHistory = false;
            close({ fromHistory: true });
        }

        // close() asked for a refresh on its way out; now that we're back
        // on the originating page, actually do it.
        if (pendingReload) {
            pendingReload = false;
            window.location.reload();
        }
    });

    window.addEventListener("message", function (event) {

        if (event.origin !== window.location.origin) return;

        const data = event.data;

        if (!data || data.source !== "cypher-task-panel") return;

        // Esc pressed inside the panel. A keydown in an iframe never
        // reaches the hosting document, so the panel forwards it up.
        if (data.type === "close") {
            close();
        }
    });

    window.addEventListener("resize", function () {
        if (root && root.classList.contains("show")) {
            applyWidth(parseInt(root.querySelector(".task-drawer").style.width, 10));
        }
    });

})();
