/* AI Assist for the Social Studio composer.
 *
 * Two on-demand actions, both producing a DRAFT the user edits before saving:
 *   #aiGenBtn  - draft an on-brand, per-platform caption from the task brief +
 *                selected media + the client's brand knowledge base.
 *   #aiAltBtn  - fill empty alt-text on the selected images.
 *
 * CSRF is added automatically by csrf.js. Listeners are delegated on document
 * and guarded, so they survive Turbo navigations and bind exactly once. The
 * endpoints are exposed by the template only when AI_ENABLED, so this file is
 * inert otherwise.
 */
(function () {
    "use strict";
    if (window.__aiCaptionInit) return;
    window.__aiCaptionInit = true;

    function toast(msg, type) {
        if (typeof window.showToast === "function") window.showToast(msg, type);
    }
    function fire(el) {
        el.dispatchEvent(new Event("input", { bubbles: true }));
    }
    function uniq(arr) {
        return arr.filter(function (v, i) { return arr.indexOf(v) === i; });
    }
    function busy(btn, on) {
        btn.disabled = on;
        btn.classList.toggle("is-loading", on);
    }
    function selectedMedia() {
        return Array.prototype.slice.call(document.querySelectorAll(
            'input[name="task_file_ids"]:checked, input[name="upload_media"]:checked'));
    }

    function applyCaption(data) {
        var cap = document.getElementById("captionInput");
        var text = data.caption || "";
        if (data.hashtags && data.hashtags.length) {
            var tagline = data.hashtags.map(function (h) { return "#" + h; }).join(" ");
            text = text ? text + "\n\n" + tagline : tagline;
        }
        if (cap) { cap.value = text; fire(cap); }

        // Per-platform overrides are optional hand-tuning; only fill the empty
        // ones so a user's customised text is never silently clobbered.
        var per = data.per_platform || {};
        Object.keys(per).forEach(function (pf) {
            var el = document.querySelector('textarea[name="caption_' + pf + '"]');
            if (el && !el.value.trim()) { el.value = per[pf]; fire(el); }
        });

        var fc = document.getElementById("firstComment");
        if (fc && !fc.value.trim() && data.first_comment) {
            fc.value = data.first_comment; fire(fc);
        }
        var flag = document.getElementById("aiAssisted");
        if (flag) flag.value = "1";
    }

    async function onGenerate(btn) {
        var cap = document.getElementById("captionInput");
        var taskEl = document.querySelector('input[name="task_id"]');
        var clientEl = document.getElementById("clientSelect");
        var taskId = (taskEl && taskEl.value) || "";
        var clientId = (clientEl && clientEl.value) || "";
        var platforms = uniq(Array.prototype.slice.call(
            document.querySelectorAll('input[name="account_ids"]:checked'))
            .map(function (c) { return c.dataset.platform; })
            .filter(Boolean));
        var mediaKeys = selectedMedia()
            .map(function (c) { return c.dataset.key; }).filter(Boolean);

        if (!taskId && !mediaKeys.length) {
            toast("Add a brief (link a task) or some media first.", "error");
            return;
        }
        if (cap && cap.value.trim() &&
            !window.confirm("Replace the current caption with an AI draft?")) {
            return;
        }

        busy(btn, true);
        try {
            var body = new URLSearchParams();
            if (taskId) body.set("task_id", taskId);
            if (clientId) body.set("client_id", clientId);
            body.set("platforms", platforms.join(","));
            body.set("media_keys", mediaKeys.join(","));
            var r = await fetch(window.AI_CAPTION_URL, {
                method: "POST", credentials: "same-origin",
                headers: { "X-Requested-With": "fetch" }, body: body,
            });
            var data = await r.json().catch(function () { return {}; });
            if (!r.ok) { toast(data.error || "Couldn't generate a caption.", "error"); return; }
            applyCaption(data);
            toast("Caption drafted — review and edit before publishing.", "success");
        } catch (e) {
            toast("Network error — please try again.", "error");
        } finally {
            busy(btn, false);
        }
    }

    async function onAltText(btn) {
        var images = selectedMedia().filter(function (c) {
            return (c.dataset.mime || "").indexOf("image") === 0;
        });
        if (!images.length) {
            toast("Select at least one image first.", "error");
            return;
        }
        busy(btn, true);
        var filled = 0;
        for (var i = 0; i < images.length; i++) {
            var c = images[i];
            var label = c.closest(".cmp-asset");
            var altInput = label && label.querySelector(".cmp-alt");
            if (!altInput || altInput.value.trim()) continue;   // never overwrite
            try {
                var body = new URLSearchParams({ object_key: c.dataset.key || "" });
                var r = await fetch(window.AI_ALT_URL, {
                    method: "POST", credentials: "same-origin",
                    headers: { "X-Requested-With": "fetch" }, body: body,
                });
                var data = await r.json().catch(function () { return {}; });
                if (r.ok && data.alt_text) {
                    altInput.value = data.alt_text; fire(altInput); filled++;
                }
            } catch (e) { /* skip this image, keep going */ }
        }
        busy(btn, false);
        toast(
            filled ? ("Alt-text added to " + filled + " image" + (filled > 1 ? "s" : "") + ".")
                   : "No empty alt-text fields to fill.",
            filled ? "success" : "error");
    }

    document.addEventListener("click", function (event) {
        var gen = event.target.closest && event.target.closest("#aiGenBtn");
        if (gen) { event.preventDefault(); onGenerate(gen); return; }
        var alt = event.target.closest && event.target.closest("#aiAltBtn");
        if (alt) { event.preventDefault(); onAltText(alt); }
    });
})();
