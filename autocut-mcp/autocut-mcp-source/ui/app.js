/* AUTOCUT 剪辑控制台 — 前端逻辑
   与后端约定: /api/scan /api/probe /api/render /api/status/<id> /api/textplan
              /api/textjob /api/templates /api/template_render /output/<name>
   设计语言见 docs/UI_DESIGN.md */
'use strict';

// ---------------- 图标 ----------------
const SVG = {
  scissors: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><line x1="20" y1="4" x2="8.12" y2="15.88"/><line x1="14.47" y1="14.48" x2="20" y2="20"/><line x1="8.12" y1="8.12" x2="12" y2="12"/>',
  folder: '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
  search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  film: '<rect x="2" y="2" width="20" height="20" rx="2.18" ry="2.18"/><line x1="7" y1="2" x2="7" y2="22"/><line x1="17" y1="2" x2="17" y2="22"/><line x1="2" y1="12" x2="22" y2="12"/><line x1="2" y1="7" x2="7" y2="7"/><line x1="2" y1="17" x2="7" y2="17"/><line x1="17" y1="17" x2="22" y2="17"/><line x1="17" y1="7" x2="22" y2="7"/>',
  music: '<path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>',
  play: '<polygon points="6 4 20 12 6 20 6 4"/>',
  plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
  trash: '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  grip: '<circle cx="9" cy="6" r="1.2"/><circle cx="9" cy="12" r="1.2"/><circle cx="9" cy="18" r="1.2"/><circle cx="15" cy="6" r="1.2"/><circle cx="15" cy="12" r="1.2"/><circle cx="15" cy="18" r="1.2"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  up: '<polyline points="18 15 12 9 6 15"/>',
  down: '<polyline points="6 9 12 15 18 9"/>',
  cam: '<path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
  mic: '<path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/>'
};
const $ = id => document.getElementById(id);
function ic(name, size) { size = size || 16; return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' + (SVG[name] || '') + '</svg>'; }
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtDur(sec) {
  sec = Math.max(0, Number(sec) || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const ss = (h ? String(h).padStart(2, '0') + ':' : '') + String(m).padStart(2, '0') + ':' + String(Math.floor(s)).padStart(2, '0') + '.' + String(Math.round((s - Math.floor(s)) * 1000)).padStart(3, '0').slice(0, 3);
  return ss;
}
function toast(msg, type) {
  const w = $('toasts');
  const t = document.createElement('div');
  t.className = 'toast ' + (type || '');
  t.innerHTML = msg;
  w.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transition = 'opacity .25s'; setTimeout(() => t.remove(), 260); }, 3200);
}
async function postJSON(url, obj) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(obj || {}) });
  let d; try { d = await r.json(); } catch (e) { d = { error: 'HTTP ' + r.status + '（响应非 JSON）' }; }
  return d;
}
function setStatus(el, msg, cls) { el.textContent = msg; el.className = 'statline ' + (cls || ''); }
function logAppend(el, text) { el.textContent += (el.textContent && !el.textContent.endsWith('\n') ? '\n' : '') + text; el.scrollTop = el.scrollHeight; }

// ---------------- 状态 ----------------
let media = { videos: [], audios: [] };
let filter = '';
let selMedia = null;            // {kind,path,name}
let durCache = {};              // path -> 秒
let clips = [];                 // [{src, src_in, dur, kind, name}]
let selClip = -1;
let timers = {};                // taskId -> interval
const ACTIVE_VIEW = { auto: true, timeline: false, text: false, templates: false, grade: false };

// ---------------- 标签页 ----------------
document.querySelectorAll('#tabs .tab').forEach(btn => {
  btn.addEventListener('click', () => switchView(btn.dataset.view));
});
function switchView(v) {
  Object.keys(ACTIVE_VIEW).forEach(k => ACTIVE_VIEW[k] = (k === v));
  document.querySelectorAll('#tabs .tab').forEach(b => b.classList.toggle('active', b.dataset.view === v));
  document.querySelectorAll('.view').forEach(sec => sec.classList.toggle('active', sec.id === 'view-' + v));
}

