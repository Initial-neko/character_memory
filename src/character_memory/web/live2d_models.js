/* Local model management. Calls and rendering remain owned by Voice/Live2D. */
(() => {
  const CM = window.CM;
  let generation = 0, characterId = null;
  const limit = 64 * 1024 * 1024;
  const current = (id, turn) => characterId === id && generation === turn && CM.dom.drawerBody.querySelector('[data-live2d-manager]');
  const endpoint = id => '/v1/characters/' + encodeURIComponent(id) + '/live2d';
  function draw(id, data, message = '') {
    const body = CM.dom.drawerBody;
    body.innerHTML = `<section data-live2d-manager>
      <p data-live2d-status>${data.available ? `已绑定：<strong>${CM.escapeHtml(data.name || 'Live2D 模型')}</strong>` : '当前未绑定 Live2D，通话继续使用普通头像。'}</p>
      ${data.available ? `<p>动作 ${data.capabilities?.motions?.length || 0} 组 · 表情 ${data.capabilities?.expressions?.length || 0} 个</p>` : ''}
      <p>选择包含 model3.json、moc3、纹理和配套资源的 ZIP（最多 64 MiB）。导入失败会保留当前模型。</p>
      <label class="ui-field"><span>Live2D 模型 ZIP</span><input type="file" accept=".zip,application/zip" aria-label="Live2D 模型 ZIP" data-live2d-file title="选择本地导出的完整模型 ZIP；只绑定当前角色，不改变普通头像"></label>
      <div class="ui-actions"><button type="button" class="primary" data-live2d-import disabled title="校验 ZIP 后绑定当前角色；已有模型将被更换">${data.available ? '更换模型' : '导入并绑定'}</button>
      <button type="button" data-live2d-unbind ${data.available ? '' : 'disabled'} title="解除当前角色的 Live2D 绑定，保留普通头像与已保存资源">解除绑定</button></div>
      <p class="ui-hint">绑定后，在该角色的通话页面打开 Live2D 即可预览；自动动作和表情随同一个开关启用。</p>
      <p data-live2d-result role="status" aria-live="polite">${CM.escapeHtml(message)}</p>
    </section>`;
  }
  async function open(id = CM.state.characterId) {
    const profile = CM.state.characters.find(item => item.id === id);
    if (!profile) return;
    characterId = id;
    const turn = ++generation;
    CM.features.sidebarCollapse?.closeMobile?.();
    CM.openDrawer(`${profile.name || id} · Live2D 模型`, '导入、更换或解除当前角色的模型绑定');
    draw(id, {available:false}, '正在读取模型…');
    try {
      const data = await CM.api(endpoint(id));
      if (current(id, turn)) draw(id, data);
    } catch (error) {
      if (current(id, turn)) CM.dom.drawerBody.querySelector('[data-live2d-result]').textContent = error.message;
    }
  }
  async function change(remove) {
    const id = characterId, turn = generation;
    if (!current(id, turn)) return;
    const file = CM.dom.drawerBody.querySelector('[data-live2d-file]').files?.[0];
    const result = CM.dom.drawerBody.querySelector('[data-live2d-result]');
    if (!remove && (!file || file.size > limit || !file.name.toLowerCase().endsWith('.zip'))) {
      result.textContent = '请选择不超过 64 MiB 的 ZIP 模型文件。'; return;
    }
    const controls = [...CM.dom.drawerBody.querySelectorAll('button,input')];
    const disabled = controls.map(item => item.disabled);
    controls.forEach(item => item.disabled = true);
    result.textContent = remove ? '正在解除绑定…' : '正在上传并校验模型…';
    try {
      let data;
      if (remove) data = await CM.api(endpoint(id), {method:'DELETE'});
      else {
        const response = await fetch(endpoint(id), {method:'POST', headers:{'Content-Type':'application/zip'}, body:file});
        const payload = await response.json();
        if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '模型导入失败');
        data = payload;
      }
      await CM.emit('live2dModelChanged', {characterId:id});
      if (current(id, turn)) draw(id, data, remove ? '已解除绑定，普通头像保持不变。' : '模型已绑定，可以进入通话预览。');
    } catch (error) {
      if (current(id, turn)) {
        result.textContent = error.message;
        controls.forEach((item, index) => item.disabled = disabled[index]);
      }
    }
  }
  CM.dom.characterList.addEventListener('click', event => {
    const button = event.target.closest('[data-character-live2d]');
    if (button) { event.preventDefault(); open(button.dataset.characterLive2d); }
  });
  CM.dom.drawerBody.addEventListener('change', event => {
    if (event.target.matches('[data-live2d-file]')) {
      CM.dom.drawerBody.querySelector('[data-live2d-import]').disabled = !event.target.files?.length;
    }
  });
  CM.dom.drawerBody.addEventListener('click', event => {
    if (event.target.closest('[data-live2d-import]')) change(false);
    if (event.target.closest('[data-live2d-unbind]')) change(true);
  });
  CM.registerFeature('live2dModels', {open});
})();
