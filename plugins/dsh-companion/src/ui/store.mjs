/** Minimal external store so every surface reads the same call draft state. */
export function createStore(initial) {
  let state = initial;
  const listeners = new Set();
  return {
    getSnapshot: () => state,
    get: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    set(patch) {
      state = {...state, ...patch};
      for (const listener of listeners) listener();
    },
  };
}