// ---------------- 素材库 ----------------
async function scan() {
  const root = $('rootInput').value.trim();
  $('btnScan').disabled = true;
  try {
    const d = await postJSON('/api/scan', { root });
    if (d.error) { toast('扫描失败：' + esc(d.error), 'err'); return; }
    media = d;
    $('libRoot').textContent = d.root || '';
    renderMedia();
  } finally { $('btnScan').disabled = false; }
}
function filteredMedia() {
  const q = filter.toLowerCase();
  const keep = list => (list || []).filter(m => !q || (m.name || '').toLowerCase().includes(q) || (m.rel || '').toLowerCase().includes(q));
  return { videos: keep(media.videos), audios: keep(media.audios) };
}
function renderMedia() {
  const box = $('mediaList');
  const f = filteredMedia();
  let html = '';
  const kindOf = p => (/\.(mp3|wav|aac|m4a|flac|ogg)$/i.test(p) ? 'a' : 'v');
  if (!f.videos.length && !f.audios.length) {
    html += '<div class="empty" style="margin:10px">' + ic('folder', 26) + '<b>没有素材</b><br>换个根目录扫描，或在下方面板直接添加</div>';
  } else {
    const sec = (title, arr, k) => {
      if (!arr.length) return '';
      let s = '<div class="lib-kind">' + title + ' · ' + arr.length + '</div>';
      arr.forEach(m => {
        const sel = selMedia && selMedia.path === m.path;
        const dur = durCache[m.path];
        s += '<div class="m-item' + (sel ? ' sel' : '') + '" data-path="' + esc(m.path) + '" data-kind="' + k + '" data-name="' + esc(m.name) + '">'
          + '<span class="ic ' + k + '">' + ic(k === 'v' ? 'film' : 'music', 16) + '</span>'
          + '<span class="nm"><span class="t">' + esc(m.name) + '</span><span class="p">' + esc(m.rel || m.path) + '</span></span>'
          + (dur ? '<span class="dur">' + fmtDur(dur) + '</span>' : '') + '</div>';
      });
      return s;
    };
    html += sec('视频', f.videos, 'v') + sec('音频', f.audios, 'a');
  }
  box.innerHTML = html;
  box.querySelectorAll('.m-item[data-path]').forEach(el => el.addEventListener('click', () => selectMedia(el.dataset.path, el.dataset.kind, el.dataset.name)));
  // 后台补探测时长：串行 + 失败缓存 + 原位更新（避免并发探测风暴/整表重绘）
  if (!window.__probeBusy) {
    const need = (f.videos.concat(f.audios)).filter(m => durCache[m.path] === undefined);
    if (need.length) {
      window.__probeBusy = true;
      (async () => {
        for (const m of need) {
          if (durCache[m.path] !== undefined) continue;
          let dur = 0;
          try {
            const d = await postJSON('/api/probe', { path: m.path });
            if (d && !d.error && d.duration) dur = d.duration;
          } catch (e) { dur = 0; }
          durCache[m.path] = dur;
          if (dur) {
            document.querySelectorAll('#mediaList .m-item[data-path]').forEach(el => {
              if (el.dataset.path === m.path) {
                let dEl = el.querySelector('.dur');
                if (!dEl) { dEl = document.createElement('span'); dEl.className = 'dur'; el.appendChild(dEl); }
                dEl.textContent = fmtDur(dur);
              }
            });
          }
        }
        window.__probeBusy = false;
      })();
    }
  }
}
async function probePath(path) {
  if (durCache[path] !== undefined) return durCache[path];
  try {
    const d = await postJSON('/api/probe', { path });
    if (d.error) { durCache[path] = 0; return 0; }
    const dur = Number(d.duration != null ? d.duration : (d.format && d.format.duration));
    durCache[path] = isFinite(dur) ? dur : 0;
    return durCache[path];
  } catch (e) { durCache[path] = 0; return 0; }
}
function selectMedia(path, kind, name) {
  selMedia = { path, kind, name: name || path.split(/[\\/]/).pop() };
  renderMedia();
  const icName = kind === 'v' ? 'film' : 'music';
  $('icMediaInfo').className = 'ic ' + (kind === 'v' ? 'v' : 'a');
  $('icMediaInfo').innerHTML = ic(icName, 18);
  $('infoName').textContent = selMedia.name; $('infoName').style.color = '';
  $('infoPath').textContent = path;
  probePath(path).then(dur => { $('infoDur').textContent = dur ? fmtDur(dur) : '…'; });
  if (kind === 'v') { $('tcVideo').value = path; $('aeVideo').value = path; $('grVideo').value = path; if (showAeMeta) showAeMeta(); if (showGrMeta) showGrMeta(); }
  if (kind === 'a' && $('aeBgm')) $('aeBgm').value = path;
}
$('btnScan').addEventListener('click', scan);
$('mediaSearch').addEventListener('input', e => { filter = e.target.value.trim(); renderMedia(); });

// ---------------- 时间线片段 ----------------
function clipStart(i) { let t = 0; for (let k = 0; k < i; k++) t += (clips[k] ? clips[k].dur : 0); return t; }
function totalDur() { return clips.reduce((a, c) => a + (Number(c.dur) || 0), 0); }
function renderClips() {
  const box = $('clipList');
  $('clipCount').textContent = clips.length;
  $('clipStats').textContent = clips.length + ' 段 · ' + fmtDur(totalDur());
  if (!clips.length) {
    box.innerHTML = '<div class="empty" style="margin:2px">' + ic('plus', 24) + '<b>时间线为空</b><br>在左侧素材库选中素材 → “添加到时间线”</div>';
    $('clipProps').style.display = 'none'; selClip = -1;
    return;
  }
  let html = '';
  clips.forEach((c, i) => {
    const start = clipStart(i), end = start + c.dur;
    html += '<div class="clip-row' + (i === selClip ? ' sel' : '') + '" draggable="true" data-i="' + i + '">'
      + '<span class="grip">' + ic('grip', 14) + '</span>'
      + '<span class="idx">' + String(i + 1).padStart(2, '0') + '</span>'
      + '<span class="badge ' + c.kind + '">' + (c.kind === 'v' ? 'V' : 'A') + '</span>'
      + '<span class="nm"><span class="t">' + esc(c.name) + '</span><span class="p">' + esc(c.src) + '</span></span>'
      + '<span class="tc">入 ' + fmtDur(c.src_in) + '</span>'
      + '<span class="tc"><b>' + fmtDur(c.dur) + '</b></span>'
      + '<span class="tc">' + fmtDur(start) + ' → ' + fmtDur(end) + '</span>'
      + '<span class="tools" style="display:flex;gap:4px">'
      + '<button class="btn sm ghost" data-act="del" title="删除">' + ic('trash', 13) + '</button></span></div>';
  });
  box.innerHTML = html;
  box.querySelectorAll('.clip-row').forEach(row => {
    const i = +row.dataset.i;
    row.addEventListener('click', ev => {
      if (ev.target.closest('[data-act]')) return;
      selClip = i; renderClips(); renderProps();
    });
    row.querySelector('[data-act="del"]').addEventListener('click', () => { clips.splice(i, 1); if (selClip >= clips.length) selClip = clips.length - 1; renderClips(); renderProps(); toast('已删除片段 ' + (i + 1)); });
    row.addEventListener('dragstart', ev => { ev.dataTransfer.setData('text/plain', String(i)); row.classList.add('drag'); });
    row.addEventListener('dragend', () => row.classList.remove('drag'));
    row.addEventListener('dragover', ev => ev.preventDefault());
    row.addEventListener('drop', ev => {
      ev.preventDefault();
      const from = +ev.dataTransfer.getData('text/plain');
      const to = +row.dataset.i;
      if (from === to) return;
      const [x] = clips.splice(from, 1); clips.splice(to, 0, x);
      selClip = to; renderClips(); renderProps();
    });
  });
}
function renderProps() {
  const box = $('clipProps');
  if (selClip < 0 || selClip >= clips.length) { box.style.display = 'none'; return; }
  const c = clips[selClip];
  box.style.display = 'block';
  const start = clipStart(selClip);
  $('propFields').innerHTML = ''
    + '<div><label class="lbl">源内入点(秒)</label><input class="inp mono" type="number" step="0.1" min="0" id="propSrcIn" value="' + c.src_in + '"></div>'
    + '<div><label class="lbl">时长(秒)</label><input class="inp mono" type="number" step="0.1" min="0.1" id="propDur" value="' + c.dur + '"></div>'
    + '<div><label class="lbl">占位(起→止)</label><div class="hint mono" style="padding:8px 2px">' + fmtDur(start) + ' → ' + fmtDur(start + c.dur) + '</div></div>'
    + '<div style="display:flex;gap:6px;align-items:flex-end">'
    + '<button class="btn sm" id="propUp" title="上移">' + ic('up', 13) + '</button>'
    + '<button class="btn sm" id="propDown" title="下移">' + ic('down', 13) + '</button>'
    + '<button class="btn sm ghost" id="propCopy" title="复制片段">' + ic('copy', 13) + '</button></div>';
  $('propSrcIn').addEventListener('change', e => { c.src_in = Math.max(0, +e.target.value || 0); renderClips(); renderProps(); });
  $('propDur').addEventListener('change', e => { c.dur = Math.max(0.1, +e.target.value || 0.1); renderClips(); renderProps(); });
  $('propUp').addEventListener('click', () => { if (selClip > 0) { const x = clips.splice(selClip, 1)[0]; clips.splice(selClip - 1, 0, x); selClip--; renderClips(); renderProps(); } });
  $('propDown').addEventListener('click', () => { if (selClip < clips.length - 1) { const x = clips.splice(selClip, 1)[0]; clips.splice(selClip + 1, 0, x); selClip++; renderClips(); renderProps(); } });
  $('propCopy').addEventListener('click', () => { clips.splice(selClip + 1, 0, Object.assign({}, c)); selClip++; renderClips(); renderProps(); toast('已复制片段'); });
}
function addClip() {
  if (!selMedia) { toast('先在左侧素材库选中一个素材', 'err'); switchView('timeline'); return; }
  const dur = Math.max(0.2, +$('clipDur').value || 3);
  clips.push({ src: selMedia.path, src_in: 0, dur, kind: selMedia.kind, name: selMedia.name });
  selClip = clips.length - 1;
  renderClips(); renderProps();
  toast('已添加 ' + esc(selMedia.name) + '（' + dur + 's）', 'ok');
}
$('btnAddClip').addEventListener('click', addClip);
$('btnClear').addEventListener('click', () => {
  if (clips.length && !confirm('清空全部 ' + clips.length + ' 个片段？')) return;
  clips = []; selClip = -1; renderClips(); renderProps();
});

