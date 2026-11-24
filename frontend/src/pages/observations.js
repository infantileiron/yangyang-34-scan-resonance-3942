import { download, get, importFile, post } from '../api/client.js';
import { table } from '../components/common/data.js';
import { intro } from '../components/common/frame.js';
import { fieldControl, formError } from '../components/common/forms.js';
import { BatchStatus, Role, batchStatusName } from '../constants/enums.js';
import { bindDownloads, bindForm, notify } from '../hooks/actions.js';
import { formValues } from '../types/form.js';
import { esc, timeLabel } from '../utils/html.js';

const measure = value => value === null || value === undefined ? '—' : esc(value);

function effectiveCell(item, key) {
  const effective = item.effective;
  if (!effective) return measure(item.data[key]);
  const changed = item.data[key] !== effective[key];
  return `${measure(effective[key])}${changed ? `<span class="badge" title="原值 ${esc(item.data[key])} · 补正批次 #${effective.batch_id}">补正</span>` : ''}`;
}
function correctionRows(batch) {
  return (batch.items || []).map(item => ({
    ...item,
    old: item.old_amplitude === null || item.old_streak === null ? '未登记' : `${item.old_amplitude} / ${item.old_streak}`,
    next: `${item.amplitude} / ${item.streak}`,
  }));
}

