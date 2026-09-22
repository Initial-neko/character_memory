/* Dev Console control levels.
 *
 * The Dev Console is hand-written markup, not a schema like the Settings
 * Center, so the level contract lives in the DOM instead of in a served field
 * list: a block declares `data-level="common|advanced|diagnostic"`, and the
 * nearest declaring ancestor decides what a control is. That is the whole
 * vocabulary -- one attribute, three values.
 *
 * The rule that has to survive a future edit is the one the Settings Center
 * already encodes in `field_level()`: **a level nobody declared is not a level
 * that gets promoted.** `levelOf` answers `diagnostic` for anything it does not
 * recognise -- `undefined`, `""`, a typo, a value from a later version -- so a
 * control added without a level lands in the level that is out of the way
 * instead of on the first screen. Forgetting must under-expose, never clutter.
 *
 * Two jobs, both of them enhancement over markup that already satisfies the
 * contract (tests/test_dev_console_levels.py asserts that it does, so the
 * static page is correct with this file absent or broken):
 *
 *   1. `sweep` finds every control with no declaring ancestor and moves it into
 *      its card's diagnostic group, creating one when the card has none. This
 *      is the runtime half of the default above: a control that skipped the
 *      markup convention still does not reach the first screen.
 *   2. `refreshSummaries` rewrites each group's <summary> from the controls the
 *      group actually holds -- name, count, and the first few labels -- so a
 *      closed group says what is inside it and cannot drift from its contents
 *      the way a hand-written summary would.
 *
 * The file stays loadable outside a browser (no `document` at the top level,
 * `module.exports` when there is a CommonJS `module`) so the default rule can be
 * exercised directly by the test suite.
 */
(function (global) {
  "use strict";

  var LEVELS = ["common", "advanced", "diagnostic"];
  var DEFAULT_LEVEL = "diagnostic";
  var LEVEL_WORD = {common: "常用", advanced: "高级", diagnostic: "诊断"};
  // Long enough to identify the group, short enough to stay one line.
  var MAX_LABELS = 3;
  var CONTROL_SELECTOR = "button, input, select, textarea";
  var GROUP_SELECTOR = "details.level-group";
  var CONTAINER_SELECTOR = ".card, header, section, article";

  /** The level of a declared value, or the hidden one when it declares nothing. */
  function levelOf(value) {
    var level = typeof value === "string" ? value.trim() : "";
    return LEVELS.indexOf(level) === -1 ? DEFAULT_LEVEL : level;
  }

  /** The nearest ancestor that declares a level, or null. */
  function declaringAncestor(element) {
    for (var node = element.parentElement; node; node = node.parentElement) {
      if (node.dataset && node.dataset.level) return node;
    }
    return null;
  }

  /** The block a control belongs to: the box the sweep may restructure. */
  function containerOf(element) {
    var box = element.closest ? element.closest(CONTAINER_SELECTOR) : null;
    return box || document.querySelector("main") || document.body;
  }

  /** Text this element owns, i.e. not the text of the controls nested in it. */
  function ownText(element) {
    var text = "";
    for (var index = 0; index < element.childNodes.length; index += 1) {
      if (element.childNodes[index].nodeType === 3) text += element.childNodes[index].textContent;
    }
    return text.replace(/\s+/g, " ").trim();
  }

  /** What a person would call this control, for the group summary. */
  function controlLabel(control) {
    if (control.dataset && control.dataset.label) return control.dataset.label;

    var tag = control.tagName.toLowerCase();
    if (tag === "button" || (tag === "input" && ["button", "submit", "reset"].indexOf(control.type) !== -1)) {
      var caption = ownText(control);
      if (caption) return caption;
    }
    var wraps = control.closest ? control.closest("label") : null;
    if (wraps) {
      var wrapped = ownText(wraps);
      if (wrapped) return wrapped;
    }
    if (control.id) {
      var paired = document.querySelector('label[for="' + control.id + '"]');
      if (paired) {
        var pairedText = ownText(paired);
        if (pairedText) return pairedText;
      }
    }
    if (control.placeholder) return control.placeholder;
    return control.id || tag;
  }

  /** The diagnostic group of a container, created if the card has none yet. */
  function diagnosticGroup(container) {
    var existing = container.querySelector(':scope > ' + GROUP_SELECTOR + '[data-level="diagnostic"]');
    if (existing) return existing;

    var details = document.createElement("details");
    details.className = "level-group";
    details.dataset.level = "diagnostic";
    details.dataset.title = "诊断 · 未分级的控件";
    details.dataset.hint = "没有声明 level，默认收在这里";
    var summary = document.createElement("summary");
    summary.textContent = "诊断 · 未分级的控件";
    details.appendChild(summary);
    container.appendChild(details);
    return details;
  }

  /** Move every control that declares nothing into its card's diagnostic group. */
  function sweep(root) {
    var moved = [];
    var controls = root.querySelectorAll(CONTROL_SELECTOR);
    for (var index = 0; index < controls.length; index += 1) {
      var control = controls[index];
      if (declaringAncestor(control)) continue;
      // The raw payload blocks hold no controls, but a card is allowed to put
      // one there without this pass dragging it back out.
      if (control.closest && control.closest(".debug-output")) continue;
      // The label is the control's unit of meaning -- moving the input alone
      // would leave the text behind.
      var unit = (control.closest && control.closest("label")) || control;
      diagnosticGroup(containerOf(control)).appendChild(unit);
      moved.push(control.id || control.tagName.toLowerCase());
    }
    return moved;
  }

  /** "高级 · 调度与上限（6 项）：A · B · C … — hint", built from the group. */
  function groupSummary(details) {
    var level = levelOf(details.dataset.level);
    var labels = [];
    var controls = details.querySelectorAll(CONTROL_SELECTOR);
    for (var index = 0; index < controls.length; index += 1) {
      var label = controlLabel(controls[index]);
      if (label && labels.indexOf(label) === -1) labels.push(label);
    }
    var title = details.dataset.title || LEVEL_WORD[level] || level;
    // A group of prose (the extension-slot note) has no controls to count, so
    // it says what it is without claiming an item count of zero.
    var text = controls.length ? title + "（" + controls.length + " 项）" : title;
    if (labels.length) {
      text += "：" + labels.slice(0, MAX_LABELS).join(" · ") + (labels.length > MAX_LABELS ? " …" : "");
    }
    if (details.dataset.hint) text += " — " + details.dataset.hint;
    return text;
  }

  function refreshSummaries(root) {
    var groups = root.querySelectorAll(GROUP_SELECTOR);
    for (var index = 0; index < groups.length; index += 1) {
      var summary = groups[index].querySelector(":scope > summary");
      if (summary) summary.textContent = groupSummary(groups[index]);
    }
  }

  var api = {
    LEVELS: LEVELS,
    DEFAULT_LEVEL: DEFAULT_LEVEL,
    levelOf: levelOf,
    controlLabel: controlLabel,
    groupSummary: groupSummary,
    applyLevels: function (root) {
      var scope = root || document;
      var moved = sweep(scope);
      refreshSummaries(scope);
      api.lastSweep = moved;
      return moved;
    },
    // What the last sweep had to rescue. Empty on a page whose markup declares
    // every level, which is what the browser test asserts.
    lastSweep: [],
  };

  global.CMDevLevels = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  if (typeof document === "undefined") return;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { api.applyLevels(document); });
  } else {
    api.applyLevels(document);
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
