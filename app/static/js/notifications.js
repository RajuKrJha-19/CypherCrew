/*
    Notification + mentions bells (topbar, permanent shell - see
    base_app.html's comment on why global shell scripts live in head).

    Two independent panels sharing one implementation, parameterised by
    category ("activity" for the bell, "mention" for the @ icon - see
    app.models.notification.Notification.category). Behaviour, by
    design:

      - The badge is a plain unread dot, not a count - it just needs
        to say "something's here", not total your inbox for you.
      - Opening a panel (clicking its button) marks everything in that
        category read server-side immediately - there is no separate
        "mark all read" control.
      - The dot itself stays lit until the panel is CLOSED, so opening
        it to peek doesn't erase the "something changed" signal out
        from under you before you've actually looked - closing is the
        deliberate dismissal.
      - Opening one panel closes the other; clicking anywhere outside
        both closes whichever is open.
*/
(function () {

    const widgets = [];

    function createWidget(config) {

        const btn = document.getElementById(config.btnId);
        const badge = document.getElementById(config.badgeId);
        const panel = document.getElementById(config.panelId);
        const list = document.getElementById(config.listId);

        if (!btn || !badge || !panel || !list) return null;

        let lastSeenId = Number(
            localStorage.getItem(config.lastSeenKey) || 0
        );

        let firstLoadDone = false;

        function escapeHtml(value) {
            if (!value) return "";

            return String(value)
                .replaceAll("&", "&amp;")
                .replaceAll("<", "&lt;")
                .replaceAll(">", "&gt;")
                .replaceAll('"', "&quot;")
                .replaceAll("'", "&#039;");
        }

        // "2h ago" reads instantly where an absolute timestamp needs a
        // moment of arithmetic - the standard notification-feed
        // convention (Slack, GitHub, Linear). Falls back to the
        // preformatted absolute string once it's more than a week old,
        // where a date is more useful than "7d ago".
        function timeAgo(isoString, fallback) {
            if (!isoString) return fallback || "";

            const then = new Date(isoString);

            if (isNaN(then.getTime())) return fallback || "";

            const seconds = Math.round((Date.now() - then.getTime()) / 1000);

            if (seconds < 30) return "Just now";
            if (seconds < 60) return seconds + "s ago";

            const minutes = Math.round(seconds / 60);
            if (minutes < 60) return minutes + "m ago";

            const hours = Math.round(minutes / 60);
            if (hours < 24) return hours + "h ago";

            const days = Math.round(hours / 24);
            if (days < 7) return days === 1 ? "Yesterday" : days + "d ago";

            return fallback || (days + "d ago");
        }

        function render(items) {
            if (!items.length) {
                list.innerHTML = `
                    <div class="notification-empty">
                        <i class="${config.emptyIcon}"></i>
                        <strong>${config.emptyTitle}</strong>
                        <span>${config.emptySubtitle}</span>
                    </div>
                `;
                return;
            }

            list.innerHTML = items.map(function (item) {
                const unreadClass = item.is_read ? "" : " unread";
                const link = item.link || "#";
                const timeLabel = timeAgo(item.created_at_iso, item.created_at);

                return `
                    <a
                        class="notification-item${unreadClass}"
                        href="${escapeHtml(link)}"
                        data-notification-id="${item.id}"
                    >
                        ${item.is_read ? "" : '<span class="notification-dot-marker" aria-hidden="true"></span>'}
                        <strong>${escapeHtml(item.title)}</strong>
                        <span>${escapeHtml(item.message || "")}</span>
                        <small>${escapeHtml(timeLabel)}</small>
                    </a>
                `;
            }).join("");
        }

        async function fetchItems(checkSound) {
            try {
                const response = await fetch(
                    window.CYPHER_NOTIFICATION_API +
                        "?limit=10&category=" + config.category,
                    { cache: "no-store" }
                );

                const data = await response.json();

                const count = data.unread_count || 0;

                // The panel is open -> already marked read server-side
                // (see the button handler), so the dot has nothing to
                // show regardless of what the poll says. Otherwise it
                // simply reflects whether anything unread exists.
                badge.hidden = panel.classList.contains("show") || count === 0;

                render(data.notifications || []);

                const latestId =
                    data.notifications && data.notifications.length
                        ? Number(data.notifications[0].id)
                        : 0;

                if (!firstLoadDone) {
                    firstLoadDone = true;

                    if (latestId > lastSeenId) {
                        lastSeenId = latestId;
                        localStorage.setItem(config.lastSeenKey, String(lastSeenId));
                    }

                    return;
                }

                if (checkSound && latestId > lastSeenId) {
                    playNotificationSound();

                    lastSeenId = latestId;
                    localStorage.setItem(config.lastSeenKey, String(lastSeenId));
                }

            } catch (error) {
                console.log(config.category + " fetch error:", error);
            }
        }

        function open() {
            panel.classList.add("show");
            btn.setAttribute("aria-expanded", "true");

            fetchItems(false);

            // Marking read is the point of opening - see the file
            // banner. Fire-and-forget: the dot is already hidden
            // below regardless of when this resolves.
            fetch(
                window.CYPHER_NOTIFICATION_MARK_READ +
                    "?category=" + config.category,
                { method: "POST" }
            ).catch(function () {});
        }

        function close() {
            if (!panel.classList.contains("show")) return;

            panel.classList.remove("show");
            btn.setAttribute("aria-expanded", "false");
            badge.hidden = true;
        }

        function isOpen() {
            return panel.classList.contains("show");
        }

        const widget = { close, open, isOpen, fetchItems };

        btn.addEventListener("click", function (event) {
            event.stopPropagation();

            const opening = !isOpen();

            widgets.forEach(function (w) {
                if (w && w !== widget) w.close();
            });

            if (opening) open();
            else close();
        });

        panel.addEventListener("click", function (event) {
            event.stopPropagation();
        });

        // Clicking an unread notification marks just that one as read in
        // the background - doesn't block the navigation the link already
        // triggers, so opening an item behaves the way every other link
        // on the page does.
        list.addEventListener("click", function (event) {
            const item = event.target.closest(".notification-item.unread");

            if (!item) return;

            const id = item.dataset.notificationId;

            if (!id) return;

            fetch("/notifications/" + id + "/mark-read", {
                method: "POST"
            }).catch(function (error) {
                console.log("Mark-one-read failed:", error);
            });
        });

        fetchItems(false);

        return widget;
    }

    let soundAllowed = false;
    const sound = document.getElementById("notificationSound");

    function unlockSound() {
        soundAllowed = true;

        if (sound) {
            sound.play()
                .then(function () {
                    sound.pause();
                    sound.currentTime = 0;
                })
                .catch(function () {});
        }

        document.removeEventListener("click", unlockSound);
        document.removeEventListener("keydown", unlockSound);
    }

    document.addEventListener("click", unlockSound);
    document.addEventListener("keydown", unlockSound);

    function playNotificationSound() {
        if (!soundAllowed || !sound) return;

        sound.pause();
        sound.currentTime = 0;

        sound.play().catch(function (error) {
            console.log("Notification audio blocked:", error);
        });
    }

    const notificationWidget = createWidget({
        btnId: "notificationBtn",
        badgeId: "notificationBadge",
        panelId: "notificationPanel",
        listId: "notificationList",
        category: "activity",
        lastSeenKey: "cypher_last_notification_id",
        emptyIcon: "fa-regular fa-bell-slash",
        emptyTitle: "You're all caught up",
        emptySubtitle: "New activity on your tasks will show up here.",
    });

    const mentionsWidget = createWidget({
        btnId: "mentionsBtn",
        badgeId: "mentionsBadge",
        panelId: "mentionsPanel",
        listId: "mentionsList",
        category: "mention",
        lastSeenKey: "cypher_last_mention_id",
        emptyIcon: "fa-solid fa-at",
        emptyTitle: "No mentions yet",
        emptySubtitle: "Tag someone with @Full Name in a comment or a task description and they'll show up here.",
    });

    if (notificationWidget) widgets.push(notificationWidget);
    if (mentionsWidget) widgets.push(mentionsWidget);

    if (!widgets.length) return;

    document.addEventListener("click", function () {
        widgets.forEach(function (w) { w.close(); });
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
            widgets.forEach(function (w) { w.close(); });
        }
    });

    // Existing call sites (e.g. tasks/detail.html after posting a
    // comment) force an immediate resync rather than waiting for the
    // poll - refresh every panel, since a comment can both trigger
    // regular activity and mention someone.
    window.fetchNotifications = function (checkSound) {
        widgets.forEach(function (w) { w.fetchItems(checkSound); });
    };

    widgets.forEach(function (w) {
        setInterval(function () { w.fetchItems(true); }, 5000);
    });

})();
