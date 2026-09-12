// Run with: node --test web/tests/calendar_ui.test.cjs
// Exercises the application controller with FullCalendar's public API mocked.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const source = readFileSync(new URL('../../static/js/calendar.js', 'file://' + __dirname + '/'), 'utf8');

class Element {
  constructor(dataset = {}) {
    this.dataset = dataset;
    this.hidden = true;
    this.handlers = {};
    this.attrs = {};
    this.textContent = '';
    this.isConnected = true;
    this.scrollTop = 0;
    this.top = 0;
    this.children = [];
    this.classes = new Set();
    this.classList = {
      toggle: (key, value) => value ? this.classes.add(key) : this.classes.delete(key),
      add: key => this.classes.add(key), remove: key => this.classes.delete(key)
    };
  }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  setAttribute(key, value) { this.attrs[key] = value; }
  removeAttribute(key) { delete this.attrs[key]; }
  getAttribute(key) { return this.attrs[key]; }
  closest(selector) { return selector === '.fc-scroller' ? this.scroller || null : this; }
  querySelector() { return this.child || this; }
  querySelectorAll() { return this.children; }
  appendChild(child) { this.children.push(child); }
  getBoundingClientRect() { return {top: this.top}; }
  scrollIntoView() { this.scrolled = true; }
  focus() { this.focused = true; }
  click() { this.handlers.click(); }
}

function harness(fetcher) {
  const ids = {};
  const el = id => ids[id] ||= new Element();
  const root = el('fullcalendar-root');
  root.dataset = {eventsUrl: '/events', timeZone: 'Europe/London'};
  const filters = ['event', 'work', 'task', 'reminder'].map(category => new Element({calendarCategory: category}));
  let calendar;
  let ready;
  const frames = [];
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : ['2026-09-12T10:00:00Z'])); }
  }
  let modalShows = 0;
  let fetches = 0;
  const document = {
    getElementById: el, querySelectorAll: () => filters,
    addEventListener: (event, fn) => { if (event === 'DOMContentLoaded') ready = fn; },
    createElement: () => new Element()
  };
  const range = {startStr: '2026-09-01T00:00:00', endStr: '2026-10-01T00:00:00'};
  class Calendar {
    constructor(element, options) {
      calendar = this;
      this.options = options;
      this.events = [];
      this.view = {type: 'listMonth', activeStart: new Date('2026-09-01'), activeEnd: new Date('2026-10-01')};
    }
    render() {}
    getEvents() { return this.events; }
    gotoDate(day) { this.goto = day; }
    formatDate(date, options) { return new Intl.DateTimeFormat('en-GB', {...options, timeZone: 'UTC'}).format(date); }
    refetchEvents() { request(); }
  }
  const context = {
    document, Date: FixedDate, Intl, Set, URL, console,
    FullCalendar: {Calendar}, bootstrap: {Modal: class { show() { modalShows++; } }},
    fetch: (...args) => { fetches++; return fetcher(...args); },
    window: {location: {origin: 'http://localhost'}, matchMedia: () => ({matches: true}),
      requestAnimationFrame: fn => frames.push(fn), setInterval() {}}
  };
  vm.runInNewContext(source, context);
  ready();
  function request(info = range) {
    calendar.options.loading(true);
    calendar.options.events(info, events => {
      calendar.events = events.map(event => ({...event, startStr: event.start, endStr: event.end || ''}));
      calendar.options.eventsSet();
      calendar.options.loading(false);
    }, () => calendar.options.loading(false));
  }
  async function flush() {
    await new Promise(resolve => setImmediate(resolve));
    while (frames.length) frames.shift()();
  }
  return {el, root, filters, calendar, request, flush, get fetches() { return fetches; }, get modalShows() { return modalShows; }};
}
const response = events => Promise.resolve({ok: true, json: () => Promise.resolve(events)});
const events = [
  {title: 'Meeting', start: '2026-09-12T11:00:00', end: '2026-09-12T12:00:00', extendedProps: {category: 'event'}},
  {title: 'Shift', start: '2026-09-12T09:00:00', extendedProps: {category: 'work'}}
];

