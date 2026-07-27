/*
    Square-crop step in front of the profile-picture upload.

    Avatars are drawn into a circle everywhere (top bar, user rows, the
    92px profile hero), so a picture that isn't square gets centre-cropped
    by object-fit whether the person likes it or not - and a phone photo
    is never square. This puts that decision in their hands BEFORE the
    upload: pick a file, drag/zoom to choose the square, save.

    Progressive enhancement, deliberately. The form still works with this
    script absent or with an old browser: nothing here is required for an
    upload, and if either capability we need is missing (canvas.toBlob for
    the crop, DataTransfer to hand the result back to the file input) the
    script bows out and the untouched original is what gets posted.

    The crop is done client-side and re-encoded as a JPEG no larger than
    512x512, which also means a 4 MB camera shot arrives as ~50 KB - the
    server's 5 MB cap stops being something anyone hits.

    Geometry, all in CSS pixels of the square viewport V:
        baseScale = V / min(naturalW, naturalH)   -> smallest "cover" fit
        scale     = baseScale * zoom              -> zoom is 1..MAX_ZOOM
        offset    = image top-left relative to the viewport, clamped so
                    the image can never be dragged off the square.
    The export reverses it: source rect on the natural image is
    (-offset / scale) at size (V / scale).
*/
(function () {
    "use strict";

    var VIEWPORT = 260;      // px, the square the person is choosing
    var MAX_ZOOM = 4;
    var MAX_OUTPUT = 512;    // px, cap on the saved avatar
    var MIN_OUTPUT = 128;
    var JPEG_QUALITY = 0.9;

    function supported() {
        return !!(window.DataTransfer && window.FileReader &&
                  document.createElement("canvas").toBlob);
    }

    function clamp(value, low, high) {
        return Math.min(high, Math.max(low, value));
    }

    function open(file, input, form) {

        var overlay = document.createElement("div");
        overlay.className = "avatar-crop-overlay";

        var box = document.createElement("div");
        box.className = "avatar-crop-box";
        box.setAttribute("role", "dialog");
        box.setAttribute("aria-modal", "true");
        box.setAttribute("aria-label", "Crop profile picture");

        box.innerHTML =
            "<h3>Crop your picture</h3>" +
            "<p>Drag to reposition, use the slider to zoom. The circle is " +
            "exactly what other people will see.</p>" +
            '<div class="avatar-crop-stage">' +
            '<div class="avatar-crop-viewport"><img alt=""></div>' +
            '<div class="avatar-crop-mask" aria-hidden="true"></div>' +
            "</div>" +
            '<label class="avatar-crop-zoom">' +
            '<i class="fa-solid fa-magnifying-glass-minus" aria-hidden="true"></i>' +
            '<input type="range" min="1" max="' + MAX_ZOOM + '" step="0.01" value="1"' +
            ' aria-label="Zoom">' +
            '<i class="fa-solid fa-magnifying-glass-plus" aria-hidden="true"></i>' +
            "</label>" +
            '<div class="avatar-crop-actions">' +
            '<button type="button" class="btn-secondary" data-crop-cancel>Cancel</button>' +
            '<button type="button" class="btn" data-crop-save>' +
            '<i class="fa-solid fa-check"></i> Save picture</button>' +
            "</div>";

        var viewport = box.querySelector(".avatar-crop-viewport");
        var image = box.querySelector(".avatar-crop-viewport img");
        var zoomInput = box.querySelector(".avatar-crop-zoom input");
        var cancelBtn = box.querySelector("[data-crop-cancel]");
        var saveBtn = box.querySelector("[data-crop-save]");

        overlay.appendChild(box);

        var objectUrl = URL.createObjectURL(file);
        var baseScale = 1;
        var scale = 1;
        var offsetX = 0;
        var offsetY = 0;

        function paint() {
            image.style.width = (image.naturalWidth * scale) + "px";
            image.style.height = (image.naturalHeight * scale) + "px";
            image.style.left = offsetX + "px";
            image.style.top = offsetY + "px";
        }

        function clampOffsets() {
            offsetX = clamp(offsetX, VIEWPORT - image.naturalWidth * scale, 0);
            offsetY = clamp(offsetY, VIEWPORT - image.naturalHeight * scale, 0);
        }

        // Re-zoom around the middle of the viewport, so the part being
        // looked at stays put instead of sliding away under the zoom.
        function setZoom(zoom) {
            var next = baseScale * clamp(zoom, 1, MAX_ZOOM);
            var midX = (VIEWPORT / 2 - offsetX) / scale;
            var midY = (VIEWPORT / 2 - offsetY) / scale;

            scale = next;
            offsetX = VIEWPORT / 2 - midX * scale;
            offsetY = VIEWPORT / 2 - midY * scale;

            clampOffsets();
            paint();
        }

        function close() {
            URL.revokeObjectURL(objectUrl);
            document.removeEventListener("keydown", onKeydown);
            overlay.remove();
        }

        function onKeydown(event) {
            if (event.key === "Escape") {
                cancel();
            }
        }

        // Cancelling must also empty the input: leaving the uncropped file
        // selected would let the Upload button post the very thing the
        // person just backed out of cropping.
        function cancel() {
            input.value = "";
            close();
        }

        image.addEventListener("load", function () {
            baseScale = VIEWPORT / Math.min(image.naturalWidth, image.naturalHeight);
            scale = baseScale;
            offsetX = (VIEWPORT - image.naturalWidth * scale) / 2;
            offsetY = (VIEWPORT - image.naturalHeight * scale) / 2;
            paint();
            saveBtn.focus();
        });

        image.addEventListener("error", function () {
            close();
            input.value = "";
            window.alert("That file could not be read as an image.");
        });

        image.src = objectUrl;

        var dragging = false;
        var startX = 0;
        var startY = 0;
        var startOffsetX = 0;
        var startOffsetY = 0;

        viewport.addEventListener("pointerdown", function (event) {
            dragging = true;
            startX = event.clientX;
            startY = event.clientY;
            startOffsetX = offsetX;
            startOffsetY = offsetY;
            viewport.setPointerCapture(event.pointerId);
            event.preventDefault();
        });

        viewport.addEventListener("pointermove", function (event) {
            if (!dragging) return;
            offsetX = startOffsetX + (event.clientX - startX);
            offsetY = startOffsetY + (event.clientY - startY);
            clampOffsets();
            paint();
        });

        function endDrag(event) {
            if (!dragging) return;
            dragging = false;
            try {
                viewport.releasePointerCapture(event.pointerId);
            } catch (err) {
                /* pointer already gone - nothing to release */
            }
        }

        viewport.addEventListener("pointerup", endDrag);
        viewport.addEventListener("pointercancel", endDrag);

        viewport.addEventListener("wheel", function (event) {
            event.preventDefault();
            var zoom = parseFloat(zoomInput.value) - event.deltaY * 0.002;
            zoom = clamp(zoom, 1, MAX_ZOOM);
            zoomInput.value = zoom;
            setZoom(zoom);
        }, { passive: false });

        zoomInput.addEventListener("input", function () {
            setZoom(parseFloat(zoomInput.value));
        });

        cancelBtn.addEventListener("click", cancel);

        overlay.addEventListener("click", function (event) {
            if (event.target === overlay) cancel();
        });

        document.addEventListener("keydown", onKeydown);

        saveBtn.addEventListener("click", function () {

            var sourceSize = VIEWPORT / scale;
            var out = clamp(Math.round(sourceSize), MIN_OUTPUT, MAX_OUTPUT);

            var canvas = document.createElement("canvas");
            canvas.width = out;
            canvas.height = out;

            var ctx = canvas.getContext("2d");
            // JPEG has no alpha: paint white first so a transparent PNG
            // crops to a white-backed avatar rather than a black one.
            ctx.fillStyle = "#ffffff";
            ctx.fillRect(0, 0, out, out);
            ctx.drawImage(
                image,
                -offsetX / scale, -offsetY / scale, sourceSize, sourceSize,
                0, 0, out, out
            );

            saveBtn.disabled = true;

            canvas.toBlob(function (blob) {

                if (!blob) {
                    saveBtn.disabled = false;
                    window.alert("Could not prepare the cropped image.");
                    return;
                }

                var cropped = new File([blob], "avatar.jpg",
                                       { type: "image/jpeg" });
                var transfer = new DataTransfer();
                transfer.items.add(cropped);
                input.files = transfer.files;

                close();

                if (form.requestSubmit) {
                    form.requestSubmit();
                } else {
                    form.submit();
                }

            }, "image/jpeg", JPEG_QUALITY);
        });

        document.body.appendChild(overlay);
    }

    function bind() {

        var input = document.getElementById("avatarInput");
        if (!input || input.dataset.cropperBound === "1") return;
        if (!supported()) return;

        var form = input.form;
        if (!form) return;

        // Turbo re-runs page scripts on every visit; the flag keeps a
        // second run from stacking a duplicate handler on the same input.
        input.dataset.cropperBound = "1";

        input.addEventListener("change", function () {
            var file = input.files && input.files[0];
            if (file) open(file, input, form);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bind);
    } else {
        bind();
    }
})();
