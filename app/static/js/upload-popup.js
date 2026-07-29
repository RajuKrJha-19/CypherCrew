/*
    The upload popup.

    Replaces "pick files into a bare <input>, press a button, wait for the
    page to come back". You press Upload, a popup opens, you drop files
    into it or pick them, and each file gets its own row with its own
    progress bar and its own x. Closing the popup abandons the lot.

    Two sites use it, and they store files differently:

      multipart - task submissions. The file goes straight to R2 in 8 MB
                  parts through presigned URLs, never through this app.
                  The task already exists, so an upload produces a real
                  TaskFile row immediately.

      direct    - reference files on the create form. There is no task
                  yet (StorageService.upload_task_file refuses without an
                  id), so the file is streamed to a staging prefix and the
                  key is carried in a hidden field until the task is
                  saved.

    Both are driven by the same rows, the same queue and the same cancel
    semantics; only the transport differs.

    Uploads run ONE AT A TIME. Five parallel video uploads on an office
    connection make all five bars crawl, which reads as "it's broken".
    Sequential means the first file is genuinely done first.

    Everything here is progressive enhancement. The plain <input> and the
    plain form are still in the page for anyone without JavaScript; this
    file hides them and takes over only once it has loaded.
*/
(function () {
    "use strict";

    var PART_SIZE = 8 * 1024 * 1024;

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute("content") : "";
    }

    function humanSize(bytes) {
        if (!bytes) return "0 KB";
        var units = ["B", "KB", "MB", "GB"];
        var i = 0;
        var n = bytes;
        while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
        return (i === 0 ? n : n.toFixed(1)) + " " + units[i];
    }

    function cancelled() {
        return Object.assign(new Error("Upload cancelled."),
                             { name: "AbortError" });
    }

    function wasCancelled(error) {
        return error && (error.name === "AbortError"
                         || error.name === "CancelledError");
    }

    /* JSON over the wrapped fetch, so csrf.js attaches the token. */
    function postJSON(url, payload, signal) {
        return fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload || {}),
            signal: signal
        }).then(function (response) {
            return response.json().catch(function () {
                throw new Error("The server sent back something unreadable "
                                + "(HTTP " + response.status + ").");
            });
        }).then(function (data) {
            if (!data.success) {
                throw new Error(data.message || "Request failed.");
            }
            return data;
        });
    }

    // ----------------------------------------------------------------
    // Transports
    // ----------------------------------------------------------------

    /*
        PUT one part to storage. XMLHttpRequest rather than fetch because
        xhr.upload.onprogress is the only way a browser reports how much
        of a request body has gone out - fetch cannot do it at all.
    */
    function putPart(url, blob, onProgress, signal) {
        return new Promise(function (resolve, reject) {
            if (signal && signal.aborted) { reject(cancelled()); return; }

            var xhr = new XMLHttpRequest();
            xhr.open("PUT", url, true);

            /*
                Reject here rather than waiting for the aborted request to
                report back. It normally would - but if it ever does not,
                the row sits at whatever percentage it reached and the
                queue never starts the next file. Settling twice is a
                no-op on a promise, so this is free insurance.
            */
            function handleAbort() { xhr.abort(); reject(cancelled()); }
            if (signal) signal.addEventListener("abort", handleAbort);

            xhr.upload.addEventListener("progress", function (event) {
                if (event.lengthComputable) onProgress(event.loaded);
            });

            xhr.onreadystatechange = function () {
                if (xhr.readyState !== 4) return;
                if (signal) signal.removeEventListener("abort", handleAbort);
                if (signal && signal.aborted) { reject(cancelled()); return; }

                if (xhr.status >= 200 && xhr.status < 300) {
                    var etag = xhr.getResponseHeader("ETag");
                    /*
                        The part is stored, but the browser cannot read
                        the ETag unless the bucket's CORS policy exposes
                        it - and without ETags the completion call is
                        rejected, which looks like a random failure at the
                        very end of a long upload.
                    */
                    if (!etag) {
                        reject(new Error(
                            "Storage accepted the file but did not expose an "
                            + "ETag. The bucket's CORS policy needs "
                            + "\"ExposeHeaders\": [\"ETag\"]."));
                        return;
                    }
                    resolve(etag);
                } else if (xhr.status === 0) {
                    // Not the network: status 0 is the browser refusing to
                    // hand over a response it considers cross-origin.
                    reject(new Error(
                        "The browser could not reach storage. This is usually "
                        + "the bucket's CORS policy not allowing PUT from "
                        + "this site."));
                } else {
                    reject(new Error("Storage rejected the upload (HTTP "
                                     + xhr.status + ")."));
                }
            };

            xhr.send(blob);
        });
    }

    /* Submission: initiate -> parts -> complete, aborting on any failure
       so a half-finished multipart upload is not left billing storage. */
    function multipartUpload(endpoints, file, onProgress, signal) {
        var uploadId = null;
        var objectKey = null;

        return postJSON(endpoints.initiate, {
            filename: file.name,
            content_type: file.type
        }, signal).then(function (initiated) {
            uploadId = initiated.upload_id;
            objectKey = initiated.object_key;

            var parts = [];
            var partNumber = 1;
            var offset = 0;
            var sentBytes = 0;

            function nextPart() {
                if (offset >= file.size) return Promise.resolve();

                var end = Math.min(offset + PART_SIZE, file.size);
                var blob = file.slice(offset, end);

                return postJSON(endpoints.partUrl, {
                    object_key: objectKey,
                    upload_id: uploadId,
                    part_number: partNumber
                }, signal).then(function (part) {
                    return putPart(part.url, blob, function (loaded) {
                        onProgress(sentBytes + loaded);
                    }, signal);
                }).then(function (etag) {
                    parts.push({ ETag: etag, PartNumber: partNumber });
                    sentBytes += blob.size;
                    offset = end;
                    partNumber += 1;
                    return nextPart();
                });
            }

            return nextPart().then(function () {
                return postJSON(endpoints.complete, {
                    object_key: objectKey,
                    upload_id: uploadId,
                    parts: parts,
                    original_filename: initiated.original_filename,
                    stored_filename: initiated.stored_filename
                }, signal);
            }).then(function (completed) {
                return { file_id: completed.file_id, name: file.name };
            });
        }).catch(function (error) {
            if (uploadId && objectKey) {
                postJSON(endpoints.abort, {
                    object_key: objectKey,
                    upload_id: uploadId
                }).catch(function () { /* best effort */ });
            }
            throw error;
        });
    }

    /* One streamed POST per file. XHR again, for the same progress reason
       - and the CSRF header by hand, because csrf.js only wraps fetch.
       Used by reference staging and by client assets. */
    function directUpload(endpoints, file, onProgress, signal, extra) {
        return new Promise(function (resolve, reject) {
            if (signal && signal.aborted) { reject(cancelled()); return; }

            var form = new FormData();
            form.append("file", file);
            // Fields the page owns rather than the popup - the client-asset
            // category picker, for one. Read per file, so changing the
            // picker mid-batch applies to what has not gone yet.
            Object.keys(extra || {}).forEach(function (name) {
                form.append(name, extra[name]);
            });

            var xhr = new XMLHttpRequest();
            xhr.open("POST", endpoints.stage, true);
            xhr.setRequestHeader("X-CSRFToken", csrfToken());

            // Settled from here, not from the aborted request coming back
            // - see putPart.
            function handleAbort() { xhr.abort(); reject(cancelled()); }
            if (signal) signal.addEventListener("abort", handleAbort);

            xhr.upload.addEventListener("progress", function (event) {
                if (event.lengthComputable) onProgress(event.loaded);
            });

            xhr.onreadystatechange = function () {
                if (xhr.readyState !== 4) return;
                if (signal) signal.removeEventListener("abort", handleAbort);
                if (signal && signal.aborted) { reject(cancelled()); return; }

                var data = null;
                try { data = JSON.parse(xhr.responseText); } catch (e) { }

                if (xhr.status >= 200 && xhr.status < 300 && data
                    && data.success) {
                    resolve(data);
                } else if (xhr.status === 413) {
                    reject(new Error("That file is larger than this server "
                                     + "accepts in one request."));
                } else {
                    reject(new Error((data && data.message)
                                     || "Upload failed (HTTP " + xhr.status
                                     + ")."));
                }
            };

            xhr.send(form);
        });
    }

    // ----------------------------------------------------------------
    // The popup
    // ----------------------------------------------------------------

    function UploadPopup(trigger) {
        this.trigger = trigger;
        this.mode = trigger.dataset.uploadMode || "multipart";
        this.endpoints = JSON.parse(trigger.dataset.uploadEndpoints || "{}");
        this.accept = trigger.dataset.uploadAccept || "";
        this.title = trigger.dataset.uploadTitle || "Upload files";
        this.reload = trigger.dataset.uploadReload === "true";
        this.stagingTarget = trigger.dataset.uploadTarget || "";
        this.extraFields = JSON.parse(trigger.dataset.uploadFields || "{}");

        /*
            What Done does, which is a separate question from how the bytes
            travel. "server" posts the batch to a commit endpoint; "form"
            writes the results into hidden inputs for the surrounding form
            to carry. Reference files are the only ones that need "form" -
            they have no task to commit against yet - so the transport's
            usual partner is the default and either can be overridden.
        */
        this.commitMode = trigger.dataset.uploadCommit
            || (this.mode === "direct" ? "form" : "server");

        this.rows = [];
        this.running = false;
        this.open = false;
        this.build();

        var self = this;
        trigger.addEventListener("click", function (event) {
            event.preventDefault();
            self.show();
        });
    }

    UploadPopup.prototype.build = function () {
        var self = this;

        var overlay = document.createElement("div");
        overlay.className = "uploader-overlay";
        overlay.setAttribute("role", "dialog");
        overlay.setAttribute("aria-modal", "true");
        overlay.setAttribute("aria-label", this.title);
        overlay.innerHTML =
            '<div class="uploader-box">'
            + '<div class="uploader-head">'
            + '<h3></h3>'
            + '<button type="button" class="uploader-x" aria-label="Close">'
            + '<i class="fa-solid fa-xmark"></i></button>'
            + '</div>'
            + '<div class="uploader-drop" tabindex="0">'
            + '<i class="fa-solid fa-cloud-arrow-up"></i>'
            + '<p><strong>Drag &amp; drop files here</strong></p>'
            + '<p class="uploader-drop-sub">or</p>'
            + '<button type="button" class="btn btn-secondary uploader-pick">'
            + 'Choose files</button>'
            + '</div>'
            + '<div class="uploader-rows"></div>'
            + '<div class="uploader-foot">'
            + '<span class="uploader-status"></span>'
            + '<div class="uploader-foot-actions">'
            + '<button type="button" class="btn btn-secondary uploader-close">'
            + 'Cancel</button>'
            + '<button type="button" class="btn uploader-done" disabled>'
            + 'Done</button>'
            + '</div></div></div>';

        overlay.querySelector("h3").textContent = this.title;

        this.overlay = overlay;
        this.box = overlay.querySelector(".uploader-box");
        this.drop = overlay.querySelector(".uploader-drop");
        this.rowsEl = overlay.querySelector(".uploader-rows");
        this.statusEl = overlay.querySelector(".uploader-status");
        this.doneBtn = overlay.querySelector(".uploader-done");

        // Its own input, so the popup does not depend on the page's
        // original <input> still being around.
        var input = document.createElement("input");
        input.type = "file";
        input.multiple = true;
        input.hidden = true;
        if (this.accept) input.accept = this.accept;
        this.input = input;
        overlay.appendChild(input);

        overlay.querySelector(".uploader-pick").addEventListener(
            "click", function () { input.click(); });

        input.addEventListener("change", function () {
            self.add(Array.prototype.slice.call(input.files));
            // Reset, so choosing the same file twice in a row still fires.
            input.value = "";
        });

        ["dragenter", "dragover"].forEach(function (name) {
            self.drop.addEventListener(name, function (event) {
                event.preventDefault();
                self.drop.classList.add("is-over");
            });
        });
        ["dragleave", "drop"].forEach(function (name) {
            self.drop.addEventListener(name, function (event) {
                event.preventDefault();
                self.drop.classList.remove("is-over");
            });
        });
        self.drop.addEventListener("drop", function (event) {
            var dropped = event.dataTransfer && event.dataTransfer.files;
            if (dropped && dropped.length) {
                self.add(Array.prototype.slice.call(dropped));
            }
        });

        // The whole overlay swallows drops too, so a near-miss outside the
        // dashed box does not make the browser navigate to the file.
        overlay.addEventListener("dragover", function (e) {
            e.preventDefault();
        });
        overlay.addEventListener("drop", function (e) { e.preventDefault(); });

        overlay.querySelector(".uploader-x")
            .addEventListener("click", function () { self.requestClose(); });
        overlay.querySelector(".uploader-close")
            .addEventListener("click", function () { self.requestClose(); });
        overlay.addEventListener("click", function (event) {
            if (event.target === overlay) self.requestClose();
        });

        this.doneBtn.addEventListener("click", function () {
            self.commit();
        });

        this.onKey = function (event) {
            if (event.key === "Escape" && self.open) {
                event.preventDefault();
                self.requestClose();
            }
        };

        document.body.appendChild(overlay);
    };

    UploadPopup.prototype.show = function () {
        this.open = true;
        this.overlay.classList.add("show");
        document.addEventListener("keydown", this.onKey);
        // Opening straight into the picker is what people expect from a
        // button labelled Upload; the drop zone stays for the second file.
        if (!this.rows.length) this.input.click();
    };

    UploadPopup.prototype.hide = function () {
        this.open = false;
        this.overlay.classList.remove("show");
        document.removeEventListener("keydown", this.onKey);
    };

    UploadPopup.prototype.add = function (files) {
        var self = this;
        files.forEach(function (file) { self.addRow(file); });
        self.pump();
        self.refresh();
    };

    UploadPopup.prototype.addRow = function (file) {
        var self = this;

        var row = {
            file: file,
            state: "waiting",
            controller: null,
            result: null,
            el: document.createElement("div")
        };

        row.el.className = "uploader-row";
        row.el.innerHTML =
            '<div class="uploader-row-main">'
            + '<span class="uploader-row-name"></span>'
            + '<span class="uploader-row-meta"></span>'
            + '<div class="uploader-bar"><span></span></div>'
            + '</div>'
            + '<button type="button" class="uploader-row-x" '
            + 'aria-label="Cancel this file">'
            + '<i class="fa-solid fa-xmark"></i></button>';

        row.el.querySelector(".uploader-row-name").textContent = file.name;
        row.bar = row.el.querySelector(".uploader-bar span");
        row.meta = row.el.querySelector(".uploader-row-meta");
        row.meta.textContent = humanSize(file.size) + " · Waiting";

        row.el.querySelector(".uploader-row-x")
            .addEventListener("click", function () { self.remove(row); });

        this.rows.push(row);
        this.rowsEl.appendChild(row.el);
        return row;
    };

    /*
        x, and what it means depends on where the file got to:
          uploading -> abort the request; nothing was ever stored
          done      -> the bytes ARE in storage, so tell the server to
                       delete them. Cancel has to mean cancel, otherwise
                       an abandoned file quietly stays attached.
    */
    UploadPopup.prototype.remove = function (row) {
        var self = this;

        if (row.state === "uploading" && row.controller) {
            row.controller.abort();
            // The queue's catch drops the row; nothing more to do here.
            return Promise.resolve();
        }

        var discarded = Promise.resolve();
        if (row.state === "done" && row.result) {
            row.meta.textContent = "Removing…";
            discarded = this.discard(row.result).catch(function (error) {
                console.error("Could not discard upload:", error);
            });
        }

        return discarded.then(function () {
            self.drop_(row);
        });
    };

    UploadPopup.prototype.drop_ = function (row) {
        var index = this.rows.indexOf(row);
        if (index !== -1) this.rows.splice(index, 1);
        if (row.el.parentNode) row.el.parentNode.removeChild(row.el);
        this.refresh();
    };

    /* Two shapes of discard, matching what the upload produced: a row with
       an id, or a staged object with a key. */
    UploadPopup.prototype.discard = function (result) {
        if (result.file_id != null) {
            return postJSON(
                this.endpoints.discard.replace("__ID__", result.file_id), {});
        }
        return postJSON(this.endpoints.discard,
                        { object_key: result.object_key });
    };

    /* Values the page owns, read fresh for every file. */
    UploadPopup.prototype.readExtra = function () {
        var out = {};
        var fields = this.extraFields;
        Object.keys(fields).forEach(function (name) {
            var el = document.querySelector(fields[name]);
            if (el) out[name] = el.value;
        });
        return out;
    };

    /* One at a time. */
    UploadPopup.prototype.pump = function () {
        var self = this;
        if (this.running) return;

        var next = this.rows.filter(function (r) {
            return r.state === "waiting";
        })[0];
        if (!next) { this.refresh(); return; }

        this.running = true;
        next.state = "uploading";
        next.controller = new AbortController();
        next.meta.textContent = humanSize(next.file.size) + " · 0%";
        next.el.classList.add("is-uploading");
        this.refresh();

        function onProgress(loaded) {
            var pct = next.file.size
                ? Math.min(100, (loaded / next.file.size) * 100)
                : 100;
            next.bar.style.width = pct + "%";
            next.meta.textContent = humanSize(next.file.size) + " · "
                + pct.toFixed(0) + "%";
        }

        var transport = this.mode === "multipart"
            ? multipartUpload : directUpload;

        transport(this.endpoints, next.file, onProgress,
                  next.controller.signal, this.readExtra())
            .then(function (result) {
                next.state = "done";
                next.result = result;
                next.controller = null;
                next.bar.style.width = "100%";
                next.el.classList.remove("is-uploading");
                next.el.classList.add("is-done");
                next.meta.textContent = humanSize(next.file.size)
                    + " · Uploaded";
            })
            .catch(function (error) {
                next.controller = null;
                if (wasCancelled(error)) {
                    // Cancelling means the row goes away entirely - a
                    // cancelled row left sitting there reads as failed.
                    self.drop_(next);
                    return;
                }
                next.state = "error";
                next.el.classList.remove("is-uploading");
                next.el.classList.add("is-error");
                next.bar.style.width = "0%";
                next.meta.textContent = error && error.message
                    ? error.message : "Upload failed.";
                console.error("Upload failed:", error);
            })
            .then(function () {
                self.running = false;
                self.refresh();
                self.pump();
            });
    };

    UploadPopup.prototype.counts = function () {
        var out = { waiting: 0, uploading: 0, done: 0, error: 0 };
        this.rows.forEach(function (r) { out[r.state] += 1; });
        return out;
    };

    UploadPopup.prototype.refresh = function () {
        var c = this.counts();
        var busy = c.uploading + c.waiting;

        // Done only once nothing is still moving - otherwise it would
        // commit a batch that is half uploaded.
        this.doneBtn.disabled = busy > 0 || c.done === 0;

        this.overlay.classList.toggle("has-rows", this.rows.length > 0);

        if (busy) {
            this.statusEl.textContent = busy + " file"
                + (busy === 1 ? "" : "s") + " to go"
                + (c.done ? " · " + c.done + " uploaded" : "");
        } else if (c.done) {
            this.statusEl.textContent = c.done + " file"
                + (c.done === 1 ? "" : "s") + " ready"
                + (c.error ? " · " + c.error + " failed" : "");
        } else if (c.error) {
            this.statusEl.textContent = c.error + " failed";
        } else {
            this.statusEl.textContent = "";
        }
    };

    /*
        Closing throws the batch away, which is what was asked for - but
        silently binning finished uploads on a stray Escape is the kind of
        thing you only forgive once. So when there is something to lose,
        the first close asks.
    */
    UploadPopup.prototype.requestClose = function () {
        var c = this.counts();

        if ((c.done || c.uploading || c.waiting) && !this.confirming) {
            this.confirming = true;
            this.box.classList.add("is-confirming");
            var self = this;

            var bar = document.createElement("div");
            bar.className = "uploader-confirm";
            bar.innerHTML =
                '<span></span>'
                + '<button type="button" class="btn btn-sm btn-secondary '
                + 'uploader-confirm-no">Keep them</button>'
                + '<button type="button" class="btn btn-sm btn-danger '
                + 'uploader-confirm-yes">Discard</button>';
            bar.querySelector("span").textContent =
                "Close and discard " + (c.done + c.uploading + c.waiting)
                + " file(s)?";

            bar.querySelector(".uploader-confirm-no")
                .addEventListener("click", function () {
                    self.confirming = false;
                    self.box.classList.remove("is-confirming");
                    bar.remove();
                });
            bar.querySelector(".uploader-confirm-yes")
                .addEventListener("click", function () {
                    bar.remove();
                    self.confirming = false;
                    self.box.classList.remove("is-confirming");
                    self.cancelAll();
                });

            this.box.appendChild(bar);
            return;
        }

        this.cancelAll();
    };

    UploadPopup.prototype.cancelAll = function () {
        var self = this;
        var rows = this.rows.slice();

        this.hide();

        // Aborts first so nothing new starts while the discards run.
        rows.forEach(function (row) {
            if (row.state === "uploading" && row.controller) {
                row.controller.abort();
            }
            row.state = "cancelled";
        });

        Promise.all(rows.map(function (row) {
            if (!row.result) return Promise.resolve();
            return self.discard(row.result).catch(function (error) {
                // The storage GC sweeps whatever this misses, so a failed
                // discard must not stop the popup from closing.
                console.error("Could not discard upload:", error);
            });
        })).then(function () {
            self.reset();
        });
    };

    UploadPopup.prototype.reset = function () {
        this.rows = [];
        this.rowsEl.innerHTML = "";
        this.running = false;
        this.confirming = false;
        this.refresh();
    };

    /*
        Done. This is the only path that keeps anything, and for
        submissions it is also the only thing that tells anyone: the
        per-file uploads are deliberately silent, so a five-file batch
        produces one activity entry and one notification rather than five.
    */
    UploadPopup.prototype.commit = function () {
        var self = this;
        var kept = this.rows.filter(function (r) { return r.state === "done"; });
        if (!kept.length) return;

        this.doneBtn.disabled = true;
        this.statusEl.textContent = "Finishing…";

        if (this.commitMode === "form") {
            this.writeStaged(kept);
            this.rows = [];        // handed to the form; not ours to discard
            this.reset();
            this.hide();
            return;
        }

        postJSON(this.endpoints.commit, {
            file_ids: kept.map(function (r) { return r.result.file_id; })
        }).then(function () {
            self.rows = [];
            self.hide();
            if (self.reload) window.location.reload();
        }).catch(function (error) {
            self.doneBtn.disabled = false;
            self.statusEl.textContent = error && error.message
                ? error.message : "Could not finish the upload.";
        });
    };

    /* Reference mode: the staged keys ride along as hidden JSON on the
       create form, and the create route turns them into TaskFile rows. */
    UploadPopup.prototype.writeStaged = function (kept) {
        var target = this.stagingTarget
            ? document.querySelector(this.stagingTarget) : null;
        if (!target) return;

        var existing = [];
        var field = target.querySelector('input[name="staged_reference_files"]');
        if (field) {
            try { existing = JSON.parse(field.value) || []; } catch (e) { }
        } else {
            field = document.createElement("input");
            field.type = "hidden";
            field.name = "staged_reference_files";
            target.appendChild(field);
        }

        kept.forEach(function (row) {
            existing.push({
                object_key: row.result.object_key,
                original_filename: row.result.original_filename,
                mime_type: row.result.mime_type,
                file_size: row.result.file_size,
                bucket_name: row.result.bucket_name
            });
        });
        field.value = JSON.stringify(existing);

        var list = target.querySelector("[data-staged-list]");
        if (list) {
            list.innerHTML = "";
            existing.forEach(function (item) {
                var chip = document.createElement("span");
                chip.className = "uploader-chip";
                chip.textContent = item.original_filename;
                list.appendChild(chip);
            });
        }
    };

    // ----------------------------------------------------------------

    function init() {
        document.querySelectorAll("[data-upload-popup]").forEach(
            function (trigger) {
                if (trigger.dataset.uploadReady) return;
                trigger.dataset.uploadReady = "1";
                new UploadPopup(trigger);

                // The plain control this replaces. Hidden only now, so a
                // page whose JavaScript failed to load still has a working
                // upload rather than no upload at all.
                var replaces = trigger.dataset.uploadReplaces;
                if (replaces) {
                    document.querySelectorAll(replaces).forEach(function (el) {
                        // Both: `hidden` for assistive tech, and the class
                        // because author rules like `form{display:flex}`
                        // outrank the user agent's [hidden] no matter the
                        // specificity, so `hidden` alone hides nothing.
                        el.hidden = true;
                        el.classList.add("uploader-replaced");
                    });
                }
            });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
    document.addEventListener("turbo:load", init);
})();
