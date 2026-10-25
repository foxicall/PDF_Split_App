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
function setBusy(value) { busy = value; for (const id of ['upload','clear','split','file','retry-summary']) $(id).disabled = value; }
function setMode() { $('depth-field').hidden = $('mode').value !== 'bookmarks'; $('pages-field').hidden = $('mode').value !== 'fixed'; }
function setOperation() {
  const summary = $('operation').value === 'summary';
  $('split-options').hidden = summary; $('summary-options').hidden = !summary;
  $('split').textContent = summary ? '產生文件摘要' : '拆分並打包 ZIP';
  if (summary) $('gemini-settings').open = true;
}
$('operation').addEventListener('change', setOperation);
$('mode').addEventListener('change', setMode);
$('file').addEventListener('change', () => { $('filename').textContent = $('file').files[0]?.name || '尚未選擇檔案'; });
function render(data) {
  const newUpload = uploadId !== data.upload_id;
  const newlyPaused = data.state === 'summary_paused' && snapshot?.state !== 'summary_paused';
  snapshot = data; uploadId = data.upload_id;
  $('retry-panel').hidden = data.state !== 'summary_paused';
  if (newlyPaused) { $('retry-consent').checked = false; $('retry-key').value = ''; }
  if (data.state === 'summary_paused') $('retry-info').textContent = `已完成 ${data.resume_completed} / ${data.resume_chunks} 段；目前共 ${data.summary_requests} 次請求嘗試；暫存剩餘 ${data.retry_retention_seconds} 秒。接續會從未完成步驟開始，範圍與詳細程度維持原設定。`;
  const processing = ['processing','downloading'].includes(data.state);
  for (const id of ['upload','clear','file']) $(id).disabled = processing;
  $('settings').hidden = data.state !== 'uploaded';
  $('clear').hidden = ['empty','expired'].includes(data.state);
  $('result').hidden = !['ready','downloading','downloaded'].includes(data.state);
  $('download').hidden = data.state !== 'ready';
  $('download').href = '/' + token + '/download';
  $('document-result').hidden = !['summarized','summary_partial'].includes(data.state);
  $('document-result').querySelector('h2').textContent = data.state === 'summary_partial' ? '部分摘要（未完成）' : '文件摘要結果';
  const partial = data.state === 'summary_paused' && data.resume_completed > 0;
  const exportParent = partial ? $('retry-panel') : $('document-result');
  if ($('download-summary').parentNode !== exportParent) exportParent.appendChild($('download-summary'));
  $('download-summary').hidden = !['summarized','summary_partial'].includes(data.state) && !partial;
  $('download-summary').textContent = partial || data.state === 'summary_partial' ? '下載已完成段落（部分 Markdown）' : '下載 Markdown 摘要（.md）';
  $('retry-summary').disabled = data.retry_after_seconds > 0;
  if (data.state === 'summary_paused' && data.retry_after_seconds > 0) $('retry-info').textContent += ` Google 要求至少再等待 ${data.retry_after_seconds} 秒。`;

  $('download-summary').href = '/' + token + '/summary.md';
  $('cancel-summary').hidden = !['processing','summary_paused'].includes(data.state) || data.operation !== 'summary';
  $('cancel-summary').disabled = data.cancel_requested;
  const labels = {empty:'選擇檔案即可開始。',uploaded:'上傳完成，請選擇處理方式。',processing:'正在拆分與打包，請稍候…',ready:'ZIP 已就緒，可以下載。',downloading:'下載傳輸中…',downloaded:'下載串流已送出，伺服器檔案已清除。',uploading:'先前上傳未完成，請重新上傳。',summarized:'摘要作業結束，原始暫存檔已清除。',cancelled:'摘要已取消，暫存檔已清除；請重新上傳以開始新工作。'};
  if (data.state === 'processing' && data.operation === 'summary') {
    const progress = data.progress || {};
    labels.processing = data.cancel_requested ? '正在取消；完成目前頁面擷取或 Google 請求後停止（單次請求最長 90 秒），已送出請求仍可能計費。' : progress.stage === 'retry_wait' ? `Google 暫時無法回應，等待 ${progress.wait_seconds} 秒後重試（整次最多額外 2 次）…` : ['summarizing','integrating','requesting'].includes(progress.stage) ? `${progress.stage === 'integrating' ? '正在整合各段重點' : '正在逐段摘要'}：已完成 ${progress.done} / ${progress.total} 步驟，共 ${data.summary_requests} 次請求嘗試…` : `正在擷取文字：${progress.done || 0} / ${progress.total || '…'} 頁`;
  }
  $('status').textContent = data.error || labels[data.state] || '請重新上傳。';
  $('summary-notice').hidden = !data.rows.some(row => String(row['摘要']).startsWith('摘要暫停'));
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
    $('summary-start').max = $('summary-end').max = data.page_count;
    if (newUpload) { $('summary-start').value = 1; $('summary-end').value = Math.min(data.page_count, 50); $('copy-status').textContent = ''; }
    setMode();
    setOperation();
  }
  if (data.document_summary) {
    const result = data.document_summary;
    $('document-summary').value = result.text;
    $('summary-coverage').textContent = `已檢查所選第 ${result.start_page}–${result.requested_end_page} 頁；可擷取文字共 ${result.text_chars.toLocaleString()} 字元，分 ${result.chunk_count} 段，${result.api_requests} 次請求嘗試（含重試）。` + (result.unreadable_pages ? ` 部分內容缺漏：${result.unreadable_pages} 頁擷取失敗，未納入摘要。` : '') + (result.blank_pages?.length ? ` 第 ${result.blank_pages.join('、')} 頁無可擷取文字（未執行 OCR）。` : '');
  } else { $('document-summary').value = ''; $('summary-coverage').textContent = ''; }
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
  catch (error) { if (error.status === 404) { token = ''; snapshot = null; sessionStorage.removeItem('pdf-session'); $('settings').hidden = $('result').hidden = $('document-result').hidden = $('cancel-summary').hidden = $('retry-panel').hidden = true; $('retry-key').value = ''; $('document-summary').value = ''; } $('status').textContent = error.message; }
}
$('upload').addEventListener('click', async () => {
  const file = $('file').files[0];
  if (!file || !file.name.toLowerCase().endsWith('.pdf') || !file.size || file.size > 1024 ** 3) { $('status').textContent = '請選擇大於 0 且不超過 1GB 的 PDF。'; return; }
  setBusy(true); $('retry-panel').hidden = true; $('retry-key').value = ''; $('settings').hidden = $('result').hidden = $('document-result').hidden = true; $('document-summary').value = ''; $('progress').value = 0;
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
    const options = {upload_id:uploadId, operation:$('operation').value, retain_for_retry:$('retain-for-retry').checked, summary_style:$('summary-style').value, start_page:Number($('summary-start').value), end_page:Number($('summary-end').value), mode:$('mode').value, depth:Number($('depth').value || 1), pages:Number($('pages').value), gemini:$('gemini').checked, allow_multiple_requests:$('gemini').checked, retry_transient:$('gemini').checked, api_key:$('api-key').value};
    await api('/' + token + '/process', options); $('api-key').value = ''; $('status').textContent = options.operation === 'summary' ? '正在準備文件摘要…' : '正在拆分與打包…'; $('settings').hidden = true;
  } catch(error) { $('status').textContent = error.message; }
  finally { setBusy(false); }
});
$('retry-summary').addEventListener('click', async () => {
  if (!$('retry-consent').checked) { $('status').textContent = '請先勾選同意接續重試，才會重新傳送文字。'; return; }
  setBusy(true);
  try {
    await api('/' + token + '/retry-summary', {upload_id:uploadId, gemini:true, retry_transient:true, api_key:$('retry-key').value});
    $('retry-key').value = ''; $('retry-consent').checked = false; $('retry-panel').hidden = true;
  } catch(error) { $('status').textContent = error.message; }
  finally { setBusy(false); }
});
$('cancel-summary').addEventListener('click', async () => {
  try { await api('/' + token + '/cancel', {}); await poll(); }
  catch(error) { $('status').textContent = error.message; }
});
$('copy-summary').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('document-summary').value); $('copy-status').textContent = '摘要已複製。'; }
  catch(_) { $('document-summary').focus(); $('document-summary').select(); $('copy-status').textContent = '請使用複製選單或長按選取的摘要文字。'; }
});
$('download-summary').addEventListener('click', event => {
  if (window.__pdfNativeDownload && !window.__pdfNativeMarkdown) {
    event.preventDefault();
    $('copy-status').textContent = '此 APK 版本尚不支援 Markdown 儲存。請更新至 1.1.0 以上，或用手機瀏覽器開啟本站匯出。';
  }
});
$('clear').addEventListener('click', async () => {
  try { await api('/' + token + '/clear', {}); $('retry-key').value=''; $('retry-consent').checked=false; $('file').value=''; $('filename').textContent='尚未選擇檔案'; $('progress').value=0; await poll(); }
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
