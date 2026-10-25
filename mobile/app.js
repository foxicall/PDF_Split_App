'use strict';
const $ = id => document.getElementById(id);
let token = sessionStorage.getItem('pdf-session') || '';
let uploadId = '', busy = false, snapshot = null, installPrompt = null;
async function api(path, data, raw = false) {
  let response;
  try {
    response = await fetch(path, {cache: 'no-store', ...(data === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': raw ? 'application/octet-stream' : 'application/json'},
      body: raw ? data : JSON.stringify(data)
    })});
  } catch (_) { throw new Error('目前離線或無法連線到伺服器。請確認網路後重試。'); }
  const value = await response.json();
  if (!response.ok) { const error = new Error(value.error || '連線失敗，請稍後重試。'); error.status = response.status; throw error; }
  return value;
}
async function getSession() {
  if (!token) { const value = await api('/sessions', {}); token = value.token; sessionStorage.setItem('pdf-session', token); }
  return '/' + token + '/';
}
function setBusy(value) { busy = value; for (const id of ['upload','clear','split','file']) $(id).disabled = value; }
function setMode() { $('depth-field').hidden = $('mode').value !== 'bookmarks'; $('pages-field').hidden = $('mode').value !== 'fixed'; }
$('mode').addEventListener('change', setMode);
$('file').addEventListener('change', () => { $('filename').textContent = $('file').files[0]?.name || '尚未選擇檔案'; });
function render(data) {
  snapshot = data; uploadId = data.upload_id;
  const processing = ['processing','downloading'].includes(data.state);
  for (const id of ['upload','clear','file']) $(id).disabled = processing;
  $('settings').hidden = data.state !== 'uploaded';
  $('clear').hidden = ['empty','expired'].includes(data.state);
  $('result').hidden = !['ready','downloading','downloaded'].includes(data.state);
  $('download').hidden = data.state !== 'ready';
  $('download').href = '/' + token + '/download';
  const labels = {empty:'選擇檔案即可開始。',uploaded:'上傳完成，請選擇拆分方式。',processing:'正在拆分與打包，請稍候…',ready:'ZIP 已就緒，可以下載。',downloading:'下載傳輸中…',downloaded:'下載串流已送出，伺服器檔案已清除。',uploading:'先前上傳未完成，請重新上傳。'};
  $('status').textContent = data.error || labels[data.state] || '請重新上傳。';
  if (data.state === 'uploaded') {
    $('document-info').textContent = `${data.filename} · ${data.page_count} 頁`;
    $('warnings').textContent = data.warnings.join(' ');
    const levels = data.levels.map(String);
    const current = $('depth').value;
    if ([...$('depth').options].map(o => o.value).join() !== levels.join()) {
      $('depth').replaceChildren(...levels.map(level => new Option('第 ' + level + ' 層', level)));
      if (levels.includes(current)) $('depth').value = current;
    }
    $('mode').options[0].disabled = !levels.length;
    if (!levels.length) { $('mode').value = 'fixed'; $('warnings').textContent = '沒有有效書籤，請改用固定頁數拆分。'; }
    $('pages').max = data.page_count;
    setMode();
  }
  $('chapters').replaceChildren(...data.rows.map(row => {
    const item = document.createElement('div'); item.className = 'chapter';
    const title = document.createElement('strong'); title.textContent = row['章節'];
    const pages = document.createElement('p'); pages.textContent = `第 ${row['起始頁']}–${row['結束頁']} 頁 · ${row['頁數']} 頁`;
    const summary = document.createElement('p'); summary.textContent = row['摘要'] === '未啟用' ? '' : row['摘要'];
    item.append(title, pages, summary); return item;
  }));
}
async function poll() {
  if (busy || !token) return;
  try { render(await api('/' + token + '/details')); }
  catch (error) { if (error.status === 404) { token = ''; sessionStorage.removeItem('pdf-session'); $('settings').hidden = $('result').hidden = true; } $('status').textContent = error.message; }
}
$('upload').addEventListener('click', async () => {
  const file = $('file').files[0];
  if (!file || !file.name.toLowerCase().endsWith('.pdf') || !file.size || file.size > 1024 ** 3) { $('status').textContent = '請選擇大於 0 且不超過 1GB 的 PDF。'; return; }
  setBusy(true); $('settings').hidden = $('result').hidden = true; $('progress').value = 0;
  try {
    const base = await getSession();
    const job = await api(base + 'start', {name:file.name, size:file.size});
    for (let offset = 0; offset < file.size; offset += job.chunkSize) {
      const end = Math.min(offset + job.chunkSize, file.size);
      await api(base + `chunk?id=${encodeURIComponent(job.id)}&offset=${offset}`, file.slice(offset,end), true);
      $('progress').value = end / file.size * 100; $('status').textContent = `上傳中：${$('progress').value.toFixed(1)}%`;
    }
    await api(base + 'finish', {id:job.id});
    setBusy(false); await poll();
  } catch(error) { if (error.status === 404) { token=''; sessionStorage.removeItem('pdf-session'); } $('status').textContent = error.message; }
  finally { setBusy(false); }
});
$('split').addEventListener('click', async () => {
  setBusy(true);
  try {
    const options = {upload_id:uploadId, mode:$('mode').value, depth:Number($('depth').value || 1), pages:Number($('pages').value), gemini:$('gemini').checked, api_key:$('api-key').value};
    await api('/' + token + '/process', options); $('api-key').value = ''; $('status').textContent = '正在拆分與打包…'; $('settings').hidden = true;
  } catch(error) { $('status').textContent = error.message; }
  finally { setBusy(false); }
});
$('clear').addEventListener('click', async () => {
  try { await api('/' + token + '/clear', {}); $('file').value=''; $('filename').textContent='尚未選擇檔案'; $('progress').value=0; await poll(); }
  catch(error) { $('status').textContent = error.message; }
});
window.addEventListener('beforeinstallprompt', event => { event.preventDefault(); installPrompt=event; $('install').hidden=false; });
$('install').addEventListener('click', async () => { if (installPrompt) { await installPrompt.prompt(); await installPrompt.userChoice; installPrompt=null; $('install').hidden=true; } });
window.addEventListener('appinstalled', () => { $('install').hidden=true; });
window.addEventListener('offline', () => { $('status').textContent='目前離線。重新連線後才能上傳與拆分。'; });
if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => { $('install-help').textContent += ' 此瀏覽器無法啟用安裝快取。'; });
if (!window.isSecureContext) $('install-help').textContent='目前使用非安全連線；請改用 HTTPS 網址以啟用手機安裝。';
if (!navigator.onLine) $('status').textContent='目前離線。重新連線後才能上傳與拆分。';
setInterval(poll, 2000); poll();
