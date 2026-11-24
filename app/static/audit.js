const save = document.querySelector('#save-run');
save?.addEventListener('click', async () => {
  const message = document.querySelector('#feedback');
  const { run, sample, tab } = save.dataset;
  save.disabled = true;
  message.textContent = '核算中…';
  try {
    const request = run ? { method:'POST' } : {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({sample}),
    };
    const res = await fetch(run ? `/api/runs/${run}/rerun` : '/api/runs', request);
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || '归档失败');
    if (run) message.textContent = '复算一致，归档编号不变。';
    else location.assign(`/survey/${tab}?${new URLSearchParams({sample,run:data.id})}`);
  } catch (error) {
    message.textContent = `${error.message}，请重试。`;
  } finally { save.disabled = false; }
});

const mergeForm = document.querySelector('#merge-form');
mergeForm?.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = document.querySelector('#merge-submit');
  const message = document.querySelector('#merge-feedback');
  const runA = document.querySelector('#merge-run-a').value;
  const runB = document.querySelector('#merge-run-b').value;
  if (runA === runB) {
    message.textContent = '请选择两份不同的站档。';
    return;
  }
  button.disabled = true;
  message.textContent = '合档核算与签发中…';
  try {
    const res = await fetch('/api/merges', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({ runs:[runA, runB] }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || '合档签发失败');
    message.textContent = data.reused ? '合档已签发，沿用原编号。' : '合档签发成功。';
    location.assign(`/survey/archive?${new URLSearchParams({ merge:data.id })}`);
  } catch (error) {
    message.textContent = `${error.message}，可重试，未占用站档。`;
  } finally { button.disabled = false; }
});
