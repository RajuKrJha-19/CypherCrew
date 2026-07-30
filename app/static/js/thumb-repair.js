/*
    Self-healing thumbnails.

    A tile's <img> points straight at a presigned URL for the generated
    webp, which is what keeps a large grid fast: no redirect hop, no Flask
    request per tile. The cost of that directness is that nothing checks
    the object is still there. A row can say `ready` while the object has
    been deleted underneath it - a pruned bucket, a bucket swapped for a
    new one - and then every layer is faithfully serving a URL to
    something that is not there. The tile renders broken and no log
    anywhere disagrees.

    Verifying up front would be a HEAD per tile on every render, which is
    exactly the per-tile cost the direct URL exists to avoid. So the check
    lives here, on the error path, where only a genuinely broken tile pays
    for it: swap in ?repair=1, which asks the server to confirm the object
    really is gone and put the row back to pending for regeneration. That
    request answers with the original as a stand-in, so the tile shows
    something now and the real thumbnail is there next time.

    Once per tile. A second failure means the repair did not help, and a
    handler that kept retrying would sit in a loop hammering the route.
*/
(function () {

    function repair(img) {
        var next = img.dataset.repairSrc;

        // Whether or not there is somewhere to go, this tile has had its
        // one attempt.
        delete img.dataset.repairSrc;

        if (!next) {
            // Nothing to fall back to: hide the broken-image glyph and let
            // the tile's own background show instead.
            img.style.visibility = "hidden";
            return;
        }

        img.src = next;
    }

    function attach(img) {
        if (img.dataset.repairBound) return;
        img.dataset.repairBound = "1";

        img.addEventListener("error", function () { repair(img); });

        // A tile that already failed before this script ran - the common
        // case, since images load while the page is still parsing.
        // complete + naturalWidth 0 is the only honest test for it.
        if (img.complete && img.naturalWidth === 0) repair(img);
    }

    function init() {
        document
            .querySelectorAll("img[data-repair-src]")
            .forEach(attach);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    // Turbo swaps the body; the new tiles need binding too. attach() skips
    // anything already bound, so re-running is free.
    document.addEventListener("turbo:load", init);

})();
