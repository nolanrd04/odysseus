document.getElementById('go').addEventListener('click', async () => {
  const input = document.getElementById('file');
  const result = document.getElementById('result');
  if (!input.files.length) {
    result.textContent = 'Pick a .dwg file first.';
    return;
  }
  result.className = '';
  result.textContent = 'Converting...';
  const form = new FormData();
  form.append('file', input.files[0]);
  try {
    const res = await fetch('/api/dwgtest/convert', { method: 'POST', body: form });
    const data = await res.json();
    result.className = res.ok ? 'ok' : 'error';
    result.textContent = JSON.stringify(data, null, 2);
  } catch (e) {
    result.className = 'error';
    result.textContent = 'Request failed: ' + e;
  }
});
