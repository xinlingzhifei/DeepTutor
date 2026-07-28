"use client";

import { usePathname, useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useReducer,
  useState,
} from "react";
import {
  fetchAuthStatus,
  type AuthStatus,
  type TenantSummary,
} from "@/lib/auth";
import { switchTenant as requestTenantSwitch } from "@/lib/tenant-api";

export interface TenantState {
  scopeKey: string | null;
  tenants: TenantSummary[];
  activeTenantId: string | null;
  loading: boolean;
  loadError: string | null;
  switching: boolean;
  switchError: string | null;
  retryTenantId: string | null;
}

export const INITIAL_TENANT_STATE: TenantState = {
  scopeKey: null,
  tenants: [],
  activeTenantId: null,
  loading: true,
  loadError: null,
  switching: false,
  switchError: null,
  retryTenantId: null,
};

export type TenantLoadAction =
  | { type: "load-start"; scopeKey: string; clear: boolean }
  | { type: "load-success"; status: AuthStatus }
  | { type: "load-failure" };

export type TenantAction =
  | TenantLoadAction
  | { type: "switch-start" }
  | { type: "switch-success"; activeTenantId: string }
  | { type: "switch-failure"; tenantId: string; error: string }
  | { type: "switch-settled" };

export function reduceTenantState(
  state: TenantState,
  action: TenantAction,
): TenantState {
  switch (action.type) {
    case "load-start":
      return action.clear
        ? {
            ...INITIAL_TENANT_STATE,
            scopeKey: action.scopeKey,
            switching: state.switching,
          }
        : {
            ...state,
            scopeKey: action.scopeKey,
            loading: true,
            loadError: null,
          };
    case "load-success":
      return {
        ...state,
        tenants: action.status.tenants ?? [],
        activeTenantId: action.status.active_tenant_id ?? null,
        loading: false,
        loadError: null,
      };
    case "load-failure":
      return {
        ...state,
        loading: false,
        loadError: TENANT_STATUS_LOAD_ERROR,
      };
    case "switch-start":
      return {
        ...state,
        switching: true,
        switchError: null,
        retryTenantId: null,
      };
    case "switch-success":
      return {
        ...state,
        activeTenantId: action.activeTenantId,
        switchError: null,
        retryTenantId: null,
      };
    case "switch-failure":
      return {
        ...state,
        switchError: action.error,
        retryTenantId: action.tenantId,
      };
    case "switch-settled":
      return { ...state, switching: false };
  }
}

export const TENANT_STATUS_LOAD_ERROR =
  "Could not load organizations. Try again.";

export function createTenantController({
  loadStatus,
  requestSwitch,
  emit,
}: {
  loadStatus: () => Promise<AuthStatus | null>,
  requestSwitch: (
    tenantId: string,
  ) => Promise<{ active_tenant_id: string }>,
  emit: (action: TenantAction) => void,
}) {
  let statusGeneration = 0;
  let switchGeneration = 0;
  let switchLock: object | null = null;
  let currentScopeKey = "";
  let active = true;

  async function refresh(
    { scopeKey, clear }: { scopeKey: string; clear: boolean },
    invalidateSwitch = true,
  ) {
    if (!active) return;
    currentScopeKey = scopeKey;
    if (invalidateSwitch) switchGeneration += 1;
    const currentGeneration = ++statusGeneration;
    emit({ type: "load-start", scopeKey, clear });
    const status = await loadStatus().catch(() => null);

    if (!active || currentGeneration !== statusGeneration) return;
    if (status) emit({ type: "load-success", status });
    else emit({ type: "load-failure" });
  }

  async function switchTenant(tenantId: string): Promise<boolean> {
    if (!active || switchLock) return false;
    const request = {};
    const currentGeneration = ++switchGeneration;
    switchLock = request;
    emit({ type: "switch-start" });
    let succeeded = false;

    try {
      const result = await requestSwitch(tenantId);
      succeeded = true;
      if (active && currentGeneration === switchGeneration) {
        emit({
          type: "switch-success",
          activeTenantId: result.active_tenant_id,
        });
      }
    } catch (error) {
      if (active && currentGeneration === switchGeneration) {
        emit({
          type: "switch-failure",
          tenantId: tenantId.trim(),
          error: displayError(error),
        });
      }
    } finally {
      if (switchLock === request) {
        switchLock = null;
        if (active) {
          void refresh({ scopeKey: currentScopeKey, clear: false }, false);
          emit({ type: "switch-settled" });
        }
      }
    }
    return succeeded && active;
  }

  return {
    activate() {
      active = true;
    },
    refresh,
    switchTenant,
    dispose() {
      active = false;
      statusGeneration += 1;
      switchGeneration += 1;
    },
  };
}

interface TenantContextValue {
  tenants: TenantSummary[];
  activeTenantId: string | null;
  activeTenant: TenantSummary | null;
  loading: boolean;
  loadError: string | null;
  switching: boolean;
  error: string | null;
  switchTenant: (tenantId: string) => Promise<void>;
  retrySwitch: () => Promise<void>;
  retryStatus: () => Promise<void>;
}

const TenantContext = createContext<TenantContextValue | null>(null);

function displayError(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  return "Could not switch organization. Try again.";
}

export function TenantProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [state, dispatch] = useReducer(reduceTenantState, INITIAL_TENANT_STATE);
  const [controller] = useState(() =>
    createTenantController({
      loadStatus: fetchAuthStatus,
      requestSwitch: requestTenantSwitch,
      emit: dispatch,
    }),
  );

  useEffect(() => {
    controller.activate();
    const handleFocus = () =>
      void controller.refresh({ scopeKey: pathname, clear: true });
    window.addEventListener("focus", handleFocus);
    void controller.refresh({ scopeKey: pathname, clear: true });

    return () => {
      window.removeEventListener("focus", handleFocus);
      controller.dispose();
    };
  }, [controller, pathname]);

  const performSwitch = useCallback(
    async (tenantId: string) => {
      if (await controller.switchTenant(tenantId)) router.refresh();
    },
    [controller, router],
  );

  const retrySwitch = useCallback(async () => {
    if (!state.retryTenantId) return;
    await performSwitch(state.retryTenantId);
  }, [performSwitch, state.retryTenantId]);

  const retryStatus = useCallback(async () => {
    await controller.refresh({ scopeKey: pathname, clear: true });
  }, [controller, pathname]);

  const statusIsCurrent = state.scopeKey === pathname;
  const tenants = statusIsCurrent ? state.tenants : [];
  const activeTenantId = statusIsCurrent ? state.activeTenantId : null;
  const activeTenant =
    tenants.find(tenant => tenant.tenant_id === activeTenantId) ?? null;

  const value: TenantContextValue = {
    tenants,
    activeTenantId,
    activeTenant,
    loading: statusIsCurrent ? state.loading : true,
    loadError: statusIsCurrent ? state.loadError : null,
    switching: statusIsCurrent && state.switching,
    error: statusIsCurrent ? state.switchError : null,
    switchTenant: performSwitch,
    retrySwitch,
    retryStatus,
  };

  return (
    <TenantContext.Provider value={value}>{children}</TenantContext.Provider>
  );
}

export function useTenant(): TenantContextValue {
  const context = useContext(TenantContext);
  if (!context) {
    throw new Error("useTenant must be used inside TenantProvider");
  }
  return context;
}