// ---------------- 渲染 (时间线) ----------------
function buildTimelineJson() {
  let t = 0;
  const vclips = [], aclips = [];
  clips.forEach(c => {
    const base = { src: c.src, start: Math.round(t * 100) / 100, dur: Math.round(c.dur * 100) / 100, src_in: Math.round((c.src_in || 0) * 100) / 100 };
    if (c.kind === 'v') vclips.push(Object.assign({}, base, { x: 0, y: 0, scale: 1.0, opacity: 1.0 }));
    else aclips.push(Object.assign({}, base, { volume: 1.0, fade_in: 0.02, fade_out: 0.05 }));
    t += c.dur;
  });
  const tracks = [];
  if (vclips.length) tracks.push({ id: 'V1', type: 'video', clips: vclips });
  if (aclips.length) tracks.push({ id: 'A1', type: 'audio', clips: aclips });
  return { width: +$('outW').value || 1920, height: +$('outH').value || 1080, fps: +$('outFps').value || 25, tracks };
}
$('btnRender').addEventListener('click', async () => {
  if (!clips.length) { toast('时间线为空，先添加片段', 'err'); return; }
  const tl = buildTimelineJson();
  $('jsonOut').value = JSON.stringify(tl, null, 2);
  const btn = $('btnRender'); btn.disabled = true;
  setStatus($('tlStatus'), '已提交渲染…');
  $('tlLog').textContent = ''; $('tlBar').style.width = '0%'; $('tlOut').classList.remove('show');
  const d = await postJSON('/api/render', { timeline_json: tl, output_name: $('outName').value || 'edit_result' });
  if (d.error) { setStatus($('tlStatus'), '提交失败：' + d.error, 'err'); btn.disabled = false; return; }
  trackTask(d.task_id, { st: $('tlStatus'), bar: $('tlBar'), log: $('tlLog'), out: $('tlOut'), vid: $('tlPreview'), path: $('tlOutPath') }, () => { btn.disabled = false; });
});
$('btnCopyJson').addEventListener('click', () => {
  const v = $('jsonOut').value;
  if (!v) { toast('还没有可复制的 JSON', 'err'); return; }
  navigator.clipboard.writeText(v).then(() => toast('timeline JSON 已复制', 'ok')).catch(() => toast('复制失败', 'err'));
});

// ---------------- 任务轮询（通用） ----------------
function trackTask(taskId, ui, onFinish) {
  if (timers[taskId]) clearInterval(timers[taskId]);
  timers[taskId] = setInterval(async () => {
    try {
      const t = await (await fetch('/api/status/' + taskId)).json();
      ui.log.textContent = (t.log || []).join('\n'); ui.log.scrollTop = ui.log.scrollHeight;
      ui.bar.style.width = (t.progress || 0) + '%';
      if (t.status === 'done') {
        clearInterval(timers[taskId]); delete timers[taskId];
        setStatus(ui.st, '✅ 完成', 'ok');
        if (t.output) {
          ui.out.classList.add('show');
          ui.vid.src = '/output/' + encodeURIComponent(t.output);
          ui.path.textContent = '输出: ' + t.output;
        }
        if (onFinish) onFinish(t);
      } else if (t.status === 'error') {
        clearInterval(timers[taskId]); delete timers[taskId];
        setStatus(ui.st, '❌ ' + (t.error || '失败'), 'err');
        if (onFinish) onFinish(t);
      }
    } catch (e) { /* 忽略瞬时错误 */ }
  }, 900);
}

