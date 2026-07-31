/* Attendance top-bar widget.
 *
 * Polls /attendance/status and reflects it on the pill. The widget lives in the
 * ERP shell (data-turbo-permanent), so within the ERP this runs once. But
 * crossing to Social Studio and back rebuilds the ERP topbar and re-runs this
 * script, so it must RE-INIT against the fresh DOM rather than early-return -
 * otherwise the new pill never wires up and the old poll timer keeps firing at
 * detached nodes. The timer is stored on window so a re-run clears the prior
 * one; the document-level listeners bind exactly once and read the live `el`.
 */
(function () {
    "use strict";

    var API = window.CYPHER_ATTENDANCE;
    if (!API) return;

    var POLL_MS = 45000;
    var el = {};

    function q() {
        el = {
            widget: document.getElementById("checkinWidget"),
            trigger: document.getElementById("checkinTrigger"),
            dot: document.getElementById("checkinDot"),
            label: document.getElementById("checkinLabel"),
            panel: document.getElementById("checkinPanel"),
            since: document.getElementById("checkinSince"),
            idleHint: document.getElementById("checkinIdleHint"),
            btnIn: document.getElementById("checkinBtnIn"),
            btnOut: document.getElementById("checkinBtnOut"),
            btnSnooze: document.getElementById("checkinBtnSnooze"),
            note: document.getElementById("checkinNote")
        };
    }

    function show(node, on) { if (node) node.hidden = !on; }

    function setNote(msg) {
        if (!el.note) return;
        el.note.textContent = msg || "";
        el.note.hidden = !msg;
    }

    function render(s) {
        if (!el.widget) return;
        el.widget.hidden = false;

        var checkedIn = !!s.checked_in;
        var idle = checkedIn && !s.has_active_task;
        var snoozed = !!s.snoozed_until && new Date(s.snoozed_until) > new Date();

        el.dot.className = "checkin-dot "
            + (!checkedIn ? "is-out" : (idle && !snoozed ? "is-idle" : "is-in"));
        el.label.textContent = checkedIn ? "Checked in" : "Checked out";
        if (el.since) {
            el.since.textContent = checkedIn && s.since_label
                ? ("Since " + s.since_label) : "";
        }
        show(el.idleHint, idle && !snoozed);
        show(el.btnIn, !!s.can_checkin);
        show(el.btnOut, !!s.can_checkout);
        show(el.btnSnooze, idle);

        if (!checkedIn && s.source === "zoho") {
            setNote("Check in on Zoho People to start your day.");
        } else if (snoozed) {
            setNote("Idle reminders snoozed.");
        } else {
            setNote("");
        }
    }

    function poll() {
        if (!el.widget) return;
        fetch(API.status, { headers: { "Accept": "application/json" } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (s) { if (s) render(s); })
            .catch(function () { /* transient - next poll retries */ });
    }

    function act(url) {
        return fetch(url, { method: "POST", headers: { "Accept": "application/json" } })
            .then(function (r) { return r.json().then(function (b) {
                return { ok: r.ok, body: b }; }); })
            .then(function (res) {
                if (res.ok) { render(res.body); }
                else { setNote((res.body && res.body.error) || "Something went wrong."); }
            })
            .catch(function () { setNote("Network error - please retry."); });
    }

    function openPanel(on) {
        if (!el.panel || !el.trigger) return;
        var open = on === undefined ? el.panel.hidden : on;
        el.panel.hidden = !open;
        el.trigger.setAttribute("aria-expanded", open ? "true" : "false");
    }

    // Document-level listeners bind ONCE and read the live `el`, so they keep
    // working after a shell rebuild without stacking.
    if (!window.__cypherAttendanceDocBound) {
        window.__cypherAttendanceDocBound = true;
        document.addEventListener("click", function (e) {
            if (el.widget && !el.widget.contains(e.target)) openPanel(false);
        });
        document.addEventListener("visibilitychange", function () {
            if (!document.hidden) poll();
        });
    }

    function init() {
        q();
        if (!el.widget) return;

        // Element-level listeners on the CURRENT nodes. Old nodes were removed
        // with the previous shell, so their listeners went with them.
        el.trigger.addEventListener("click", function (e) {
            e.stopPropagation();
            openPanel();
        });
        if (el.btnIn) el.btnIn.addEventListener("click", function () { act(API.checkin); });
        if (el.btnOut) el.btnOut.addEventListener("click", function () { act(API.checkout); });
        if (el.btnSnooze) el.btnSnooze.addEventListener("click", function () { act(API.snooze); });

        // Clear any timer left by a previous shell, then start fresh.
        if (window.__cypherAttendanceTimer) {
            clearInterval(window.__cypherAttendanceTimer);
        }
        poll();
        window.__cypherAttendanceTimer = setInterval(poll, POLL_MS);
    }

    init();
})();
