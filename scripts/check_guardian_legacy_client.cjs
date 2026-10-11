// Consume actual local API responses with an unmodified, pinned mini-program revision.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const repo = process.argv[2], ref = process.argv[3];
const source = path => execFileSync('git', ['-C', repo, 'show', `${ref}:${path}`], {encoding: 'utf8'});
const js = path => source(path).replace(/^import .*;$/gm, '').replace(/export /g, '');
const ctx = extra => ({console, setTimeout, clearTimeout, Uint8Array, ArrayBuffer, ...extra});
function page(def) {
  return {...def, data: JSON.parse(JSON.stringify(def.data || {})), setData(patch) {
    for (const [key, value] of Object.entries(patch)) {
      const parts = key.split('.'); let current = this.data;
      for (const part of parts.slice(0, -1)) current = current[part];
      current[parts.at(-1)] = value;
    }
  }};
}
(async () => {
  let foodFns;
  const wx = {getStorageSync: () => 'test-token', removeStorageSync() {}, showToast() {}};
  vm.runInNewContext(js('utils/food-submission.js') + '\nexpose({submissionView,submitFood});', ctx({
    expose: value => {foodFns = value}, wx, login: async () => {}, config: {apiBaseUrl: 'https://example.test'}
  }));
  let submissionDef, used = 0;
  vm.runInNewContext(js('pages/pet-space/food-submissions.js'), ctx({
    Page: value => {submissionDef = value}, wx: {...wx, navigateBack() {}},
    api: async () => {}, requireSession: () => true, hasSession: () => true,
    accountKey: () => 'test-account', errorText: e => e.message,
    login: async () => {}, submitFood: foodFns.submitFood, submissionView: foodFns.submissionView,
    downloadPetImage: async url => url
  }));
  const template = source('pages/pet-space/food-submissions.wxml');
  assert.ok(template.includes('selected.statusText'));
  for (const row of input.submissions) {
    const view = foodFns.submissionView(row);
    assert.equal(view.statusText, row.status_label);
    const p = page(submissionDef); p.data.selected = view;
    p.getOpenerEventChannel = () => ({emit: () => {used++}});
    const before = used; p.useInChat();
    assert.equal(used - before, row.status !== 'cancelled' && row.recognition_status === 'success' ? 1 : 0);
    assert.equal(['pending','processing'].includes(view.recognition_status), ['pending','processing'].includes(row.recognition_status));
  }
  console.log('PASS legacy upload labels, polling states, successful analysis entry and failed/empty guards');

  const profile = input.responses.find(r => r.method === 'PATCH' && r.body.item?.food_product === 'B').body.item;
  let store = {}, dietFns, listDef, calls = [];
  const storeFns = {loadStore: () => store, saveStore: patch => {store = {...store, ...patch}}};
  vm.runInNewContext(js('utils/diet-history.js') + '\nexpose({petDiet,rememberDiet,dietHistory});', ctx({
    ...storeFns, expose: value => {dietFns = value}
  }));
  vm.runInNewContext(js('pages/pet-space/food-list.js'), ctx({
    Page: value => {listDef = value}, wx, accountKey: () => 'test-account',
    requireSession: () => true, hasSession: () => true, errorText: e => e.message,
    ...dietFns, api: async (path, method = 'GET', data) => {
      calls.push({path, method, data});
      return method === 'PATCH' ? {ok: true, item: profile} : {ok: true, items: [profile]};
    }
  }));
  const list = page(listDef); list.petId = profile.id; list._active = true;
  await list.load(); assert.equal(list.data.items[0].product, 'B');
  list.edit(); list.data.form = {brand: '乙牌', product: 'B'};
  await list.save();
  const save = calls.find(c => c.method === 'PATCH');
  assert.equal(save.path, '/cat-profiles/' + profile.id);
  assert.deepEqual(JSON.parse(JSON.stringify(save.data)), {diet: {brand: '乙牌', product: 'B'}});
  assert.equal(list.data.error, ''); assert.equal(list.data.saving, false);
  assert.ok(input.responses.some(r => r.method === 'PATCH' && r.status === 200 && r.body.item?.food_product === 'B'));
  assert.ok(!calls.some(c => c.path.includes('diet-history')));
  console.log('PASS legacy current-food page and PATCH payload with unchanged profile response');

  let chatFns, responseBody;
  vm.runInNewContext(js('utils/assistant-chat.js') + '\nexpose({chatMethods,mapChatMessage});', ctx({
    expose: value => {chatFns = value}, wx, ...storeFns,
    requireSession: () => true, hasSession: () => true, login: async () => {},
    mapPet: value => value, errorText: e => e.message, accountKey: () => 'test-account',
    setModalData: (p, patch) => p.setData(patch), config: {apiBaseUrl: 'https://example.test'},
    request: async () => ({data: responseBody}),
    api: async () => input.responses.find(r => r.path === '/api/miniprogram/chat/conversations' && r.status === 201).body
  }));
  const chats = input.responses.filter(r => r.method === 'POST' && r.path.endsWith('/messages') && r.status === 200);
  assert.ok(chats.some(r => r.body.message.result?.recommendation?.status === 'ready'));
  assert.ok(chats.some(r => r.body.message.result?.recognition_status === 'unrecognized'));
  for (const r of chats) {
    responseBody = r.body;
    const p = page({data: {boundPet: profile, chatMessages: [], sending: false, inputMessage: '旧版请求'}, ...chatFns.chatMethods});
    p.scrollChat = () => {};
    await p.sendCloudMessage({message: '旧版请求'}, '旧版请求');
    assert.equal(p.data.sending, false);
    assert.equal(p.data.chatMessages.length, responseBody.messages.length + 1);
    assert.ok(p.data.chatMessages.slice(1).every(m => !m.error && m.text));
    assert.deepEqual(Array.from(p.data.chatMessages.slice(1), m => m.id), responseBody.messages.map(m => m.id));
    for (const m of responseBody.messages) {
      const view = chatFns.mapChatMessage(m);
      assert.equal(view.text, m.content);
      assert.equal(view.candidates.length, m.result?.food_candidates?.length || 0);
    }
  }
  console.log(`PASS legacy conversation creation, ordered chat results and recommendation/recognition metadata (${chats.length} responses)`);
  console.log(`PASS new backend responses consumed by unmodified legacy client ${ref}`);
})().catch(error => {console.error(error); process.exitCode = 1});
