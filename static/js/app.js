/* LifeOS — small progressive enhancements.
 *
 * Every page works without this file: forms post and redirect as normal.
 * With it, a few forms update part of the page in place instead of
 * reloading, search filters as you type, and sheets open as dialogs.
 *
 *   <form data-swap="#target">   posts in the background and replaces
 *                                #target with the HTML the server returns
 *                                (the view sees an X-Fragment header).
 *   <form data-live="#target">   GET form: re-fetches #target as you type.
 *   <button data-open-dialog="id">   opens <dialog id="id"> as a modal.
 *   <button data-close-dialog>       closes the dialog it sits in.
 */
(function () {
  "use strict";

  var ICONS = {success: "check", info: "alert", warning: "alert", error: "alert"};

  function svgIcon(name) {
    var ns = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(ns, "svg");
    svg.setAttribute("class", "icon");
    svg.setAttribute("width", "20");
    svg.setAttribute("height", "20");
    svg.setAttribute("aria-hidden", "true");
    var use = document.createElementNS(ns, "use");
    use.setAttribute("href", "#i-" + name);
    svg.appendChild(use);
    return svg;
  }

  function dismissLater(toast) {
    window.setTimeout(function () { toast.remove(); }, 5000);
  }

  function showToast(text, level) {
    var region = document.getElementById("toasts");
    if (!region || !text) return;
    level = level || "success";
    var toast = document.createElement("div");
    toast.className = "toast " + level;
    toast.appendChild(svgIcon(ICONS[level] || "check"));
    var body = document.createElement("span");
    body.className = "toast-text";
    body.textContent = text;
    toast.appendChild(body);
    region.appendChild(toast);
    dismissLater(toast);
  }

  function toastFrom(response) {
    var text = response.headers.get("X-Toast");
    if (text) showToast(decodeURIComponent(text), response.headers.get("X-Toast-Level") || "success");
  }

  function replace(target, html) {
    var template = document.createElement("template");
    template.innerHTML = html.trim();
    var fresh = template.content.firstElementChild;
    if (!fresh) return null;
    target.replaceWith(fresh);
    return fresh;
  }

  // Background form posts ----------------------------------------------------

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!(form instanceof HTMLFormElement)) return;

    if (form.dataset.live) {
      event.preventDefault();
      liveSearch(form);
      return;
    }
    if (!form.dataset.swap) return;
    var target = document.querySelector(form.dataset.swap);
    if (!target) return;
    event.preventDefault();

    var buttons = form.querySelectorAll("button");
    buttons.forEach(function (button) { button.disabled = true; });
    var refocus = form.dataset.refocus;

    fetch(form.action, {
      method: "POST",
      body: new FormData(form, event.submitter || undefined),
      headers: {"X-Fragment": "1"},
      credentials: "same-origin"
    }).then(function (response) {
      if (!response.ok) throw new Error("Request failed");
      toastFrom(response);
      return response.text();
    }).then(function (html) {
      var swapped = replace(target, html);
      syncProposalGroup(swapped);
      if (refocus) {
        var field = document.querySelector(refocus);
        if (field) field.focus();
      }
    }).catch(function () {
      // Fall back to an ordinary post so nothing is lost.
      form.removeAttribute("data-swap");
      form.submit();
    });
  });

  function syncProposalGroup(node) {
    if (!node) return;
    var group = node.closest("[data-proposal-group]");
    if (!group) return;
    var pending = group.querySelectorAll('[data-proposal-status="pending"]').length;
    var addAll = group.querySelector("[data-add-all-form]");
    if (addAll && pending < 2) addAll.remove();
  }

  // Search as you type --------------------------------------------------------

  var liveTimer;
  var liveRequest = 0;

  function liveSearch(form) {
    var target = document.querySelector(form.dataset.live);
    if (!target) return;
    var params = new URLSearchParams(new FormData(form));
    Array.from(params.keys()).forEach(function (key) {
      if (!params.get(key)) params.delete(key);
    });
    var query = params.toString();
    var url = form.action.split("?")[0] + (query ? "?" + query : "");
    var thisRequest = ++liveRequest;
    fetch(url, {headers: {"X-Fragment": "1"}, credentials: "same-origin"})
      .then(function (response) {
        if (!response.ok) throw new Error("Request failed");
        return response.text();
      })
      .then(function (html) {
        if (thisRequest !== liveRequest) return;
        var current = document.querySelector(form.dataset.live);
        if (current) replace(current, html);
        window.history.replaceState(null, "", url);
      })
      .catch(function () { form.removeAttribute("data-live"); form.submit(); });
  }

  document.addEventListener("input", function (event) {
    var form = event.target.form;
    if (!form || !form.dataset.live || event.target.type !== "search") return;
    window.clearTimeout(liveTimer);
    liveTimer = window.setTimeout(function () { liveSearch(form); }, 250);
  });

  // Sheets ------------------------------------------------------------------

  document.addEventListener("click", function (event) {
    var opener = event.target.closest("[data-open-dialog]");
    if (opener) {
      var dialog = document.getElementById(opener.dataset.openDialog);
      if (dialog && typeof dialog.showModal === "function") {
        // preventDefault also stops a native command="show-modal" opening it twice.
        event.preventDefault();
        if (!dialog.open) dialog.showModal();
      }
      return;
    }
    var closer = event.target.closest("[data-close-dialog]");
    if (closer) {
      var parent = closer.closest("dialog");
      if (parent) { event.preventDefault(); parent.close(); }
    }
  });

  // Tap outside a sheet to close it (Safari has no closedby="any" yet).
  function outside(event, dialog) {
    var rect = dialog.getBoundingClientRect();
    return event.clientX < rect.left || event.clientX > rect.right ||
      event.clientY < rect.top || event.clientY > rect.bottom;
  }
  document.querySelectorAll("dialog.sheet").forEach(function (dialog) {
    var pressedOutside = false;
    dialog.addEventListener("pointerdown", function (event) { pressedOutside = outside(event, dialog); });
    dialog.addEventListener("pointerup", function (event) {
      if (pressedOutside && outside(event, dialog)) dialog.close();
    });
  });

  // Server-rendered messages and highlighted rows --------------------------------

  document.querySelectorAll(".toast").forEach(dismissLater);
  document.addEventListener("click", function (event) {
    var close = event.target.closest(".toast-close");
    if (close) close.closest(".toast").remove();
  });

  var highlighted = document.querySelector(".is-highlighted");
  if (highlighted) highlighted.scrollIntoView({block: "center"});

  // Ask chat ------------------------------------------------------------------

  function initAskChat() {
    var root = document.querySelector("[data-ask-chat]");
    if (!root) return;

    var messages = document.getElementById("askMessages");
    var form = document.getElementById("askForm");
    var input = document.getElementById("askInput");
    var fileInput = document.getElementById("askFile");
    var filePill = document.getElementById("askFilePill");
    var fileName = document.getElementById("askFileName");
    var fileClear = document.getElementById("askFileClear");
    var sendBtn = document.getElementById("askSendBtn");
    var updatesUrl = root.dataset.updatesUrl;
    var lastMessageId = parseInt(root.dataset.lastMessageId || "0", 10) || 0;
    var pollTimer = null;
    var polling = false;
    var sending = false;

    function appendHtml(html) {
      var template = document.createElement("template");
      template.innerHTML = html.trim();
      messages.appendChild(template.content);
    }

    function autoSize() {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 132) + "px";
    }

    function syncSendButton() {
      var hasText = input.value.trim().length > 0;
      var hasFile = !!(fileInput && fileInput.files && fileInput.files.length > 0);
      sendBtn.hidden = !(hasText || hasFile);
    }

    function clearAttachment() {
      if (fileInput) fileInput.value = "";
      if (filePill) filePill.hidden = true;
      if (fileName) fileName.textContent = "";
      syncSendButton();
    }

    function stopPolling() {
      polling = false;
      if (pollTimer) {
        window.clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function schedulePoll() {
      if (!polling) return;
      pollTimer = window.setTimeout(pollOnce, 1000);
    }

    function startPolling() {
      if (polling) return;
      polling = true;
      schedulePoll();
    }

    function pollOnce() {
      if (!polling) return;
      fetch(updatesUrl + "?after=" + encodeURIComponent(String(lastMessageId)), {
        headers: {"X-Fragment": "1"},
        credentials: "same-origin"
      }).then(function (response) {
        if (!response.ok) throw new Error("Poll failed");
        return response.json();
      }).then(function (data) {
        if (typeof data.last_message_id === "number" && data.last_message_id > lastMessageId) {
          lastMessageId = data.last_message_id;
          root.dataset.lastMessageId = String(lastMessageId);
        }
        if (data.html) {
          messages.querySelectorAll("[data-ask-typing], [data-ask-error]").forEach(function (el) { el.remove(); });
          appendHtml(data.html);
        }
        if (data.polling) {
          schedulePoll();
        } else {
          messages.querySelectorAll("[data-ask-typing]").forEach(function (el) { el.remove(); });
          stopPolling();
        }
      }).catch(function () {
        if (polling) pollTimer = window.setTimeout(pollOnce, 2500);
      });
    }

    input.addEventListener("input", function () {
      autoSize();
      syncSendButton();
    });

    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        // Block submission during IME composition (including Safari's keyCode 229 ordering quirk).
        if (event.isComposing || event.keyCode === 229) return;
        if (!sendBtn.hidden && !sending) form.requestSubmit();
      }
    });

    if (fileInput) {
      fileInput.addEventListener("change", function () {
        if (fileInput.files && fileInput.files.length > 0) {
          fileName.textContent = fileInput.files[0].name;
          filePill.hidden = false;
        } else {
          filePill.hidden = true;
        }
        syncSendButton();
      });
    }
    if (fileClear) {
      fileClear.addEventListener("click", clearAttachment);
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (sending) return;
      var hasText = input.value.trim().length > 0;
      var hasFile = !!(fileInput && fileInput.files && fileInput.files.length > 0);
      if (!hasText && !hasFile) return;

      sending = true;
      sendBtn.disabled = true;
      var payload = new FormData(form);

      fetch(form.action, {
        method: "POST",
        body: payload,
        headers: {"X-Fragment": "1"},
        credentials: "same-origin"
      }).then(function (response) {
        toastFrom(response);
        if (!response.ok) {
          return response.text().then(function (msg) { throw new Error(msg || "Could not send message."); });
        }
        return response.text();
      }).then(function (html) {
        var empty = document.getElementById("askEmpty");
        if (empty) empty.remove();
        messages.querySelectorAll("[data-ask-typing], [data-ask-error]").forEach(function (el) { el.remove(); });
        appendHtml(html);
        input.value = "";
        autoSize();
        clearAttachment();
        startPolling();
      }).catch(function (err) {
        if (err && err.message) showToast(err.message, "error");
      }).finally(function () {
        sending = false;
        sendBtn.disabled = false;
        syncSendButton();
      });
    });

    root.addEventListener("click", function (event) {
      var promptBtn = event.target.closest("[data-ask-prompt]");
      if (promptBtn) {
        event.preventDefault();
        input.value = promptBtn.dataset.askPrompt;
        autoSize();
        syncSendButton();
        form.requestSubmit();
        return;
      }
      var changeBtn = event.target.closest("[data-ask-change]");
      if (changeBtn) {
        event.preventDefault();
        input.value = changeBtn.dataset.askChange;
        autoSize();
        syncSendButton();
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      }
    });

    root.addEventListener("submit", function (event) {
      var targetForm = event.target;
      if (!(targetForm instanceof HTMLFormElement)) return;
      if (targetForm.hasAttribute("data-ask-new")) {
        event.preventDefault();
        stopPolling();
        fetch(targetForm.action, {
          method: "POST",
          body: new FormData(targetForm),
          headers: {"X-Fragment": "1"},
          credentials: "same-origin"
        }).then(function (response) {
          if (!response.ok) throw new Error("Failed");
          toastFrom(response);
          return response.text();
        }).then(function (html) {
          messages.innerHTML = html;
          lastMessageId = 0;
          root.dataset.lastMessageId = "0";
        }).catch(function () {
          targetForm.removeAttribute("data-ask-new");
          targetForm.submit();
        });
        return;
      }
      if (targetForm.hasAttribute("data-ask-retry")) {
        event.preventDefault();
        var errorBox = targetForm.closest("[data-ask-error]");
        fetch(targetForm.action, {
          method: "POST",
          body: new FormData(targetForm),
          headers: {"X-Fragment": "1"},
          credentials: "same-origin"
        }).then(function (response) {
          toastFrom(response);
          if (!response.ok) {
            return response.text().then(function (msg) {
              var err = new Error(msg || "Could not retry.");
              err.status = response.status;
              throw err;
            });
          }
          return response.text();
        }).then(function (html) {
          if (errorBox) replace(errorBox, html);
          else appendHtml(html);
          startPolling();
        }).catch(function (err) {
          if (err && err.status === 409) {
            if (errorBox) errorBox.remove();
            return;
          }
          targetForm.removeAttribute("data-ask-retry");
          targetForm.submit();
        });
      }
    });

    autoSize();
    syncSendButton();
    if (root.dataset.polling === "true") {
      startPolling();
    }
  }

  initAskChat();

  window.FamilyApp = {toast: showToast};
}());
