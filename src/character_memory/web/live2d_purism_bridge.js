/* Purism v1.1 + pixi-live2d-display 0.4 compatibility; no Core binary here. */
(() => {
  window.Live2DCubismCore = window.PurismCore;
  const fromMoc = window.PurismCore.Model.fromMoc;
  window.PurismCore.Model.fromMoc = function (moc) {
    const model = fromMoc.call(this, moc);
    if (!model) return model;
    model.drawables.renderOrders = model.renderOrders;
    const maskSources = new Set();
    for (const masks of model.drawables.masks) {
      for (const index of masks) maskSources.add(index);
    }
    // Pixi clears its mask target every frame. Redraw static mask geometry too.
    const reset = model.drawables.resetDynamicFlags;
    model.drawables.resetDynamicFlags = function (...args) {
      const result = reset.apply(this, args);
      for (const index of maskSources) this.dynamicFlags[index] |= 32;
      return result;
    };
    return model;
  };
})();
