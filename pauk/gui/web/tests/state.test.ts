import { describe, expect, it, vi } from "vitest";
import { Store } from "../src/core/state";

interface Counter {
  a: number;
  b: number;
}

describe("Store", () => {
  it("get() returns the initial state before the first set()", () => {
    const store = new Store<Counter>({ a: 1, b: 2 });
    expect(store.get()).toEqual({ a: 1, b: 2 });
  });

  it("set() merges the patch over the current state instead of replacing it whole", () => {
    const store = new Store<Counter>({ a: 1, b: 2 });
    store.set({ a: 10 });
    expect(store.get()).toEqual({ a: 10, b: 2 });
  });

  it("subscribe() receives the new state on every set()", () => {
    const store = new Store<Counter>({ a: 1, b: 2 });
    const listener = vi.fn();
    store.subscribe(listener);

    store.set({ a: 5 });

    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenCalledWith({ a: 5, b: 2 });
  });

  it("calling the unsubscribe function stops further notifications for that listener", () => {
    const store = new Store<Counter>({ a: 1, b: 2 });
    const listener = vi.fn();
    const unsubscribe = store.subscribe(listener);

    unsubscribe();
    store.set({ a: 99 });

    expect(listener).not.toHaveBeenCalled();
  });

  it("several subscribers are notified independently of each other", () => {
    const store = new Store<Counter>({ a: 1, b: 2 });
    const first = vi.fn();
    const second = vi.fn();
    store.subscribe(first);
    store.subscribe(second);

    store.set({ b: 3 });

    expect(first).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("a failing subscriber does not stop the others from getting the state, and the error is logged", () => {
    const store = new Store<Counter>({ a: 1, b: 2 });
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const after = vi.fn();
    store.subscribe(() => {
      throw new Error("subscriber failed");
    });
    store.subscribe(after);

    expect(() => store.set({ a: 5 })).not.toThrow();

    expect(after).toHaveBeenCalledWith({ a: 5, b: 2 });
    expect(consoleError).toHaveBeenCalledTimes(1);
    consoleError.mockRestore();
  });

  it("notify() calls subscribers with the current state without changing it", () => {
    const store = new Store<Counter>({ a: 1, b: 2 });
    const listener = vi.fn();
    store.subscribe(listener);

    store.notify();

    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenCalledWith({ a: 1, b: 2 });
    expect(store.get()).toEqual({ a: 1, b: 2 });
  });

  it("a subscriber that calls a nested set() inside its callback does not make LATER subscribers of the same round see a stale state", () => {
    // Regression: with one snapshot for the whole round, a later subscriber
    // overwrote the result of a nested set() with stale state.
    const store = new Store<Counter>({ a: 1, b: 2 });
    let nested = false;
    let lastSeenByLateSubscriber: Counter | undefined;

    store.subscribe((state) => {
      if (!nested && state.a === 10) {
        nested = true;
        store.set({ b: 99 }); // nested set() inside the early subscriber
      }
    });
    store.subscribe((state) => {
      lastSeenByLateSubscriber = state; // late subscriber, no idempotency guard
    });

    store.set({ a: 10 });

    expect(lastSeenByLateSubscriber).toEqual({ a: 10, b: 99 }); // not the stale { a: 10, b: 2 }
  });
});
