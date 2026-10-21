"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

// Evaluate the shipped card, without constructing its DOM-dependent element.
const source = fs.readFileSync(
  path.join(__dirname, "../custom_components/cardata/frontend/bmw-cardata-vehicle-card.js"),
  "utf8"
);
const card = vm.runInNewContext(
  source + "\n;({ normalizeState, compactStateLabel, isOpenState, isOnState, localize });",
  { window: {}, HTMLElement: class {}, customElements: { get: () => true } }
);
const translate = (key) => card.localize("de", key);
const rawState = (raw) => ({ state: raw, attributes: {} });
const normalizedState = (raw, normalized) => ({ state: normalized, attributes: { raw_value: raw } });

for (const [raw, normalized, expected] of [
  ["doorsTiltCabin", "doors_tilt_cabin", "Türen gekippt"],
  ["doorsOnly", "doors_only", "Nur Türen"],
  ["CHARGINGACTIVE", "chargingactive", "Lädt"],
  ["NOCHARGING", "nocharging", "Lädt nicht"],
]) {
  test(`card keeps its existing German label for ${raw} in both modes`, () => {
    const original = rawState(raw);
    const normalizedObject = normalizedState(raw, normalized);
    assert.equal(card.compactStateLabel(original, translate), expected);
    assert.equal(card.compactStateLabel(normalizedObject, translate), expected);
    assert.equal(card.normalizeState(normalizedObject), card.normalizeState(original));
  });
}

for (const [raw, normalized, open, on] of [
  ["OPEN", "open", true, false],
  ["CLOSED", "closed", false, false],
  ["true", "true", true, true],
  ["false", "false", false, false],
  ["CHARGINGACTIVE", "chargingactive", false, true],
]) {
  test(`card indicators agree for ${raw} in both modes`, () => {
    for (const state of [rawState(raw), normalizedState(raw, normalized)]) {
      assert.equal(card.isOpenState(state), open);
      assert.equal(card.isOnState(state), on);
    }
  });
}

for (const state of ["unknown", "unavailable"]) {
  test(`actual HA ${state} wins over a retained raw_value attribute`, () => {
    const unavailable = { state, attributes: { raw_value: "OPEN" } };
    assert.equal(card.normalizeState(unavailable), "");
    assert.equal(card.compactStateLabel(unavailable, translate), "—");
    assert.equal(card.isOpenState(unavailable), false);
    assert.equal(card.isOnState(unavailable), false);
  });
}

test("absent raw_value retains the existing state fallback", () => {
  assert.equal(card.normalizeState({ state: "OPEN" }), "open");
  assert.equal(card.normalizeState({ state: "OPEN", attributes: { raw_value: null } }), "open");
  assert.equal(card.normalizeState(undefined), "");
});
