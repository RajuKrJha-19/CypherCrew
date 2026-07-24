/*
    Lazy video-frame thumbnails.

    There's no server-side video thumbnail (no ffmpeg), so we borrow a
    real frame from the clip itself - but only when the tile scrolls into
    view, and only its metadata + the seek range, never the whole file.
    That's what keeps a grid of large clips from stampeding R2 the way an
    eager <video> once did.

    Each thumbnail is a <video class="js-video-thumb" data-vsrc="...#t=0.5"
    preload="none">. We set src (and seek) on intersection; a fallback
    poster sits behind it, so if the frame can't load the tile still reads
    as a video rather than a broken box.
*/
(function () {
    function load(video) {
        if (video.dataset.loaded) return;
        video.dataset.loaded = "1";
        video.preload = "metadata";
        // #t=0.5 in the src tells the browser which frame to paint.
        video.src = video.dataset.vsrc;
        try { video.load(); } catch (e) {}
    }

    function init() {
        var videos = document.querySelectorAll(".js-video-thumb[data-vsrc]:not([data-loaded])");
        if (!videos.length) return;

        if (!("IntersectionObserver" in window)) {
            videos.forEach(load);
            return;
        }

        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (entry) {
                if (entry.isIntersecting) {
                    load(entry.target);
                    io.unobserve(entry.target);
                }
            });
        }, { rootMargin: "250px" });

        videos.forEach(function (v) { io.observe(v); });
    }

    if (window.App && window.App.ready) {
        window.App.ready(init);
    } else if (document.readyState !== "loading") {
        init();
    } else {
        document.addEventListener("DOMContentLoaded", init);
    }

    // Turbo re-renders the body; re-scan for fresh tiles each visit.
    document.addEventListener("turbo:load", init);
})();
