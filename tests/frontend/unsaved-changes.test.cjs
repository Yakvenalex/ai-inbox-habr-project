const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
function setup() {
  const handlers = {}, prompts = [];
  let answer = false;
  const window = {addEventListener: (name, fn) => handlers[name] = fn, location:{assign:url=>prompts.push('navigate')}};
  const document = {addEventListener: (name, fn) => handlers[name] = fn};
  runInNewContext(readFileSync('src/ai_inbox/presentation/web/static/unsaved-changes.js','utf8'), {window,document});
  window.InboxUnsaved.ask = async () => {prompts.push('confirm'); return answer;};
  return {api:window.InboxUnsaved,handlers,prompts,answer: value => answer=value};
}
function event(extra={}) {return {prevented:false,preventDefault(){this.prevented=true;},button:0,defaultPrevented:false,...extra};}
function link(extra={}) {return {hasAttribute:()=>false,target:'',protocol:'http:',href:'http://example.test/drafts',getAttribute:()=>'/drafts',...extra};}

test('tracks content, attachments and undo without browser storage', () => {
  const {api}=setup(); let fields={body:'Saved',attachment_ids:[]}; const notices=[];
  const tracker=api.track(()=>fields, dirty=>notices.push(dirty));
  assert.equal(api.hasChanges(),false);
  fields.attachment_ids.push('file'); tracker.check(); assert.equal(api.hasChanges(),true);
  fields.attachment_ids=[]; tracker.check(); assert.equal(api.hasChanges(),false);
  fields.body='AI supplied text'; tracker.check(); assert.equal(api.hasChanges(),true);
  tracker.saved(); assert.equal(api.hasChanges(),false);
  assert.deepEqual(notices,[true,false,true,false]);
});
test('save acknowledgement clears only the submitted snapshot, not newer edits', () => {
  const {api}=setup(); let body='old'; const tracker=api.track(()=>({body}));
  body='submitted'; const submitted=tracker.snapshot(); body='typed during network request';
  tracker.saved(submitted); assert.equal(tracker.dirty(),true);
  body='submitted'; assert.equal(tracker.dirty(),false);
});
test('dirty editors prevent unload; saved editors do not', () => {
  const {api,handlers}=setup(); let body='old'; const tracker=api.track(()=>({body}));
  let e=event(); handlers.beforeunload(e); assert.equal(e.prevented,false);
  body='new'; e=event(); handlers.beforeunload(e); assert.equal(e.prevented,true); assert.equal(e.returnValue,'');
  tracker.saved(); e=event(); handlers.beforeunload(e); assert.equal(e.prevented,false);
});
test('cancelled navigation keeps changes; accepted navigation prompts only once', async () => {
  const {api,handlers,prompts,answer}=setup(); let body='old'; api.track(()=>({body})); body='new';
  const target={closest:()=>link()}; let e=event({target}); await handlers.click(e);
  assert.equal(e.prevented,true); assert.equal(api.hasChanges(),true);
  answer(true); e=event({target}); await handlers.click(e); assert.equal(e.prevented,true);
  e=event(); handlers.beforeunload(e); assert.equal(e.prevented,false); assert.equal(prompts.length,3);
  e=event(); handlers.beforeunload(e); assert.equal(e.prevented,true); // Approval is consumed once.
});
test('downloads, new tabs, anchors, mailto and modified clicks do not lose the current editor', async () => {
  const {api,handlers,prompts}=setup(); let body='old'; api.track(()=>({body})); body='new';
  for (const item of [link({hasAttribute:()=>true}),link({target:'_blank'}),link({getAttribute:()=>'#section'}),link({protocol:'mailto:'})]) {
    const e=event({target:{closest:()=>item}}); await handlers.click(e); assert.equal(e.prevented,false);
  }
  handlers.click(event({ctrlKey:true,target:{closest:()=>link()}}));
  assert.equal(prompts.length,0); assert.equal(api.hasChanges(),true);
});
test('already prevented navigation never produces a second prompt', () => {
  const {api,handlers,prompts}=setup(); let body='old'; api.track(()=>({body})); body='new';
  handlers.click(event({defaultPrevented:true,target:{closest:()=>link()}}));
  assert.equal(prompts.length,0);
});
test('multiple registered editors and explicit navigation permission', async () => {
  const {api,handlers,answer}=setup(); let a='a',b='b'; const one=api.track(()=>a); api.track(()=>b);
  a='new'; assert.equal(await api.confirmLeave(),false); one.saved(); assert.equal(await api.confirmLeave(),true);
  b='new'; answer(true); assert.equal(await api.confirmLeave(),true);
  api.allowNavigation(); const e=event(); handlers.beforeunload(e); assert.equal(e.prevented,false);
  assert.equal(api.hasChanges(),true); // Permission is not a false save acknowledgement.
});
