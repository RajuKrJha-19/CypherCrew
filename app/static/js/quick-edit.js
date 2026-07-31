/*
    Inline quick-edit.

    Any element marked data-qe-field="priority|assignee|deadline" (plus
    data-qe-task and data-qe-value) becomes a click target that opens a
    small popover and PATCHes that one field via /tasks/<id>/quick-update -
    no full edit form, no page reload. The tile updates in place.

    This is the daily-speed win for owners/reviewers: reassigning a task or
    nudging a deadline is one click from the board or list, not a 4-click
    round-trip through the edit page.
*/
(function () {

    // Loaded as a per-page body script, but every listener below is delegated
    // on document/window, so it must bind exactly ONCE. Turbo re-executes body
    // scripts on each navigation; without this guard the handlers stack and a
    // single click fires N times -> N popovers and duplicate quick-update POSTs.
    if (window.__qeInit) return;
    window.__qeInit = true;

    const PRIORITIES = ["Low", "Medium", "High", "Urgent"];
    const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

    let pop = null;        // the open popover element
    let anchor = null;     // the trigger being edited
    let assignees = null;  // cached {id,name} list

    function toast(msg, type) {
        if (typeof window.showToast === "function") {
            window.showToast(msg, type || "success");
        }
    }

    function close() {
        if (pop) { pop.remove(); pop = null; }
        anchor = null;
    }

    function place(el) {
        const r = anchor.getBoundingClientRect();
        const w = 220;
        el.style.position = "fixed";
        el.style.left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8)) + "px";
        // Prefer below; flip above if it would spill off the bottom.
        const below = r.bottom + 6;
        el.style.top = (below + 220 > window.innerHeight ? Math.max(8, r.top - 6 - 200) : below) + "px";
    }

    function shell() {
        // Drop any stale popover but keep `anchor` - the caller has just
        // set it and still needs it to read the current value.
        if (pop) { pop.remove(); pop = null; }
        const el = document.createElement("div");
        el.className = "qe-pop";
        document.body.appendChild(el);
        pop = el;
        place(el);
        return el;
    }

    async function save(field, taskId, value) {
        try {
            const res = await fetch("/tasks/" + taskId + "/quick-update", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ field: field, value: value })
            });
            return await res.json();
        } catch (e) {
            return { success: false, message: "Network error." };
        }
    }

    function fmtDeadline(value, mode) {
        if (!value) return mode === "full" ? "-" : "No deadline";
        const d = new Date(value);
        if (isNaN(d.getTime())) return value;
        const day = String(d.getDate()).padStart(2, "0");
        const mon = MONTHS[d.getMonth()];
        if (mode === "full") {
            let h = d.getHours();
            const m = String(d.getMinutes()).padStart(2, "0");
            const ap = h >= 12 ? "PM" : "AM";
            h = h % 12 || 12;
            return day + " " + mon + " " + d.getFullYear() + ", " +
                   String(h).padStart(2, "0") + ":" + m + " " + ap;
        }
        return day + " " + mon;
    }

    // --- editors -----------------------------------------------------------

    function editPriority() {
        const el = shell();
        const cur = anchor.dataset.qeValue;
        PRIORITIES.forEach(function (p) {
            const b = document.createElement("button");
            b.type = "button";
            b.className = "qe-opt" + (p === cur ? " active" : "");
            b.innerHTML = '<span class="priority-dot ' + p.toLowerCase() + '">' + p + "</span>";
            b.addEventListener("click", async function () {
                const r = await save("priority", anchor.dataset.qeTask, p);
                if (r.success) {
                    anchor.textContent = p;
                    anchor.classList.remove("low", "medium", "high", "urgent");
                    anchor.classList.add(p.toLowerCase());
                    anchor.dataset.qeValue = p;
                    toast("Priority set to " + p);
                } else {
                    toast(r.message || "Could not update.", "error");
                }
                close();
            });
            el.appendChild(b);
        });
    }

    function editDeadline() {
        const el = shell();
        el.innerHTML =
            '<input type="datetime-local" class="qe-date" value="' +
            (anchor.dataset.qeValue || "") + '">' +
            '<div class="qe-actions">' +
            '<button type="button" class="qe-btn qe-clear">Clear</button>' +
            '<button type="button" class="qe-btn qe-primary qe-save">Save</button>' +
            "</div>";
        const input = el.querySelector(".qe-date");
        setTimeout(function () { input.focus(); }, 0);

        async function commit(value) {
            const r = await save("deadline", anchor.dataset.qeTask, value);
            if (r.success) {
                anchor.textContent = fmtDeadline(value, anchor.dataset.qeFmt || "short");
                anchor.dataset.qeValue = value;
                toast(value ? "Deadline updated" : "Deadline cleared");
            } else {
                toast(r.message || "Could not update.", "error");
            }
            close();
        }

        el.querySelector(".qe-save").addEventListener("click", function () { commit(input.value); });
        el.querySelector(".qe-clear").addEventListener("click", function () { commit(""); });
        input.addEventListener("keydown", function (e) {
            if (e.key === "Enter") { e.preventDefault(); commit(input.value); }
        });
    }

    async function editAssignee() {
        const el = shell();
        el.innerHTML =
            '<input type="text" class="qe-search" placeholder="Search people...">' +
            '<div class="qe-list">Loading...</div>';
        const search = el.querySelector(".qe-search");
        const list = el.querySelector(".qe-list");
        setTimeout(function () { search.focus(); }, 0);

        if (!assignees) {
            try {
                const r = await fetch("/tasks/assignees");
                assignees = (await r.json()).users || [];
            } catch (e) {
                assignees = [];
            }
        }
        if (!pop) return; // closed while loading

        function render(term) {
            const cur = anchor.dataset.qeValue;
            const t = (term || "").toLowerCase();
            list.innerHTML = "";
            const matches = assignees.filter(function (u) {
                return u.name.toLowerCase().indexOf(t) !== -1;
            });
            if (!matches.length) {
                list.innerHTML = '<div class="qe-empty">No matches</div>';
                return;
            }
            matches.forEach(function (u) {
                const b = document.createElement("button");
                b.type = "button";
                b.className = "qe-opt" + (String(u.id) === String(cur) ? " active" : "");
                b.textContent = u.name;
                b.addEventListener("click", async function () {
                    const r = await save("assignee", anchor.dataset.qeTask, u.id);
                    if (r.success) {
                        anchor.textContent = u.name;
                        anchor.dataset.qeValue = u.id;
                        toast("Reassigned to " + u.name);
                    } else {
                        toast(r.message || "Could not reassign.", "error");
                    }
                    close();
                });
                list.appendChild(b);
            });
        }

        render("");
        search.addEventListener("input", function () { render(search.value); });
    }

    // --- wiring ------------------------------------------------------------

    // Capture phase so we can stop the click before the card's own handler
    // (which would open the task drawer) sees it. We only stop propagation
    // for an actual trigger; clicks inside the popover fall through to their
    // own listeners untouched.
    document.addEventListener("click", function (e) {

        const trigger = e.target.closest("[data-qe-field]");

        if (trigger) {
            e.preventDefault();
            e.stopPropagation();
            if (anchor === trigger) { close(); return; }  // toggle off
            anchor = trigger;
            const field = trigger.dataset.qeField;
            if (field === "priority") editPriority();
            else if (field === "deadline") editDeadline();
            else if (field === "assignee") editAssignee();
            return;
        }

        if (pop && !e.target.closest(".qe-pop")) close();

    }, true);

    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && pop) { e.stopPropagation(); close(); }
    }, true);

    window.addEventListener("resize", close);
    // Capture-phase scroll, but ignore scrolls INSIDE the popover itself - the
    // assignee list has its own overflow-y:auto, and closing on its first
    // scroll tick made that list impossible to scroll.
    window.addEventListener("scroll", function (e) {
        if (pop && !(e.target && e.target.closest
                     && e.target.closest(".qe-pop"))) {
            close();
        }
    }, true);
    document.addEventListener("turbo:before-visit", close);
})();