// ---------------- 文本/字幕剪辑 ----------------
let tcMode = 'keep';
document.querySelectorAll('#tcModeSeg button').forEach(b => {
  b.addEventListener('click', () => {
    tcMode = b.dataset.v;
    document.querySelectorAll('#tcModeSeg button').forEach(x => x.classList.toggle('on', x === b));
  });
});
$('tcMatch').addEventListener('change', e => {
  const ph = { keywords: '关键词用 | 或 , 分隔，如：要保留|精彩', regex: '正则表达式，如：^\\d+$', items: '段索引，0 起，逗号分隔，如：0,2,5' }[e.target.value] || '';
  $('tcValue').placeholder = ph;
});
$('btnPickVideo').addEventListener('click', () => {
  if (selMedia && selMedia.kind === 'v') $('tcVideo').value = selMedia.path;
  else toast('素材库中还没有选中视频', 'err');
});
function tcParams() {
  return { video: $('tcVideo').value.trim(), srt: $('tcSrt').value.trim(),
           mode: tcMode, match: $('tcMatch').value, value: $('tcValue').value.trim(),
           padding: $('tcPadding').value, min_keep_dur: $('tcMinDur').value };
}
function tcUI() { return { st: $('tcStatus'), bar: $('tcBar'), log: $('tcLog'), out: $('tcOut'), vid: $('tcPreview'), path: $('tcOutPath') }; }
$('btnTranscribe').addEventListener('click', async () => {
  const p = $('tcVideo').value.trim();
  if (!p) { toast('先填写视频路径', 'err'); return; }
  const btn = $('btnTranscribe'); btn.disabled = true;
  setStatus($('tcStatus'), '已提交转写…（首次会下载模型）');
  $('tcLog').textContent = ''; $('tcBar').style.width = '0%'; $('tcOut').classList.remove('show');
  const d = await postJSON('/api/textjob', { type: 'transcribe', path: p, model: $('tcModel').value });
  if (d.error) { setStatus($('tcStatus'), '提交失败：' + d.error, 'err'); btn.disabled = false; return; }
  trackTask(d.task_id, tcUI(), t => {
    btn.disabled = false;
    if (t.result && t.result.srt) {
      $('tcSrt').value = t.result.srt; $('tcSrtHint').textContent = '转写完成：' + t.result.segments + ' 段 · ' + t.result.srt;
      if (t.result.json) $('aeSeg').value = t.result.json;   // 词级 JSON 供删语气词
      if ($('aeSubs').value.trim() === '') $('aeSubs').value = t.result.srt; // 成片字幕默认用句级 SRT
    }
  });
});
$('btnPlan').addEventListener('click', async () => {
  const p = tcParams();
  if (!p.video || !p.srt) { toast('先填写视频与 SRT 路径', 'err'); return; }
  if (!p.value) { toast('先填写筛选内容（关键词/正则/索引）', 'err'); return; }
  setStatus($('tcStatus'), '规划中…');
  const d = await postJSON('/api/textplan', p);
  if (d.error) { setStatus($('tcStatus'), '规划失败：' + d.error, 'err'); return; }
  $('tcPlanJson').value = JSON.stringify(d, null, 2);
  const note = d.note ? ' · ' + d.note : '';
  $('tcSummary').textContent = '保留 ' + d.kept_segments + '/' + d.total_segments + ' 段 · 成片约 ' + d.kept_seconds + 's' + note;
  setStatus($('tcStatus'), '规划完成，可继续「文本剪辑成片」或复制 JSON 交给 render_timeline', 'ok');
});
$('btnTextCut').addEventListener('click', async () => {
  const p = tcParams();
  if (!p.video || !p.srt) { toast('先填写视频与 SRT 路径', 'err'); return; }
  if (!p.value) { toast('先填写筛选内容', 'err'); return; }
  const btn = $('btnTextCut'); btn.disabled = true;
  setStatus($('tcStatus'), '已提交文本剪辑…');
  $('tcLog').textContent = ''; $('tcBar').style.width = '0%'; $('tcOut').classList.remove('show');
  const name = 'textcut_' + new Date().toISOString().replace(/[-T:]/g, '').slice(0, 14);
  const d = await postJSON('/api/textjob', Object.assign({}, p, { type: 'text_cut', output_name: name }));
  if (d.error) { setStatus($('tcStatus'), '提交失败：' + d.error, 'err'); btn.disabled = false; return; }
  trackTask(d.task_id, tcUI(), t => { btn.disabled = false; });
});
$('btnBurn').addEventListener('click', async () => {
  const p = tcParams();
  if (!p.video || !p.srt) { toast('先填写视频与 SRT 路径', 'err'); return; }
  const btn = $('btnBurn'); btn.disabled = true;
  setStatus($('tcStatus'), '已提交字幕烧录…');
  $('tcLog').textContent = ''; $('tcBar').style.width = '0%'; $('tcOut').classList.remove('show');
  const name = 'burn_' + new Date().toISOString().replace(/[-T:]/g, '').slice(0, 14);
  const d = await postJSON('/api/textjob', {
    type: 'burn', video: p.video, srt: p.srt, output_name: name,
    fontname: $('tcFont').value || 'Microsoft YaHei', fontsize: +$('tcFontSize').value || 0,
    primary: $('tcColor').value || '#FFFFFF'
  });
  if (d.error) { setStatus($('tcStatus'), '提交失败：' + d.error, 'err'); btn.disabled = false; return; }
  trackTask(d.task_id, tcUI(), t => { btn.disabled = false; });
});
$('btnCopyPlan').addEventListener('click', () => {
  const v = $('tcPlanJson').value;
  if (!v) { toast('还没有可复制的内容', 'err'); return; }
  navigator.clipboard.writeText(v).then(() => toast('已复制', 'ok')).catch(() => toast('复制失败', 'err'));
});

