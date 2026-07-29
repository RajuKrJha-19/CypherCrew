/*
    teams-chat.js — the chat view's DOM.

    Listens for `teams:sync` (dispatched by teams-realtime.js) and paints.
    It never fetches the sync endpoint itself; the only requests it makes
    are the writes a user explicitly triggers. Keeping that boundary sharp
    is what makes replacing polling with SSE a one-file change.

    Every message on screen — first paint and poll alike — is HTML the
    server rendered from teams/_message.html. There is deliberately no
    client-side message renderer to drift out of step with it.
*/
(function () {
    "use strict";

    var BOTTOM_SLACK_PX = 80;

    // A short, opinionated row rather than the full emoji picker. Reacting
    // is a one-click gesture; making someone search a grid for 👍 turns it
    // into a decision. The composer still has the full picker.
    var QUICK_REACTIONS = ["👍", "🎉", "❤️", "😄", "👀", "🙏", "🔥", "✅"];

    function init() {
        var root = document.querySelector("[data-teams-chat]");
        if (!root) {
            // Not a chat page (browse, new channel). The poller still runs
            // shell-only so the sidebar's unread state stays live.
            if (window.TeamsRealtime) window.TeamsRealtime.attach(null, 0);
            return;
        }

        // Idempotent per DOM. init() is invoked from two places (see boot()
        // at the foot of this file) and both can fire for the same page, but
        // Turbo hands every navigation a fresh <body>, so the flag lives on
        // the node rather than in a module variable that would block the
        // second channel you open.
        if (root.dataset.tmReady === "1") return;
        root.dataset.tmReady = "1";

        var channelId = parseInt(root.dataset.channel, 10);
        var scroller = root.querySelector("[data-tm-scroll]");
        var list = root.querySelector("[data-tm-list]");
        var empty = root.querySelector("[data-tm-empty]");
        var composer = root.querySelector("[data-tm-composer]");
        var input = root.querySelector("[data-tm-input]");
        var typingBar = root.querySelector("[data-tm-typing]");

        scrollToBottom(scroller);

        if (window.TeamsRealtime) {
            window.TeamsRealtime.attach(
                channelId, parseInt(root.dataset.cursor, 10) || 0);
        }

        // ---- incoming ---------------------------------------------------

        App.on(document, "teams:sync", function (event) {
            var payload = event.detail || {};

            // Pin to the bottom only if the reader was already there.
            // Yanking someone away from what they were reading because a
            // message arrived is the most irritating bug a chat app has.
            var pinned = atBottom(scroller);

            (payload.messages || []).forEach(function (m) {
                if (m.ch !== channelId) return;
                var optimistic = m.cid && list.querySelector(
                    '[data-cid="' + cssEscape(m.cid) + '"][data-pending]');
                if (optimistic) {
                    // Reconcile: this is our own bubble coming back with a
                    // real id. Replace rather than append, or the sender
                    // sees everything they say twice.
                    optimistic.outerHTML = m.html;
                    return;
                }
                if (list.querySelector('[data-id="' + m.id + '"]')) return;
                list.insertAdjacentHTML("beforeend", m.html);
            });

            // Edits, deletes and reactions. Applied by id and idempotent,
            // so the deliberate overlap in the server's cursor is harmless.
            (payload.changed || []).forEach(function (m) {
                var node = list.querySelector('[data-id="' + m.id + '"]');
                if (node) node.outerHTML = m.html;
            });

            if ((payload.messages || []).length && empty) empty.hidden = true;

            if (pinned && (payload.messages || []).length) {
                scrollToBottom(scroller);
                markRead(channelId, window.TeamsRealtime.cursor());
            }

            // The open thread rides this same tick.
            var panel = pane(root);
            if (panel && !panel.hidden && (payload.thread || []).length) {
                appendThread(panel, payload.thread);
            }
            // A reply changes its root's "N replies" line, so the root
            // arrives through `changed` and its clone in the pane goes
            // stale unless it is replaced too.
            if (panel && !panel.hidden) {
                (payload.changed || []).forEach(function (m) {
                    var inPane = panel.querySelector('[data-id="' + m.id + '"]');
                    if (inPane) inPane.outerHTML = m.html;
                });
            }

            paintSidebar(payload, channelId);
            paintTyping(typingBar, payload.typing);
            paintPresence(payload.presence);
        });

        // ---- sending ----------------------------------------------------

        if (composer && input) {
            wireAttachments(root, composer, input, list, empty, scroller, channelId);

            App.on(composer, "submit", function (event) {
                event.preventDefault();
                send(composer, input, list, empty, scroller, channelId);
            });

            App.on(input, "keydown", function (event) {
                if (event.key === "Enter" && !event.shiftKey && !mentionPickerOpen()) {
                    event.preventDefault();
                    send(composer, input, list, empty, scroller, channelId);
                } else if (window.TeamsRealtime) {
                    window.TeamsRealtime.active();
                    if (input.value.trim()) window.TeamsRealtime.setTyping();
                }
            });

            App.on(input, "input", function () { autoGrow(input); });
            autoGrow(input);
        }

        // ---- message actions --------------------------------------------
        // Delegated: the list is replaced constantly, so per-node listeners
        // would leak on every poll.

        // Delegated at the shell, so it covers the thread pane's copies of
        // the same message controls without a second set of listeners.
        var shell = root.closest(".tm-shell") || root;

        App.on(shell, "click", function (event) {
            var target = event.target.closest(
                "[data-react],[data-react-open],[data-delete],[data-edit]," +
                "[data-reply],[data-thread]");
            if (!target) return;

            if (target.dataset.react) {
                react(target.dataset.react, target.dataset.emoji);
            } else if (target.dataset.reactOpen) {
                openReactionPicker(target, target.dataset.reactOpen);
            } else if (target.dataset.delete) {
                if (!window.confirm("Delete this message?")) return;
                request("DELETE", "/teams/api/messages/" + target.dataset.delete)
                    .then(function () {
                        if (window.TeamsRealtime) window.TeamsRealtime.poke();
                    });
            } else if (target.dataset.edit) {
                startEdit(shell, target.dataset.edit);
            } else if (target.dataset.reply || target.dataset.thread) {
                openThread(root, target.dataset.reply || target.dataset.thread);
            }
        });

        wireThreadPane(root);

        App.on(document, "click", function (event) {
            if (window.TeamsRealtime) window.TeamsRealtime.active();
            // Dismiss the reaction popover on any click that isn't in it or
            // on the button that opened it.
            if (picker &&
                !event.target.closest(".tm-react-picker") &&
                !event.target.closest("[data-react-open]")) {
                closeReactionPicker();
            }
        });

        App.on(document, "keydown", function (event) {
            if (event.key !== "Escape") return;
            if (picker) { closeReactionPicker(); return; }
            closeThread(root);
        });
    }

    // ---- reactions ------------------------------------------------------

    function react(messageId, emoji) {
        if (!emoji) return;
        closeReactionPicker();
        post("/teams/api/messages/" + messageId + "/react", { emoji: emoji })
            .then(function (data) {
                if (!data || !data.message) return;
                // Replace every copy - the same message can be on screen
                // twice when its thread is open.
                document.querySelectorAll('[data-id="' + messageId + '"]')
                    .forEach(function (node) {
                        node.outerHTML = data.message.html;
                    });
            });
    }

    var picker = null;

    function closeReactionPicker() {
        if (picker) { picker.remove(); picker = null; }
    }

    function openReactionPicker(anchor, messageId) {
        // Second click on the same button closes it.
        if (picker && picker.dataset.for === messageId) {
            closeReactionPicker();
            return;
        }
        closeReactionPicker();

        picker = document.createElement("div");
        picker.className = "tm-react-picker";
        picker.dataset.for = messageId;
        picker.innerHTML = QUICK_REACTIONS.map(function (emoji) {
            return '<button type="button" data-react="' + messageId +
                '" data-emoji="' + emoji + '">' + emoji + "</button>";
        }).join("");

        // Appended to <body>, not to the message: the message list clips
        // its overflow, and a popover inside it would be cut off at the
        // top and bottom rows.
        //
        // That also puts it outside the .tm-shell subtree, so the delegated
        // handler there cannot see these buttons — the picker carries its
        // own listener. It is removed with the element, so nothing leaks.
        picker.addEventListener("click", function (event) {
            var button = event.target.closest("[data-emoji]");
            if (!button) return;
            event.stopPropagation();
            react(button.dataset.react, button.dataset.emoji);
        });

        document.body.appendChild(picker);

        var box = anchor.getBoundingClientRect();
        var width = picker.offsetWidth;
        picker.style.top = Math.max(8, box.top - picker.offsetHeight - 6) + "px";
        picker.style.left =
            Math.max(8, Math.min(box.right - width, window.innerWidth - width - 8)) + "px";
    }

    // ---- attachments ----------------------------------------------------

    /* Files chosen but not yet sent. Kept in a module-level list rather
       than in the <input type="file">, because a file input's FileList is
       read-only - you cannot remove one item from it, and "attached three
       files, changed my mind about the second" is the normal case. */
    var pending = [];

    function wireAttachments(root, composer, input, list, empty, scroller, channelId) {
        var picker = composer.querySelector("[data-tm-file]");
        var button = composer.querySelector("[data-tm-attach]");
        var tray = root.querySelector("[data-tm-pending]");
        var zone = root.querySelector("[data-tm-dropzone]");
        if (!picker || !tray) return;

        function repaint() {
            tray.hidden = pending.length === 0;
            tray.innerHTML = pending.map(function (file, index) {
                return '<span class="tm-pending-file">' +
                    '<i class="fa-solid fa-paperclip"></i>' +
                    '<span>' + escapeText(file.name) + '</span>' +
                    '<button type="button" data-tm-drop="' + index +
                    '" aria-label="Remove">&times;</button></span>';
            }).join("");
        }

        function add(files) {
            var limit = (window.TEAMS_MAX_UPLOAD_MB || 25) * 1024 * 1024;
            var rejected = [];
            [].forEach.call(files, function (file) {
                if (file.size > limit) { rejected.push(file.name); return; }
                pending.push(file);
            });
            if (rejected.length) {
                // Told here rather than after a round trip: the browser
                // already knows the size, and uploading 40 MB to be
                // refused is the worst possible way to find out.
                alertOnce(rejected.length + " file(s) over the size limit were skipped.");
            }
            repaint();
        }

        App.on(button, "click", function () { picker.click(); });
        App.on(picker, "change", function () {
            add(picker.files);
            picker.value = "";        // so re-picking the same file fires again
        });

        App.on(tray, "click", function (event) {
            var remove = event.target.closest("[data-tm-drop]");
            if (!remove) return;
            pending.splice(parseInt(remove.dataset.tmDrop, 10), 1);
            repaint();
        });

        // Paste a screenshot straight into the conversation.
        App.on(input, "paste", function (event) {
            var items = (event.clipboardData || {}).items || [];
            var files = [].filter.call(items, function (i) { return i.kind === "file"; })
                          .map(function (i) { return i.getAsFile(); })
                          .filter(Boolean);
            if (files.length) { event.preventDefault(); add(files); }
        });

        // Drag and drop over the whole conversation. Counted rather than
        // toggled: dragging across a child element fires dragleave on the
        // parent, and a naive toggle makes the overlay flicker.
        if (zone) {
            var depth = 0;
            App.on(root, "dragenter", function (event) {
                if (!hasFiles(event)) return;
                event.preventDefault();
                depth += 1; zone.hidden = false;
            });
            App.on(root, "dragover", function (event) {
                if (hasFiles(event)) event.preventDefault();
            });
            App.on(root, "dragleave", function () {
                depth = Math.max(0, depth - 1);
                if (!depth) zone.hidden = true;
            });
            App.on(root, "drop", function (event) {
                if (!hasFiles(event)) return;
                event.preventDefault();
                depth = 0; zone.hidden = true;
                add(event.dataTransfer.files);
            });
        }

        // Exposed so send() can consume and clear them.
        composer._tmPending = {
            take: function () { var out = pending.slice(); pending = []; repaint(); return out; },
            restore: function (files) { pending = files; repaint(); },
            count: function () { return pending.length; }
        };
    }

    function hasFiles(event) {
        var dt = event.dataTransfer;
        return !!(dt && dt.types && [].indexOf.call(dt.types, "Files") !== -1);
    }

    var alerted = false;
    function alertOnce(message) {
        if (alerted) return;
        alerted = true;
        window.setTimeout(function () { alerted = false; }, 3000);
        window.alert(message);
    }

    function escapeText(value) {
        var div = document.createElement("div");
        div.textContent = String(value);
        return div.innerHTML;
    }

    // ---- threads --------------------------------------------------------

    function pane(root) {
        var shell = root.closest(".tm-shell") || document;
        return shell.querySelector("[data-tm-thread]");
    }

    function openThread(root, rootMessageId) {
        var panel = pane(root);
        if (!panel) return;

        var source = root.querySelector('[data-id="' + rootMessageId + '"]');
        if (!source) return;

        // The root is already on screen and already rendered by
        // teams/_message.html — clone it rather than asking the server for
        // a second copy of something the page is holding.
        var host = panel.querySelector("[data-tm-thread-root]");
        host.innerHTML = source.outerHTML;
        // Its hover actions would reopen the thread from inside the thread.
        var actions = host.querySelector(".tm-actions");
        if (actions) actions.remove();

        panel.querySelector("[data-tm-thread-list]").innerHTML = "";
        panel.querySelector("[data-tm-thread-count]").textContent = "";
        panel.hidden = false;
        panel.dataset.root = rootMessageId;
        document.body.classList.add("tm-thread-open");

        if (window.TeamsRealtime) {
            window.TeamsRealtime.attachThread(parseInt(rootMessageId, 10));
        }

        var input = panel.querySelector("[data-tm-thread-input]");
        if (input) input.focus();
    }

    function closeThread(root) {
        var panel = pane(root);
        if (!panel || panel.hidden) return;
        panel.hidden = true;
        delete panel.dataset.root;
        document.body.classList.remove("tm-thread-open");
        if (window.TeamsRealtime) window.TeamsRealtime.detachThread();
    }

    function wireThreadPane(root) {
        var panel = pane(root);
        if (!panel) return;

        var closeBtn = panel.querySelector("[data-tm-thread-close]");
        if (closeBtn) {
            App.on(closeBtn, "click", function () { closeThread(root); });
        }

        var form = panel.querySelector("[data-tm-thread-composer]");
        var input = panel.querySelector("[data-tm-thread-input]");
        if (!form || !input) return;

        function sendReply() {
            var body = input.value.trim();
            if (!body || !panel.dataset.root) return;
            var parentId = parseInt(panel.dataset.root, 10);
            input.value = "";
            autoGrow(input);

            post(form.action, {
                body: body,
                parent_id: parentId,
                client_msg_id: "c-" + Math.random().toString(36).slice(2, 12)
            }).then(function (data) {
                if (!data || !data.ok) { input.value = body; return; }
                if (window.TeamsRealtime) {
                    window.TeamsRealtime.threadCursor(data.message.id);
                    window.TeamsRealtime.poke();
                }
                appendThread(panel, [data.message]);
            });
        }

        App.on(form, "submit", function (event) {
            event.preventDefault();
            sendReply();
        });

        App.on(input, "keydown", function (event) {
            if (event.key === "Enter" && !event.shiftKey && !mentionPickerOpen()) {
                event.preventDefault();
                sendReply();
            }
        });

        App.on(input, "input", function () { autoGrow(input); });
    }

    function appendThread(panel, entries) {
        var list = panel.querySelector("[data-tm-thread-list]");
        var scroller = panel.querySelector("[data-tm-thread-scroll]");
        var added = 0;

        entries.forEach(function (m) {
            if (list.querySelector('[data-id="' + m.id + '"]')) return;
            list.insertAdjacentHTML("beforeend", m.html);
            added += 1;
        });

        if (added) {
            var count = list.children.length;
            panel.querySelector("[data-tm-thread-count]").textContent =
                count + (count === 1 ? " reply" : " replies");
            scrollToBottom(scroller);
        }
    }

    // ---- helpers --------------------------------------------------------

    /* The @-mention picker also binds Enter, at document level, and a
       textarea listener runs before it. Without this check, choosing a
       name from the picker would post the half-written message instead. */
    function mentionPickerOpen() {
        var box = document.querySelector(".mention-suggest");
        return !!(box && !box.hidden);
    }

    function send(form, input, list, empty, scroller, channelId) {
        var body = input.value.trim();
        var files = form._tmPending ? form._tmPending.take() : [];

        // A file with no caption is a message; a caption with no file is a
        // message; neither is nothing.
        if (!body && !files.length) return;

        // A client id makes the send idempotent: a retry after a timeout
        // resolves to the message already stored instead of posting twice.
        var cid = "c-" + Math.random().toString(36).slice(2, 12);

        input.value = "";
        autoGrow(input);
        if (empty) empty.hidden = true;

        // Optimistic bubble, replaced when the real one arrives.
        list.insertAdjacentHTML("beforeend",
            pendingBubble(cid, body || (files.length + " file(s)…")));
        scrollToBottom(scroller);

        var request = files.length
            ? upload(channelId, body, cid, files)
            : post(form.action, { body: body, client_msg_id: cid });

        request
            .then(function (data) {
                if (!data || !data.ok) throw new Error(data && data.error);
                var node = list.querySelector(
                    '[data-cid="' + cssEscape(cid) + '"][data-pending]');
                if (node && data.message) node.outerHTML = data.message.html;
                if (window.TeamsRealtime) {
                    window.TeamsRealtime.cursor(data.message.id);
                    window.TeamsRealtime.poke();
                }
                scrollToBottom(scroller);
            })
            .catch(function () {
                var node = list.querySelector(
                    '[data-cid="' + cssEscape(cid) + '"][data-pending]');
                if (node) node.classList.add("tm-failed");
                // Put the text AND the files back - they were taken off the
                // tray on the way in, and losing someone's attachments to a
                // dropped request means they have to find them again.
                if (!input.value) input.value = body;
                if (files.length && form._tmPending) form._tmPending.restore(files);
            });
    }

    /* Files go as multipart, not JSON: the browser streams a FormData body,
       so a 25 MB file never has to be base64'd into a string first. */
    function upload(channelId, body, cid, files) {
        var payload = new FormData();
        payload.append("body", body || "");
        payload.append("client_msg_id", cid);
        files.forEach(function (file) { payload.append("files", file); });

        return fetch("/teams/api/channels/" + channelId + "/upload", {
            method: "POST",
            credentials: "same-origin",
            // Content-Type is deliberately unset: the browser has to add
            // its own multipart boundary, and naming the type here would
            // overwrite it with one that has none.
            headers: { "Accept": "application/json" },
            body: payload
        }).then(function (response) { return response.json(); });
    }

    function pendingBubble(cid, body) {
        var div = document.createElement("div");
        div.textContent = body;          // escape via the DOM, never by hand
        return '<div class="tm-msg is-own tm-pending" data-pending="1" data-cid="'
            + cid + '"><div class="tm-avatar"></div><div class="tm-body">'
            + '<p class="tm-text">' + div.innerHTML + "</p></div></div>";
    }

    function startEdit(list, messageId) {
        var node = list.querySelector('[data-id="' + messageId + '"]');
        if (!node) return;
        var text = node.querySelector(".tm-text");
        if (!text || node.querySelector("textarea")) return;

        var original = text.textContent.trim();
        var box = document.createElement("textarea");
        box.className = "tm-edit-box";
        box.value = original;
        text.replaceWith(box);
        box.focus();

        box.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                box.replaceWith(text);
            } else if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                var value = box.value.trim();
                if (!value || value === original) { box.replaceWith(text); return; }
                request("PATCH", "/teams/api/messages/" + messageId, { body: value })
                    .then(function (data) {
                        if (data && data.message) node.outerHTML = data.message.html;
                    });
            }
        });
    }

    function markRead(channelId, upTo) {
        if (!upTo) return;
        post("/teams/api/channels/" + channelId + "/read", { up_to: upTo });
    }

    function paintSidebar(payload, activeChannelId) {
        (payload.channels || []).forEach(function (row) {
            var isActive = row.id === activeChannelId;
            var dot = document.querySelector('[data-teams-unread="' + row.id + '"]');
            if (dot) dot.hidden = !row.unread || isActive;

            var count = document.querySelector('[data-teams-count="' + row.id + '"]');
            if (count) {
                var show = row.unread && !isActive && row.count;
                count.hidden = !show;
                if (show) count.textContent = row.count > 99 ? "99+" : row.count;
            }

            var link = document.querySelector('[data-teams-channel="' + row.id + '"]');
            if (link) link.classList.toggle("has-unread", !!row.unread && !isActive);
        });
    }

    function paintPresence(presence) {
        (presence || []).forEach(function (row) {
            document.querySelectorAll('[data-presence-user="' + row.u + '"]')
                .forEach(function (dot) {
                    // One class carries the state so CSS owns the colour and
                    // there is no palette duplicated in JavaScript.
                    dot.className = "tm-presence is-" + row.s;
                    dot.title = row.s.charAt(0).toUpperCase() + row.s.slice(1);
                });
        });
    }

    function paintTyping(bar, typing) {
        if (!bar) return;
        if (!typing || !typing.length) { bar.hidden = true; return; }
        var names = typing.map(function (t) { return t.n; });
        bar.textContent = names.length === 1
            ? names[0] + " is typing…"
            : names.slice(0, 2).join(" and ") + " are typing…";
        bar.hidden = false;
    }

    function atBottom(scroller) {
        if (!scroller) return true;
        return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight
            < BOTTOM_SLACK_PX;
    }

    function scrollToBottom(scroller) {
        if (scroller) scroller.scrollTop = scroller.scrollHeight;
    }

    function autoGrow(input) {
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 180) + "px";
    }

    function cssEscape(value) {
        return String(value).replace(/["\\]/g, "\\$&");
    }

    // csrf.js wraps fetch and attaches X-CSRFToken to same-origin mutating
    // requests, so nothing here manages the token.
    function request(method, url, body) {
        return fetch(url, {
            method: method,
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            body: body ? JSON.stringify(body) : undefined
        }).then(function (response) { return response.json(); });
    }

    function post(url, body) { return request("POST", url, body); }

    /*
        Deliberately NOT App.ready(). That helper registers turbo:load with
        {once:true}, which is right for the inline body scripts Turbo
        re-executes on every visit — but this file is loaded once from
        <head> and never runs again, so a one-shot listener would wire up
        exactly one navigation and then go quiet.

        It also has to cover the cold load: Turbo dispatches its first
        turbo:load during startup, which can land before this deferred
        script has registered for it. Missing that is how the composer ends
        up inert on the very first page you open. So: a persistent listener
        for subsequent visits, plus a direct call for the current document.
        init() is idempotent per DOM, so the overlap is harmless.
    */
    document.addEventListener("turbo:load", init);

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init, { once: true });
    } else {
        init();
    }
})();
