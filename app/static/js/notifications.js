(function () {
    const btn = document.getElementById("notificationBtn");
    const badge = document.getElementById("notificationBadge");
    const panel = document.getElementById("notificationPanel");
    const list = document.getElementById("notificationList");
    const markReadBtn = document.getElementById("markNotificationsRead");
    const sound = document.getElementById("notificationSound");

    if (!btn || !badge || !panel || !list) return;

    let lastSeenId = Number(
        localStorage.getItem("cypher_last_notification_id") || 0
    );

    let soundAllowed = false;
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

    function render(items) {
        if (!items.length) {
            list.innerHTML = `
                <div class="notification-empty">
                    <i class="fa-regular fa-bell-slash"></i>
                    <strong>You're all caught up</strong>
                    <span>New activity on your tasks will show up here.</span>
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
                    ${item.is_read ? "" : '<span class="notification-dot" aria-hidden="true"></span>'}
                    <strong>${escapeHtml(item.title)}</strong>
                    <span>${escapeHtml(item.message || "")}</span>
                    <small>${escapeHtml(timeLabel)}</small>
                </a>
            `;
        }).join("");
    }

    async function fetchNotifications(checkSound) {
        try {
            const response = await fetch(
                window.CYPHER_NOTIFICATION_API + "?limit=10",
                {
                    cache: "no-store"
                }
            );

            const data = await response.json();

            const count = data.unread_count || 0;

            badge.textContent = count;
            badge.style.display = count > 0 ? "flex" : "none";

            if (markReadBtn) {
                markReadBtn.disabled = count === 0;
            }

            render(data.notifications || []);

            const latestId =
                data.notifications && data.notifications.length
                    ? Number(data.notifications[0].id)
                    : 0;

            if (!firstLoadDone) {
                firstLoadDone = true;

                if (latestId > lastSeenId) {
                    lastSeenId = latestId;
                    localStorage.setItem(
                        "cypher_last_notification_id",
                        String(lastSeenId)
                    );
                }

                return;
            }

            if (checkSound && latestId > lastSeenId) {
                playNotificationSound();

                lastSeenId = latestId;

                localStorage.setItem(
                    "cypher_last_notification_id",
                    String(lastSeenId)
                );
            }

        } catch (error) {
            console.log("Notification fetch error:", error);
        }
    }
    window.fetchNotifications = fetchNotifications;

    btn.addEventListener("click", function (event) {
        event.stopPropagation();

        panel.classList.toggle("show");

        fetchNotifications(false);
    });

    panel.addEventListener("click", function (event) {
        event.stopPropagation();
    });

    // Clicking an unread notification marks just that one as read in
    // the background - doesn't block the navigation the link already
    // triggers, so opening a notification behaves the way every other
    // link on the page does.
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

    document.addEventListener("click", function () {
        panel.classList.remove("show");
    });

    if (markReadBtn) {
        markReadBtn.addEventListener("click", async function () {
            await fetch(
                window.CYPHER_NOTIFICATION_MARK_READ,
                {
                    method: "POST"
                }
            );

            fetchNotifications(false);
        });
    }

    fetchNotifications(false);

    setInterval(function () {
        fetchNotifications(true);
    }, 5000);
})();