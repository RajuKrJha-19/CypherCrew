/*
    teams-realtime.js — the transport, and nothing else.

    There is no websocket layer in this app: gunicorn runs sync workers, so
    a held-open connection is a held-open worker, and two of those is the
    whole server. So Teams polls — but adaptively, and through exactly one
    endpoint (/teams/api/sync) that returns everything the shell needs per
    tick. Four pollers would be four times the requests for data that comes
    out of the same three tables.

    Cadence:
        visible + recently used   ->  ~2s   (server-supplied)
        visible + idle > 60s      ->  ~30s
        hidden tab                ->  ~15s
        after a send / more:true  ->  immediately

    The server has the last word: every response carries next_poll_ms, so
    the cadence can be retuned in production without touching this file.

    This module deliberately knows nothing about the DOM of a message. It
    fetches, and it dispatches `teams:sync` with the payload. teams-chat.js
    does the rendering. That split is what makes swapping this for SSE a
    contained change: the replacement dispatches the same event with the
    same payload, and nothing downstream notices.
*/
(function () {
    "use strict";

    // setTimeout, not setInterval: the interval changes every tick, and a
    // fixed interval cannot slow down. Also self-limiting — the next tick
    // is only scheduled once the previous response has landed, so a slow
    // server can never accumulate a queue of in-flight polls.
    //
    // The Teams shell is re-injected when crossing the ERP<->Teams
    // boundary (data-turbo-permanent holds within a shell, not across
    // shells), so tear the previous instance down first or every crossing
    // adds another poller. Same scar as notifications.js.
    if (window.__teamsPoller) {
        window.__teamsPoller.stop();
    }

    var IDLE_AFTER_MS = 60000;

    var state = {
        timer: null,
        stopped: false,
        inFlight: false,
        channelId: null,
        cursorId: 0,          // highest message id held
        since: null,          // change-sweep cursor, echoed from the server
        threadRootId: null,   // open thread, if any
        threadCursorId: 0,
        lastInput: Date.now(),
        typing: false,
        failures: 0
    };

    function meta(name, fallback) {
        var el = document.querySelector('meta[name="' + name + '"]');
        var value = el && parseInt(el.getAttribute("content"), 10);
        return value > 0 ? value : fallback;
    }

    var CADENCE = {
        active: meta("teams-poll-active", 2000),
        hidden: meta("teams-poll-hidden", 15000),
        idle: meta("teams-poll-idle", 30000)
    };

    function interval(serverSuggested) {
        if (document.visibilityState === "hidden") return CADENCE.hidden;
        if (Date.now() - state.lastInput > IDLE_AFTER_MS) return CADENCE.idle;
        return serverSuggested || CADENCE.active;
    }

    function schedule(ms) {
        clearTimeout(state.timer);
        if (state.stopped) return;
        state.timer = setTimeout(tick, ms);
    }

    function url() {
        var params = new URLSearchParams();
        if (state.channelId) {
            params.set("channel", state.channelId);
            params.set("after", state.cursorId || 0);
            if (state.typing) params.set("typing", "1");
        }
        // An open thread rides the SAME tick as the channel rather than
        // polling on its own - two pollers for one screen would double the
        // request rate for the sake of a side panel.
        if (state.threadRootId) {
            params.set("thread", state.threadRootId);
            params.set("tafter", state.threadCursorId || 0);
        }
        if (state.since) params.set("since", state.since);
        params.set("focus", document.visibilityState === "hidden" ? "0" : "1");
        return "/teams/api/sync?" + params.toString();
    }

    function tick() {
        if (state.stopped || state.inFlight) return;
        state.inFlight = true;

        fetch(url(), {
            headers: { "Accept": "application/json" },
            credentials: "same-origin"
        })
            .then(function (response) {
                if (!response.ok) throw new Error("sync " + response.status);
                return response.json();
            })
            .then(function (payload) {
                state.inFlight = false;
                state.failures = 0;
                state.typing = false;
                state.since = payload.cursor || state.since;

                // Advance the cursor before dispatching, so a handler that
                // triggers another poll can't re-request what it just got.
                (payload.messages || []).forEach(function (m) {
                    if (m.id > state.cursorId) state.cursorId = m.id;
                });
                (payload.thread || []).forEach(function (m) {
                    if (m.id > state.threadCursorId) state.threadCursorId = m.id;
                });

                document.dispatchEvent(
                    new CustomEvent("teams:sync", { detail: payload }));

                // `more` means the server truncated the delta. Come straight
                // back rather than trickling a backlog out one tick at a
                // time — catching up after lunch should not take a minute.
                schedule(payload.more ? 0 : interval(payload.next_poll_ms));
            })
            .catch(function () {
                state.inFlight = false;
                state.failures += 1;
                // Exponential backoff, capped. Without it, a 500 loop from
                // a dozen open tabs at 2s is a self-inflicted outage on a
                // server that is already unwell.
                var wait = Math.min(30000, 2000 * Math.pow(2, state.failures - 1));
                schedule(wait);
            });
    }

    var poller = {
        /* Point the poller at a conversation (or null for shell-only). */
        attach: function (channelId, cursorId) {
            state.channelId = channelId || null;
            state.cursorId = cursorId || 0;
            state.since = null;
            schedule(0);
        },
        /* Highest message id currently held — teams-chat.js keeps this
           honest when it renders an optimistic bubble. */
        cursor: function (value) {
            if (typeof value === "number" && value > state.cursorId) {
                state.cursorId = value;
            }
            return state.cursorId;
        },
        /* Open/close a thread. Resets the thread cursor so the pane loads
           the whole conversation, then keeps it live on the same tick. */
        attachThread: function (rootId) {
            state.threadRootId = rootId || null;
            state.threadCursorId = 0;
            schedule(0);
        },
        detachThread: function () {
            state.threadRootId = null;
            state.threadCursorId = 0;
        },
        threadRoot: function () { return state.threadRootId; },
        threadCursor: function (value) {
            if (typeof value === "number" && value > state.threadCursorId) {
                state.threadCursorId = value;
            }
            return state.threadCursorId;
        },
        /* Poll now — after a send, so the sender isn't the last to know. */
        poke: function () { schedule(0); },
        /* Flag "still typing" onto the next tick instead of spending a
           request of its own on it. */
        setTyping: function () { state.typing = true; },
        /* Any keypress or click counts as attention, which is what keeps
           the cadence fast while someone is actually working. */
        active: function () { state.lastInput = Date.now(); },
        stop: function () {
            state.stopped = true;
            clearTimeout(state.timer);
            document.removeEventListener("visibilitychange", onVisibility);
        }
    };

    function onVisibility() {
        if (document.visibilityState === "visible") {
            // Coming back to the tab should show current state at once, not
            // whatever was true 15 seconds ago.
            state.lastInput = Date.now();
            schedule(0);
        } else {
            schedule(interval(null));
        }
    }

    document.addEventListener("visibilitychange", onVisibility);

    window.__teamsPoller = poller;
    window.TeamsRealtime = poller;

    // Turbo tears down page scripts between visits; make sure leaving Teams
    // fully stops the poller. Just clearing the timer left state.stopped false,
    // so the document-level visibilitychange listener (bound on the persistent
    // document) would resume /teams/api/sync from any ERP page. stop() clears
    // the timer AND flips the flag, and is idempotent.
    if (window.App && window.App.onCleanup) {
        window.App.onCleanup(function () { poller.stop(); });
    }
})();
