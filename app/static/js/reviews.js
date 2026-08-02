/* AI-draft button on the review reply inbox: POST for a draft, drop it into
 * that review's reply box for the human to edit + post. Delegated + guarded
 * (survives Turbo, binds once). CSRF added by csrf.js.
 */
(function () {
    "use strict";
    if (window.__reviewsInit) return;
    window.__reviewsInit = true;

    function toast(msg, type) {
        if (typeof window.showToast === "function") window.showToast(msg, type);
    }

    async function draft(btn) {
        var id = btn.getAttribute("data-target");
        var form = document.querySelector('.rev-reply-form[data-review-id="' + id + '"]');
        var box = form && form.querySelector(".rev-reply-input");
        if (!box) return;
        if (box.value.trim() &&
            !window.confirm("Replace the current reply with an AI draft?")) {
            return;
        }
        btn.classList.add("is-loading");
        btn.disabled = true;
        try {
            var r = await fetch(btn.getAttribute("data-draft-url"), {
                method: "POST", credentials: "same-origin",
                headers: { "X-Requested-With": "fetch" },
            });
            var data = await r.json().catch(function () { return {}; });
            if (!r.ok) { toast(data.error || "Couldn't draft a reply.", "error"); return; }
            box.value = data.reply || "";
            box.dispatchEvent(new Event("input", { bubbles: true }));
            toast("Draft ready — review and edit before posting.", "success");
        } catch (e) {
            toast("Network error — please try again.", "error");
        } finally {
            btn.classList.remove("is-loading");
            btn.disabled = false;
        }
    }

    document.addEventListener("click", function (event) {
        var btn = event.target.closest && event.target.closest("[data-draft-url]");
        if (btn) { event.preventDefault(); draft(btn); }
    });
})();
