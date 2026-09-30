// Minimal DOM stand-in so the real render code (src/dom.ts `h`) runs under node --test without jsdom.
export class FakeNode {
  children: FakeNode[] = [];
  attrs: Record<string, string> = {};
  listeners: Record<string, ((e: unknown) => void)[]> = {};
  style: { cssText: string } = { cssText: "" };
  value = "";
  textContent = "";
  tag: string;
  constructor(tag: string) {
    this.tag = tag;
  }
  setAttribute(k: string, v: string): void {
    this.attrs[k] = v;
  }
  addEventListener(t: string, f: (e: unknown) => void): void {
    (this.listeners[t] ??= []).push(f);
  }
  append(...kids: FakeNode[]): void {
    this.children.push(...kids);
  }
  replaceChildren(...kids: FakeNode[]): void {
    this.children = kids;
  }
  fire(t: string): void {
    for (const f of this.listeners[t] ?? []) f({ preventDefault() {} });
  }
  /** All visible text plus attribute values, as the DOM would expose them. */
  dump(): string {
    return [this.textContent, ...Object.values(this.attrs), this.value, ...this.children.map((c) => c.dump())].join(" ");
  }
  find(pred: (n: FakeNode) => boolean): FakeNode | undefined {
    if (pred(this)) return this;
    for (const c of this.children) {
      const r = c.find(pred);
      if (r) return r;
    }
    return undefined;
  }
}

export function installFakeDom(): void {
  (globalThis as unknown as { document: unknown }).document = {
    createElement: (tag: string) => new FakeNode(tag),
    createTextNode: (t: string) => {
      const n = new FakeNode("#text");
      n.textContent = t;
      return n;
    },
  };
  (globalThis as unknown as { Node: unknown }).Node = FakeNode;
}
