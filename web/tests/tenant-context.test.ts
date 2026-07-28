import test from "node:test";
import assert from "node:assert/strict";

import { switchTenant } from "../lib/tenant-api";
import type { AuthStatus } from "../lib/auth";
import {
  createTenantController,
  INITIAL_TENANT_STATE,
  reduceTenantState,
  type TenantAction,
} from "../context/TenantContext";
import { getTenantSwitcherMode } from "../components/tenant/TenantSwitcher";

const ALPHA = { tenant_id: "tenant-a", name: "Alpha", status: "active" };
const BETA = { tenant_id: "tenant-b", name: "Beta", status: "active" };
const STATUS_A: AuthStatus = {
  enabled: true,
  authenticated: true,
  active_tenant_id: ALPHA.tenant_id,
  tenants: [ALPHA],
};
const STATUS_B: AuthStatus = {
  ...STATUS_A,
  active_tenant_id: BETA.tenant_id,
  tenants: [BETA],
};
const PROFILE_STATUS: AuthStatus = {
  ...STATUS_A,
  active_tenant_id: "tenant-profile",
  tenants: [
    { tenant_id: "tenant-profile", name: "Profile", status: "active" },
  ],
};

function loadedTenantState() {
  return reduceTenantState(
    { ...INITIAL_TENANT_STATE, scopeKey: "/home" },
    {
      type: "load-success",
      status: STATUS_A,
    },
  );
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(complete => {
    resolve = complete;
  });
  return { promise, resolve };
}

function statusController(
  loadStatus: () => Promise<AuthStatus | null>,
  emit: (action: TenantAction) => void,
) {
  return createTenantController({
    loadStatus,
    requestSwitch: async tenantId => ({ active_tenant_id: tenantId }),
    emit,
  });
}

