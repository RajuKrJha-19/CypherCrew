/*
    Lazy video-frame thumbnails (fallback only).

    The real thumbnail is generated server-side (ffmpeg -> cached webp) and
    served as a plain <img>. This script is the FALLBACK for clips whose
    server thumbnail isn't ready yet: it borrows a real frame from the clip
    itself - but cheaply. Two hard rules keep the grid fast:

      1. Lazy: a tile only starts loading once it's near the viewport.
      2. Throttled: at most MAX_CONCURRENT clips load at a time. A gallery
         with 30 videos must never open 30 streams to R2 at once - that is
         what made the page hang. New tiles queue and start as slots free.

    Painting the frame is the tricky part: a plain <video preload=metadata>
    (even with #t= in the URL) usually shows a BLACK box, because the
    browser loads metadata but never decodes a frame until told to. So once
    metadata is in we seek a little past the start via currentTime, which
    forces a decode; the 'seeked' frame is what gets painted.

    Markup: <video class="js-video-thumb" data-vsrc="<preview-url>#t=0.5"
    preload="none" muted playsinline>. A gradient poster + play badge sit
    behind it, so a clip that can't decode still reads as a video.
*/
(function () {
    // How many clips may be fetching a frame at the same time. Small on
    // purpose: the browser caps connections per host anyway, and we'd
    // rather fill the grid steadily than choke the tab on open.
    var MAX_CONCURRENT = 2;
    // If a clip never signals it's done (slow network, dead file), free its
    // slot after this long so the queue can't stall.
    var LOAD_TIMEOUT_MS = 8000;

    var active = 0;
    var queue = [];

    function paintFrame(video) {
        var target = 0.5;
        var d = video.duration;
        if (d && isFinite(d) && d > 0) {
            // ~10% in (skips black intros), clamped to a sane 0.3-2s window.
            target = Math.min(Math.max(d * 0.1, 0.3), Math.min(2, Math.max(d - 0.05, 0.1)));
        }
        try { video.currentTime = target; } catch (e) {}
    }

    function load(video) {
        active++;

        var finished = false;
        function finish() {
            if (finished) return;
            finished = true;
            active--;
            pump();
        }

        video.preload = "metadata";
        video.muted = true;
        video.setAttribute("playsinline", "");

        video.addEventListener("loadedmetadata", function () { paintFrame(video); }, { once: true });

        // Once the seek lands we have our still - free the slot for the next
        // clip. (We don't wait for the whole file, just the one frame.)
        video.addEventListener("seeked", finish, { once: true });

        // Belt and braces: some engines only paint after a play; do it
        // muted, then immediately pause so it stays a still.
        video.addEventListener("loadeddata", function () {
            var p = video.play();
            if (p && typeof p.then === "function") {
                p.then(function () { video.pause(); }).catch(function () {});
            }
        }, { once: true });

        video.addEventListener("error", function () {
            video.style.display = "none";  // leave the fallback poster showing
            finish();
        }, { once: true });

        setTimeout(finish, LOAD_TIMEOUT_MS);

        // Strip the URL fragment - we drive the seek from JS, which is what
        // actually forces the paint.
        video.src = (video.dataset.vsrc || "").split("#")[0];
        try { video.load(); } catch (e) {}
    }

    function pump() {
        while (active < MAX_CONCURRENT && queue.length) {
            load(queue.shift());
        }
    }

    function enqueue(video) {
        if (video.dataset.queued) return;
        video.dataset.queued = "1";
        queue.push(video);
        pump();
    }

    function init() {
        var videos = document.querySelectorAll(".js-video-thumb[data-vsrc]:not([data-queued])");
        if (!videos.length) return;

        // No IntersectionObserver (old browser): queue them all - the
        // concurrency cap still keeps it from stampeding.
        if (!("IntersectionObserver" in window)) {
            videos.forEach(enqueue);
            return;
        }

        // Lazy everywhere: a tile is queued only as it nears the viewport,
        // so opening the page never kicks off more than what's on screen.
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    enqueue(entry.target);
                    io.unobserve(entry.target);
                }
            });
        }, { rootMargin: "300px" });

        videos.forEach(function (v) { io.observe(v); });
    }

    // Run as soon as the DOM is ready (this is a deferred script, so that
    // is usually right now) and again on every Turbo visit. init is
    // idempotent - it skips tiles it has already queued - so extra calls
    // are harmless.
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    document.addEventListener("turbo:load", init);
})();