// ---------------- 模板 ----------------
let TEMPLATES = [];
async function loadTemplates() {
  let d;
  try { d = await (await fetch('/api/templates')).json(); } catch (e) { d = { error: String(e) }; }
  if (d.error || !d.templates) return;
  TEMPLATES = d.templates;
  renderTemplates();
}
function fieldControl(f) {
  const key = f.key, label = esc(f.label || key), req = f.required !== false;
  const wrap = '<div><label class="lbl">' + label + (req ? '' : ' <span class="hint">(可选)</span>') + '</label>';
  const name = 'name="p_' + key + '"';
  const val = f.default != null ? f.default : '';
  if (f.type === 'select') {
    const opts = (f.options || []).map(o => '<option value="' + esc(o.value) + '"' + (String(o.value) === String(f.default) ? ' selected' : '') + '>' + esc(o.label || o.value) + '</option>').join('');
    return wrap + '<select class="sel" ' + name + '>' + opts + '</select>' + (f.help ? '<div class="hint">' + esc(f.help) + '</div>' : '') + '</div>';
  }
  const isNum = f.type === 'number';
  const attrs = ' ' + name + ' value="' + esc(val) + '"' + (isNum ? ' type="number" step="' + (f.step || 'any') + '"' : ' type="text"') + (f.help ? ' placeholder="' + esc(f.help) + '"' : '');
  return wrap + '<input class="inp mono" ' + attrs + '></div>';
}
function renderTemplates() {
  const box = $('tplList');
  if (!TEMPLATES.length) { box.innerHTML = '<div class="empty" style="grid-column:1/-1">没有可用模板</div>'; return; }
  box.innerHTML = TEMPLATES.map(t => {
    const fields = (t.params || []).map(fieldControl).join('');
    return '<div class="tpl" data-id="' + esc(t.id) + '">'
      + '<p class="t">' + esc(t.name) + '</p><p class="d">' + esc(t.description || '') + '</p>'
      + '<div class="meta">id: ' + esc(t.id) + '</div>'
      + '<form>' + fields
      + '<div class="row" style="justify-content:flex-end"><button type="submit" class="btn acc sm">▶ 渲染</button></div></form></div>';
  }).join('');
  box.querySelectorAll('.tpl').forEach(card => {
    card.addEventListener('click', ev => {
      if (ev.target.closest('form')) return;              // 点表单内不折叠
      card.classList.toggle('open');
    });
    const form = card.querySelector('form');
    form.addEventListener('submit', async ev => {
      ev.preventDefault();
      const t = TEMPLATES.find(x => x.id === card.dataset.id);
      const params = {};
      form.querySelectorAll('[name]').forEach(inp => {
        const key = inp.name.slice(2);
        const f = (t.params || []).find(x => x.key === key);
        let v = inp.value.trim();
        if (v === '') { if (f && f.required === false) return; }
        if (f && f.type === 'number') v = Number(v);
        params[key] = v;
      });
      runTemplate(t.id, t.name, params);
    });
  });
}
async function runTemplate(id, name, params) {
  $('tplRunCard').style.display = 'block';
  $('tplRunCard').scrollIntoView({ behavior: 'smooth', block: 'start' });
  $('tplRunTitle').textContent = '渲染 · ' + name;
  setStatus($('tplStatus'), '已提交…'); $('tplLog').textContent = ''; $('tplBar').style.width = '0%'; $('tplOut').classList.remove('show');
  const oname = id + '_' + new Date().toISOString().replace(/[-T:]/g, '').slice(0, 14);
  const d = await postJSON('/api/template_render', { template_id: id, params, output_name: oname });
  if (d.error) { setStatus($('tplStatus'), '提交失败：' + d.error, 'err'); return; }
  trackTask(d.task_id, { st: $('tplStatus'), bar: $('tplBar'), log: $('tplLog'), out: $('tplOut'), vid: $('tplPreview'), path: $('tplOutPath') }, () => {});
}



// ---------------- 一键智能成片 (01) ----------------
let aeMode = 'talk';
document.querySelectorAll('#aeModeSeg button').forEach(b => b.addEventListener('click', () => {
  aeMode = b.dataset.v;
  document.querySelectorAll('#aeModeSeg button').forEach(x => x.classList.toggle('on', x === b));
  const talk = aeMode === 'talk', beat = aeMode === 'beat';
  $('aeTalkFields').style.display = talk ? '' : 'none';
  $('aeBeatFields').style.display = beat ? '' : 'none';
  $('aeLookWrap').style.display = beat ? 'none' : '';
  const hints = { talk: '自动去静音 · 可删语气词 · 字幕随剪辑自动重排', clean: '只删除静音，其余画面声音原样保留', beat: '按 BGM 节拍自动切镜并混音' };
  $('aeModeHint').textContent = hints[aeMode] || '';
}));
let aeBeat = 1;
document.querySelectorAll('#aeBeatSeg button').forEach(b => b.addEventListener('click', () => {
  aeBeat = +b.dataset.v;
  document.querySelectorAll('#aeBeatSeg button').forEach(x => x.classList.toggle('on', x === b));
}));
function showAeMeta() {
  const p = $('aeVideo').value.trim();
  if (!p) { $('aeVideoMeta').textContent = ''; return; }
  probePath(p).then(d => { $('aeVideoMeta').textContent = d ? fmtDur(d) : ''; });
}
$('aeVideo').addEventListener('input', showAeMeta);
$('aePickVideo').addEventListener('click', () => {
  if (selMedia && selMedia.kind === 'v') { $('aeVideo').value = selMedia.path; showAeMeta(); }
  else toast('素材库中还没有选中视频', 'err');
});
$('aePickBgm').addEventListener('click', () => {
  if (selMedia && selMedia.kind === 'a') $('aeBgm').value = selMedia.path;
  else toast('素材库中还没有选中音频', 'err');
});
$('btnAuto').addEventListener('click', async () => {
  const v = $('aeVideo').value.trim();
  if (!v) { toast('先选择视频素材', 'err'); return; }
  const body = { mode: aeMode, video: v };
  if (aeMode === 'beat') {
    const bgm = $('aeBgm').value.trim();
    if (!bgm) { toast('卡点模式需要 BGM', 'err'); return; }
    body.bgm_file = bgm;
    body.target_dur = +$('aeTarget').value || 30;
    body.beat_unit = aeBeat;
  } else {
    body.segments_json = $('aeSeg').value.trim();
    body.subtitle_srt = $('aeSubs').value.trim();
    body.fillers = $('aeFillers').value || '嗯,啊,呃,那个,这个,就是,然后,um,uh';
    body.look = $('aeLook').value || 'none';
    body.threshold = +$('aeThr').value || -35;
    body.min_silence = +$('aeMinSil').value || 0.3;
    body.padding = +$('aePad').value || 0.15;
  }
  const btn = $('btnAuto'); btn.disabled = true;
  setStatus($('aeStatus'), '已提交智能成片…');
  $('aeLog').textContent = ''; $('aeBar').style.width = '0%'; $('aeOut').classList.remove('show'); $('aeSummary').textContent = '';
  const d = await postJSON('/api/autoedit', body);
  if (d.error) { setStatus($('aeStatus'), '提交失败：' + d.error, 'err'); btn.disabled = false; return; }
  trackTask(d.task_id, { st: $('aeStatus'), bar: $('aeBar'), log: $('aeLog'), out: $('aeOut'), vid: $('aePreview'), path: $('aeOutPath') }, t => {
    btn.disabled = false;
    if (t.result) {
      const r = t.result;
      $('aeSummary').textContent = '原 ' + (r.src_dur_s != null ? r.src_dur_s : '?') + 's → 成片 ' + (r.out_dur_s != null ? r.out_dur_s : '?') + 's (' + (r.ratio != null ? r.ratio : '?') + '%) · ' + ((r.notes || []).join(' + ') || '完成');
    }
  });
});