test("switchTenant sends the normalized tenant id in an authenticated PUT request", async () => {
  const originalFetch = globalThis.fetch;
  let capturedInput: RequestInfo | URL | undefined;
  let capturedInit: RequestInit | undefined;

  globalThis.fetch = async (input, init) => {
    capturedInput = input;
    capturedInit = init;
    return new Response(JSON.stringify({ active_tenant_id: "tenant-b" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  try {
    const result = await switchTenant("  tenant-b  ");

    assert.deepEqual(result, { active_tenant_id: "tenant-b" });
    assert.equal(capturedInput, "/api/v1/tenants/active");
    assert.equal(capturedInit?.method, "PUT");
    assert.equal(
      new Headers(capturedInit?.headers).get("Content-Type"),
      "application/json",
    );
    assert.equal(capturedInit?.body, JSON.stringify({ tenant_id: "tenant-b" }));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("switchTenant rejects an empty tenant id without making a request", async () => {
  const originalFetch = globalThis.fetch;
  let fetchCount = 0;

  globalThis.fetch = async () => {
    fetchCount += 1;
    return new Response(JSON.stringify({ active_tenant_id: "" }), {
      status: 200,
    });
  };

  try {
    await assert.rejects(
      () => switchTenant("   "),
      /organization is required/i,
    );
    assert.equal(fetchCount, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("switchTenant surfaces a non-success backend response", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(JSON.stringify({ detail: "Tenant access denied" }), {
      status: 403,
      headers: { "Content-Type": "application/json" },
    });

  try {
    await assert.rejects(
      () => switchTenant("tenant-b"),
      /Tenant access denied/,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("switchTenant rejects invalid JSON from a successful response", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response("not-json", {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });

  try {
    await assert.rejects(
      () => switchTenant("tenant-b"),
      /invalid organization response/i,
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("switchTenant rejects responses that do not confirm the requested tenant", async () => {
  const originalFetch = globalThis.fetch;

  try {
    for (const body of [{}, { active_tenant_id: "tenant-a" }]) {
      globalThis.fetch = async () =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });

      await assert.rejects(
        () => switchTenant("tenant-b"),
        /did not confirm the requested organization/i,
      );
    }
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("tenant switch state preserves the active tenant on failure and applies a confirmed retry", () => {
  const started = reduceTenantState(loadedTenantState(), {
    type: "switch-start",
  });
  const failedPending = reduceTenantState(started, {
    type: "switch-failure",
    tenantId: "tenant-b",
    error: "Tenant access denied",
  });
  const failed = reduceTenantState(failedPending, { type: "switch-settled" });

  assert.equal(failed.activeTenantId, "tenant-a");
  assert.equal(failed.switching, false);
  assert.equal(failed.switchError, "Tenant access denied");
  assert.equal(failed.retryTenantId, "tenant-b");
  const retrying = reduceTenantState(failed, { type: "switch-start" });
  const succeededPending = reduceTenantState(retrying, {
    type: "switch-success",
    activeTenantId: "tenant-b",
  });
  const succeeded = reduceTenantState(succeededPending, {
    type: "switch-settled",
  });

  assert.equal(succeeded.activeTenantId, "tenant-b");
  assert.equal(succeeded.switching, false);
  assert.equal(succeeded.switchError, null);
  assert.equal(succeeded.retryTenantId, null);
});

test("tenant reloads preserve the PUT lock state until explicit settlement", () => {
  let state = reduceTenantState(loadedTenantState(), {
    type: "switch-start",
  });
  state = reduceTenantState(state, {
    type: "load-start",
    scopeKey: "/profile",
    clear: true,
  });
  state = reduceTenantState(state, {
    type: "load-success",
    status: STATUS_A,
  });
  assert.equal(state.switching, true);
  state = reduceTenantState(state, { type: "switch-settled" });
  assert.equal(state.switching, false);
});

test("tenant switcher only renders a single tenant as a label when it is active", () => {
  assert.equal(getTenantSwitcherMode([], null), "hidden");
  assert.equal(getTenantSwitcherMode([ALPHA], "tenant-a"), "label");
  assert.equal(getTenantSwitcherMode([ALPHA], null), "action");
  assert.equal(getTenantSwitcherMode([ALPHA], "stale-tenant"), "action");
  assert.equal(getTenantSwitcherMode([ALPHA, BETA], "tenant-a"), "select");
});

test("a failed initial status load exposes retry and a successful retry restores tenants", async () => {
  const responses = [null, STATUS_A];
  let state = INITIAL_TENANT_STATE;
  const loader = statusController(
    async () => responses.shift() ?? null,
    action => {
      state = reduceTenantState(state, action);
    },
  );

  await loader.refresh({ scopeKey: "/home", clear: true });
  assert.equal(state.tenants.length, 0);
  assert.equal(state.loadError, "Could not load organizations. Try again.");

  await loader.refresh({ scopeKey: "/home", clear: true });
  assert.deepEqual(state.tenants, [ALPHA]);
  assert.equal(state.activeTenantId, "tenant-a");
  assert.equal(state.loadError, null);

  loader.dispose();
});

test("tenant reload ignores stale route or focus responses and unmounted completions", async () => {
  const first = deferred<AuthStatus | null>();
  const second = deferred<AuthStatus | null>();
  const unmounted = deferred<AuthStatus | null>();
  const responses = [first.promise, second.promise, unmounted.promise];
  let state = INITIAL_TENANT_STATE;
  const loader = statusController(
    async () => (await responses.shift()) ?? null,
    action => {
      state = reduceTenantState(state, action);
    },
  );

  const firstRefresh = loader.refresh({ scopeKey: "/home", clear: true });
  const secondRefresh = loader.refresh({ scopeKey: "/profile", clear: true });

  first.resolve(STATUS_A);
  await firstRefresh;
  assert.equal(state.scopeKey, "/profile");
  assert.equal(state.loading, true);
  assert.equal(state.tenants.length, 0);

  second.resolve({
    ...STATUS_A,
    active_tenant_id: BETA.tenant_id,
    tenants: [BETA],
  });
  await secondRefresh;
  assert.equal(state.activeTenantId, "tenant-b");

  const unmountedRefresh = loader.refresh({
    scopeKey: "/profile",
    clear: false,
  });
  loader.dispose();
  unmounted.resolve(STATUS_A);
  await unmountedRefresh;
  assert.equal(state.activeTenantId, "tenant-b");
  assert.equal(state.loading, true);
});

test("tenant controller resumes after a Strict Mode effect replay", async () => {
  const firstStatus = deferred<AuthStatus | null>();
  const secondStatus = deferred<AuthStatus | null>();
  const calibratedStatus = deferred<AuthStatus | null>();
  const responses = [
    firstStatus.promise,
    secondStatus.promise,
    calibratedStatus.promise,
  ];
  let state = INITIAL_TENANT_STATE;
  let loadCalls = 0;
  let switchCalls = 0;
  const controller = createTenantController({
    loadStatus: async () => {
      loadCalls += 1;
      return (await responses.shift()) ?? null;
    },
    requestSwitch: async tenantId => {
      switchCalls += 1;
      return { active_tenant_id: tenantId };
    },
    emit: action => {
      state = reduceTenantState(state, action);
    },
  });

  controller.activate();
  const firstRefresh = controller.refresh({
    scopeKey: "/home",
    clear: true,
  });
  controller.dispose();
  controller.activate();
  const secondRefresh = controller.refresh({
    scopeKey: "/profile",
    clear: true,
  });
  assert.equal(loadCalls, 2);

  secondStatus.resolve(PROFILE_STATUS);
  await secondRefresh;
  assert.equal(state.scopeKey, "/profile");
  assert.equal(state.activeTenantId, "tenant-profile");
  assert.equal(state.loading, false);

  firstStatus.resolve(STATUS_A);
  await firstRefresh;
  assert.equal(state.scopeKey, "/profile");
  assert.equal(state.activeTenantId, "tenant-profile");

  assert.equal(await controller.switchTenant("tenant-b"), true);
  assert.equal(switchCalls, 1);
  assert.equal(loadCalls, 3);

  calibratedStatus.resolve(STATUS_B);
  await new Promise<void>(resolve => setImmediate(resolve));
  assert.equal(state.activeTenantId, "tenant-b");
  assert.equal(state.loading, false);
  assert.equal(state.switching, false);
  controller.dispose();
});

test("a slow tenant PUT stays locked across focus or route refresh and recalibrates", async () => {
  const scenarios = [
    { scopeKey: "/home", before: STATUS_A, after: STATUS_B },
    { scopeKey: "/profile", before: PROFILE_STATUS, after: PROFILE_STATUS },
  ];

  for (const scenario of scenarios) {
    const pendingPut = deferred<{ active_tenant_id: string }>();
    const beforeStatus = deferred<AuthStatus | null>();
    const afterStatus = deferred<AuthStatus | null>();
    const statusResponses = [beforeStatus.promise, afterStatus.promise];
    let state = loadedTenantState();
    let transportCalls = 0;
    let settledActions = 0;
    let staleSuccesses = 0;
    const controller = createTenantController({
      loadStatus: async () => (await statusResponses.shift()) ?? null,
      requestSwitch: async () => {
        transportCalls += 1;
        return pendingPut.promise;
      },
      emit: action => {
        if (action.type === "switch-settled") settledActions += 1;
        if (action.type === "switch-success") staleSuccesses += 1;
        state = reduceTenantState(state, action);
      },
    });

    const tenantSwitch = controller.switchTenant("tenant-b");
    const refresh = controller.refresh({
      scopeKey: scenario.scopeKey,
      clear: true,
    });
    assert.deepEqual(state.tenants, []);
    assert.equal(state.activeTenantId, null);
    assert.equal(state.switching, true);
    await controller.switchTenant("tenant-c");
    assert.equal(transportCalls, 1);

    beforeStatus.resolve(scenario.before);
    await refresh;
    assert.equal(state.switching, true);

    pendingPut.resolve({ active_tenant_id: "tenant-b" });
    await tenantSwitch;
    assert.equal(settledActions, 1);
    assert.equal(staleSuccesses, 0);

    afterStatus.resolve(scenario.after);
    await new Promise<void>(resolve => setImmediate(resolve));
    assert.equal(state.scopeKey, scenario.scopeKey);
    assert.equal(state.activeTenantId, scenario.after.active_tenant_id);
    assert.equal(state.switching, false);
    controller.dispose();
  }
});
