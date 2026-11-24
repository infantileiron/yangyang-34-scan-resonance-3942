// 补正批次页面纯渲染冒烟测试：用 fetch 打桩，直接把页面拼成字符串检查。
import assert from 'node:assert/strict';
import { observationPage } from '../src/pages/observations.js';

const spec = {
  source: '矿物标本条纹校靶扫描记录及扫描车共振计测次',
  record_name: '扫描参数配对',
  fields: [
    { key: 'desk', label: '扫描台', type: 'text', example: '矿物三号台' },
    { key: 'scan', label: '扫描批次编号', type: 'text', example: 'SK-03-X21' },
    { key: 'amplitude', label: '扫描车共振幅值（μm）', type: 'number', min: 0, max: 200, example: 12.4 },
    { key: 'streak', label: '暗纹平均长度（px）', type: 'number', min: 0, max: 1000, example: 6.8 },
  ],
  parameters: [{ key: 'alert', label: '单次纹影长度复勘线（px）', type: 'number', default: 9 }],
};

const observations = {
  total: 2,
  offset: 0,
  items: [
    { id: 1, data: { desk: '矿物三号台', scan: 'SK-03-X01', amplitude: 12.0, streak: 6.0 },
      effective: { amplitude: 12.4, streak: 6.8, batch_id: 7, batch_title: '三号台补正' } },
    { id: 2, data: { desk: '窗边一号台', scan: 'CB-01-01', amplitude: 6.0, streak: 12.0 }, effective: null },
  ],
};

const batches = [
  { id: 7, title: '三号台补正', note: '', actor: 'analyst@demo.local', status: 'applied',
    status_label: '已生效', reason: null, created_at: '2026-11-24T00:00:00+00:00', verified_by: 'analyst@demo.local',
    verified_at: '2026-11-24T00:00:00+00:00', item_count: 1,
    items: [{ desk: '矿物三号台', scan: 'SK-03-X01', amplitude: 12.4, streak: 6.8,
              old_amplitude: 12.0, old_streak: 6.0, effective: true, observation_id: 1 }] },
  { id: 8, title: '撞车补正', note: '', actor: 'analyst@demo.local', status: 'conflicted',
    status_label: '待复核', reason: '矿物三号台 / SK-03-X01 已被批次 #7 补正为 12.4/6.8',
    created_at: '2026-11-24T00:00:00+00:00', verified_by: 'analyst@demo.local',
    verified_at: '2026-11-24T00:00:00+00:00', item_count: 1,
    items: [{ desk: '矿物三号台', scan: 'SK-03-X01', amplitude: 12.6, streak: 7.0,
              old_amplitude: 12.0, old_streak: 6.0, effective: false, observation_id: 1 }] },
];

globalThis.fetch = async url => {
  const body = String(url).includes('/corrections') ? batches : observations;
  return { ok: true, status: 200, json: async () => body };
};

const supervisor = { role: 'supervisor', email: 'supervisor@demo.local', token: 't' };
const html = await observationPage(spec, supervisor, { page: 'observations' });

assert.match(html, /成批补正/);
assert.match(html, /data-verify="8"/);          // 待复核批次可整批核对
assert.doesNotMatch(html, /data-verify="7"/);   // 已生效批次不再核对
assert.match(html, /已被批次 #7 补正为 12\.4\/6\.8/); // 待复核原因可查
assert.match(html, /补正<\/span>/);              // 台账显示补正标记
assert.match(html, /12\.4/);
assert.match(html, /原值 12/);

const viewer = { role: 'viewer', email: 'viewer@demo.local', token: 't' };
const readOnly = await observationPage(spec, viewer, { page: 'observations' });
assert.doesNotMatch(readOnly, /correction-form/);
assert.doesNotMatch(readOnly, /data-verify/);
assert.match(readOnly, /已被批次 #7 补正为 12\.4\/6\.8/);

console.log('observation page render ok');
