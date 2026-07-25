/*
    Bulk edit - multi-select on the tasks List view.

    Tick rows, then reassign / re-prioritise / re-deadline the whole
    selection in one gesture instead of opening each task. The value picker
    reuses the inline quick-edit popover styles (.qe-pop / .qe-opt / ...);
    the change is applied by POST /tasks/bulk-update, which enforces the
    same per-task rules server-side. Managers only - the checkboxes and bar
    are only rendered when the user can manage tasks.
*/
(function () {

    const PRIORITIES = ["Low", "Medium", "High", "Urgent"];
    let assignees = null;
    let pop = null;

    function selectedIds() {
        return Array.prototype.slice
            .call(document.querySelectorAll(".tasks-row-select:checked"))
            .map(function (cb) { return cb.value; });
    }

    function toast(msg, type) {
        if (typeof window.showToast === "function") {
            window.showToast(msg, type || "success");
        }
    }

    function updateBar() {
        const bar = document.getElementById("bulkBar");
        if (!bar) return;

        const ids = selectedIds();
        const countEl = document.getElementById("bulkCount");
        if (countEl) countEl.textContent = ids.length;
        bar.hidden = ids.length === 0;

        const all = document.querySelectorAll(".tasks-row-select");
        const selectAll = document.querySelector(".tasks-select-all");
        if (selectAll) {
            selectAll.checked = all.length > 0 && ids.length === all.length;
            selectAll.indeterminate = ids.length > 0 && ids.length < all.length;
        }
    }

    function closePop() {
        if (pop) { pop.remove(); pop = null; }
    }

    function shell(anchor) {
        closePop();
        const el = document.createElement("div");
        el.className = "qe-pop";
        document.body.appendChild(el);
        const r = anchor.getBoundingClientRect();
        el.style.position = "fixed";
        el.style.left = Math.max(8, Math.min(r.left, window.innerWidth - 240)) + "px";
        // The bar sits at the bottom, so open the picker ABOVE the button.
        el.style.top = Math.max(8, r.top - 8 - 210) + "px";
        pop = el;
        return el;
    }

    async function apply(field, value) {
        const ids = selectedIds();
        if (!ids.length) { closePop(); return; }

        let data;
        try {
            const res = await fetch("/tasks/bulk-update", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ task_ids: ids, field: field, value: value }),
            });
            data = await res.json();
        } catch (e) {
            data = { success: false, message: "Network error." };
        }

        closePop();

        if (data.success) {
            toast(data.message || "Updated.");
            // Reload so every changed row shows its new value.
            setTimeout(function () { window.location.reload(); }, 650);
        } else {
            toast(data.message || "Could not update.", "error");
        }
    }

    function pickPriority(anchor) {
        const el = shell(anchor);
        PRIORITIES.forEach(function (p) {
            const b = document.createElement("button");
            b.type = "button";
            b.className = "qe-opt";
            b.innerHTML = '<span class="priority-dot ' + p.toLowerCase() + '">' + p + "</span>";
            b.addEventListener("click", function () { apply("priority", p); });
            el.appendChild(b);
        });
    }

    function pickDeadline(anchor) {
        const el = shell(anchor);
        el.innerHTML =
            '<input type="datetime-local" class="qe-date">' +
            '<div class="qe-actions">' +
            '<button type="button" class="qe-btn qe-clear">Clear</button>' +
            '<button type="button" class="qe-btn qe-primary qe-save">Set</button>' +
            "</div>";
        const input = el.querySelector(".qe-date");
        setTimeout(function () { input.focus(); }, 0);
        el.querySelector(".qe-save").addEventListener("click", function () { apply("deadline", input.value); });
        el.querySelector(".qe-clear").addEventListener("click", function () { apply("deadline", ""); });
    }

    async function pickAssignee(anchor) {
        const el = shell(anchor);
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
        if (!pop) return;

        function render(term) {
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
                b.className = "qe-opt";
                b.textContent = u.name;
                b.addEventListener("click", function () { apply("assignee", u.id); });
                list.appendChild(b);
            });
        }
        render("");
        search.addEventListener("input", function () { render(search.value); });
    }

    // --- wiring -----------------------------------------------------------

    document.addEventListener("change", function (e) {
        const t = e.target;
        if (t.classList && t.classList.contains("tasks-row-select")) {
            updateBar();
        } else if (t.classList && t.classList.contains("tasks-select-all")) {
            const checked = t.checked;
            document.querySelectorAll(".tasks-row-select").forEach(function (cb) {
                cb.checked = checked;
            });
            updateBar();
        }
    });

    document.addEventListener("click", function (e) {
        const btn = e.target.closest(".bulk-btn");
        if (btn) {
            e.preventDefault();
            const field = btn.dataset.bulkField;
            if (field === "priority") pickPriority(btn);
            else if (field === "deadline") pickDeadline(btn);
            else if (field === "assignee") pickAssignee(btn);
            return;
        }
        if (e.target.closest("#bulkClear")) {
            document.querySelectorAll(".tasks-row-select, .tasks-select-all").forEach(function (cb) {
                cb.checked = false;
                cb.indeterminate = false;
            });
            updateBar();
            closePop();
            return;
        }
        if (pop && !e.target.closest(".qe-pop") && !e.target.closest(".bulk-btn")) {
            closePop();
        }
    });

    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") closePop();
    });

    document.addEventListener("turbo:before-visit", closePop);
})();
