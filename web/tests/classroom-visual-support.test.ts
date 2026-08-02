import assert from "node:assert/strict";
import test from "node:test";

import { assertOnlyClassroomInfrastructureWebSockets } from "./e2e/support/classroom-network-audit";

const HTTP_CLASSROOM_URL = "http://127.0.0.1:3000/visual-baseline/classroom";

test("classroom visual audit allows only the same-origin Next development socket", () => {
  assert.doesNotThrow(() =>
    assertOnlyClassroomInfrastructureWebSockets([], HTTP_CLASSROOM_URL),
  );
  assert.doesNotThrow(() =>
    assertOnlyClassroomInfrastructureWebSockets(
      ["ws://127.0.0.1:3000/_next/webpack-hmr?id=fixture"],
      HTTP_CLASSROOM_URL,
    ),
  );
  assert.doesNotThrow(() =>
    assertOnlyClassroomInfrastructureWebSockets(
      ["wss://classroom.example/_next/webpack-hmr?id=fixture"],
      "https://classroom.example/visual-baseline/classroom",
    ),
  );
});

test("classroom visual audit rejects non-infrastructure WebSockets", () => {
  for (const webSocketUrl of [
    "ws://unexpected.example/_next/webpack-hmr?id=fixture",
    "ws://127.0.0.1:3001/_next/webpack-hmr?id=fixture",
    "wss://127.0.0.1:3000/_next/webpack-hmr?id=fixture",
    "ws://127.0.0.1:3000/_next/webpack-hmr/",
    "ws://127.0.0.1:3000/_next/webpack-hmr-evil",
    "ws://127.0.0.1:3000/api/v1/ws",
  ]) {
    assert.throws(() =>
      assertOnlyClassroomInfrastructureWebSockets(
        [webSocketUrl],
        HTTP_CLASSROOM_URL,
      ),
    );
  }
});

test("classroom visual audit requires a corresponding HTTP page origin", () => {
  assert.throws(() =>
    assertOnlyClassroomInfrastructureWebSockets(
      ["ws://127.0.0.1:3000/_next/webpack-hmr"],
      "file:///visual-baseline/classroom",
    ),
  );
});
