/*
    The look before the leap.

    Publishing was a single unguarded click: Schedule and Publish now both
    submitted straight through, and what actually went out - the post type each
    platform resolved to, the caption after per-channel overrides, the exact
    instant per channel, whether a channel would be refused outright - was only
    reported afterwards, in a flash message. The person publishing found out
    what they had published by reading the result.

    This intercepts that submit and asks the server what the button will do,
    then shows the answer and lets them decide.

    Two things are deliberate:

      - The content is fetched, not built here. A review assembled from what
        this page already knows could only ever repeat the page's own
        assumptions; the whole value is that it reflects what the SERVER will
        send. So the markup comes from /social/posts/<id>/review.

      - The server is the gate, not this script. The review carries a
        fingerprint of the post it described, which goes back with the
        submission; schedule_post refuses if the post has changed since. A
        review the client can skip - or that a colleague's edit can silently
        invalidate - is not a review.

    Capture phase, ahead of Turbo, the same shape social-publish-confirm.js and
    confirm-submit.js use.
*/
(function () {

    if (window.__cypherPublishReview) return;
    window.__cypherPublishReview = true;

    var OVERLAY = "publish-review-overlay";

    function esc(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    }

    function closeAny() {
        var open = document.getElementById(OVERLAY);
        if (open) open.remove();
        document.removeEventListener("keydown", onKeydown, true);
    }

    function onKeydown(event) {
        if (event.key !== "Escape") return;
        // The media viewer owns Escape while it is up - closing the review out
        // from under it would leave a viewer with nothing behind it.
        if (document.querySelector(".media-viewer.show")) return;
        event.stopPropagation();
        closeAny();
    }

    function shell(bodyHtml, footHtml) {
        var overlay = document.createElement("div");
        overlay.id = OVERLAY;
        overlay.className = "publish-review-overlay";

        overlay.innerHTML =
            '<div class="publish-review-box" role="dialog" aria-modal="true"'
            + ' aria-label="Review before publishing">'
            + '<div class="publish-review-body">' + bodyHtml + '</div>'
            + '<div class="publish-review-actions">' + footHtml + '</div>'
            + '</div>';

        overlay.addEventListener("click", function (event) {
            if (event.target === overlay) closeAny();
        });

        document.body.appendChild(overlay);
        document.addEventListener("keydown", onKeydown, true);
        return overlay;
    }

    function confirmLabel(count, blocked) {
        if (!count) return "Nothing to publish";
        var noun = count === 1 ? "channel" : "channels";
        var label = "Publish to " + count + " " + noun;
        if (blocked) {
            label += " — " + blocked + (blocked === 1 ? " blocked" : " blocked");
        }
        return label;
    }

    function open(form, mode) {
        var postId = form.getAttribute("data-publish-review");
        var timeField = form.querySelector('input[name="schedule"]');
        var raw = timeField ? timeField.value : "";

        var url = "/social/posts/" + encodeURIComponent(postId) + "/review"
            + "?publish_mode=" + encodeURIComponent(mode)
            + "&schedule=" + encodeURIComponent(raw);

        var overlay = shell(
            '<div class="publish-review-loading">'
            + '<i class="fa-solid fa-circle-notch fa-spin"></i>'
            + ' Checking what will be published…</div>',
            '<button type="button" class="btn btn-secondary"'
            + ' data-review-cancel>Cancel</button>');

        fetch(url, {
            credentials: "same-origin",
            headers: { "X-Requested-With": "fetch" },
        }).then(function (response) {
            if (!response.ok) throw new Error("HTTP " + response.status);
            return response.text();
        }).then(function (html) {
            var body = overlay.querySelector(".publish-review-body");
            var foot = overlay.querySelector(".publish-review-actions");
            body.innerHTML = html;

            var panel = body.querySelector(".pubrev");
            var fingerprint = panel ? panel.getAttribute("data-fingerprint") : "";
            var rows = body.querySelectorAll(".pubrev-row");
            var blocked = body.querySelectorAll(".pubrev-row.is-block").length;
            var going = rows.length - blocked;

            foot.innerHTML =
                '<button type="button" class="btn btn-secondary"'
                + ' data-review-cancel>Cancel</button>'
                + '<button type="button" class="btn" data-review-go'
                + (going ? "" : " disabled")
                + '>' + esc(confirmLabel(going, blocked)) + '</button>';

            var go = foot.querySelector("[data-review-go]");
            if (go && going) {
                go.addEventListener("click", function () {
                    // Hand the fingerprint back so the server can refuse a
                    // confirmation that no longer describes this post.
                    var field = form.querySelector(
                        'input[name="review_fingerprint"]');
                    if (!field) {
                        field = document.createElement("input");
                        field.type = "hidden";
                        field.name = "review_fingerprint";
                        form.appendChild(field);
                    }
                    field.value = fingerprint;

                    // The button that was clicked carries publish_mode, and a
                    // programmatic submit would drop it - so send it as a
                    // field instead.
                    var modeField = form.querySelector(
                        'input[name="publish_mode"]');
                    if (!modeField) {
                        modeField = document.createElement("input");
                        modeField.type = "hidden";
                        modeField.name = "publish_mode";
                        form.appendChild(modeField);
                    }
                    modeField.value = mode;

                    form.dataset.publishReviewed = "1";
                    closeAny();
                    form.requestSubmit();
                });
                go.focus();
            }
        }).catch(function () {
            var body = overlay.querySelector(".publish-review-body");
            body.innerHTML =
                '<p class="pubrev-alert is-bad">Could not load the review.'
                + ' Nothing has been published.</p>';
        });

        overlay.addEventListener("click", function (event) {
            if (event.target.closest("[data-review-cancel]")) closeAny();
        });
    }

    document.addEventListener("submit", function (event) {

        var form = event.target;
        if (!form || form.nodeName !== "FORM") return;
        if (!form.hasAttribute("data-publish-review")) return;

        // Already reviewed and confirmed - let it through.
        if (form.dataset.publishReviewed === "1") {
            delete form.dataset.publishReviewed;
            return;
        }

        // Which button was pressed decides whether this is "now" or a
        // scheduled time, and the answer changes what the review must say.
        var submitter = event.submitter;
        var mode = (submitter && submitter.name === "publish_mode")
            ? submitter.value : "schedule";

        event.preventDefault();
        event.stopPropagation();   // or Turbo submits it anyway

        open(form, mode);

    }, true);

    // Turbo swaps the body; an overlay left behind would hang over the next
    // page with no way to dismiss it.
    document.addEventListener("turbo:before-render", closeAny);

})();
