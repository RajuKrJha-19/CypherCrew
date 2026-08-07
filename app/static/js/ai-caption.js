/* AI Assist for the Social Studio composer.
 *
 * On-demand actions, all producing a DRAFT the user edits before saving:
 *   #aiGenBtn / #aiRegenBtn  - draft an on-brand, per-platform caption from the
 *                task brief + selected media + the client's brand knowledge base.
 *   [data-ai-rewrite]  - one-click transform of the caption already in the box
 *                (Shorten / Expand / Rephrase / formal / casual / emojis /
 *                grammar), so the AI edits an AI OR hand-written caption.
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

    var lastVariations = [];
    // Hashtags + SEO keywords from the latest generation, so a variation the
    // user clicks gets the SAME tags/keywords block appended as the main draft.
    var lastHashtags = [];
    var lastKeywords = [];

    // Compose the caption box's text: the caption, then a blank line and up to
    // five #hashtags, then a blank line and the SEO keywords in [a, b, c] form.
    function composeCaption(text, hashtags, keywords) {
        var out = text || "";
        if (hashtags && hashtags.length) {
            var tagline = hashtags.map(function (h) {
                return "#" + String(h).replace(/^#/, "");
            }).join(" ");
            out = out ? out + "\n\n" + tagline : tagline;
        }
        if (keywords && keywords.length) {
            var kw = "[" + keywords.join(", ") + "]";
            out = out ? out + "\n\n" + kw : kw;
        }
        return out;
    }
    // The AIUsage row for the latest generation that hasn't yet been resolved
    // (saved => "used", or superseded by a re-generate => "discarded"). This is
    // the keep-rate ROI signal; it is best-effort and never blocks the user.
    var pendingUsageId = null;

    function toast(msg, type) {
        if (typeof window.showToast === "function") window.showToast(msg, type);
    }

    function reportOutcome(id, outcome) {
        if (!id || !window.AI_USAGE_OUTCOME_URL) return;
        var url = window.AI_USAGE_OUTCOME_URL.replace("/0/", "/" + id + "/");
        try {
            fetch(url, {
                method: "POST", credentials: "same-origin",
                headers: { "X-Requested-With": "fetch" },
                body: new URLSearchParams({ outcome: outcome }),
                keepalive: true,          // survive the page navigation on save
            });
        } catch (e) { /* a metric must never disrupt the user */ }
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
        lastHashtags = data.hashtags || [];
        lastKeywords = data.keywords || [];
        var text = composeCaption(data.caption || "", lastHashtags, lastKeywords);
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

        // Keep-rate: a fresh generation supersedes any prior un-saved one.
        var newId = data.ai_usage_id || null;
        if (pendingUsageId && pendingUsageId !== newId) {
            reportOutcome(pendingUsageId, "discarded");
        }
        pendingUsageId = newId;

        renderVariations(data.variations || []);
    }

    // Alternative captions rendered as clickable cards; click one to swap it
    // into the main caption. Text via textContent (untrusted model output).
    function renderVariations(list) {
        var box = document.getElementById("aiVariations");
        if (!box) return;
        lastVariations = list;
        box.innerHTML = "";
        if (!list.length) { box.hidden = true; return; }
        var head = document.createElement("div");
        head.className = "cmp-ai-var-head";
        head.textContent = "Other options — click to use:";
        box.appendChild(head);
        list.forEach(function (text, i) {
            var card = document.createElement("button");
            card.type = "button";
            card.className = "cmp-ai-var";
            card.setAttribute("data-ai-variation", String(i));
            card.textContent = text;
            box.appendChild(card);
        });
        box.hidden = false;
    }

    async function onGenerate(btn) {
        var cap = document.getElementById("captionInput");
        var ctx = captionContext();
        var taskId = ctx.taskId, clientId = ctx.clientId, platforms = ctx.platforms;
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
            var toneEl = document.getElementById("aiTone");
            if (toneEl && toneEl.value) body.set("tone", toneEl.value);
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

    // Task / client / platforms currently selected in the composer - the
    // context both Generate and Rewrite send so output stays on-brand and
    // within each platform's limit.
    function captionContext() {
        var taskEl = document.querySelector('input[name="task_id"]');
        var clientEl = document.getElementById("clientSelect");
        return {
            taskId: (taskEl && taskEl.value) || "",
            clientId: (clientEl && clientEl.value) || "",
            platforms: uniq(Array.prototype.slice.call(
                document.querySelectorAll('input[name="account_ids"]:checked'))
                .map(function (c) { return c.dataset.platform; })
                .filter(Boolean)),
        };
    }

    // One-click transform of whatever is already in the caption box (works on
    // an AI draft OR a hand-written caption). On failure the caption is left
    // untouched - we never silently keep the old text as if it succeeded.
    async function onRewrite(btn, action) {
        var cap = document.getElementById("captionInput");
        var text = (cap && cap.value.trim()) || "";
        if (!text) { toast("Write or generate a caption first.", "error"); return; }

        var ctx = captionContext();
        busy(btn, true);
        try {
            var body = new URLSearchParams();
            body.set("text", cap.value);
            body.set("action", action);
            if (ctx.taskId) body.set("task_id", ctx.taskId);
            if (ctx.clientId) body.set("client_id", ctx.clientId);
            body.set("platforms", ctx.platforms.join(","));
            var r = await fetch(window.AI_REWRITE_URL, {
                method: "POST", credentials: "same-origin",
                headers: { "X-Requested-With": "fetch" }, body: body,
            });
            var data = await r.json().catch(function () { return {}; });
            if (!r.ok || !data.caption) {
                toast(data.error || "Couldn't rewrite the caption.", "error");
                return;
            }
            cap.value = data.caption; fire(cap);
            var flag = document.getElementById("aiAssisted");
            if (flag) flag.value = "1";
            // Keep-rate: a rewrite supersedes any prior un-saved draft.
            var newId = data.ai_usage_id || null;
            if (pendingUsageId && pendingUsageId !== newId) {
                reportOutcome(pendingUsageId, "discarded");
            }
            pendingUsageId = newId;
            toast("Caption updated — review before publishing.", "success");
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

    // Saving the compose form with a pending AI generation = the draft was
    // kept. Delegated so it survives Turbo; fires before Turbo submits.
    document.addEventListener("submit", function (event) {
        if (!pendingUsageId || !event.target) return;
        var cap = document.getElementById("captionInput");
        if (cap && event.target.contains && event.target.contains(cap)) {
            reportOutcome(pendingUsageId, "used");
            pendingUsageId = null;
        }
    });

    document.addEventListener("click", function (event) {
        if (!event.target.closest) return;
        var gen = event.target.closest("#aiGenBtn");
        if (gen) { event.preventDefault(); onGenerate(gen); return; }
        var alt = event.target.closest("#aiAltBtn");
        if (alt) { event.preventDefault(); onAltText(alt); return; }
        var regen = event.target.closest("#aiRegenBtn");
        if (regen) { event.preventDefault(); onGenerate(regen); return; }
        var rw = event.target.closest("[data-ai-rewrite]");
        if (rw) {
            event.preventDefault();
            onRewrite(rw, rw.getAttribute("data-ai-rewrite"));
            return;
        }
        var vary = event.target.closest("[data-ai-variation]");
        if (vary) {
            event.preventDefault();
            var idx = parseInt(vary.getAttribute("data-ai-variation"), 10);
            var cap = document.getElementById("captionInput");
            if (cap && lastVariations[idx] != null) {
                // A variation carries the same hashtags + keywords as the main
                // draft, so switching to it keeps the full block, not bare text.
                cap.value = composeCaption(
                    lastVariations[idx], lastHashtags, lastKeywords);
                fire(cap);
            }
            var flag = document.getElementById("aiAssisted");
            if (flag) flag.value = "1";
            var box = document.getElementById("aiVariations");
            if (box) {
                box.querySelectorAll(".cmp-ai-var").forEach(function (c) {
                    c.classList.remove("sel");
                });
            }
            vary.classList.add("sel");
        }
    });
})();
