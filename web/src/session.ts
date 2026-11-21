// Hosted mode (task 062): signed-in name + Sign out in the top bar; any 401 returns to the login page.
// Localhost mode (auth off) shows nothing.
import { h } from "./dom";
import { icon } from "./icons";

export interface SessionInfo {
  auth: boolean;
  signed_in?: boolean;
  name: string | null;
  admin: boolean;
}

export const LOGIN_PATH = "/login.html";

/** True when a response means the session ended (hosted mode only). */
export function sessionEnded(s: SessionInfo | null, status: number): boolean {
  return !!s?.auth && status === 401;
}

export async function mountSession(host: HTMLElement): Promise<SessionInfo | null> {
  let s: SessionInfo | null = null;
  try {
    s = (await (await fetch("/api/session", { credentials: "same-origin" })).json()) as SessionInfo;
  } catch {
    return null;
  }
  if (!s.auth) return s;
  const info = s;
  const orig = window.fetch.bind(window);
  window.fetch = async (...args: Parameters<typeof fetch>): Promise<Response> => {
    const r = await orig(...args);
    if (sessionEnded(info, r.status)) location.assign(LOGIN_PATH);
    return r;
  };
  const out = async (): Promise<void> => {
    await orig("/api/logout", { method: "POST", credentials: "same-origin" }).catch(() => undefined);
    location.assign(LOGIN_PATH);
  };
  const who = `${s.name ?? "?"}${s.admin ? " (admin)" : ""}`;
  host.append(
    h(
      "button",
      { type: "button", class: "top-btn", title: `Signed in as ${who}. Sign out`, "aria-label": `Sign out ${who}`, onclick: () => void out() },
      icon("logout", 18),
      h("span", { class: "top-label" }, s.name ?? "Sign out"),
    ),
  );
  return s;
}
