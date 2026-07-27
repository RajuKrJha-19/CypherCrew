/*
    Changing a task's status, from wherever.

    Three surfaces do it - dragging a card on the board, the dropdown in
    the list's Status column, and the stepper on the task page - and all
    three post to the same /tasks/kanban/update-status endpoint. This
    holds the two things they must agree on:

      askReason()   the dialog shown before a task leaves Core Review or
                    Client Review. Coming back out of a review overturns
                    somebody's decision, so it takes a written reason, and
                    the move does not happen until one is given. Cancel
                    means nothing moved.

      changeStatus() the request itself, which also handles the case where
                    a caller forgot to ask: the server answers a missing
                    reason with needs_reason, and we prompt and retry
                    rather than showing an error the user cannot act on.

    Scheduled and Published are not here because they are not reachable
    this way at all - see task_status.DRAG_LOCKED_STATUSES. The UI hides
    them and the server refuses them.
*/
(function () {
    "use strict";

    if (window.CypherStatus) return;

    var MIN_REASON = 4;

    function toast(message, type) {
        if (typeof window.showToast === "function") {
            window.showToast(message, type || "success");
        }
    }

    /**
     * Ask why a task is leaving a review. Resolves with the reason, or
     * null if the person backed out.
     */
    function askReason(options) {
        var opts = options || {};

        return new Promise(function (resolve) {

            var overlay = document.createElement("div");
            overlay.className = "status-reason-overlay";

            var box = document.createElement("div");
            box.className = "status-reason-box";
            box.setAttribute("role", "dialog");
            box.setAttribute("aria-modal", "true");
            box.setAttribute("aria-label", "Reason for moving this task");

            box.innerHTML =
                "<h3>Why is this leaving " + escapeHtml(opts.from || "review") + "?</h3>" +
                "<p>" +
                (opts.title
                    ? "<b>" + escapeHtml(opts.title) + "</b> "
                    : "") +
                "moves to <b>" + escapeHtml(opts.to || "") + "</b>. " +
                "The assignee sees this on the task timeline.</p>" +
                '<textarea rows="3" maxlength="500" ' +
                'placeholder="e.g. thumbnail needs redoing before this goes out"></textarea>' +
                '<p class="status-reason-error" hidden></p>' +
                '<div class="status-reason-actions">' +
                '<button type="button" class="btn btn-secondary" data-cancel>Cancel</button>' +
                '<button type="button" class="btn" data-confirm>Move task</button>' +
                "</div>";

            var field = box.querySelector("textarea");
            var error = box.querySelector(".status-reason-error");
            var cancel = box.querySelector("[data-cancel]");
            var confirm = box.querySelector("[data-confirm]");

            function close(value) {
                document.removeEventListener("keydown", onKey);
                overlay.remove();
                resolve(value);
            }

            function submit() {
                var value = (field.value || "").trim();

                if (value.length < MIN_REASON) {
                    error.textContent =
                        "Give a short reason (at least " + MIN_REASON + " characters).";
                    error.hidden = false;
                    field.focus();
                    return;
                }

                close(value);
            }

            function onKey(event) {
                if (event.key === "Escape") close(null);
                // Ctrl/Cmd+Enter submits, the way every other multi-line
                // box in the app does.
                if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) submit();
            }

            cancel.addEventListener("click", function () { close(null); });
            confirm.addEventListener("click", submit);
            overlay.addEventListener("click", function (event) {
                if (event.target === overlay) close(null);
            });
            document.addEventListener("keydown", onKey);

            overlay.appendChild(box);
            document.body.appendChild(overlay);
            field.focus();
        });
    }

    function escapeHtml(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    function post(taskId, status, reason) {
        return fetch("/tasks/kanban/update-status", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                task_id: taskId,
                status: status,
                reason: reason || "",
            }),
        }).then(function (res) {
            return res.json().then(function (data) {
                return { ok: res.ok, data: data || {} };
            });
        });
    }

    /**
     * Move a task, asking for a reason first when the status it is
     * leaving needs one. Resolves { moved: bool, message: string }.
     */
    async function changeStatus(options) {
        var opts = options || {};
        var reason = "";

        if (opts.needsReason) {
            reason = await askReason({
                from: opts.from,
                to: opts.to,
                title: opts.title,
            });

            if (reason === null) return { moved: false, cancelled: true };
        }

        var result;

        try {
            result = await post(opts.taskId, opts.to, reason);
        } catch (err) {
            return {
                moved: false,
                message: "Unable to update task. Check your connection and try again.",
            };
        }

        // The caller did not know a reason was needed - ask now rather
        // than reporting a failure the user cannot do anything about.
        if (!result.data.success && result.data.needs_reason) {
            reason = await askReason({
                from: result.data.from_status || opts.from,
                to: opts.to,
                title: opts.title,
            });

            if (reason === null) return { moved: false, cancelled: true };

            try {
                result = await post(opts.taskId, opts.to, reason);
            } catch (err) {
                return {
                    moved: false,
                    message: "Unable to update task. Check your connection and try again.",
                };
            }
        }

        if (!result.data.success) {
            return {
                moved: false,
                message: result.data.message || "Unable to update task status.",
            };
        }

        return { moved: true, message: result.data.message || "" };
    }

    window.CypherStatus = {
        askReason: askReason,
        changeStatus: changeStatus,
        toast: toast,
        MIN_REASON: MIN_REASON,
    };
})();