test('filters cached events without another network request', async () => {
  const h = harness(() => response(events));
  h.request(); await h.flush();
  assert.equal(h.calendar.events.length, 2);
  h.filters[1].click(); await h.flush();
  assert.equal(h.calendar.events.length, 1);
  assert.equal(h.calendar.events[0].title, 'Meeting');
  assert.equal(h.fetches, 1);
  assert.equal(h.filters[1].attrs['aria-pressed'], 'false');
});

test('failure hides empty-state content and retry recovers', async () => {
  let fail = true;
  const h = harness(() => fail ? Promise.reject(new Error('offline')) : response(events));
  h.request(); await h.flush();
  assert.equal(h.el('calendarLoadError').hidden, false);
  assert.equal(h.root.classes.has('has-load-error'), true);
  fail = false;
  h.el('calendarRetry').click(); await h.flush();
  assert.equal(h.el('calendarLoadError').hidden, true);
  assert.equal(h.root.classes.has('has-load-error'), false);
  assert.equal(h.calendar.events.length, 2);
});

test('returning to a cached range clears a later range failure', async () => {
  let fail = false;
  const h = harness(() => fail ? Promise.reject(new Error('offline')) : response(events));
  h.request(); await h.flush();
  fail = true;
  h.request({startStr: '2026-10-01T00:00:00', endStr: '2026-11-01T00:00:00'});
  await h.flush();
  assert.equal(h.el('calendarLoadError').hidden, false);
  h.request(); await h.flush();
  assert.equal(h.el('calendarLoadError').hidden, true);
  assert.equal(h.root.classes.has('has-load-error'), false);
  assert.equal(h.calendar.events.length, 2);
  assert.equal(h.fetches, 2);
});

test('today is highlighted and same-month jump scrolls its list container', async () => {
  const h = harness(() => response(events));
  const header = new Element({date: '2026-09-12'});
  header.scroller = new Element(); header.top = 250;
  h.root.children = [header];
  h.root.querySelector = () => header;
  h.request(); await h.flush();
  assert.equal(header.classes.has('is-today'), true);
  assert.equal(header.attrs['aria-current'], 'date');
  header.scroller.scrollTop = 0;
  h.el('calendarJumpToday').click(); await h.flush();
  assert.equal(h.calendar.goto, '2026-09-12');
  assert.equal(header.scroller.scrollTop, 250);
  assert.equal(header.focused, true);
});

test('empty today retains context and jump focuses the next scheduled date', async () => {
  const h = harness(() => response([{title: 'Tomorrow', start: '2026-09-13T09:00:00', extendedProps: {category: 'event'}}]));
  const header = new Element({date: '2026-09-13'});
  h.root.children = [header]; h.root.querySelector = () => null;
  h.request(); await h.flush();
  assert.equal(h.el('calendarTodaySummary').textContent, 'Nothing scheduled today');
  h.el('calendarJumpToday').click(); await h.flush();
  assert.equal(header.focused, true);
});

test('event rows open from the keyboard and omit blank metadata', async () => {
  const h = harness(() => response(events));
  const row = new Element();
  const event = {...events[0], start: new Date('2026-09-12T11:00:00Z'), end: new Date('2026-09-12T12:00:00Z'), extendedProps: {type: 'Calendar event'}};
  h.calendar.options.eventDidMount({el: row, event, view: {type: 'listMonth'}});
  let prevented = false;
  row.handlers.keydown({key: 'Enter', preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal(h.modalShows, 1);
  assert.equal(h.el('eventDetailLocation').hidden, true);
  assert.match(h.el('eventDetailWhen').textContent, /11:00 – 12:00/);
  h.el('eventDetailModal').handlers['hidden.bs.modal']();
  assert.equal(row.focused, true);
});
