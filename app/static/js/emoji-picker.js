/*
    Emoji picker for comments, descriptions and captions.

    The Instagram/Facebook shape: a smiley button beside the field, a
    popover with a search box, category tabs and a grid, and the character
    dropped in at the cursor rather than appended at the end.

    Attaching it is one attribute. Any <textarea> or <input> carrying
    data-emoji-field gets a trigger button the first time it is focused - which
    is what makes it work on the reply and edit boxes that task detail
    builds in JavaScript after the page has rendered, without those
    template strings having to know this file exists.

    Everything is local: no font, no sprite sheet, no CDN. The characters
    are the platform's own emoji font, exactly as they render in the
    comment they end up in.

    One picker element is built for the whole document and reused, and all
    the listeners are delegated, so a Turbo navigation neither leaks a
    panel nor needs a re-bind.
*/
(function () {
    "use strict";

    if (window.__emojiPickerBound) return;
    window.__emojiPickerBound = true;

    var RECENT_KEY = "cc.emoji.recent";
    var RECENT_MAX = 24;

    // Curated rather than exhaustive: the ones people actually reach for
    // in work comments, with the keywords they would search by.
    var CATEGORIES = [
        {
            key: "smileys",
            label: "Smileys",
            icon: "😀",
            emoji: [
                ["😀", "grin happy smile"], ["😃", "smile happy joy"],
                ["😄", "smile happy laugh"], ["😁", "beam grin"],
                ["😆", "laugh satisfied"], ["😅", "sweat laugh relief"],
                ["🤣", "rofl rolling laugh"], ["😂", "joy tears laugh"],
                ["🙂", "slight smile"], ["😉", "wink"],
                ["😊", "blush smile happy"], ["😇", "innocent angel"],
                ["🥰", "love hearts adore"], ["😍", "heart eyes love"],
                ["😘", "kiss"], ["😋", "yum tasty"],
                ["😎", "cool sunglasses"], ["🤩", "star struck wow"],
                ["🥳", "party celebrate"], ["🙃", "upside down"],
                ["😌", "relieved calm"], ["😔", "pensive sad"],
                ["😴", "sleep tired"], ["😪", "sleepy"],
                ["😮", "surprised open mouth"], ["😯", "hushed"],
                ["😲", "astonished shock"], ["🥱", "yawn bored"],
                ["😢", "cry sad"], ["😭", "sob crying"],
                ["😤", "triumph huff"], ["😠", "angry"],
                ["😡", "rage mad"], ["🤔", "thinking hmm"],
                ["🤨", "raised eyebrow doubt"], ["😐", "neutral"],
                ["😑", "expressionless"], ["😬", "grimace awkward"],
                ["🙄", "eye roll"], ["😳", "flushed embarrassed"],
                ["🥺", "pleading please"], ["😱", "scream fear"],
                ["😰", "anxious sweat"], ["🤯", "mind blown"],
                ["😷", "mask sick"], ["🤒", "sick fever"],
                ["🤗", "hug"], ["🤭", "oops giggle"],
                ["🤫", "shh quiet"], ["😶", "no mouth speechless"]
            ]
        },
        {
            key: "gestures",
            label: "Gestures",
            icon: "👍",
            emoji: [
                ["👍", "thumbs up yes approve good"],
                ["👎", "thumbs down no"], ["👌", "ok perfect"],
                ["✌️", "peace victory"], ["🤞", "fingers crossed luck"],
                ["🤝", "handshake deal agree"], ["👏", "clap applause bravo"],
                ["🙌", "raised hands celebrate"], ["🙏", "please thanks pray"],
                ["💪", "muscle strong"], ["✍️", "writing"],
                ["👋", "wave hello hi bye"], ["🤙", "call me"],
                ["👇", "down below"], ["👆", "up above"],
                ["👉", "right point"], ["👈", "left point"],
                ["✋", "stop hand"], ["🖐️", "hand"],
                ["🫡", "salute yes sir"], ["🫶", "heart hands love"],
                ["👀", "eyes look watching"], ["🧠", "brain smart"],
                ["👤", "person user"], ["👥", "people team"]
            ]
        },
        {
            key: "hearts",
            label: "Hearts",
            icon: "❤️",
            emoji: [
                ["❤️", "red heart love"], ["🧡", "orange heart"],
                ["💛", "yellow heart"], ["💚", "green heart"],
                ["💙", "blue heart"], ["💜", "purple heart"],
                ["🖤", "black heart"], ["🤍", "white heart"],
                ["🩷", "pink heart"], ["💖", "sparkling heart"],
                ["💗", "growing heart"], ["💓", "beating heart"],
                ["💕", "two hearts"], ["💘", "cupid arrow"],
                ["💝", "heart gift"], ["💔", "broken heart"],
                ["❣️", "heart exclamation"], ["💞", "revolving hearts"]
            ]
        },
        {
            key: "work",
            label: "Work",
            icon: "💼",
            emoji: [
                ["✅", "check done complete tick"],
                ["☑️", "checkbox done"], ["✔️", "check mark"],
                ["❌", "cross no wrong"], ["⚠️", "warning caution"],
                ["🚫", "blocked forbidden"], ["🔴", "red dot urgent"],
                ["🟡", "yellow dot"], ["🟢", "green dot ok"],
                ["🔵", "blue dot"], ["⏰", "alarm deadline time"],
                ["⏳", "hourglass waiting pending"],
                ["📅", "calendar date schedule"], ["🗓️", "calendar"],
                ["📌", "pin important"], ["📍", "location pin"],
                ["📎", "attachment clip"], ["🔗", "link url"],
                ["📝", "note memo write"], ["📄", "document file page"],
                ["📁", "folder"], ["📊", "chart bar stats"],
                ["📈", "growth up trend"], ["📉", "down decline"],
                ["💼", "briefcase work business"], ["🗂️", "files organise"],
                ["🔍", "search find review"], ["🔎", "search"],
                ["💡", "idea lightbulb suggestion"],
                ["🛠️", "tools fix"], ["⚙️", "settings gear"],
                ["🐛", "bug issue"], ["🚀", "launch ship rocket fast"],
                ["🔥", "fire hot great urgent"],
                ["⭐", "star favourite"], ["✨", "sparkles new shiny"],
                ["🎯", "target goal focus"], ["🏆", "trophy win"],
                ["🎉", "party celebrate done"], ["🎊", "confetti"],
                ["💯", "hundred perfect"], ["🙌", "hands celebrate"],
                ["📢", "announce megaphone"], ["📣", "shout promote"],
                ["🔔", "bell reminder notify"], ["💬", "comment speech"],
                ["✉️", "email mail"], ["📞", "call phone"],
                ["💻", "laptop computer dev"], ["🖥️", "desktop screen"],
                ["📱", "mobile phone"], ["🎬", "clapper video film shoot"],
                ["🎥", "camera video"], ["📷", "camera photo"],
                ["🎨", "art design palette"], ["🖌️", "brush design"],
                ["✏️", "pencil edit"], ["🖊️", "pen"],
                ["📦", "package deliver"], ["💰", "money budget"],
                ["🧾", "invoice receipt"], ["⌛", "time up"]
            ]
        },
        {
            key: "social",
            label: "Social",
            icon: "📣",
            emoji: [
                ["👉", "swipe point"], ["👇", "link below"],
                ["🔗", "link in bio"], ["📲", "download app"],
                ["💥", "boom impact"], ["🌟", "glowing star"],
                ["💫", "dizzy sparkle"], ["🆕", "new"],
                ["🆓", "free"], ["🔝", "top"],
                ["🎁", "gift giveaway offer"], ["🏷️", "tag price sale"],
                ["💸", "discount money off"], ["🛒", "cart shop buy"],
                ["🛍️", "shopping bags"], ["📸", "photo camera flash"],
                ["🤳", "selfie"], ["🎤", "mic podcast voice"],
                ["🎧", "headphones audio music"], ["🎵", "music note"],
                ["📺", "tv watch"], ["▶️", "play watch video"],
                ["#️⃣", "hashtag"], ["‼️", "attention"],
                ["❗", "exclamation important"], ["❓", "question"],
                ["💭", "thought"], ["🗣️", "speaking word of mouth"]
            ]
        },
        {
            key: "nature",
            label: "Nature",
            icon: "🌿",
            emoji: [
                ["🌞", "sun sunny"], ["🌙", "moon night"],
                ["⭐", "star"], ["☁️", "cloud"],
                ["🌧️", "rain"], ["⛈️", "storm"],
                ["🌈", "rainbow"], ["❄️", "snow cold winter"],
                ["🌊", "wave water sea"], ["🔥", "fire"],
                ["🌱", "seedling growth new"], ["🌿", "herb plant"],
                ["🍀", "clover luck"], ["🌸", "blossom flower"],
                ["🌺", "hibiscus flower"], ["🌻", "sunflower"],
                ["🌹", "rose flower"], ["💐", "bouquet"],
                ["🌍", "earth world global"], ["🐶", "dog puppy"],
                ["🐱", "cat"], ["🦊", "fox"], ["🐼", "panda"],
                ["🦁", "lion"], ["🐝", "bee busy"], ["🦋", "butterfly"],
                ["🐢", "turtle slow"], ["🐌", "snail slow"]
            ]
        },
        {
            key: "food",
            label: "Food",
            icon: "☕",
            emoji: [
                ["☕", "coffee break"], ["🍵", "tea chai"],
                ["🥤", "drink soda"], ["🧃", "juice"],
                ["🍺", "beer cheers"], ["🥂", "cheers celebrate toast"],
                ["🍾", "champagne celebrate"], ["🍕", "pizza"],
                ["🍔", "burger"], ["🍟", "fries"],
                ["🌮", "taco"], ["🍜", "noodles"],
                ["🍛", "curry rice"], ["🍰", "cake"],
                ["🎂", "birthday cake"], ["🍪", "cookie"],
                ["🍫", "chocolate"], ["🍩", "donut"],
                ["🍎", "apple fruit"], ["🍌", "banana"],
                ["🥗", "salad healthy"], ["🍿", "popcorn watch"]
            ]
        },
        {
            key: "travel",
            label: "Travel",
            icon: "✈️",
            emoji: [
                ["✈️", "plane flight travel"], ["🚗", "car drive"],
                ["🚕", "taxi"], ["🚌", "bus"], ["🚲", "bike cycle"],
                ["🛵", "scooter delivery"], ["🚆", "train"],
                ["🚢", "ship boat"], ["🏠", "home house"],
                ["🏢", "office building work"], ["🏬", "store shop"],
                ["🏝️", "island holiday leave"], ["🏖️", "beach vacation"],
                ["⛺", "camp"], ["🗺️", "map plan"],
                ["🧳", "luggage trip"], ["🌆", "city evening"],
                ["🎡", "fair event"], ["🎪", "circus event"]
            ]
        }
    ];

    var panel = null;
    var searchInput = null;
    var gridEl = null;
    var tabsEl = null;
    var activeField = null;
    var activeCategory = "recent";

    // The panel is built once and appended to <body>. Turbo swaps <body> on
    // navigation, detaching that cached node - so drop the caches before a
    // render and buildPanel() rebuilds against the fresh body. Without this the
    // picker renders into a detached node and nothing appears after any nav.
    ["turbo:before-render", "turbo:before-cache"].forEach(function (evt) {
        document.addEventListener(evt, function () {
            panel = searchInput = gridEl = tabsEl = null;
        });
    });

    // ---- recents -----------------------------------------------------

    function readRecent() {
        try {
            var raw = window.localStorage.getItem(RECENT_KEY);
            var list = raw ? JSON.parse(raw) : [];
            return Array.isArray(list) ? list : [];
        } catch (err) {
            return [];   // private mode, quota, corrupt value - never fatal
        }
    }

    function rememberRecent(char) {
        try {
            var list = readRecent().filter(function (c) { return c !== char; });
            list.unshift(char);
            window.localStorage.setItem(
                RECENT_KEY, JSON.stringify(list.slice(0, RECENT_MAX))
            );
        } catch (err) {
            /* not worth failing an insert over */
        }
    }

    function keywordsFor(char) {
        for (var i = 0; i < CATEGORIES.length; i++) {
            var found = CATEGORIES[i].emoji.filter(function (pair) {
                return pair[0] === char;
            })[0];
            if (found) return found[1];
        }
        return "";
    }

    // ---- panel -------------------------------------------------------

    function buildPanel() {
        if (panel) return panel;

        panel = document.createElement("div");
        panel.className = "emoji-panel";
        panel.hidden = true;
        panel.setAttribute("role", "dialog");
        panel.setAttribute("aria-label", "Choose an emoji");

        panel.innerHTML =
            '<div class="emoji-search">' +
            '<i class="fa-solid fa-magnifying-glass" aria-hidden="true"></i>' +
            '<input type="text" placeholder="Search emoji" aria-label="Search emoji">' +
            "</div>" +
            '<div class="emoji-tabs" role="tablist"></div>' +
            '<div class="emoji-grid"></div>';

        searchInput = panel.querySelector(".emoji-search input");
        tabsEl = panel.querySelector(".emoji-tabs");
        gridEl = panel.querySelector(".emoji-grid");

        var tabs = [{ key: "recent", icon: "🕘", label: "Recent" }].concat(
            CATEGORIES.map(function (c) {
                return { key: c.key, icon: c.icon, label: c.label };
            })
        );

        tabs.forEach(function (tab) {
            var button = document.createElement("button");
            button.type = "button";
            button.className = "emoji-tab";
            button.dataset.category = tab.key;
            button.title = tab.label;
            button.setAttribute("aria-label", tab.label);
            button.textContent = tab.icon;
            tabsEl.appendChild(button);
        });

        tabsEl.addEventListener("click", function (event) {
            var tab = event.target.closest(".emoji-tab");
            if (!tab) return;
            activeCategory = tab.dataset.category;
            searchInput.value = "";
            renderGrid();
        });

        searchInput.addEventListener("input", renderGrid);

        searchInput.addEventListener("keydown", function (event) {
            if (event.key !== "Enter") return;
            event.preventDefault();
            var first = gridEl.querySelector(".emoji-cell");
            if (first) insert(first.textContent);
        });

        // mousedown, not click: the field must not lose its selection
        // before we insert at the cursor.
        gridEl.addEventListener("mousedown", function (event) {
            var cell = event.target.closest(".emoji-cell");
            if (!cell) return;
            event.preventDefault();
            insert(cell.textContent);
        });

        document.body.appendChild(panel);
        return panel;
    }

    function renderGrid() {
        var query = (searchInput.value || "").trim().toLowerCase();
        var cells = [];

        if (query) {
            CATEGORIES.forEach(function (category) {
                category.emoji.forEach(function (pair) {
                    if (pair[1].indexOf(query) !== -1) cells.push(pair[0]);
                });
            });
        } else if (activeCategory === "recent") {
            cells = readRecent();
            if (!cells.length) {
                // An empty first tab reads as "broken", so fall back to
                // the ones most likely to be wanted.
                cells = ["👍", "🙌", "🎉", "🔥", "✅", "❤️", "😀", "🙏"];
            }
        } else {
            CATEGORIES.forEach(function (category) {
                if (category.key !== activeCategory) return;
                cells = category.emoji.map(function (pair) { return pair[0]; });
            });
        }

        [].forEach.call(tabsEl.children, function (tab) {
            tab.classList.toggle(
                "active", !query && tab.dataset.category === activeCategory
            );
        });

        if (!cells.length) {
            gridEl.innerHTML = '<p class="emoji-empty">No emoji found.</p>';
            return;
        }

        gridEl.innerHTML = "";

        cells.forEach(function (char) {
            var cell = document.createElement("button");
            cell.type = "button";
            cell.className = "emoji-cell";
            cell.tabIndex = -1;
            cell.title = keywordsFor(char).split(" ")[0] || char;
            cell.textContent = char;
            gridEl.appendChild(cell);
        });
    }

    function place(trigger) {
        var rect = trigger.getBoundingClientRect();

        panel.hidden = false;              // measure it before positioning
        var height = panel.offsetHeight;
        var width = panel.offsetWidth;

        var top = rect.bottom + 6;
        if (top + height > window.innerHeight - 8) {
            top = Math.max(8, rect.top - height - 6);
        }

        var left = rect.left;
        if (left + width > window.innerWidth - 8) {
            left = Math.max(8, window.innerWidth - width - 8);
        }

        panel.style.top = top + "px";
        panel.style.left = left + "px";
    }

    function open(trigger, field) {
        buildPanel();
        activeField = field;
        activeCategory = "recent";
        searchInput.value = "";
        renderGrid();
        place(trigger);
        panel.dataset.open = "1";
        searchInput.focus();
    }

    function close() {
        if (!panel || panel.hidden) return;
        panel.hidden = true;
        delete panel.dataset.open;
        activeField = null;
    }

    function insert(char) {
        if (!activeField) return;

        var start = activeField.selectionStart;
        var end = activeField.selectionEnd;

        if (typeof start === "number" && activeField.setRangeText) {
            activeField.setRangeText(char, start, end, "end");
        } else {
            // Inputs that do not support selection ranges (rare) still get
            // the character rather than nothing.
            activeField.value = (activeField.value || "") + char;
        }

        // Autosizing textareas, character counters and the composer's live
        // preview all listen for this.
        activeField.dispatchEvent(new Event("input", { bubbles: true }));
        activeField.focus();

        rememberRecent(char);
    }

    // ---- wiring ------------------------------------------------------

    function ensureTrigger(field) {
        if (!field || field.dataset.emojiReady === "1") return;
        field.dataset.emojiReady = "1";

        var trigger = document.createElement("button");
        trigger.type = "button";          // never submits the form it sits in
        trigger.className = "emoji-trigger";
        trigger.title = "Insert emoji";
        trigger.setAttribute("aria-label", "Insert emoji");
        trigger.innerHTML = '<i class="fa-regular fa-face-smile"></i>';

        trigger.addEventListener("click", function (event) {
            event.preventDefault();
            event.stopPropagation();

            if (panel && panel.dataset.open === "1" && activeField === field) {
                close();
                return;
            }
            open(trigger, field);
        });

        field.insertAdjacentElement("afterend", trigger);
    }

    //: Fields only. The composer has its own quick-insert buttons carrying
    //: data-emoji, and they must not grow a trigger of their own.
    var FIELD_SELECTOR =
        "textarea[data-emoji-field], input[data-emoji-field]";

    var scanQueued = false;

    function scan() {
        scanQueued = false;
        var fields = document.querySelectorAll(FIELD_SELECTOR);
        for (var i = 0; i < fields.length; i++) ensureTrigger(fields[i]);
    }

    function queueScan() {
        if (scanQueued) return;
        scanQueued = true;
        window.requestAnimationFrame(scan);
    }

    // Three ways in, because the fields arrive three ways: with the page,
    // after a Turbo navigation, and - for the reply and edit boxes task
    // detail builds - the moment something is clicked. The rAF coalesces
    // that last one down to one pass per frame, and ensureTrigger is a
    // no-op on a field it has already handled.
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", scan);
    } else {
        scan();
    }

    document.addEventListener("turbo:load", scan);
    document.addEventListener("turbo:render", scan);
    document.addEventListener("click", queueScan, true);
    document.addEventListener("focusin", queueScan, true);

    document.addEventListener("click", function (event) {
        if (!panel || panel.hidden) return;
        if (panel.contains(event.target)) return;
        if (event.target.closest(".emoji-trigger")) return;
        close();
    });

    document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") close();
    });

    window.addEventListener("resize", close);
})();
