/* Instagram grid planner — drag upcoming posts to reorder the feed.
 *
 * Only cells marked .is-movable (scheduled / not-yet-published) are draggable;
 * published cells are fixed anchors. On drop we send the new order of the
 * movable cells to the server, which swaps their scheduled times to match.
 * A drag never fires a click, so clicking a cell still opens the post.
 */
(function () {
    "use strict";
    if (window.__gridPlannerInit) return;
    window.__gridPlannerInit = true;

    var grid = document.getElementById("igGrid");
    if (!grid || !window.GRID_REORDER_URL) return;

    function toast(msg, type) {
        if (typeof window.showToast === "function") window.showToast(msg, type);
    }

    grid.addEventListener("dragstart", function (e) {
        var cell = e.target.closest(".ig-cell.is-movable");
        if (!cell) { e.preventDefault(); return; }        // published = not draggable
        cell.classList.add("dragging");
        try {
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("text/plain", cell.dataset.targetId || "");
        } catch (_) { /* older browsers */ }
    });

    grid.addEventListener("dragend", function () {
        var d = grid.querySelector(".dragging");
        if (d) d.classList.remove("dragging");
    });

    grid.addEventListener("dragover", function (e) {
        var over = e.target.closest(".ig-cell.is-movable");
        var dragging = grid.querySelector(".dragging");
        if (!over || !dragging || over === dragging) return;
        e.preventDefault();                               // allow the drop
        var r = over.getBoundingClientRect();
        // Insert before when the cursor is in the top-left half of the target,
        // after otherwise — natural for a wrapping grid.
        var before = (e.clientY < r.top + r.height / 2)
            || (e.clientX < r.left + r.width / 2);
        grid.insertBefore(dragging, before ? over : over.nextSibling);
    });

    grid.addEventListener("drop", function (e) { e.preventDefault(); persist(); });

    function persist() {
        var ids = Array.prototype.slice
            .call(grid.querySelectorAll(".ig-cell.is-movable"))
            .map(function (c) { return c.dataset.targetId; })
            .filter(Boolean);
        if (ids.length < 2) return;
        var body = new URLSearchParams();
        body.set("order", ids.join(","));
        if (window.GRID_CLIENT) body.set("client", window.GRID_CLIENT);
        fetch(window.GRID_REORDER_URL, {
            method: "POST", credentials: "same-origin",
            headers: { "X-Requested-With": "fetch" }, body: body,
        }).then(function (r) { return r.json().catch(function () { return {}; }); })
          .then(function (d) {
              toast(d && d.ok
                  ? "Feed order updated — scheduled times adjusted."
                  : "Couldn't reorder — please refresh and try again.",
                  d && d.ok ? "success" : "error");
          })
          .catch(function () {
              toast("Couldn't reorder — please refresh and try again.", "error");
          });
    }
})();