// ---------------- 调色 (05) ----------------
const GR_PRESETS = ['none', 'vivid', 'warm', 'cool', 'soft', 'food', 'cinema', 'bw'];
function showGrMeta() {
  const p = $('grVideo').value.trim();
  if (!p) { $('grMeta').textContent = ''; return; }
  probePath(p).then(d => { $('grMeta').textContent = d ? fmtDur(d) : ''; });
}
$('grVideo').addEventListener('input', showGrMeta);
$('grPick').addEventListener('click', () => {
  if (selMedia && selMedia.kind === 'v') { $('grVideo').value = selMedia.path; showGrMeta(); }
  else toast('素材库中还没有选中视频', 'err');
});
$('grApply').addEventListener('click', async () => {
  const v = $('grVideo').value.trim();
  if (!v) { toast('先选择视频素材', 'err'); return; }
  const params = {
    src_video: v, preset: $('grPreset').value,
    temperature: +$('grTemp').value || 0, tint: +$('grTint').value || 0,
    exposure_pct: +$('grExpo').value || 0, contrast_pct: +$('grContrast').value || 0,
    saturation_pct: +$('grSat').value || 0, gamma: +$('grGamma').value || 1,
    vignette_pct: +$('grVig').value || 0, grain_pct: +$('grGrain').value || 0,
    lut: $('grLut').value.trim()
  };
  const btn = $('grApply'); btn.disabled = true;
  setStatus($('grStatus'), '已提交调色…');
  $('grLog').textContent = ''; $('grBar').style.width = '0%'; $('grOut').classList.remove('show'); $('grSummary').textContent = '';
  const name = 'grade_' + new Date().toISOString().replace(/[-T:]/g, '').slice(0, 14);
  const d = await postJSON('/api/template_render', { template_id: 'color_grade', params, output_name: name });
  if (d.error) { setStatus($('grStatus'), '提交失败：' + d.error, 'err'); btn.disabled = false; return; }
  trackTask(d.task_id, { st: $('grStatus'), bar: $('grBar'), log: $('grLog'), out: $('grOut'), vid: $('grPreview'), path: $('grOutPath') }, t => {
    btn.disabled = false;
    if (t.params) { $('grSummary').textContent = '预设: ' + (t.params.preset || 'custom'); }
  });
});

// ---------------- 启动 ----------------
document.addEventListener('DOMContentLoaded', () => {
  $('logoScissors').innerHTML = ic('scissors', 18);
  $('icFolder').innerHTML = ic('folder', 15);
  $('icSearch').innerHTML = ic('search', 14);
  $('icMediaInfo').innerHTML = ic('file', 18);
  $('icMediaInfo').className = 'ic';
  // 图标元素保持 inline 布局
  ['icFolder', 'icSearch'].forEach(id => { $(id).style.display = 'inline-flex'; });
  $('btnScan').innerHTML = ic('search', 13) + ' 扫描';
  $('btnAddClip').innerHTML = ic('plus', 14) + ' 添加到时间线';
  $('btnRender').innerHTML = ic('play', 13) + ' 渲染成片';
  $('btnTranscribe').innerHTML = ic('mic', 14) + ' 转写生成字幕';
  $('btnPlan').innerHTML = '① 规划';
  $('btnTextCut').innerHTML = '② 文本剪辑成片';
  $('btnBurn').innerHTML = '③ 烧录字幕';
  $('btnClear').innerHTML = ic('trash', 12) + ' 清空';
  $('btnCopyJson').innerHTML = ic('copy', 12) + ' 复制';
  $('btnCopyPlan').innerHTML = ic('copy', 12) + ' 复制 JSON';
  $('btnPickVideo').innerHTML = '从素材库取';
  switchView('auto');
  $('tcMatch').dispatchEvent(new Event('change'));
  renderClips();
  scan();
  loadTemplates();
});
// ================= 06 可视化审片 v2 (时间轴 + 缩略图 + 逐镜头修改) =================
const DIR = { plan: [], cur: -1, cands: {}, loaded: false };
const BEATCOL = { p1: '#4e79a7', p2: '#f28e2b', p3: '#e15759', p4: '#76b7b2', p5: '#59a14f', p6: '#edc948', '?': '#888' };
function dirSrc(s) { return s.src_abs || s.src_rel || ''; }
function dirMid(s) { return (Number(s.t0) + Number(s.t1)) / 2; }
function dirFrame(src, t, w) { return '/api/dir_frame?src=' + encodeURIComponent(src || '') + '&t=' + (Number(t) || 0) + '&w=' + (w || 160); }
function dirTs(sec) { const m = Math.floor(sec / 60), x = sec % 60; return String(m).padStart(2, '0') + ':' + String(Math.floor(x)).padStart(2, '0'); }
function dirRoleCN(r) { return ({ wide: '全景', medium: '中景', mcu: '中近景', closeup: '近景', reaction: '反应' })[r] || r || '-'; }

async function loadDir() {
  const d = await postJSON('/api/direct_plan', {});
  if (d.error) { toast(d.error, 'err'); return; }
  DIR.plan = (d.plan || []).map((s, i) => Object.assign({}, s, { i: i, src_abs: s.src || s.src_rel || '' }));
  DIR.cands = d.candidates_by_beat || {};
  DIR.cur = -1; DIR.loaded = true;
  recomputeSeg();
  renderDirStrip(); renderDirList();
  updProg();
  const outEl = $('dirOut');
  if (outEl) {
    if (d.output_url) outEl.innerHTML = '当前成片: <a target="_blank" href="/output/' + d.output_url + '">另开窗口</a> · 计划来源: ' + (d.plan_source || '-');
    else outEl.textContent = '计划来源: ' + (d.plan_source || '-') + '（尚未渲染当前计划）';
  }
  setDirPlayer(d.output_url ? ('/output/' + d.output_url) : '');
  const e = $('dirEmpty'); if (e) e.style.display = '';
}

