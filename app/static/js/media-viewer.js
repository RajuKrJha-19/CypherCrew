/*
    Shared in-page media viewer.

    Before this, every "preview" path in the app (gallery double-click,
    gallery context menu, kanban file popup double-click, task detail
    Preview button) did window.open(url, "_blank"), so watching a video
    meant leaving the page and coming back. This plays the file in an
    overlay on the same page instead, with fullscreen and an overflow
    menu for the actions that used to be the only reason to open a tab.

    Usage - a single file:
        CypherMediaViewer.open({
            url: "/tasks/files/12/preview",     // required
            downloadUrl: "/tasks/files/12/download",
            filename: "final-cut.mp4",
            mime: "video/mp4"
        });

    Usage - a set you can step through without closing the overlay
    (arrow keys, the on-stage chevrons, or a swipe on touch):
        CypherMediaViewer.open({
            items: [file, file, file],          // same shape as above
            index: 4                            // which one to show first
        });

    Callers that pass a single file get no navigation chrome at all, so
    the two forms can coexist on the same page.

    The overlay markup is built once on first open and reused, so
    including this script costs nothing on pages where it never fires.
*/

window.CypherMediaViewer = (function () {

    let overlay = null;
    let stage = null;
    let titleEl = null;
    let menu = null;
    let menuBtn = null;
    let fullscreenBtn = null;
    let prevBtn = null;
    let nextBtn = null;
    let counterEl = null;
    let currentFile = null;
    let lastFocused = null;
    let openedAt = 0;

    //: The set being stepped through. A single-file open() is just a set
    //: of one, which keeps every code path below identical.
    let items = [];
    let index = 0;

    //: Neighbouring images already pulled into the browser cache, so
    //: arrowing along a row of photos doesn't flash empty on each step.
    const preloaded = new Set();

    function kindOf(mime, url) {

        mime = mime || "";

        if (mime.startsWith("image/")) return "image";
        if (mime.startsWith("video/")) return "video";
        if (mime.startsWith("audio/")) return "audio";
        if (mime === "application/pdf") return "pdf";

        // The kanban popup sends a mime for every file, but the gallery
        // context menu can pass "-" for rows with no stored mime_type.
        // Fall back to the extension so those still preview properly.
        const ext = (url || "").split("?")[0].split(".").pop().toLowerCase();

        if (["jpg", "jpeg", "png", "gif", "webp", "svg", "avif"].includes(ext)) return "image";
        if (["mp4", "webm", "ogv", "mov", "m4v"].includes(ext)) return "video";
        if (["mp3", "wav", "ogg", "m4a", "aac"].includes(ext)) return "audio";
        if (ext === "pdf") return "pdf";

        return "other";
    }

    function build() {

        overlay = document.createElement("div");
        overlay.className = "media-viewer";
        overlay.setAttribute("role", "dialog");
        overlay.setAttribute("aria-modal", "true");
        overlay.setAttribute("aria-label", "File preview");

        overlay.innerHTML = `
            <div class="media-viewer-panel">

                <div class="media-viewer-bar">

                    <div class="media-viewer-title" id="mediaViewerTitle"></div>

                    <div class="media-viewer-count" id="mediaViewerCount" aria-live="polite" hidden></div>

                    <div class="media-viewer-tools">

                        <button type="button" class="media-viewer-tool" data-mv="fullscreen"
                            aria-label="Fullscreen" title="Fullscreen">
                            <i class="fa-solid fa-expand"></i>
                        </button>

                        <button type="button" class="media-viewer-tool" data-mv="menu"
                            aria-label="More options" title="More options" aria-haspopup="true" aria-expanded="false">
                            <i class="fa-solid fa-ellipsis-vertical"></i>
                        </button>

                        <button type="button" class="media-viewer-tool" data-mv="close"
                            aria-label="Close" title="Close (Esc)">
                            <i class="fa-solid fa-xmark"></i>
                        </button>

                    </div>

                    <div class="media-viewer-menu" id="mediaViewerMenu"></div>

                </div>

                <div class="media-viewer-body">

                    <button type="button" class="media-viewer-nav prev" data-mv="prev"
                        aria-label="Previous file" title="Previous (←)" hidden>
                        <i class="fa-solid fa-chevron-left"></i>
                    </button>

                    <div class="media-viewer-stage" id="mediaViewerStage"></div>

                    <button type="button" class="media-viewer-nav next" data-mv="next"
                        aria-label="Next file" title="Next (→)" hidden>
                        <i class="fa-solid fa-chevron-right"></i>
                    </button>

                </div>

            </div>
        `;

        document.body.appendChild(overlay);

        stage = overlay.querySelector("#mediaViewerStage");
        titleEl = overlay.querySelector("#mediaViewerTitle");
        menu = overlay.querySelector("#mediaViewerMenu");
        menuBtn = overlay.querySelector('[data-mv="menu"]');
        fullscreenBtn = overlay.querySelector('[data-mv="fullscreen"]');
        prevBtn = overlay.querySelector('[data-mv="prev"]');
        nextBtn = overlay.querySelector('[data-mv="next"]');
        counterEl = overlay.querySelector("#mediaViewerCount");

        overlay.querySelector('[data-mv="close"]').addEventListener("click", close);
        fullscreenBtn.addEventListener("click", toggleFullscreen);

        prevBtn.addEventListener("click", function (event) {
            event.stopPropagation();
            go(-1);
        });

        nextBtn.addEventListener("click", function (event) {
            event.stopPropagation();
            go(1);
        });

        bindSwipe();

        menuBtn.addEventListener("click", function (event) {
            event.stopPropagation();
            setMenuOpen(!menu.classList.contains("show"));
        });

        // Backdrop click closes, but only when the click started and ended
        // on the backdrop - otherwise dragging a video's seek bar past the
        // player's edge would close the viewer mid-scrub.
        let pressedBackdrop = false;

        overlay.addEventListener("mousedown", function (event) {
            pressedBackdrop = event.target === overlay;
        });

        overlay.addEventListener("click", function (event) {

            // Callers open on a single click, so an old double-click habit
            // lands its second click on a backdrop that was not there when
            // the gesture started. A click that arrives this soon after
            // opening was never meant to dismiss anything.
            const settled = Date.now() - openedAt > 350;

            if (event.target === overlay && pressedBackdrop && settled) {
                close();
            }

            setMenuOpen(false);
        });

        document.addEventListener("keydown", function (event) {

            if (!overlay.classList.contains("show")) return;

            if (event.key === "Escape") {
                // Step out of fullscreen first so Esc never closes the
                // viewer out from under someone who was watching it
                // fullscreen. Exiting explicitly rather than leaning on
                // the browser's own Esc handling keeps this consistent
                // across browsers.
                if (document.fullscreenElement) {
                    document.exitFullscreen().catch(function () {});
                } else {
                    close();
                }
                return;
            }

            if (event.key === "ArrowLeft" || event.key === "ArrowRight") {

                // Arrows belong to the player while a clip has focus -
                // that is how people scrub - and to a text box while one
                // is being typed in. Everywhere else they step files.
                if (isTypingTarget(event.target)) return;

                event.preventDefault();
                setMenuOpen(false);
                go(event.key === "ArrowRight" ? 1 : -1);
                return;
            }

            if (event.key === " " || event.key === "k") {
                const video = stage.querySelector("video, audio");
                if (video && event.target === document.body) {
                    event.preventDefault();
                    video.paused ? video.play() : video.pause();
                }
            }
        });

        document.addEventListener("fullscreenchange", syncFullscreenBtn);
    }

    function setMenuOpen(open) {
        if (!menu) return;
        menu.classList.toggle("show", open);
        menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
    }

    function isTypingTarget(target) {

        if (!target || !target.tagName) return false;

        const tag = target.tagName.toLowerCase();

        return (
            tag === "video" ||
            tag === "audio" ||
            tag === "input" ||
            tag === "textarea" ||
            tag === "select" ||
            target.isContentEditable === true
        );
    }

    function bindSwipe() {

        let startX = 0;
        let startY = 0;
        let tracking = false;

        stage.addEventListener("touchstart", function (event) {

            // One finger only, so pinch-zooming a photo is never read as a
            // swipe. A <video> owns its own gestures, so leave those alone.
            if (event.touches.length !== 1 || event.target.closest("video, audio")) {
                tracking = false;
                return;
            }

            tracking = true;
            startX = event.touches[0].clientX;
            startY = event.touches[0].clientY;

        }, { passive: true });

        stage.addEventListener("touchend", function (event) {

            if (!tracking) return;

            tracking = false;

            const touch = event.changedTouches[0];
            const dx = touch.clientX - startX;
            const dy = touch.clientY - startY;

            // Far enough to not be a stray tap, and clearly sideways rather
            // than the start of a scroll.
            if (Math.abs(dx) < 55 || Math.abs(dx) < Math.abs(dy) * 1.5) return;

            go(dx < 0 ? 1 : -1);

        }, { passive: true });
    }

    function go(delta) {

        if (items.length < 2) return;

        const target = index + delta;

        // Deliberately no wrap-around: hitting a hard stop at either end
        // is how people can tell they have seen everything, and it matches
        // every desktop file previewer people already know.
        if (target < 0 || target >= items.length) return;

        showAt(target);
    }

    function updateNav() {

        const many = items.length > 1;

        prevBtn.hidden = !many;
        nextBtn.hidden = !many;
        counterEl.hidden = !many;

        if (!many) return;

        counterEl.textContent = (index + 1) + " of " + items.length;

        const atStart = index === 0;
        const atEnd = index === items.length - 1;

        // Hand focus over before disabling, otherwise the button someone
        // just pressed goes dead under them and focus falls back to <body>,
        // which strands keyboard users outside the dialog.
        if (atStart && document.activeElement === prevBtn) nextBtn.focus();
        if (atEnd && document.activeElement === nextBtn) prevBtn.focus();

        prevBtn.disabled = atStart;
        nextBtn.disabled = atEnd;
    }

    function preloadNeighbours() {

        [index - 1, index + 1].forEach(function (position) {

            const file = items[position];

            if (!file || preloaded.has(file.url)) return;

            // Only images: fetching the neighbouring 200 MB video ahead of
            // time would cost far more than the wait it saves.
            if (kindOf(file.mime, file.url) !== "image") return;

            preloaded.add(file.url);

            const warm = new Image();
            warm.src = file.url;
        });
    }

    function syncFullscreenBtn() {

        if (!fullscreenBtn) return;

        const isFull = !!document.fullscreenElement;
        const icon = fullscreenBtn.querySelector("i");
        const label = isFull ? "Exit fullscreen" : "Fullscreen";

        icon.classList.toggle("fa-expand", !isFull);
        icon.classList.toggle("fa-compress", isFull);
        fullscreenBtn.setAttribute("aria-label", label);
        fullscreenBtn.setAttribute("title", label);
    }

    function toggleFullscreen() {

        const panel = overlay.querySelector(".media-viewer-panel");

        if (document.fullscreenElement) {
            document.exitFullscreen();
        } else if (panel.requestFullscreen) {
            panel.requestFullscreen().catch(function () {
                notify("Fullscreen is not available in this browser.", "error");
            });
        }
    }

    function notify(message, type) {
        if (typeof window.showToast === "function") {
            window.showToast(message, type || "success");
        }
    }

    function buildMenu(kind) {

        const media = stage.querySelector("video, audio");
        menu.innerHTML = "";

        if (media) {

            const speedRow = document.createElement("div");
            speedRow.className = "media-viewer-menu-speeds";
            speedRow.innerHTML = '<span class="media-viewer-menu-label">Speed</span>';

            [0.5, 1, 1.5, 2].forEach(function (rate) {

                const chip = document.createElement("button");
                chip.type = "button";
                chip.className = "media-viewer-speed" + (rate === 1 ? " active" : "");
                chip.textContent = rate + "x";

                chip.addEventListener("click", function (event) {
                    event.stopPropagation();
                    media.playbackRate = rate;
                    speedRow.querySelectorAll(".media-viewer-speed").forEach(function (other) {
                        other.classList.toggle("active", other === chip);
                    });
                });

                speedRow.appendChild(chip);
            });

            menu.appendChild(speedRow);

            addMenuItem("fa-repeat", "Loop", function (item) {
                media.loop = !media.loop;
                item.classList.toggle("checked", media.loop);
            });

            if (kind === "video" && document.pictureInPictureEnabled) {
                addMenuItem("fa-clone", "Picture in picture", function () {
                    if (document.pictureInPictureElement) {
                        document.exitPictureInPicture();
                    } else {
                        media.requestPictureInPicture().catch(function () {
                            notify("Picture-in-picture is not available for this file.", "error");
                        });
                    }
                });
            }

            menu.appendChild(document.createElement("hr"));
        }

        if (currentFile.downloadUrl) {
            addMenuItem("fa-download", "Download", function () {
                window.location.href = currentFile.downloadUrl;
            });
        }

        addMenuItem("fa-arrow-up-right-from-square", "Open in new tab", function () {
            window.open(currentFile.url, "_blank", "noopener");
        });

        addMenuItem("fa-link", "Copy link", function () {

            const absolute = new URL(currentFile.url, window.location.origin).href;

            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(absolute).then(function () {
                    notify("Link copied to clipboard.");
                }).catch(function () {
                    notify("Could not copy the link.", "error");
                });
            } else {
                notify("Copying needs a secure (https) connection.", "error");
            }
        });
    }

    function addMenuItem(icon, label, onClick) {

        const item = document.createElement("button");
        item.type = "button";
        item.className = "media-viewer-menu-item";
        item.innerHTML = `<i class="fa-solid ${icon}"></i><span>${label}</span>`;

        item.addEventListener("click", function (event) {
            event.stopPropagation();
            onClick(item);
        });

        menu.appendChild(item);
        return item;
    }

    function renderStage(kind) {

        stage.innerHTML = "";

        if (kind === "image") {
            const img = document.createElement("img");
            img.className = "media-viewer-image";
            img.src = currentFile.url;
            img.alt = currentFile.filename || "Preview";
            stage.appendChild(img);
            return;
        }

        if (kind === "video" || kind === "audio") {

            const media = document.createElement(kind);
            media.className = kind === "video" ? "media-viewer-video" : "media-viewer-audio";
            media.src = currentFile.url;
            media.controls = true;
            media.playsInline = true;
            media.preload = "auto";
            stage.appendChild(media);

            // open() is always called from a click/dblclick, so this runs
            // inside a user gesture and is allowed to play with sound.
            // If a browser still refuses, the controls are already there.
            const started = media.play();

            if (started && typeof started.catch === "function") {
                started.catch(function () {});
            }

            return;
        }

        if (kind === "pdf") {
            const frame = document.createElement("iframe");
            frame.className = "media-viewer-frame";
            frame.src = currentFile.url;
            frame.title = currentFile.filename || "PDF preview";
            stage.appendChild(frame);
            return;
        }

        // Archives, spreadsheets, docs - nothing the browser can render
        // inline, so offer the two things that actually work.
        const fallback = document.createElement("div");
        fallback.className = "media-viewer-fallback";
        fallback.innerHTML = `
            <i class="fa-solid fa-file-lines"></i>
            <strong>No preview available</strong>
            <p>This file type can't be shown in the browser.</p>
        `;

        const actions = document.createElement("div");
        actions.className = "media-viewer-fallback-actions";

        if (currentFile.downloadUrl) {
            const dl = document.createElement("a");
            dl.className = "btn";
            dl.href = currentFile.downloadUrl;
            dl.innerHTML = '<i class="fa-solid fa-download"></i> Download';
            actions.appendChild(dl);
        }

        const openTab = document.createElement("a");
        openTab.className = "btn btn-secondary";
        openTab.href = currentFile.url;
        openTab.target = "_blank";
        openTab.rel = "noopener noreferrer";
        openTab.innerHTML = '<i class="fa-solid fa-arrow-up-right-from-square"></i> Open in new tab';
        actions.appendChild(openTab);

        fallback.appendChild(actions);
        stage.appendChild(fallback);
    }

    function showAt(position) {

        teardownStage();

        index = position;
        currentFile = items[position];

        const kind = kindOf(currentFile.mime, currentFile.url);

        titleEl.textContent = currentFile.filename || "Preview";
        titleEl.title = currentFile.filename || "";

        renderStage(kind);
        buildMenu(kind);
        setMenuOpen(false);

        // Fullscreen only makes sense for something actually rendered.
        fullscreenBtn.hidden = kind === "other";

        // Re-trigger the fade on every step, so moving through a set reads
        // as one file replacing another rather than the panel flickering.
        stage.classList.remove("is-entering");
        void stage.offsetWidth;
        stage.classList.add("is-entering");

        updateNav();
        preloadNeighbours();
    }

    function open(input) {

        if (!input) return;

        if (!overlay) build();

        // Two accepted shapes: one file, or { items, index }. Normalising
        // to a list here means nothing below has to care which was used.
        const list = Array.isArray(input.items)
            ? input.items.filter(function (file) { return file && file.url; })
            : (input.url ? [input] : []);

        if (!list.length) return;

        items = list;
        lastFocused = document.activeElement;
        openedAt = Date.now();
        preloaded.clear();

        const wanted = Number.isInteger(input.index) ? input.index : 0;

        showAt(Math.min(Math.max(wanted, 0), items.length - 1));

        overlay.classList.add("show");
        document.body.classList.add("media-viewer-open");
    }

    function teardownStage() {

        // Clearing src stops the media element from continuing to stream
        // the file from R2 in the background - on close, and equally on
        // every step to the next file.
        const media = stage.querySelector("video, audio");

        if (media) {
            media.pause();
            media.removeAttribute("src");
            media.load();
        }

        stage.innerHTML = "";
    }

    function close() {

        if (!overlay) return;

        if (document.fullscreenElement) {
            document.exitFullscreen().catch(function () {});
        }

        teardownStage();
        setMenuOpen(false);
        overlay.classList.remove("show");
        document.body.classList.remove("media-viewer-open");

        currentFile = null;
        items = [];
        index = 0;
        preloaded.clear();

        if (lastFocused && typeof lastFocused.focus === "function") {
            lastFocused.focus();
        }
    }

    return { open: open, close: close };

})();
