/* Attendance top-bar widget.
 *
 * Polls /attendance/status and reflects it on the pill. The shell is
 * data-turbo-permanent, so this runs once for the life of the tab; a guard
 * keeps it from double-initialising if the script is ever re-evaluated.
 */
(function () {
    "use strict";

    if (window.__cypherAttendanceInit) return;
    window.__cypherAttendanceInit = true;

    var API = window.CYPHER_ATTENDANCE;
    if (!API) return;

    var POLL_MS = 45000;

    var el = {
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
    if (!el.widget) return;

    var timer = null;

    function show(node, on) {
        if (node) node.hidden = !on;
    }

    function setNote(msg) {
        if (!el.note) return;
        el.note.textContent = msg || "";
        el.note.hidden = !msg;
    }

    function render(s) {
        // Reveal the widget on the first successful status.
        el.widget.hidden = false;

        var checkedIn = !!s.checked_in;
        var idle = checkedIn && !s.has_active_task;
        var snoozed = !!s.snoozed_until && new Date(s.snoozed_until) > new Date();

        // Dot: green = working, amber = idle, grey = out.
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
        fetch(API.status, { headers: { "Accept": "application/json" } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (s) { if (s) render(s); })
            .catch(function () { /* transient - next poll retries */ });
    }

    function act(url) {
        return fetch(url, {
            method: "POST",
            headers: { "Accept": "application/json" }
        })
            .then(function (r) { return r.json().then(function (b) {
                return { ok: r.ok, body: b }; }); })
            .then(function (res) {
                if (res.ok) {
                    render(res.body);
                } else {
                    setNote((res.body && res.body.error) || "Something went wrong.");
                }
            })
            .catch(function () { setNote("Network error - please retry."); });
    }

    // --- Panel open/close ---
    function openPanel(on) {
        var open = on === undefined ? el.panel.hidden : on;
        el.panel.hidden = !open;
        el.trigger.setAttribute("aria-expanded", open ? "true" : "false");
    }

    el.trigger.addEventListener("click", function (e) {
        e.stopPropagation();
        openPanel();
    });
    document.addEventListener("click", function (e) {
        if (!el.widget.contains(e.target)) openPanel(false);
    });

    if (el.btnIn) el.btnIn.addEventListener("click", function () {
        act(API.checkin); });
    if (el.btnOut) el.btnOut.addEventListener("click", function () {
        act(API.checkout); });
    if (el.btnSnooze) el.btnSnooze.addEventListener("click", function () {
        act(API.snooze); });

    document.addEventListener("visibilitychange", function () {
        if (!document.hidden) poll();
    });

    poll();
    timer = setInterval(poll, POLL_MS);
})();
