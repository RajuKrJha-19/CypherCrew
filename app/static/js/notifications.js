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
                <p class="notification-empty">
                    No notifications
                </p>
            `;
            return;
        }

        list.innerHTML = items.map(function (item) {
            const unreadClass = item.is_read ? "" : " unread";
            const link = item.link || "#";

            return `
                <a class="notification-item${unreadClass}" href="${escapeHtml(link)}">
                    <strong>${escapeHtml(item.title)}</strong>
                    <span>${escapeHtml(item.message || "")}</span>
                    <small>${escapeHtml(item.created_at)}</small>
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