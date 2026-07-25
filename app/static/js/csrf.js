/*
    CSRF token transport.

    Flask-WTF protects every state-changing request. Rather than hand-add a
    hidden field to 40+ forms (and risk missing one and breaking it), the
    token is attached automatically, two ways so nothing slips through:

      1. fetch wrapper - adds the X-CSRFToken header to every same-origin
         mutating fetch. Covers all AJAX (quick-edit, kanban, notifications,
         comments, autosave, uploads) and Turbo form submissions, which
         Turbo sends via fetch.
      2. submit injector - on any same-origin non-GET form submission, adds
         a hidden csrf_token field if one isn't already there. Covers
         native, non-fetch form posts.

    The token comes from <meta name="csrf-token"> in the head, rendered by
    the server on every page.

    This file is loaded early and NOT deferred, so the fetch wrapper is in
    place before Turbo captures its own reference to fetch, and the submit
    listener is attached before any form can be submitted.
*/
(function () {

    function token() {
        var m = document.querySelector('meta[name="csrf-token"]');
        return m ? m.getAttribute("content") : "";
    }

    var SAFE_METHOD = /^(GET|HEAD|OPTIONS|TRACE)$/i;

    function sameOrigin(url) {
        try {
            return new URL(url, window.location.href).origin === window.location.origin;
        } catch (e) {
            return true;  // a relative URL is same-origin
        }
    }

    // 1) fetch wrapper -----------------------------------------------------
    if (window.fetch) {
        var nativeFetch = window.fetch;
        window.fetch = function (input, init) {
            init = init || {};
            var method = init.method
                || (typeof input === "object" && input && input.method)
                || "GET";
            var url = (typeof input === "string")
                ? input
                : (input && input.url) || "";

            if (!SAFE_METHOD.test(method) && sameOrigin(url)) {
                var headers = new Headers(
                    init.headers
                    || (typeof input === "object" && input && input.headers)
                    || {}
                );
                if (!headers.has("X-CSRFToken")) {
                    headers.set("X-CSRFToken", token());
                }
                init.headers = headers;
            }

            return nativeFetch.call(this, input, init);
        };
    }

    // 2) submit injector (capture phase, so it runs before Turbo's own
    // submit handler and the field is present when Turbo serialises it) ---
    document.addEventListener("submit", function (e) {
        var form = e.target;
        if (!form || form.tagName !== "FORM") return;

        var method = form.getAttribute("method") || "GET";
        if (SAFE_METHOD.test(method)) return;

        if (form.action && !sameOrigin(form.action)) return;
        if (form.querySelector('input[name="csrf_token"]')) return;

        var input = document.createElement("input");
        input.type = "hidden";
        input.name = "csrf_token";
        input.value = token();
        form.appendChild(input);
    }, true);
})();
