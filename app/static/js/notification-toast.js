/*
    Notification popups - the thing that arrives when a notification does.

    The bell already told you something happened, but only if you were
    looking at it: a dot on a 60px topbar is easy to miss while you are
    reading a task. This is the other half - the notification itself,
    briefly, in the corner, then gone.

    Deliberately NOT the same component as showToast() in toast.js. That
    one is form feedback: one line of text, an outcome, red or green. A
    notification is content - who, what, when, and somewhere to go - so it
    is a card you can click, not a sentence you dismiss.

    Behaviour worth knowing:

      - Top-right, under the topbar, sliding in from the bell it came
        from. The activity/mention panels open in that same corner, so a
        popup is suppressed entirely while either is open: you are already
        reading the list, and a card sliding over it would be in the way.
      - Three at most. A burst (a bulk reassign, say) collapses into the
        newest three rather than a column down the whole screen.
      - The dismiss timer pauses on hover and while the tab is in the
        background, so a popup that arrived while you were elsewhere is
        still there when you come back rather than having timed out to
        nobody.
      - The card is a link. Clicking it goes where the notification points
        and marks it read, exactly like clicking the row in the panel.

    Exposes window.showNotificationToast(item) where item is one entry
    from the /notifications/api payload.
*/
(function () {

    // Loaded from the topbar partial, which is data-turbo-permanent within a
    // shell but re-injected when crossing the ERP<->Studio boundary - the
    // same re-run notifications.js guards against. Nothing here holds state
    // outside the DOM, so the first instance simply stays: re-running would
    // only stack a second Escape listener and dismiss every card twice.
    if (typeof window.showNotificationToast === "function") return;

    var MAX_VISIBLE = 3;
    var DISMISS_AFTER = 6500;
    var STACK_ID = "notificationToastStack";

    // Per category: the icon, and the accent the card is tinted with. A
    // mention is addressed to you personally and reads differently from
    // general activity, which is the whole reason they are separate bells.
    var LOOKS = {
        mention: {
            icon: "fa-solid fa-at",
            className: "is-mention",
            label: "Mention"
        },
        activity: {
            icon: "fa-solid fa-bell",
            className: "is-activity",
            label: "Notification"
        }
    };

    function look(category) {
        return LOOKS[category] || LOOKS.activity;
    }

    function stack() {
        var el = document.getElementById(STACK_ID);

        if (!el) {
            el = document.createElement("div");
            el.id = STACK_ID;
            el.className = "ntoast-stack";
            // The cards announce themselves; the container must not also
            // be a live region or every arrival is read out twice.
            el.setAttribute("aria-live", "polite");
            el.setAttribute("aria-relevant", "additions");
            document.body.appendChild(el);
        }

        return el;
    }

    // "Just now" for the first half-minute, then minutes. A popup is never
    // old enough to need anything beyond that - it appeared as it arrived.
    function freshness(iso) {
        if (!iso) return "Just now";

        var then = new Date(iso);
        if (isNaN(then.getTime())) return "Just now";

        var seconds = Math.round((Date.now() - then.getTime()) / 1000);

        if (seconds < 45) return "Just now";
        if (seconds < 3600) return Math.round(seconds / 60) + "m ago";

        return Math.round(seconds / 3600) + "h ago";
    }

    function trim() {
        var cards = stack().querySelectorAll(".ntoast:not(.is-leaving)");

        for (var i = 0; i < cards.length - MAX_VISIBLE; i++) {
            var dismiss = cards[i].__ntoastDismiss;
            if (dismiss) dismiss();
        }
    }

    function showNotificationToast(item) {
        if (!item) return;

        var style = look(item.category);
        var container = stack();

        var card = document.createElement("a");
        card.className = "ntoast " + style.className;
        card.href = item.link || "#";
        card.setAttribute("role", "status");

        // Built with the DOM rather than innerHTML: title and message are
        // user-authored (task names, comment text) and textContent cannot
        // be talked into being markup.
        var icon = document.createElement("span");
        icon.className = "ntoast-icon";
        icon.setAttribute("aria-hidden", "true");

        if (item.actor_initials) {
            icon.classList.add("is-avatar");
            icon.textContent = item.actor_initials;
        } else {
            var glyph = document.createElement("i");
            glyph.className = style.icon;
            icon.appendChild(glyph);
        }

        card.appendChild(icon);

        var body = document.createElement("span");
        body.className = "ntoast-body";

        var kicker = document.createElement("span");
        kicker.className = "ntoast-kicker";

        var kickerLabel = document.createElement("span");
        kickerLabel.className = "ntoast-label";

        // A person's name is not a micro-label: the uppercase treatment that
        // suits "MENTION" turns "Raju Kr Jha" into shouting, and initialisms
        // in a name become unreadable. Only the category label gets it.
        if (item.actor_name) {
            kickerLabel.classList.add("is-person");
            kickerLabel.textContent = item.actor_name;
        } else {
            kickerLabel.textContent = style.label;
        }

        kicker.appendChild(kickerLabel);

        var time = document.createElement("span");
        time.className = "ntoast-time";
        time.textContent = freshness(item.created_at_iso);
        kicker.appendChild(time);

        body.appendChild(kicker);

        var title = document.createElement("strong");
        title.className = "ntoast-title";
        title.textContent = item.title || "New notification";
        body.appendChild(title);

        if (item.message) {
            var message = document.createElement("span");
            message.className = "ntoast-message";
            message.textContent = item.message;
            body.appendChild(message);
        }

        card.appendChild(body);

        var close = document.createElement("button");
        close.type = "button";
        close.className = "ntoast-close";
        close.setAttribute("aria-label", "Dismiss notification");
        close.innerHTML = "&times;";
        card.appendChild(close);

        var meter = document.createElement("span");
        meter.className = "ntoast-meter";
        meter.setAttribute("aria-hidden", "true");
        card.appendChild(meter);

        // --- Lifetime ------------------------------------------------------
        //
        // Time remaining is tracked by hand rather than left to a single
        // setTimeout, so hovering (or switching tabs) can genuinely pause
        // it instead of merely restarting the countdown afterwards.

        var remaining = DISMISS_AFTER;
        var startedAt = null;
        var timer = null;
        var gone = false;

        function paintMeter(fraction) {
            meter.style.transform = "scaleX(" + fraction + ")";
        }

        function pause() {
            if (gone || timer === null) return;

            clearTimeout(timer);
            timer = null;
            remaining -= Date.now() - startedAt;

            if (remaining < 0) remaining = 0;

            meter.style.transition = "none";
            paintMeter(remaining / DISMISS_AFTER);
        }

        function resume() {
            if (gone || timer !== null || document.hidden) return;

            startedAt = Date.now();
            timer = setTimeout(dismiss, remaining);

            meter.style.transition = "transform " + remaining + "ms linear";
            requestAnimationFrame(function () { paintMeter(0); });
        }

        function dismiss() {
            if (gone) return;
            gone = true;

            if (timer !== null) clearTimeout(timer);

            document.removeEventListener("visibilitychange", onVisibility);

            card.classList.add("is-leaving");
            card.classList.remove("is-shown");

            var removed = false;
            function drop() {
                if (removed) return;
                removed = true;
                card.remove();
            }

            card.addEventListener("transitionend", drop, { once: true });
            // A card in a background tab gets no transitionend (the browser
            // may never run the animation), so it would otherwise linger
            // forever. Belt and braces.
            setTimeout(drop, 400);
        }

        function onVisibility() {
            if (document.hidden) pause();
            else resume();
        }

        card.__ntoastDismiss = dismiss;

        close.addEventListener("click", function (event) {
            // The card is a link; dismissing it must not follow that link.
            event.preventDefault();
            event.stopPropagation();
            dismiss();
        });

        card.addEventListener("mouseenter", pause);
        card.addEventListener("mouseleave", resume);
        card.addEventListener("focusin", pause);
        card.addEventListener("focusout", resume);

        card.addEventListener("click", function () {
            // Same background mark-read as clicking the row in the panel -
            // fire-and-forget, so it never delays the navigation.
            if (item.id) {
                fetch("/notifications/" + item.id + "/mark-read", {
                    method: "POST"
                }).catch(function () {});
            }
            dismiss();
        });

        document.addEventListener("visibilitychange", onVisibility);

        container.appendChild(card);
        trim();

        paintMeter(1);

        requestAnimationFrame(function () {
            card.classList.add("is-shown");
            resume();
        });

        return dismiss;
    }

    // Escape closes the lot, matching the panels and every other dismissible
    // surface in the shell.
    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") return;

        var open = document.getElementById(STACK_ID);
        if (!open) return;

        open.querySelectorAll(".ntoast:not(.is-leaving)").forEach(function (card) {
            if (card.__ntoastDismiss) card.__ntoastDismiss();
        });
    });

    window.showNotificationToast = showNotificationToast;

})();
