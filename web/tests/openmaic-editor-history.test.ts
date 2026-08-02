import assert from "node:assert/strict";
import test from "node:test";

import {
  createHistory,
  pushHistory,
  redo,
  replacePresent,
  undo,
} from "../lib/openmaic-adapter/editor-history";

test("one gesture creates one undo entry", () => {
  const initial = { value: 1 };
  const next = { value: 2 };
  const history = pushHistory(createHistory(initial), next);

  assert.equal(history.past.length, 1);
  assert.deepEqual(undo(history).present, initial);
  assert.deepEqual(redo(undo(history)).present, next);
});

test("editing after undo drops the abandoned redo branch", () => {
  const initial = createHistory({ value: 1 });
  const twice = pushHistory(pushHistory(initial, { value: 2 }), { value: 3 });
  const branched = pushHistory(undo(twice), { value: 4 });

  assert.deepEqual(branched.present, { value: 4 });
  assert.deepEqual(branched.future, []);
  assert.deepEqual(redo(branched), branched);
});

test("server canonicalization replaces the present without inventing undo", () => {
  const history = pushHistory(
    createHistory<{ value: number; canonical?: boolean }>({ value: 1 }),
    { value: 2 },
  );
  const replaced = replacePresent(history, { value: 2, canonical: true });

  assert.equal(replaced.past.length, 1);
  assert.deepEqual(replaced.present, { value: 2, canonical: true });
  assert.deepEqual(replaced.future, []);
});