function renderDirStrip() {
  recomputeSeg();
  const st = $('dirStrip'); if (!st) return;
  const plan = DIR.plan; if (!plan.length) { st.innerHTML = ''; return; }
  const total = plan.reduce((a, s) => a + Math.max(0.2, s.t1 - s.t0), 0);
  let h = '';
  plan.forEach((s) => {
    const w = Math.max(10, (s.t1 - s.t0) / total * 2000);
    h += '<div data-i="' + s.i + '" title="#' + (s.i + 1) + ' ' + (s.beat || '') + ' ' + dirTs(s.t0) + '-' + dirTs(s.t1) + '" style="position:relative;min-width:' + w.toFixed(0) + 'px;height:20px;border-radius:3px;background:' + (BEATCOL[s.beat] || BEATCOL['?']) + ';opacity:' + (s.i === DIR.cur ? '1' : '.6') + ';cursor:pointer;overflow:hidden;color:#fff;font-size:9px">'
      + '<div class="dirSegFill" style="position:absolute;left:0;top:0;bottom:0;width:0%;background:rgba(255,255,255,.6)"></div>'
      + '<span style="position:relative;display:flex;height:100%;align-items:center;justify-content:center">' + (s.i + 1) + '</span></div>';
  });
  st.innerHTML = h;
  st.querySelectorAll('[data-i]').forEach(el => el.addEventListener('click', () => gotoShot(Number(el.dataset.i))));
}


function recomputeSeg() {
  let acc = 0;
  DIR.plan.forEach(s => { s._st = acc; s._en = acc + Math.max(0.2, s.t1 - s.t0); acc = s._en; });
  DIR.total = acc;
}
function activeShotAt(t) {
  let idx = -1;
  DIR.plan.forEach((s, i) => { if (t >= s._st - 0.05 && t <= s._en + 0.05) idx = i; });
  return idx;
}
function gotoShot(i) {
  recomputeSeg();
  if (i < 0 || i >= DIR.plan.length) return;
  DIR.cur = i; showDir(i);
  const v = $('dirPlayer');
  if (v && v.src && isFinite(v.duration) && v.duration > 0) {
    const st = DIR.plan[i]._st;
    try { v.currentTime = Math.max(0, Math.min(st + 0.02, v.duration - 0.05)); } catch (e) {}
    const p = v.play(); if (p && p.catch) p.catch(() => {});
  }
}
function updProg() {
  recomputeSeg();
  const v = $('dirPlayer');
  if (!v || !v.src) return;
  const t = v.currentTime || 0;
  const st = $('dirStrip'); if (!st) return;
  const bars = st.querySelectorAll('[data-i]');
  DIR.plan.forEach((s, i) => {
    const b = bars[i]; if (!b) return;
    const f = b.querySelector('.dirSegFill'); if (!f) return;
    let pct = 0;
    if (t >= s._en) pct = 100;                                   // 已播完
    else if (t >= s._st && s._en > s._st) pct = Math.min(100, Math.max(0, (t - s._st) / (s._en - s._st) * 100)); // 正在播
    f.style.width = pct + '%';
  });
  const dt = $('dirTime');
  const d = (isFinite(v.duration) && v.duration > 0) ? v.duration : (DIR.total || 0);
  if (dt && d > 0) {
    const idx = activeShotAt(t);
    if (idx >= 0) {
      const s = DIR.plan[idx];
      dt.style.display = 'block';
      dt.textContent = '▶ ' + t.toFixed(1) + 's / ' + d.toFixed(1) + 's · 片段#' + (idx + 1) + ' [' + s._st.toFixed(1) + '–' + s._en.toFixed(1) + 's] 本段已播 ' + (t - s._st).toFixed(1) + 's';
    } else {
      dt.style.display = 'block';
      dt.textContent = '▶ ' + t.toFixed(1) + 's / ' + d.toFixed(1) + 's';
    }
  }
}

function renderDirList() {
  const el = $('dirList'); if (!el) return;
  if (!DIR.plan.length) { el.innerHTML = '<div class="empty">空计划</div>'; return; }
  let h = '';
  DIR.plan.forEach((s) => {
    h += '<div class="m-item' + (s.i === DIR.cur ? ' sel' : '') + '" data-i="' + s.i + '" style="cursor:pointer;border-bottom:1px solid var(--line);padding:6px 8px;display:flex;gap:8px">'
      + '<img loading="lazy" src="' + dirFrame(dirSrc(s), dirMid(s), 150) + '" style="width:120px;height:auto;border-radius:4px;background:#000">'
      + '<div style="flex:1;min-width:0">'
      + '<div style="display:flex;gap:6px;align-items:center"><b>#' + (s.i + 1) + '</b>'
      + '<span style="font-size:10px;color:#fff;background:' + (BEATCOL[s.beat] || BEATCOL['?']) + ';border-radius:8px;padding:1px 6px">' + esc(s.beat || '-') + '</span>'
      + '<span style="font-size:10px;color:var(--txt2)">' + esc(s.spk || '') + ' · ' + dirRoleCN(s.role) + '</span></div>'
      + '<div class="t" style="font-size:12px">' + esc((s.text || '').slice(0, 60)) + '</div>'
      + '<div class="p" style="font-size:10px;color:var(--muted)">' + esc((s.src_rel || '').split('/').pop()) + ' · ' + s.t0.toFixed(2) + '~' + s.t1.toFixed(2) + 's</div>'
      + '</div></div>';
  });
  el.innerHTML = h;
  el.querySelectorAll('.m-item[data-i]').forEach(r => r.addEventListener('click', () => gotoShot(Number(r.dataset.i))));
}

