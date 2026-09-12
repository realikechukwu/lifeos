/* global FullCalendar, bootstrap */
(function () {
  "use strict";
  document.addEventListener("DOMContentLoaded", function () {
    var root = document.getElementById("fullcalendar-root");
    if (!root) return;
    var status = document.getElementById("calendarLoadStatus");
    var error = document.getElementById("calendarLoadError");
    var retry = document.getElementById("calendarRetry");
    if (typeof FullCalendar === "undefined" || typeof bootstrap === "undefined") {
      error.hidden = false;
      error.querySelector("span").textContent = "Calendar could not start. Reload to try again.";
      retry.textContent = "Reload";
      retry.addEventListener("click", function () { window.location.reload(); });
      return;
    }
    var zone = root.dataset.timeZone || "Europe/London";
    var today;
    var loading = false;
    var failed = false;
    var loaded = false;
    var revealToday = true;
    var focusToday = false;
    var previousView;
    var selected = new Set(["event", "work", "task", "reminder"]);
    var cachedRange = null;
    var cachedEvents = [];
    var requestNumber = 0;
    var categories = {"Calendar event": "event", "Ike work shift": "work", "Task due date": "task", "Reminder": "reminder"};
    var modalEl = document.getElementById("eventDetailModal");
    var modal = new bootstrap.Modal(modalEl);
    var returnFocus;

    function wallClock(now) {
      var parts = new Intl.DateTimeFormat("en-GB", {
        timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"
      }).formatToParts(now);
      var values = {};
      parts.forEach(function (part) { values[part.type] = part.value; });
      return values.year + "-" + values.month + "-" + values.day + "T" + values.hour + ":" + values.minute + ":" + values.second;
    }
    function updateToday() {
      var now = new Date();
      today = wallClock(now).slice(0, 10);
      var dateEl = document.getElementById("calendarTodayDate");
      dateEl.dateTime = today;
      dateEl.textContent = new Intl.DateTimeFormat("en-GB", {
        timeZone: zone, weekday: "short", day: "numeric", month: "short"
      }).format(now);
    }
    updateToday();

    function visibleEvents() {
      return cachedEvents.filter(function (event) {
        var props = event.extendedProps || {};
        return selected.has(props.category || categories[props.type] || "event");
      });
    }
    function inCurrentRange() {
      var date = new Date(today + "T00:00:00Z");
      return calendar.view.activeStart <= date && date < calendar.view.activeEnd;
    }
    function updateList() {
      root.querySelectorAll(".fc-list-day").forEach(function (header) {
        var current = header.dataset.date === today;
        header.classList.toggle("is-today", current);
        if (current) header.setAttribute("aria-current", "date");
        else header.removeAttribute("aria-current");
      });
      var summary = document.getElementById("calendarTodaySummary");
      if (loaded && !loading && !failed && inCurrentRange()) {
        var matching = calendar.getEvents().filter(function (event) {
          var start = event.startStr.slice(0, 10);
          var end = event.endStr && event.endStr.slice(0, 10);
          return start === today || (start < today && end &&
            (end > today || (end === today && !event.allDay && !event.endStr.includes("T00:00:00"))));
        });
        summary.textContent = matching.length ? matching.length + " item" + (matching.length === 1 ? "" : "s") + " today" :
          (selected.size === 4 ? "Nothing scheduled today" : "No matching items today");
      } else summary.textContent = "";
      if (!revealToday || loading || failed || !loaded || !inCurrentRange()) return;
      revealToday = false;
      var target = root;
      if (calendar.view.type.indexOf("list") === 0) {
        target = root.querySelector('.fc-list-day[data-date="' + today + '"]');
        if (!target) target = Array.from(root.querySelectorAll(".fc-list-day")).find(function (header) {
          return header.dataset.date > today;
        });
        target = target || document.getElementById("calendarTodayStrip");
      }
      if (target && (focusToday || target !== root)) {
        var scroller = target.closest(".fc-scroller");
        if (scroller) {
          scroller.scrollTop += target.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
        } else if (focusToday) {
          target.scrollIntoView({block: "nearest", behavior: "instant"});
        }
        if (focusToday) {
          target.setAttribute("tabindex", "-1");
          target.focus({preventScroll: true});
        }
      }
      focusToday = false;
    }
    function afterRender() { window.requestAnimationFrame(updateList); }
    function jumpToday() {
      updateToday();
      revealToday = true;
      focusToday = true;
      calendar.gotoDate(today);
      afterRender();
    }
    function detailRow(id, value) {
      var element = document.getElementById(id);
      element.textContent = value || "";
      element.closest(".detail-row").hidden = !value;
    }
    // FullCalendar 6's named-zone UTC coercion exposes wall-clock Date objects.
    // Its own formatter preserves them; browser-local formatting adds an offset.
    function formatWhen(event) {
      var dateFormat = {weekday: "short", day: "numeric", month: "short", year: "numeric"};
      var timeFormat = {hour: "2-digit", minute: "2-digit", hour12: false};
      var firstDay = calendar.formatDate(event.start, dateFormat);
      if (event.allDay) {
        var finalDay = event.end ? calendar.formatDate(new Date(event.end.valueOf() - 1), dateFormat) : firstDay;
        return firstDay + (finalDay !== firstDay ? " – " + finalDay : "") + " · All day";
      }
      var text = firstDay + " · " + calendar.formatDate(event.start, timeFormat);
      if (event.end) {
        var endDay = calendar.formatDate(event.end, dateFormat);
        text += " – " + (endDay !== firstDay ? endDay + " · " : "") + calendar.formatDate(event.end, timeFormat);
      }
      return text;
    }
    function detailLink(id, url) {
      var link = document.getElementById(id);
      link.classList.toggle("d-none", !url);
      if (url) link.href = url;
      else link.removeAttribute("href");
    }
    function openDetails(event, element) {
      var props = event.extendedProps;
      returnFocus = element;
      document.getElementById("eventDetailTitle").textContent = event.title;
      detailRow("eventDetailType", props.type || "Item");
      detailRow("eventDetailWhen", formatWhen(event));
      detailRow("eventDetailStatus", props.status);
      detailRow("eventDetailRecurrence", props.recurrenceDescription || (props.recurring ? "Repeats" : ""));
      detailRow("eventDetailAssignee", props.assignee);
      detailRow("eventDetailLocation", props.location);
      detailLink("eventDetailItemLink", props.detailUrl);
      detailLink("eventDetailGoogleLink", props.googleUrl);
      modal.show();
    }
    modalEl.addEventListener("hidden.bs.modal", function () {
      if (returnFocus && returnFocus.isConnected) returnFocus.focus({preventScroll: true});
    });

    var calendar = new FullCalendar.Calendar(root, {
      timeZone: zone,
      locale: "en-gb",
      initialDate: today,
      headerToolbar: {left: "prev,next", center: "title", right: "dayGridMonth,timeGridWeek,listMonth"},
      buttonText: {month: "Month", week: "Week", list: "List"},
      initialView: window.matchMedia("(max-width: 575.98px)").matches ? "listMonth" : "dayGridMonth",
      firstDay: 1,
      height: "auto",
      dayMaxEventRows: 3,
      nowIndicator: true,
      now: function () { return wallClock(new Date()); },
      noEventsText: "No matching items in this period.",
      eventTimeFormat: {hour: "2-digit", minute: "2-digit", hour12: false},
      slotLabelFormat: {hour: "2-digit", minute: "2-digit", hour12: false},
      listDayFormat: {weekday: "short", day: "numeric", month: "short"},
      listDaySideFormat: false,
      events: function (info, success, failure) {
        var thisRequest = ++requestNumber;
        var rangeKey = info.startStr + "/" + info.endStr;
        failed = false;
        error.hidden = true;
        root.classList.remove("has-load-error");
        if (cachedRange === rangeKey) { success(visibleEvents()); return; }
        var url = new URL(root.dataset.eventsUrl, window.location.origin);
        url.searchParams.set("start", info.startStr);
        url.searchParams.set("end", info.endStr);
        fetch(url, {headers: {Accept: "application/json"}}).then(function (response) {
          if (!response.ok) throw new Error("Calendar request failed");
          return response.json();
        }).then(function (events) {
          if (!Array.isArray(events)) throw new Error("Invalid calendar response");
          if (thisRequest !== requestNumber) { success([]); return; }
          cachedRange = rangeKey;
          cachedEvents = events;
          loaded = true;
          success(visibleEvents());
        }).catch(function (reason) {
          if (thisRequest !== requestNumber) { failure(reason); return; }
          failed = true;
          error.hidden = false;
          root.classList.add("has-load-error");
          failure(reason);
        });
      },
      loading: function (isLoading) {
        loading = isLoading;
        status.hidden = !isLoading;
        status.textContent = isLoading ? "Loading calendar…" : "";
        root.classList.toggle("is-loading", isLoading);
        root.setAttribute("aria-busy", String(isLoading));
        afterRender();
      },
      datesSet: function (info) {
        if (previousView !== info.view.type) revealToday = true;
        previousView = info.view.type;
        if (info.startStr.slice(0, 10) > today || info.endStr.slice(0, 10) <= today) revealToday = false;
        afterRender();
      },
      eventsSet: afterRender,
      eventClick: function (info) { info.jsEvent.preventDefault(); openDetails(info.event, info.el); },
      eventDidMount: function (info) {
        info.el.tabIndex = 0;
        info.el.setAttribute("role", "button");
        info.el.setAttribute("aria-haspopup", "dialog");
        info.el.setAttribute("aria-label", info.event.title + ", " + formatWhen(info.event) + ". Open details");
        info.el.addEventListener("keydown", function (event) {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openDetails(info.event, info.el); }
        });
        if (info.view.type.indexOf("list") === 0) {
          var title = info.el.querySelector(".fc-list-event-title");
          if (title) {
            var meta = document.createElement("span");
            meta.className = "calendar-event-meta";
            meta.textContent = info.event.extendedProps.type || "";
            var eventStatus = info.event.extendedProps.status;
            if (info.event.extendedProps.isOverdue) eventStatus = "Overdue";
            if (eventStatus && eventStatus !== "Scheduled") meta.textContent += " · " + eventStatus;
            title.appendChild(meta);
          }
        }
      }
    });
    document.getElementById("calendarJumpToday").addEventListener("click", jumpToday);
    retry.addEventListener("click", function () { cachedRange = null; calendar.refetchEvents(); });
    document.querySelectorAll("[data-calendar-category]").forEach(function (button) {
      button.addEventListener("click", function () {
        var category = button.dataset.calendarCategory;
        if (selected.has(category)) selected.delete(category);
        else selected.add(category);
        button.classList.toggle("active", selected.has(category));
        button.setAttribute("aria-pressed", String(selected.has(category)));
        revealToday = false;
        calendar.refetchEvents();
      });
    });
    // FullCalendar remeasures on resize without replacing the selected view.
    function checkDate() {
      var previousDay = today;
      updateToday();
      if (today !== previousDay) { cachedRange = null; calendar.refetchEvents(); }
      afterRender();
    }
    window.setInterval(checkDate, 60000);
    document.addEventListener("visibilitychange", function () { if (!document.hidden) checkDate(); });
    calendar.render();
  });
}());
