// API keys section of the Data sources window. WRITE-ONLY: the server only ever tells us whether a key
// is set and its last 4 characters. A typed value is read from the input once at submit, sent, and the
// input is cleared at once. It is never stored in state, localStorage or the DOM after that.
import type { KeyStatus } from "./api.ts";
import { h } from "./dom.ts";

export interface KeyDeps {
  list: () => Promise<KeyStatus[]>;
  put: (name: string, value: string) => Promise<unknown>;
  del: (name: string) => Promise<unknown>;
}

/** Where to find each key; shown under the row. */
const KEY_HELP: Record<string, string> = {
  OPENSKY_CLIENT_ID: "OpenSky account → Account → API client",
  OPENSKY_CLIENT_SECRET: "OpenSky account → Account → API client",
};

export const KEY_BANNER = "Key saved; source restarted. Refresh the page to see new data.";
export const KEY_CLEARED_BANNER = "Key cleared; source restarted. Refresh the page to see new data.";

export class KeysSection {
  readonly el: HTMLElement = h("div", { class: "keys" });
  private rows: KeyStatus[] = [];
  private editing: string | null = null;
  private banner = "";
  private error = "";

  private deps: KeyDeps;
  constructor(deps: KeyDeps) {
    this.deps = deps;
  }

  async load(): Promise<void> {
    try {
      this.rows = await this.deps.list();
      this.error = "";
    } catch {
      this.error = "Could not load the key list.";
    }
    this.render();
  }

  private async submit(name: string, input: HTMLInputElement): Promise<void> {
    const value = input.value;
    input.value = ""; // clear straight away; the value lives only in this local variable
    if (!value) return;
    try {
      await this.deps.put(name, value);
      this.banner = KEY_BANNER;
      this.error = "";
      this.editing = null;
    } catch {
      this.error = "The key was not saved. Check it and try again.";
    }
    await this.load();
  }

  private async clear(name: string): Promise<void> {
    try {
      await this.deps.del(name);
      this.banner = KEY_CLEARED_BANNER;
      this.error = "";
      this.editing = null;
    } catch {
      this.error = "The key was not cleared.";
    }
    await this.load();
  }

  render(): void {
    const rows = this.rows.map((k) => {
      const input = h("input", {
        type: "password",
        autocomplete: "off",
        spellcheck: "false",
        "aria-label": `New key for ${k.name}`,
        placeholder: "Paste key",
      });
      const form =
        this.editing === k.name
          ? h(
              "form",
              {
                class: "key-form",
                onsubmit: (e) => {
                  e.preventDefault();
                  void this.submit(k.name, input);
                },
              },
              input,
              h("button", { type: "submit" }, "Save"),
              h("button", { type: "button", onclick: () => { this.editing = null; this.render(); } }, "Cancel"),
            )
          : null;
      const status = k.is_set
        ? h("span", {}, `Set ••••${k.last4 ?? ""}`, k.origin === "env" ? h("span", { class: "note" }, " (from .env)") : null)
        : h("span", { class: "note" }, "Not set");
      return h(
        "div",
        { class: "key-row" },
        h("strong", {}, k.name),
        h("span", { class: "note" }, k.connector),
        status,
        h(
          "span",
          { class: "row" },
          h("button", { type: "button", onclick: () => { this.editing = k.name; this.banner = ""; this.render(); } }, k.is_set ? "Replace" : "Set"),
          k.is_set && k.origin !== "env" ? h("button", { type: "button", class: "danger", onclick: () => void this.clear(k.name) }, "Clear") : null,
        ),
        KEY_HELP[k.name] ? h("span", { class: "note key-help" }, KEY_HELP[k.name]) : null,
        form,
      );
    });
    this.el.replaceChildren(
      h("h3", {}, "API keys"),
      h("p", { class: "note" }, "Keys are write-only: they are stored on this computer and never shown again."),
      ...(this.banner ? [h("div", { class: "banner", role: "status" }, this.banner)] : []),
      ...(this.error ? [h("div", { class: "fail", role: "alert" }, this.error)] : []),
      ...(rows.length ? rows : [h("div", { class: "note" }, "No sources need a key.")]),
    );
  }
}
