/*
    data-confirm: "are you sure?" on a form, without building JavaScript in
    an HTML attribute.

    The pattern this replaces was:

        onsubmit="return confirm('Disconnect {{ a.display_name }}?')"

    which builds a JS string literal inside an HTML attribute out of
    user-authored text. Jinja escapes the name for HTML - an apostrophe
    becomes &#39; - and the HTML parser turns it back into ' BEFORE the
    JavaScript is parsed. So a Page called "O'Brien Dental" produced

        return confirm('Disconnect O'Brien Dental?')

    which is a syntax error. The handler never compiled, the dialog never
    appeared, and the form submitted immediately: the channel disconnected
    with no confirmation at all. Silent, and worst on exactly the
    destructive actions the confirm exists to guard.

    Here the message travels as an attribute VALUE, which the HTML parser
    hands over as text and never as code, so no amount of punctuation in a
    client's name can change what runs.

    Capture phase, so this decides before Turbo picks the submission up.
    csrf.js also listens in capture to inject the token; either order is
    fine - a cancelled submit never reaches the network.
*/
(function () {

    document.addEventListener("submit", function (event) {

        var form = event.target;

        if (!form || form.nodeName !== "FORM") return;

        var message = form.getAttribute("data-confirm");

        if (!message) return;

        if (!window.confirm(message)) {
            event.preventDefault();
            // Stop Turbo's own submit listener seeing it as well; without
            // this the dialog is answered "cancel" and the page navigates
            // anyway.
            event.stopPropagation();
        }

    }, true);

})();
