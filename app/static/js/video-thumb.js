/*
    Lazy video-frame thumbnails.

    There's no server-side video thumbnail (no ffmpeg), so we borrow a real
    frame from the clip - but only once the tile is on screen, and only its
    metadata + one seek range, never the whole file.

    Painting the frame is the tricky part: a plain <video preload=metadata>
    (even with #t= in the URL) usually shows a BLACK box, because the
    browser loads metadata but never decodes a frame until it's told to.
    So once metadata is in we seek a little way past the start (intros are
    often black) via currentTime, which forces a decode; the 'seeked' frame
    is what gets painted.

    Markup: <video class="js-video-thumb" data-vsrc="<preview-url>#t=0.5"
    preload="none" muted playsinline>. A gradient poster + play badge sit
    behind it, so a clip that can't decode still reads as a video.
*/
(function () {
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
        if (video.dataset.loaded) return;
        video.dataset.loaded = "1";

        video.preload = "metadata";
        video.muted = true;
        video.setAttribute("playsinline", "");

        video.addEventListener("loadedmetadata", function () { paintFrame(video); }, { once: true });

        // Belt and braces: some engines only paint after a play; do it
        // muted, then immediately pause so it stays a still.
        video.addEventListener("loadeddata", function () {
            var p = video.play();
            if (p && typeof p.then === "function") {
                p.then(function () { video.pause(); }).catch(function () {});
            }
        }, { once: true });

        video.addEventListener("error", function () {
            // Leave the fallback poster showing.
            video.style.display = "none";
        }, { once: true });

        // Strip the URL fragment - we drive the seek from JS, which is
        // what actually forces the paint.
        video.src = (video.dataset.vsrc || "").split("#")[0];
        try { video.load(); } catch (e) {}
    }

    function init() {
        var videos = document.querySelectorAll(".js-video-thumb[data-vsrc]:not([data-loaded])");
        if (!videos.length) return;

        // A container marked data-eager (the gallery) pre-loads every
        // thumbnail up front so the grid is ready the moment it opens.
        // Only metadata + one seek range per clip is fetched, and the
        // browser caps how many run at once, so it stays cheap.
        var eager = document.querySelector("[data-eager-video-thumbs]");

        if (eager || !("IntersectionObserver" in window)) {
            videos.forEach(load);
            return;
        }

        // Elsewhere (e.g. task detail) stay lazy: load as tiles approach.
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    load(entry.target);
                    io.unobserve(entry.target);
                }
            });
        }, { rootMargin: "600px" });

        videos.forEach(function (v) { io.observe(v); });
    }

    // Run as soon as the DOM is ready (this is a deferred script, so that
    // is usually right now) and again on every Turbo visit. init is
    // idempotent - it skips tiles it has already wired - so extra calls
    // are harmless. (Relying on App.ready/turbo:load alone missed the
    // first paint when that event had already fired before this loaded.)
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    document.addEventListener("turbo:load", init);
})();
