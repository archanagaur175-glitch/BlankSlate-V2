import { emit, listen } from "@tauri-apps/api/event";

export type Theme = "dark" | "light";

const KEY = "bs-theme";

export function getStoredTheme(): Theme {
  try {
    return localStorage.getItem(KEY) === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

export function applyTheme(t: Theme): void {
  const r = document.documentElement;
  r.setAttribute("data-theme", t);
  r.style.colorScheme = t;
  r.style.backgroundColor =
    t === "light" ? "rgba(241, 245, 249, 0.95)" : "rgba(10, 10, 22, 0.9)";
}

export function setTheme(t: Theme): void {
  try {
    localStorage.setItem(KEY, t);
  } catch {
  }
  applyTheme(t);
  emit("theme_changed", t).catch(() => {});
}

export function initThemeSync(): void {
  listen<Theme>("theme_changed", (e) => {
    if (e.payload === "light" || e.payload === "dark") {
      applyTheme(e.payload);
    }
  }).catch(() => {});
}
