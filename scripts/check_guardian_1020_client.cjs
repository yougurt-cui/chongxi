// Check actual API results through unmodified mini-program mapping and option handlers.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const repo = process.argv[2], requestedRef = process.argv[3] || 'origin/dev';
const ref = execFileSync('git', ['-C', repo, 'rev-parse', '--verify', '--end-of-options', requestedRef + '^{commit}'], {encoding: 'utf8'}).trim();
const source = execFileSync('git', ['-C', repo, 'show', `${ref}:utils/assistant-chat.js`], {encoding: 'utf8'})
  .replace(/^import .*;$/gm, '').replace(/export /g, '');
let methods, map;
vm.runInNewContext(source + '\nexpose(chatMethods,mapChatMessage);', {
  expose: (m, mapper) => {methods = m; map = mapper;},
});
const messages = JSON.parse(fs.readFileSync(0, 'utf8'));
let confirmations = 0, recommendations = 0, statuses = 0;
for (const raw of messages) {
  const view = map(raw);
  assert.equal(view.text, raw.content);
  assert.ok(view.blocks.length);
  assert.equal(view.candidates.length, raw.result?.food_candidates?.length || 0);
  if (raw.result?.health_analysis?.is_multiple) {
    if (view.healthSubjects) {
      assert.equal(view.healthSubjects.length, raw.result.health_analysis.subjects.length);
      assert.equal(view.healthPrompt, raw.result.health_analysis.prompt);
    } else {
      assert.ok(view.text.startsWith('**猫咪的整体饮食解读**'));
    }
    for (let i = 0; i < raw.interaction.options.length; i++) {
      let payload;
      methods.chooseChatOption.call({data: {chatMessages: [view]}, sendCloudMessage: value => {payload = value;}},
        {currentTarget: {dataset: {id: view.id, index: i}}});
      assert.equal(payload.interaction.slot, 'food_analysis_recommendation');
      assert.equal(payload.interaction.value, raw.interaction.options[i].value);
      if (payload.interaction.analysis_id) assert.equal(payload.interaction.analysis_id, raw.result.analysis_id);
      confirmations++;
    }
  }
  if (view.candidates.length) {
    recommendations++;
    const blocks = JSON.stringify(view.blocks);
    assert.ok(blocks.includes('减重'));
    for (const candidate of view.candidates) {
      assert.ok(candidate.strengths.length <= 2 && candidate.cautions.length <= 2);
      // Candidate titles/details remain in their cards, not duplicated in display blocks.
      assert.ok(!blocks.includes(candidate.product_name));
    }
  }
  if (raw.result?.daily_status) {
    statuses++;
    assert.ok(view.text.includes('次数'));
  }
}
assert.ok(confirmations >= 4 && recommendations >= 1 && statuses >= 2);
console.log(JSON.stringify({ok: true, clientRef: ref, responses: messages.length, confirmations, recommendations, statuses}));
