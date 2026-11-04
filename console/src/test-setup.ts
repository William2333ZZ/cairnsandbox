import '@testing-library/jest-dom/vitest'

// Node 26 带实验性的全局 localStorage(没给 --localstorage-file 时是 undefined),会盖掉 jsdom 的:测试里用内存实现。
class MemoryStorage implements Storage {
  private m = new Map<string, string>()
  get length() { return this.m.size }
  clear() { this.m.clear() }
  getItem(k: string) { return this.m.has(k) ? (this.m.get(k) as string) : null }
  key(i: number) { return [...this.m.keys()][i] ?? null }
  removeItem(k: string) { this.m.delete(k) }
  setItem(k: string, v: string) { this.m.set(k, String(v)) }
}
Object.defineProperty(globalThis, 'localStorage', { value: new MemoryStorage(), configurable: true })
Object.defineProperty(window, 'localStorage', { value: globalThis.localStorage, configurable: true })
