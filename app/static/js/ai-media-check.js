/* AI Media QA on the task detail page.
 *
 * A "Check media" button on each submission file runs an advisory pass against
 * the brief + the client's brand knowledge base and renders the findings
 * inline. Never blocks anything. Delegated + guarded so it survives Turbo
 * navigations and binds once. Loaded only when AI_ENABLED. Finding text is
 * model/DB output - rendered via textContent, never innerHTML.
 */
(function () {
    "use strict";
    if (window.__aiMediaCheckInit) return;
    window.__aiMediaCheckInit = true;

    var SEV = {
        error: { icon: "fa-circle-exclamation", cls: "sev-error" },
        warning: { icon: "fa-triangle-exclamation", cls: "sev-warning" },
        info: { icon: "fa-circle-info", cls: "sev-info" },
    };

    function el(tag, cls) {
        var e = document.createElement(tag);
        if (cls) e.className = cls;
        return e;
    }

    function render(container, data) {
        container.innerHTML = "";
        container.hidden = false;
        var findings = data.findings || [];

        var head = el("div", "aicheck-head " + (data.status === "clean" ? "clean" : "flagged"));
        var icon = el("i", "fa-solid " + (data.status === "clean"
            ? "fa-circle-check" : "fa-wand-magic-sparkles"));
        head.appendChild(icon);
        var text = data.status === "clean"
            ? "AI check: looks good."
            : "AI check: " + findings.length + " thing"
              + (findings.length === 1 ? "" : "s") + " to review.";
        head.appendChild(document.createTextNode(" " + text));
        container.appendChild(head);

        if (findings.length) {
            var ul = el("ul", "aicheck-list");
            findings.forEach(function (f) {
                var sev = SEV[f.severity] || SEV.info;
                var li = el("li", "aicheck-item " + sev.cls);
                li.appendChild(el("i", "fa-solid " + sev.icon));
                var msg = el("span", "aicheck-msg");
                msg.textContent = f.message || "";      // untrusted -> textContent
                li.appendChild(msg);
                ul.appendChild(li);
            });
            container.appendChild(ul);
        }
        if (data.model) {
            var meta = el("div", "aicheck-meta");
            meta.textContent = "Advisory · " + data.model;
            container.appendChild(meta);
        }
    }

    function renderError(container, message) {
        container.innerHTML = "";
        container.hidden = false;
        var head = el("div", "aicheck-head flagged");
        head.textContent = message || "Couldn't run the check.";
        container.appendChild(head);
    }

    async function run(btn) {
        var url = btn.getAttribute("data-ai-check-url");
        var target = document.querySelector(btn.getAttribute("data-target"));
        if (!url || !target) return;
        btn.classList.add("is-loading");
        btn.disabled = true;
        try {
            var r = await fetch(url, {
                method: "POST", credentials: "same-origin",
                headers: { "X-Requested-With": "fetch" },
            });
            var data = await r.json().catch(function () { return {}; });
            if (!r.ok) { renderError(target, data.error); return; }
            render(target, data);
        } catch (e) {
            renderError(target, "Network error — please try again.");
        } finally {
            btn.classList.remove("is-loading");
            btn.disabled = false;
        }
    }

    document.addEventListener("click", function (event) {
        var btn = event.target.closest && event.target.closest("[data-ai-check]");
        if (btn) { event.preventDefault(); run(btn); }
    });
})();
