// Shared toast helper - replaces blocking alert() with a small,
// dismissible, auto-expiring message in the corner of the screen.
// Usage: showToast("Something went wrong.", "error")
(function () {

    function ensureContainer() {
        let container = document.getElementById("toastContainer");

        if (!container) {
            container = document.createElement("div");
            container.id = "toastContainer";
            container.className = "toast-container";
            document.body.appendChild(container);
        }

        return container;
    }

    function showToast(message, type) {

        type = type === "success" ? "success" : "error";

        const container = ensureContainer();

        const toast = document.createElement("div");
        toast.className = "toast toast-" + type;

        // Errors interrupt (assertive); success just confirms (polite) -
        // same distinction screen readers already get from alert(),
        // without blocking the page.
        toast.setAttribute("role", type === "error" ? "alert" : "status");
        toast.setAttribute("aria-live", type === "error" ? "assertive" : "polite");

        const text = document.createElement("span");
        text.className = "toast-message";
        text.textContent = message;
        toast.appendChild(text);

        const closeBtn = document.createElement("button");
        closeBtn.type = "button";
        closeBtn.className = "toast-close";
        closeBtn.setAttribute("aria-label", "Dismiss");
        closeBtn.innerHTML = "&times;";
        toast.appendChild(closeBtn);

        let dismissed = false;

        function remove() {
            if (dismissed) return;
            dismissed = true;

            toast.classList.remove("toast-show");
            toast.addEventListener("transitionend", function () {
                toast.remove();
            }, { once: true });
        }

        closeBtn.addEventListener("click", remove);

        container.appendChild(toast);

        requestAnimationFrame(function () {
            toast.classList.add("toast-show");
        });

        setTimeout(remove, 5000);
    }

    window.showToast = showToast;

})();