function batchTable(batches, writable) {
  const rows = batches.map(batch => ({
    ...batch,
    statusHtml: `<span class="status status-${esc(batch.status)}">${esc(batchStatusName[batch.status] || batch.status_label)}</span>`,
    reasonHtml: batch.reason ? esc(batch.reason) : '—',
    action: writable && batch.status !== BatchStatus.APPLIED
      ? `<button data-verify="${batch.id}">整批核对</button>` : '—',
  }));
  const detail = batches.map(batch => `<details class="batch-detail"><summary>批次 #${batch.id} · ${esc(batch.title)} · ${esc(batchStatusName[batch.status] || batch.status_label)}${batch.reason ? ` · <span class="quiet">${esc(batch.reason)}</span>` : ''}</summary>
    ${table([{label:'扫描台',key:'desk'},{label:'扫描批次',key:'scan'},{label:'旧值（幅值/纹影）',key:'old'},{label:'补正值（幅值/纹影）',key:'next'},{label:'状态',html:row => row.effective ? '生效中' : '未生效'}], correctionRows(batch), '批次内没有测次。')}</details>`).join('');
  return `${table([
    {label:'批次',html:row => `#${row.id}`},
    {label:'名称',key:'title'},
    {label:'测次',key:'item_count'},
    {label:'状态',html:row => row.statusHtml},
    {label:'待复核原因',html:row => row.reasonHtml},
    {label:'核对时间',html:row => timeLabel(row.verified_at)},
    {label:'操作',html:row => row.action},
  ], rows, '还没有补正批次。')}${detail}`;
}

export async function observationPage(spec, user, route) {
  const offset = Math.max(0, Number.parseInt(route.id, 10) || 0);
  const [{total, items}, batches] = await Promise.all([
    get(`/observations?offset=${offset}&limit=20`),
    get('/corrections'),
  ]);
  const writable = user.role !== Role.VIEWER;
  const supervisor = user.role === Role.SUPERVISOR;
  return `${intro('数据台账', spec.source, `<span class="count">${total} <small>条${esc(spec.record_name)}</small></span>`)}
    ${writable ? `<div class="tools"><details><summary>登记一条${esc(spec.record_name)}</summary><form id="record-form"><div class="form-grid">${spec.fields.map(field => fieldControl(field)).join('')}</div>${formError}<button class="primary">保存记录</button></form></details>
    <details><summary>按模板导入 CSV</summary><form id="import-form"><p>使用 UTF-8 CSV，每次最多 500 条。重复行自动跳过。</p><button type="button" data-download="/observations/template">下载模板</button><label class="field"><span>CSV 文件</span><input type="file" name="file" accept=".csv,text/csv" required></label>${formError}<button class="primary">导入文件</button></form></details></div>` : ''}
    <section class="panel"><div class="panel-heading"><h2>${esc(spec.record_name)}明细</h2>${writable ? '<a class="text-link" href="#analysis">发起分析 →</a>' : ''}</div>
    ${table([{key:'id',label:'记录号'},{label:'扫描台',html:row => esc(row.data.desk)},{label:'扫描批次编号',html:row => esc(row.data.scan)},
      {label:'幅值（μm）',html:row => effectiveCell(row,'amplitude')},
      {label:'纹影（px）',html:row => effectiveCell(row,'streak')}],
      items, '暂无观测。分析员可先导入 CSV 或登记样本。')}
    ${total ? `<div class="pagination"><span>第 ${offset + 1}–${Math.min(total,offset + 20)} 条</span>${offset > 0 ? `<a href="#observations/${Math.max(0, offset-20)}">上一页</a>` : ''}${offset+20 < total ? `<a href="#observations/${offset+20}">下一页</a>` : ''}</div>` : ''}</section>
    ${writable ? `<section class="panel"><div class="panel-heading"><h2>成批补正（共振幅值与纹影长度）</h2></div>
      <form id="correction-form" class="panel-body"><p class="aside-note">整批提交；批内有原测次尚未登记时整批暂挂，补齐后自动重核。原测次重复登记或旧值已被其他批次补正时整批不写入，原因保留在下方待复核。</p>
      <div class="form-grid"><label class="field"><span>补正批次名称</span><input name="title" required maxlength="120" placeholder="例如 三号台复测补正"></label><label class="field"><span>补正说明</span><input name="note" maxlength="300" placeholder="来源或核对依据"></label></div>
      <div id="correction-lines"></div><button type="button" id="add-line">增加一条测次</button>${formError}<button class="primary">提交补正批次</button></form>
      ${batchTable(batches, supervisor)}</section>` : `<section class="panel"><div class="panel-heading"><h2>补正批次</h2></div>${batchTable(batches, false)}</section>`}`;
}

function lineRow(spec, optional = false) {
  const markup = `<div class="form-grid correction-line">${spec.fields.map(field => fieldControl(field)).join('')}</div>`;
  if (!optional) return markup;
  return markup.replace(/ required/g, '')
    .replace('</div>', '<button type="button" class="remove-line">移除此条</button></div>');
}

function readLines(form, spec) {
  const keys = spec.fields.map(field => field.key);
  return [...form.querySelectorAll('.correction-line')].map(line => {
    const data = new FormData(line);
    return Object.fromEntries(keys.flatMap(key => {
      const value = data.get(key);
      return value === null || String(value).trim() === '' ? [] : [[key, key === 'amplitude' || key === 'streak' ? Number(value) : String(value).trim()]];
    }));
  }).filter(line => Object.keys(line).length);
}

function correctionPayload(form, spec) {
  return {
    title: form.elements.title.value,
    note: form.elements.note.value,
    items: readLines(form, spec),
  };
}

export function wireObservations(root, spec, refresh) {
  bindDownloads(root, download);
  bindForm(root.querySelector('#record-form'), async form => {
    await post('/observations', formValues(form, spec.fields));
    notify('记录已保存');
    if (location.hash === '#observations' || !location.hash) refresh();
    else location.hash = '#observations';
  });
  bindForm(root.querySelector('#import-form'), async form => {
    const report = await importFile(form.elements.file.files[0]);
    notify(`已导入 ${report.added} 条 · 跳过 ${report.skipped} 条重复记录`);
    refresh();
  });
  const form = root.querySelector('#correction-form');
  const lines = root.querySelector('#correction-lines');
  if (form && lines) {
    lines.innerHTML = lineRow(spec);
    root.querySelector('#add-line').addEventListener('click', () => lines.insertAdjacentHTML('beforeend', lineRow(spec, true)));
    lines.addEventListener('click', event => {
      if (event.target.matches('.remove-line')) event.target.closest('.correction-line').remove();
    });
    bindForm(form, async node => {
      const batch = await post('/corrections', correctionPayload(node, spec));
      notify(`补正批次已提交：${batchStatusName[batch.status] || batch.status_label}`);
      refresh();
    });
  }
  root.querySelectorAll('[data-verify]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const batch = await post(`/corrections/${button.dataset.verify}/verify`, {});
      notify(`整批核对完成：${batchStatusName[batch.status] || batch.status_label}${batch.reason ? ` · ${batch.reason}` : ''}`, batch.status !== BatchStatus.APPLIED);
      refresh();
    } catch (err) { notify(err.message, true); button.disabled = false; }
  }));
}