function showDir(i) {
  DIR.cur = i; renderDirStrip(); renderDirList();
  const s = DIR.plan[i]; const det = $('dirDetail'); if (!det) return;
  const src = dirSrc(s);
  let strip = '';
  const n = 8;
  for (let k = 0; k < n; k++) {
    const t = Number(s.t0) + (Number(s.t1) - Number(s.t0)) * k / (n - 1 || 1);
    strip += '<img loading="lazy" src="' + dirFrame(src, t, 150) + '" style="height:74px;border-radius:4px;flex:0 0 auto">';
  }
  const cands = (DIR.cands[s.beat] || []);
  let candHtml = cands.length
    ? cands.map((c, k) => '<div class="row" style="gap:6px;margin-top:4px;align-items:center">'
        + '<img loading="lazy" src="' + dirFrame(c.file, (c.t0 + c.t1) / 2, 110) + '" style="width:88px;border-radius:4px;background:#000">'
        + '<div style="flex:1;font-size:11px;line-height:1.35"><b>' + esc(c.file.split('/').pop()) + '</b><br>' + c.t0 + '~' + c.t1 + 's · 干净:' + (c.clean ? '是' : '否') + ' · 覆盖:' + c.coverage + '</div>'
        + '<button class="btn" data-cand="' + k + '">采用</button></div>').join('')
    : '<div class="hint">无候选</div>';
  let h = '<div style="display:flex;gap:6px;align-items:center"><b>#' + (i + 1) + ' · beat ' + esc(s.beat || '') + '</b>'
    + '<span style="margin-left:auto;font-size:11px;color:var(--muted)">' + (s.reason || '') + '</span></div>'
    + '<div style="display:flex;gap:4px;overflow-x:auto;padding:6px 0">' + strip + '</div>'
    + '<label class="lbl">源文件</label><input class="inp" id="dSrc" style="width:100%" value="' + esc(s.src_rel || '') + '">'
    + '<div class="row" style="gap:6px;margin-top:6px">'
    + '<div style="flex:1"><label class="lbl">入点(s)</label><input class="inp" type="number" step="0.05" id="dT0" value="' + (Number(s.t0) || 0) + '"></div>'
    + '<div style="flex:1"><label class="lbl">出点(s)</label><input class="inp" type="number" step="0.05" id="dT1" value="' + (Number(s.t1) || 0) + '"></div></div>'
    + '<label class="lbl" style="margin-top:6px">景别</label><select class="inp" id="dRole" style="width:100%">'
    + ['wide', 'medium', 'mcu', 'closeup', 'reaction'].map(r => '<option value="' + r + '"' + (s.role === r ? ' selected' : '') + '>' + dirRoleCN(r) + '</option>').join('') + '</select>'
    + '<div style="margin-top:8px"><label class="lbl">该 beat 其它可用 take</label>' + candHtml + '</div>'
    + '<div class="row" style="margin-top:10px;gap:6px"><button class="btn acc" id="btnDirApply" style="flex:1">应用本镜头修改</button></div>'
    + '<div class="hint" style="margin-top:8px">改完点“应用”→ 顶部“保存修改”→“增量渲染”；只重编改动的镜头。</div>';
  det.innerHTML = h;
  $('btnDirApply').addEventListener('click', () => applyDirShot(i));
  det.querySelectorAll('[data-cand]').forEach(btn => btn.addEventListener('click', () => {
    const c = cands[Number(btn.dataset.cand)];
    if (!c) return;
    DIR.plan[i].src_rel = c.file; DIR.plan[i].src_abs = c.file;
    DIR.plan[i].t0 = c.t0; DIR.plan[i].t1 = c.t1; DIR.plan[i].dur_s = +(c.t1 - c.t0).toFixed(2);
    toast('已切换候选: ' + c.file.split('/').pop(), 'ok'); showDir(i);
  }));
}

function applyDirShot(i) {
  const s = DIR.plan[i];
  s.src_rel = $('dSrc').value.trim();
  s.t0 = Number($('dT0').value); s.t1 = Number($('dT1').value); s.role = $('dRole').value;
  if (s.t1 - s.t0 < 0.25) { toast('出点需大于入点', 'err'); return; }
  s.src_abs = s.src_rel; s.dur_s = +(s.t1 - s.t0).toFixed(2);
  renderDirStrip(); renderDirList();
  toast('镜头 #' + (i + 1) + ' 已应用', 'ok');
}

function setDirPlayer(url) {
  const v = $('dirPlayer'); if (!v) return;
  const dt = $('dirTime');
  if (url) { v.src = url; v.style.display = 'block'; if (dt) dt.style.display = 'block'; } else { v.removeAttribute('src'); v.style.display = 'none'; if (dt) dt.style.display = 'none'; }
  v.load && v.load();
}

function saveDir() {
  const plan = DIR.plan.map(s => ({ beat: s.beat, text: s.text, src_rel: s.src_rel, t0: s.t0, t1: s.t1, role: s.role, reason: s.reason }));
  postJSON('/api/dir_save', { plan }).then(d => toast(d.ok ? ('已保存 ' + d.shots + ' 镜') : (d.error || '保存失败'), d.ok ? 'ok' : 'err'));
}

async function renderDirIncremental() {
  if (!DIR.plan.length) return;
  const dirty = DIR.cur >= 0 ? [DIR.cur] : DIR.plan.map((_, i) => i);
  const shots = DIR.plan.map(s => ({ src_rel: s.src_rel, t0: s.t0, t1: s.t1, text: s.text || '' }));
  $('dirOut').textContent = '提交渲染…';
  const d = await postJSON('/api/dir_render', { shots: shots, dirty: dirty, width: 1280, height: 720 });
  if (d.error) { toast(d.error, 'err'); return; }
  pollDirStatus(d.task_id);
}

function pollDirStatus(taskId) {
  const iv = setInterval(async () => {
    const t = await (await fetch('/api/status/' + taskId)).json();
    if (t.error) { clearInterval(iv); toast(t.error, 'err'); return; }
    const logs = t.log || [];
    $('dirOut').textContent = logs.length ? logs[logs.length - 1] : '渲染中…';
    if (t.status === 'done') {
      clearInterval(iv);
      $('dirOut').innerHTML = '完成 → <a target="_blank" href="/output/' + t.output + '">另开窗口</a>';
      setDirPlayer('/output/' + t.output);
      toast('渲染完成', 'ok');
    } else if (t.status === 'error') {
      clearInterval(iv); $('dirOut').textContent = '';
      toast('渲染失败: ' + (t.error || ''), 'err');
    }
  }, 1500);
}

document.addEventListener('DOMContentLoaded', () => {
  [['btnDirReload', loadDir], ['btnDirSave', saveDir], ['btnDirRender', renderDirIncremental]].forEach(([id, fn]) => {
    const b = $(id); if (b) b.addEventListener('click', fn);
  });
  const tb = document.querySelector('#tabs .tab[data-view="direct"]');
  if (tb) tb.addEventListener('click', () => { if (!DIR.loaded) loadDir(); });
  const vp = $('dirPlayer');
  if (vp) ['timeupdate', 'loadedmetadata', 'seeked', 'play'].forEach(ev => vp.addEventListener(ev, updProg));
  loadDir();   // 预载计划与成片, 打开页面即可用
});
