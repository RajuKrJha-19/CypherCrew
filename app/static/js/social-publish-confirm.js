/*
    "Did you publish on X?" gate in front of Approve & Publish.

    A task can be marked, at assignment time, as targeting one or more
    social platforms. The server renders the Approve form for such a
    task (in Client Review) with a data-social-platforms attribute -
    a JSON list of {key, label, icon} for exactly the platforms that
    were ticked. This script intercepts that form's submit, shows a
    checklist asking about each one, and only lets the submit through
    once every box is checked - appending a confirmed_platforms field
    per platform so the server (the actual gate; see tasks.approve_task)
    can verify all of them arrived.

    Bound once at document level (capture phase, so it runs before
    Turbo's own submit handling and a prevented submit never reaches
    it - the same technique task-panel.js uses for link clicks). The
    modal itself is built fresh on every open and thrown away on
    close/submit - nothing is cached across calls, so there is nothing
    here that can go stale across a Turbo navigation.
*/
(function () {
    "use strict";

    function buildModal(platforms) {

        const overlay = document.createElement("div");
        overlay.className = "social-confirm-overlay";

        const box = document.createElement("div");
        box.className = "social-confirm-box";
        box.setAttribute("role", "dialog");
        box.setAttribute("aria-modal", "true");
        box.setAttribute("aria-label", "Confirm publish");

        box.innerHTML =
            "<h3>Confirm publish</h3>" +
            "<p>Before this goes live, confirm it was actually posted on " +
            "each platform below.</p>" +
            '<div class="social-confirm-list"></div>' +
            '<div class="social-confirm-actions">' +
            '<button type="button" class="btn-secondary" data-social-cancel>Cancel</button>' +
            '<button type="button" class="social-confirm-btn" data-social-confirm disabled>Confirm &amp; Publish</button>' +
            "</div>";

        const list = box.querySelector(".social-confirm-list");

        platforms.forEach(function (platform) {

            const item = document.createElement("label");
            item.className = "social-confirm-item";

            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.dataset.platformKey = platform.key;

            const icon = document.createElement("i");
            icon.className = platform.icon || "fa-solid fa-share-nodes";

            const text = document.createTextNode(
                " Did you publish on " + platform.label + "?"
            );

            item.appendChild(checkbox);
            item.appendChild(icon);
            item.appendChild(text);
            list.appendChild(item);
        });

        overlay.appendChild(box);
        return overlay;
    }

    function openConfirm(form, platforms) {

        const overlay = buildModal(platforms);
        document.body.appendChild(overlay);

        const checkboxes = Array.prototype.slice.call(
            overlay.querySelectorAll('input[type="checkbox"]')
        );
        const confirmBtn = overlay.querySelector("[data-social-confirm]");
        const cancelBtn = overlay.querySelector("[data-social-cancel]");

        function updateConfirmState() {
            confirmBtn.disabled = !checkboxes.every(function (cb) {
                return cb.checked;
            });
        }

        checkboxes.forEach(function (cb) {
            cb.addEventListener("change", updateConfirmState);
        });

        function onKeydown(event) {
            if (event.key === "Escape") close();
        }

        function close() {
            overlay.remove();
            document.removeEventListener("keydown", onKeydown);
        }

        cancelBtn.addEventListener("click", close);

        overlay.addEventListener("click", function (event) {
            if (event.target === overlay) close();
        });

        document.addEventListener("keydown", onKeydown);

        confirmBtn.addEventListener("click", function () {

            checkboxes.forEach(function (cb) {
                const hidden = document.createElement("input");
                hidden.type = "hidden";
                hidden.name = "confirmed_platforms";
                hidden.value = cb.dataset.platformKey;
                form.appendChild(hidden);
            });

            // Lets this exact submit through untouched next time - see
            // the guard at the top of the delegated listener below.
            form.dataset.socialConfirmed = "1";

            close();
            form.requestSubmit();
        });

        // First checkbox focused so keyboard users land straight in the
        // checklist rather than on the (disabled) confirm button.
        if (checkboxes[0]) checkboxes[0].focus();
    }

    document.addEventListener("submit", function (event) {

        const form = event.target;

        if (!(form instanceof HTMLFormElement)) return;
        if (!form.hasAttribute("data-social-platforms")) return;
        if (form.dataset.socialConfirmed === "1") return;

        let platforms;

        try {
            platforms = JSON.parse(
                form.getAttribute("data-social-platforms") || "[]"
            );
        } catch (error) {
            platforms = [];
        }

        if (!platforms.length) return;

        event.preventDefault();
        openConfirm(form, platforms);

    }, true);

    // Defensive: a stray navigation while the checklist is open (e.g. a
    // keyboard shortcut) shouldn't leave it stuck on top of the new page.
    document.addEventListener("turbo:before-render", function () {
        document.querySelectorAll(".social-confirm-overlay").forEach(
            function (overlay) { overlay.remove(); }
        );
    });
})();
