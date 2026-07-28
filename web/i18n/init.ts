import i18n, { type Resource } from "i18next";
import { initReactI18next } from "react-i18next";

import zhApp from "@/locales/zh/app.json";
import {
  DEFAULT_APP_LANGUAGE,
  normalizeLanguage,
  type AppLanguage,
} from "./language";

export { DEFAULT_APP_LANGUAGE, normalizeLanguage } from "./language";
export type { AppLanguage } from "./language";

let _initialized = false;

export function initI18n(language?: unknown) {
  if (_initialized) return i18n;

  const resources: Resource = {
    zh: { app: zhApp },
  };

  i18n.use(initReactI18next).init({
    resources,
    lng: normalizeLanguage(language),
    fallbackLng: DEFAULT_APP_LANGUAGE,
    // Use a single default namespace to keep lookups simple.
    // We intentionally keep keySeparator disabled so keys like "Generating..." remain valid.
    defaultNS: "app",
    ns: ["app"],
    keySeparator: false,
    interpolation: {
      escapeValue: false,
    },
    returnEmptyString: false,
    returnNull: false,
  });

  _initialized = true;
  return i18n;
}

export async function ensureLanguage(language: AppLanguage) {
  if (i18n.hasResourceBundle(language, "app")) return;
  if (language === "en") {
    const enApp = (await import("@/locales/en/app.json")).default;
    i18n.addResourceBundle("en", "app", enApp, true, true);
  }
}
