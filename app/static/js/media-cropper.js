/* Composer image reframe overlay.
 *
 * Shows an image (cross-origin presigned URL - DISPLAY only, no canvas read,
 * so no taint / no R2 CORS needed) with an aspect-locked selection the user can
 * move and resize. On Apply it POSTs the normalised rectangle to the server,
 * which cuts the pixels and returns a fresh upload object; the caller's
 * onApplied() swaps that into the composer.
 *
 * Generic + composer-agnostic: window.MediaCropper.open({
 *     imageUrl, objectKey, aspect, onApplied(data), onError(msg) }).
 * `aspect` = width/height (e.g. 0.8 for 4:5) or null for a free crop.
 */
(function () {
    "use strict";
    if (window.MediaCropper) return;

    var STAGE_MAX_W = 560, STAGE_MAX_H = 460, MIN_PX = 24;

    // label -> aspect (width/height). null = free (any shape).
    var PRESETS = [
        { key: "free", label: "Original", aspect: null },
        { key: "1:1", label: "1:1", aspect: 1 },
        { key: "4:5", label: "4:5", aspect: 0.8 },
        { key: "9:16", label: "9:16", aspect: 0.5625 },
        { key: "16:9", label: "16:9", aspect: 16 / 9 },
    ];

    function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

    function matchPreset(aspect) {
        if (!aspect) return "free";
        var best = "free", bestD = 0.03;               // tolerance
        PRESETS.forEach(function (p) {
            if (p.aspect) {
                var d = Math.abs(p.aspect - aspect);
                if (d < bestD) { bestD = d; best = p.key; }
            }
        });
        return best;
    }

    function open(opts) {
        opts = opts || {};
        var aspect = opts.aspect || null;             // width/height or null
        var dW = 0, dH = 0;                           // displayed image size (px)
        var sel = { left: 0, top: 0, w: 0, h: 0 };    // selection in display px
        var activePreset = matchPreset(aspect);

        // ---- overlay DOM ------------------------------------------------
        var ov = document.createElement("div");
        ov.className = "mc-overlay";
        ov.innerHTML =
            '<div class="mc-dialog" role="dialog" aria-modal="true" aria-label="Crop image">' +
              '<div class="mc-head">' +
                '<span class="mc-title">Crop &amp; reframe</span>' +
                '<div class="mc-presets"></div>' +
                '<button type="button" class="mc-x" aria-label="Close">&times;</button>' +
              '</div>' +
              '<div class="mc-stagewrap"><div class="mc-stage">' +
                '<img class="mc-img" alt="">' +
                '<div class="mc-sel">' +
                  '<span class="mc-h" data-h="nw"></span><span class="mc-h" data-h="ne"></span>' +
                  '<span class="mc-h" data-h="sw"></span><span class="mc-h" data-h="se"></span>' +
                  '<span class="mc-h mc-edge" data-h="n"></span><span class="mc-h mc-edge" data-h="e"></span>' +
                  '<span class="mc-h mc-edge" data-h="s"></span><span class="mc-h mc-edge" data-h="w"></span>' +
                '</div>' +
              '</div></div>' +
              '<div class="mc-foot">' +
                '<span class="mc-hint">Drag to move · handles to resize</span>' +
                '<span class="mc-actions">' +
                  '<button type="button" class="mc-btn mc-cancel">Cancel</button>' +
                  '<button type="button" class="mc-btn mc-apply">Apply crop</button>' +
                '</span>' +
              '</div>' +
            '</div>';
        document.body.appendChild(ov);

        var stage = ov.querySelector(".mc-stage");
        var img = ov.querySelector(".mc-img");
        var selEl = ov.querySelector(".mc-sel");
        var presetsBox = ov.querySelector(".mc-presets");
        var applyBtn = ov.querySelector(".mc-apply");

        // ---- preset chips ----------------------------------------------
        PRESETS.forEach(function (p) {
            var b = document.createElement("button");
            b.type = "button";
            b.className = "mc-chip" + (p.key === activePreset ? " on" : "");
            b.textContent = p.label;
            b.setAttribute("data-preset", p.key);
            presetsBox.appendChild(b);
        });

        function paint() {
            selEl.style.left = sel.left + "px";
            selEl.style.top = sel.top + "px";
            selEl.style.width = sel.w + "px";
            selEl.style.height = sel.h + "px";
        }

        // Largest centred rect of `aspect` (or full image if free) inside dW×dH.
        function resetSelection() {
            if (!aspect) { sel = { left: 0, top: 0, w: dW, h: dH }; paint(); return; }
            var w = dW, h = w / aspect;
            if (h > dH) { h = dH; w = h * aspect; }
            sel = { left: (dW - w) / 2, top: (dH - h) / 2, w: w, h: h };
            paint();
        }

        function toggleEdgeHandles() {
            // Locked aspect -> corners only (edges would break the ratio).
            selEl.querySelectorAll(".mc-edge").forEach(function (e) {
                e.style.display = aspect ? "none" : "";
            });
        }

        // ---- image load -------------------------------------------------
        img.onload = function () {
            var nW = img.naturalWidth || 1, nH = img.naturalHeight || 1;
            var scale = Math.min(STAGE_MAX_W / nW, STAGE_MAX_H / nH);
            dW = Math.max(1, Math.round(nW * scale));
            dH = Math.max(1, Math.round(nH * scale));
            stage.style.width = dW + "px";
            stage.style.height = dH + "px";
            img.style.width = dW + "px";
            img.style.height = dH + "px";
            toggleEdgeHandles();
            resetSelection();
        };
        img.onerror = function () {
            teardown();
            if (opts.onError) opts.onError("Couldn't load the image to crop.");
        };
        img.src = opts.imageUrl || "";

        // ---- gestures ---------------------------------------------------
        var drag = null;   // {mode, handle, sx, sy, start:{...}}

        function onDown(ev) {
            var handleEl = ev.target.closest(".mc-h");
            var onSel = ev.target === selEl || handleEl;
            if (!onSel) return;
            ev.preventDefault();
            drag = {
                mode: handleEl ? "resize" : "move",
                handle: handleEl ? handleEl.getAttribute("data-h") : null,
                sx: ev.clientX, sy: ev.clientY,
                start: { left: sel.left, top: sel.top, w: sel.w, h: sel.h },
            };
            window.addEventListener("pointermove", onMove);
            window.addEventListener("pointerup", onUp);
        }

        function onMove(ev) {
            if (!drag) return;
            var dx = ev.clientX - drag.sx, dy = ev.clientY - drag.sy, s = drag.start;
            if (drag.mode === "move") {
                sel.left = clamp(s.left + dx, 0, dW - s.w);
                sel.top = clamp(s.top + dy, 0, dH - s.h);
                paint();
                return;
            }
            if (aspect) resizeLocked(drag.handle, dx, dy, s);
            else resizeFree(drag.handle, dx, dy, s);
            paint();
        }

        function onUp() {
            drag = null;
            window.removeEventListener("pointermove", onMove);
            window.removeEventListener("pointerup", onUp);
        }

        // Free resize: move only the touched edges, clamp to the image.
        function resizeFree(h, dx, dy, s) {
            var l = s.left, t = s.top, r = s.left + s.w, b = s.top + s.h;
            if (h.indexOf("w") > -1) l = clamp(s.left + dx, 0, r - MIN_PX);
            if (h.indexOf("e") > -1) r = clamp(s.left + s.w + dx, l + MIN_PX, dW);
            if (h.indexOf("n") > -1) t = clamp(s.top + dy, 0, b - MIN_PX);
            if (h.indexOf("s") > -1) b = clamp(s.top + s.h + dy, t + MIN_PX, dH);
            sel = { left: l, top: t, w: r - l, h: b - t };
        }

        // Locked resize: opposite corner is the anchor; keep w/h == aspect,
        // bounded by the space available from the anchor in both axes.
        function resizeLocked(h, dx, dy, s) {
            var east = h.indexOf("e") > -1, south = h.indexOf("s") > -1;
            var ax = east ? s.left : s.left + s.w;         // anchor x
            var ay = south ? s.top : s.top + s.h;          // anchor y
            var px = clamp((east ? s.left + s.w : s.left) + dx, 0, dW);
            var py = clamp((south ? s.top + s.h : s.top) + dy, 0, dH);
            var wantW = Math.abs(px - ax);
            var maxW = east ? dW - ax : ax;
            var maxH = south ? dH - ay : ay;
            var w = Math.min(wantW, maxW, maxH * aspect);
            w = Math.max(w, MIN_PX);
            var hgt = w / aspect;
            sel = {
                left: east ? ax : ax - w,
                top: south ? ay : ay - hgt,
                w: w, h: hgt,
            };
        }

        stage.addEventListener("pointerdown", onDown);

        // ---- preset switch ---------------------------------------------
        presetsBox.addEventListener("click", function (ev) {
            var chip = ev.target.closest("[data-preset]");
            if (!chip) return;
            var key = chip.getAttribute("data-preset");
            var p = PRESETS.filter(function (x) { return x.key === key; })[0];
            if (!p) return;
            aspect = p.aspect;
            activePreset = key;
            presetsBox.querySelectorAll(".mc-chip").forEach(function (c) {
                c.classList.toggle("on", c === chip);
            });
            toggleEdgeHandles();
            resetSelection();
        });

        // ---- apply / cancel --------------------------------------------
        function teardown() { if (ov.parentNode) ov.parentNode.removeChild(ov); }
        ov.querySelector(".mc-cancel").addEventListener("click", teardown);
        ov.querySelector(".mc-x").addEventListener("click", teardown);
        ov.addEventListener("mousedown", function (ev) { if (ev.target === ov) teardown(); });

        applyBtn.addEventListener("click", function () {
            if (!dW || !dH) return;
            var body = new URLSearchParams();
            body.set("object_key", opts.objectKey || "");
            body.set("x", (sel.left / dW).toFixed(5));
            body.set("y", (sel.top / dH).toFixed(5));
            body.set("w", (sel.w / dW).toFixed(5));
            body.set("h", (sel.h / dH).toFixed(5));
            applyBtn.disabled = true;
            applyBtn.textContent = "Cropping…";
            fetch(window.MEDIA_CROP_URL, {
                method: "POST", credentials: "same-origin",
                headers: { "X-Requested-With": "fetch" }, body: body,
            }).then(function (r) {
                return r.json().catch(function () { return {}; })
                    .then(function (d) { return { ok: r.ok, d: d }; });
            }).then(function (res) {
                if (!res.ok || !res.d.object_key) {
                    applyBtn.disabled = false;
                    applyBtn.textContent = "Apply crop";
                    if (opts.onError) opts.onError(res.d.error || "Crop failed.");
                    return;
                }
                teardown();
                if (opts.onApplied) opts.onApplied(res.d);
            }).catch(function () {
                applyBtn.disabled = false;
                applyBtn.textContent = "Apply crop";
                if (opts.onError) opts.onError("Network error — please try again.");
            });
        });
    }

    window.MediaCropper = { open: open };
})();
