import { describe, expect, it, vi } from "vitest";
import { Store } from "../src/core/state";

interface Counter {
  a: number;
  b: number;
}

describe("Store", () => {
  it("get() возвращает исходное состояние до первого set()", () => {
    const store = new Store<Counter>({ a: 1, b: 2 });
    expect(store.get()).toEqual({ a: 1, b: 2 });
  });

  it("set() мержит патч поверх текущего состояния, не заменяя его целиком", () => {
    const store = new Store<Counter>({ a: 1, b: 2 });
    store.set({ a: 10 });
    expect(store.get()).toEqual({ a: 10, b: 2 });
  });

  it("subscribe() получает новое состояние при каждом set()", () => {
    const store = new Store<Counter>({ a: 1, b: 2 });
    const listener = vi.fn();
    store.subscribe(listener);

    store.set({ a: 5 });

    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenCalledWith({ a: 5, b: 2 });
  });

  it("вызванная функция отписки останавливает дальнейшие уведомления этого слушателя", () => {
    const store = new Store<Counter>({ a: 1, b: 2 });
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);

    unsubscribe();
    store.set({ a: 99 });

    expect(listener).not.toHaveBeenCalled();
  });

  it("несколько подписчиков получают уведомление независимо друг от друга", () => {
    const store = new Store<Counter>({ a: 1, b: 2 });
    const first = vi.fn();
    const second = vi.fn();
    store.subscribe(first);
    store.subscribe(second);

    store.set({ b: 3 });

    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
  });
});
