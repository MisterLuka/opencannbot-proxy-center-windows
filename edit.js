(function () {
  const fields = {
    name: document.getElementById('name'),
    description: document.getElementById('description'),
    port: document.getElementById('port'),
    model: document.getElementById('model')
  };

  function update() {
    const name = fields.name ? fields.name.value.trim() : '代理实例';
    const description = fields.description ? fields.description.value.trim() : '';
    const port = fields.port ? fields.port.value : '8766';
    const model = fields.model ? fields.model.options[fields.model.selectedIndex].text : '';
    const nameTarget = document.getElementById('preview-name');
    const descTarget = document.getElementById('preview-description');
    const endpointTarget = document.getElementById('preview-endpoint');
    const modelTarget = document.getElementById('preview-model');
    if (nameTarget) nameTarget.textContent = name || '未命名实例';
    if (descTarget) descTarget.textContent = description || '未填写用途说明';
    if (endpointTarget) endpointTarget.textContent = `http://127.0.0.1:${port}`;
    if (modelTarget) modelTarget.textContent = model;
  }

  Object.values(fields).forEach((field) => {
    if (!field) return;
    field.addEventListener('input', update);
    field.addEventListener('change', update);
  });
  update();
})();
