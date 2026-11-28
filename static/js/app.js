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
      replace(target, html);
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

  window.FamilyApp = {toast: showToast};
}());
