/*
    Free crop step in front of the profile-picture upload.

    The whole photo is shown, fitted into the stage, and a crop rectangle
    sits on top of it. That rectangle is FREE: any position, any size, any
    aspect ratio. Drag inside it to move it, drag any of the eight handles
    to resize it, or drag on bare photo to draw a new one. What gets saved
    is exactly that rectangle.

    This replaced a fixed square frame that the photo had to be panned and
    zoomed to fit inside. That model answers "which square of this photo?"
    - but the question people actually have is "which PART of my photo?",
    and a locked square cannot express it.

    One consequence worth being honest about, and the reason the circular
    preview exists: every avatar slot in the app draws a circle, so a crop
    that is much wider than it is tall still gets centre-cropped when it
    is displayed. The preview shows that live. It is information, not a
    constraint - the crop stays free and the saved file keeps the shape
    that was chosen.

    Progressive enhancement, deliberately. The form still works with this
    script absent or with an old browser: nothing here is required for an
    upload, and if either capability we need is missing (canvas.toBlob for
    the crop, DataTransfer to hand the result back to the file input) the
    script bows out and the untouched original is what gets posted.

    ------------------------------------------------------------------
    Two things here are load-bearing and easy to undo by accident.

    1. ROTATION IS BAKED INTO A CANVAS, not applied as a CSS transform.
       A transform would leave every bound and the export rect to be
       reasoned about in rotated space. Instead each quarter turn
       re-renders the photo into an offscreen canvas, and everything
       downstream treats that canvas as "the image" - so the geometry
       below never knows rotation exists. `drawImage` takes a canvas as a
       source just as happily as an <img>, and a canvas element positions
       in the DOM just like one, so it is also what the stage shows.

    2. THAT WORKING COPY IS CAPPED AT 2048px on its long edge. The output
       is at most 512, so anything beyond this is memory we pay for and
       throw away - a 12 MP phone photo is ~48 MB of RGBA per rotation
       otherwise, and every rotate would rebuild it.

    Geometry, all in CSS pixels of the stage:
        photoScale = min(STAGE / srcW, STAGE / srcH)
                     "contain", so the whole photo is always on screen -
                     there is nothing to pan, and nothing hidden to crop
                     by accident.
        photoX/Y   = the letterboxed photo's top-left in the stage
        crop       = left/top/right/bottom, clamped to the photo's bounds
                     so a crop can never include the empty stage around a
                     letterboxed photo.
    The export reverses it: source rect on the working canvas is
    ((crop.left - photoX) / photoScale, ...) at size (cropW / photoScale).
*/
(function () {
    "use strict";

    var STAGE = 300;         // px, must match .avatar-crop-stage in style.css
    var MIN_CROP = 24;       // px on the stage; small enough for a tight crop
    var MAX_OUTPUT = 512;    // px, cap on the saved avatar's LONG edge
    var WORK_MAX = 2048;     // px, long edge of the working copy (see note 2)
    var PREVIEW = 72;        // px, the "how it will look" circle
    var JPEG_QUALITY = 0.9;

    //: Which edges each handle moves. Corners move two, edges move one -
    //  that is the whole difference, and why the crop is free rather than
    //  square: no handle ever touches the edges it is not named for.
    var HANDLES = {
        nw: ["left", "top"],
        n: ["top"],
        ne: ["right", "top"],
        e: ["right"],
        se: ["right", "bottom"],
        s: ["bottom"],
        sw: ["left", "bottom"],
        w: ["left"]
    };

    function supported() {
        return !!(window.DataTransfer && window.PointerEvent &&
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
            "<p>Drag the box to move it, its edges or corners to resize, " +
            "or drag anywhere on the photo to start a new one.</p>" +
            '<div class="avatar-crop-stage">' +
            '<div class="avatar-crop-frame">' +
            Object.keys(HANDLES).map(function (h) {
                return '<span class="avatar-crop-handle ac-' + h +
                       '" data-handle="' + h + '"></span>';
            }).join("") +
            "</div>" +
            "</div>" +
            '<div class="avatar-crop-tools">' +
            '<button type="button" class="btn-ghost avatar-crop-rotate"' +
            ' data-crop-rotate>' +
            '<i class="fa-solid fa-rotate-right" aria-hidden="true"></i>' +
            " Rotate</button>" +
            '<button type="button" class="btn-ghost avatar-crop-reset"' +
            ' data-crop-reset>' +
            '<i class="fa-solid fa-arrows-left-right-to-line" aria-hidden="true"></i>' +
            " Whole photo</button>" +
            '<div class="avatar-crop-preview">' +
            '<canvas width="' + PREVIEW + '" height="' + PREVIEW + '"></canvas>' +
            "<span>Avatar</span>" +
            "</div>" +
            "</div>" +
            '<div class="avatar-crop-actions">' +
            '<button type="button" class="btn-secondary" data-crop-cancel>Cancel</button>' +
            '<button type="button" class="btn" data-crop-save>' +
            '<i class="fa-solid fa-check"></i> Save picture</button>' +
            "</div>";

        var stage = box.querySelector(".avatar-crop-stage");
        var frame = box.querySelector(".avatar-crop-frame");
        var preview = box.querySelector(".avatar-crop-preview canvas");
        var rotateBtn = box.querySelector("[data-crop-rotate]");
        var resetBtn = box.querySelector("[data-crop-reset]");
        var cancelBtn = box.querySelector("[data-crop-cancel]");
        var saveBtn = box.querySelector("[data-crop-save]");

        overlay.appendChild(box);

        var objectUrl = URL.createObjectURL(file);
        var baseImage = new Image();

        var source = null;      // working canvas: the photo, rotated + capped
        var srcW = 0;
        var srcH = 0;
        var rotation = 0;       // degrees, always a multiple of 90

        var photoScale = 1;
        var photoX = 0;
        var photoY = 0;

        // The crop, as stage-pixel edges.
        var crop = { left: 0, top: 0, right: 0, bottom: 0 };

        // ---- geometry -------------------------------------------------

        function photoRight() { return photoX + srcW * photoScale; }
        function photoBottom() { return photoY + srcH * photoScale; }

        function layoutPhoto() {
            // "contain": the whole photo, always visible.
            photoScale = Math.min(STAGE / srcW, STAGE / srcH);
            photoX = (STAGE - srcW * photoScale) / 2;
            photoY = (STAGE - srcH * photoScale) / 2;
        }

        function selectWholePhoto() {
            crop.left = photoX;
            crop.top = photoY;
            crop.right = photoRight();
            crop.bottom = photoBottom();
        }

        // A crop may never include the blank stage beside a letterboxed
        // photo, so every edge is bounded by the photo AND by leaving
        // MIN_CROP between itself and the opposite edge.
        function clampCrop() {
            crop.left = clamp(crop.left, photoX, crop.right - MIN_CROP);
            crop.top = clamp(crop.top, photoY, crop.bottom - MIN_CROP);
            crop.right = clamp(crop.right, crop.left + MIN_CROP, photoRight());
            crop.bottom = clamp(crop.bottom, crop.top + MIN_CROP, photoBottom());
        }

        function moveCrop(dx, dy) {
            var width = crop.right - crop.left;
            var height = crop.bottom - crop.top;

            crop.left = clamp(crop.left + dx, photoX, photoRight() - width);
            crop.top = clamp(crop.top + dy, photoY, photoBottom() - height);
            crop.right = crop.left + width;
            crop.bottom = crop.top + height;
        }

        function paint() {
            source.style.width = (srcW * photoScale) + "px";
            source.style.height = (srcH * photoScale) + "px";
            source.style.left = photoX + "px";
            source.style.top = photoY + "px";

            frame.style.left = crop.left + "px";
            frame.style.top = crop.top + "px";
            frame.style.width = (crop.right - crop.left) + "px";
            frame.style.height = (crop.bottom - crop.top) + "px";

            paintPreview();
        }

        // What the circular avatar slots will actually show: the crop,
        // covered into a square. Non-square crops lose their long sides
        // here - which is the point of showing it.
        function paintPreview() {
            var width = (crop.right - crop.left) / photoScale;
            var height = (crop.bottom - crop.top) / photoScale;
            var side = Math.min(width, height);

            var ctx = preview.getContext("2d");
            ctx.clearRect(0, 0, PREVIEW, PREVIEW);
            ctx.fillStyle = "#ffffff";
            ctx.fillRect(0, 0, PREVIEW, PREVIEW);
            ctx.drawImage(
                source,
                (crop.left - photoX) / photoScale + (width - side) / 2,
                (crop.top - photoY) / photoScale + (height - side) / 2,
                side, side,
                0, 0, PREVIEW, PREVIEW
            );
        }

        // ---- the working copy (see notes 1 and 2 at the top) -----------

        function buildSource() {
            var imageW = baseImage.naturalWidth;
            var imageH = baseImage.naturalHeight;

            var fit = Math.min(1, WORK_MAX / Math.max(imageW, imageH));
            var w = Math.max(1, Math.round(imageW * fit));
            var h = Math.max(1, Math.round(imageH * fit));

            var quarter = (rotation % 180) !== 0;
            var canvas = document.createElement("canvas");
            canvas.className = "avatar-crop-photo";
            canvas.width = quarter ? h : w;
            canvas.height = quarter ? w : h;

            var ctx = canvas.getContext("2d");
            ctx.translate(canvas.width / 2, canvas.height / 2);
            ctx.rotate(rotation * Math.PI / 180);
            ctx.drawImage(baseImage, -w / 2, -h / 2, w, h);

            if (source) source.remove();
            source = canvas;
            srcW = canvas.width;
            srcH = canvas.height;
            stage.insertBefore(canvas, frame);
        }

        // ---- lifecycle ------------------------------------------------

        function close() {
            URL.revokeObjectURL(objectUrl);
            document.removeEventListener("keydown", onKeydown);
            overlay.remove();
        }

        // Cancelling must also empty the input: leaving the uncropped file
        // selected would let the Upload button post the very thing the
        // person just backed out of cropping.
        function cancel() {
            input.value = "";
            close();
        }

        function onKeydown(event) {
            if (event.key === "Escape") cancel();
        }

        baseImage.addEventListener("load", function () {
            buildSource();
            layoutPhoto();
            selectWholePhoto();
            paint();
            saveBtn.focus();
        });

        baseImage.addEventListener("error", function () {
            close();
            input.value = "";
            window.alert("That file could not be read as an image.");
        });

        // An <img> applies the photo's EXIF orientation for us, and
        // drawImage carries that through to the canvas - which is why the
        // working copy is built from an image element rather than from
        // createImageBitmap, whose default ignores it. A sideways phone
        // photo would otherwise land sideways with no way back but the
        // rotate button.
        baseImage.src = objectUrl;

        // ---- pointers --------------------------------------------------

        var gesture = null;   // {kind, ...} - resize | move | draw

        // Pointer capture only keeps events coming if the pointer wanders
        // off the stage - it is an optimisation, not the gesture. It can
        // throw (NotFoundError) when the pointer is already gone, and an
        // exception here would abort the rest of the pointerdown handler,
        // leaving the gesture half set up. Always set state FIRST.
        function capture(id) {
            try {
                stage.setPointerCapture(id);
            } catch (err) {
                /* no capture - pointermove on the stage still reaches us */
            }
        }

        function stagePoint(event) {
            var rect = stage.getBoundingClientRect();
            return {
                x: clamp(event.clientX - rect.left, 0, STAGE),
                y: clamp(event.clientY - rect.top, 0, STAGE)
            };
        }

        stage.addEventListener("pointerdown", function (event) {
            var point = stagePoint(event);
            var handle = event.target.getAttribute &&
                         event.target.getAttribute("data-handle");

            if (handle) {
                gesture = { kind: "resize", edges: HANDLES[handle] };
            } else if (event.target === frame) {
                gesture = { kind: "move", x: point.x, y: point.y };
            } else {
                // Bare photo: start a new crop from here. This is the
                // fastest way to say "that bit, there" and is what every
                // desktop crop tool does.
                gesture = { kind: "draw", x: point.x, y: point.y };
                crop.left = crop.right = clamp(point.x, photoX, photoRight());
                crop.top = crop.bottom = clamp(point.y, photoY, photoBottom());
            }

            capture(event.pointerId);
            event.preventDefault();
        });

        stage.addEventListener("pointermove", function (event) {
            if (!gesture) return;

            var point = stagePoint(event);

            if (gesture.kind === "resize") {
                // Each named edge follows the pointer; the others do not
                // move at all. Corners name two edges, sides name one.
                gesture.edges.forEach(function (edge) {
                    crop[edge] = (edge === "left" || edge === "right")
                        ? clamp(point.x, photoX, photoRight())
                        : clamp(point.y, photoY, photoBottom());
                });

                // Dragging an edge past its opposite flips the rectangle
                // inside out; swap instead, so the drag simply keeps
                // working the other way round.
                if (crop.right < crop.left) {
                    var x = crop.left; crop.left = crop.right; crop.right = x;
                    gesture.edges = gesture.edges.map(flipX);
                }
                if (crop.bottom < crop.top) {
                    var y = crop.top; crop.top = crop.bottom; crop.bottom = y;
                    gesture.edges = gesture.edges.map(flipY);
                }

                clampCrop();

            } else if (gesture.kind === "move") {
                moveCrop(point.x - gesture.x, point.y - gesture.y);
                gesture.x = point.x;
                gesture.y = point.y;

            } else {
                crop.left = Math.min(gesture.x, point.x);
                crop.right = Math.max(gesture.x, point.x);
                crop.top = Math.min(gesture.y, point.y);
                crop.bottom = Math.max(gesture.y, point.y);

                crop.left = clamp(crop.left, photoX, photoRight());
                crop.right = clamp(crop.right, photoX, photoRight());
                crop.top = clamp(crop.top, photoY, photoBottom());
                crop.bottom = clamp(crop.bottom, photoY, photoBottom());
            }

            paint();
        });

        function flipX(edge) {
            if (edge === "left") return "right";
            if (edge === "right") return "left";
            return edge;
        }

        function flipY(edge) {
            if (edge === "top") return "bottom";
            if (edge === "bottom") return "top";
            return edge;
        }

        function endPointer(event) {
            // A click that never moved would leave a zero-size crop, which
            // clampCrop would then grow to MIN_CROP from a corner. Better
            // to treat it as "no new selection" and keep what was there.
            if (gesture && gesture.kind === "draw" &&
                crop.right - crop.left < MIN_CROP &&
                crop.bottom - crop.top < MIN_CROP) {
                selectWholePhoto();
            }

            clampCrop();
            paint();
            gesture = null;

            try {
                if (stage.hasPointerCapture(event.pointerId)) {
                    stage.releasePointerCapture(event.pointerId);
                }
            } catch (err) {
                /* pointer already gone - nothing to release */
            }
        }

        stage.addEventListener("pointerup", endPointer);
        stage.addEventListener("pointercancel", endPointer);

        rotateBtn.addEventListener("click", function () {
            rotation = (rotation + 90) % 360;
            buildSource();
            layoutPhoto();
            selectWholePhoto();
            paint();
        });

        resetBtn.addEventListener("click", function () {
            selectWholePhoto();
            paint();
        });

        cancelBtn.addEventListener("click", cancel);

        overlay.addEventListener("click", function (event) {
            if (event.target === overlay) cancel();
        });

        document.addEventListener("keydown", onKeydown);

        // ---- export ---------------------------------------------------

        saveBtn.addEventListener("click", function () {

            var sourceX = (crop.left - photoX) / photoScale;
            var sourceY = (crop.top - photoY) / photoScale;
            var sourceW = (crop.right - crop.left) / photoScale;
            var sourceH = (crop.bottom - crop.top) / photoScale;

            // Cap the LONG edge and keep the aspect ratio - the crop's
            // shape is the person's choice, so nothing here squares it.
            var fit = Math.min(1, MAX_OUTPUT / Math.max(sourceW, sourceH));
            var outW = Math.max(1, Math.round(sourceW * fit));
            var outH = Math.max(1, Math.round(sourceH * fit));

            var canvas = document.createElement("canvas");
            canvas.width = outW;
            canvas.height = outH;

            var ctx = canvas.getContext("2d");
            // JPEG has no alpha: paint white first so a transparent PNG
            // crops to a white-backed avatar rather than a black one.
            ctx.fillStyle = "#ffffff";
            ctx.fillRect(0, 0, outW, outH);
            ctx.drawImage(source, sourceX, sourceY, sourceW, sourceH,
                          0, 0, outW, outH);

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
