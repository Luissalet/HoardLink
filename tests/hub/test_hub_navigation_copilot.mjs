// Run with: node tests/hub/test_hub_navigation_copilot.mjs
// Execute the real adapters with a small DOM boundary; no browser or server needed.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = name => readFileSync(new URL(`../../hoard_link/hub/ui/${name}`, import.meta.url), 'utf8');

function harness() {
  const handlers = new Map();
  const state = {route:'apps', appForms:[], familyForms:[], topCalls:0};
  const elements = {
    '#family-panel':{hidden:true}, '#family-body':{hidden:false},
    '#hub-apps':{hidden:false}, '#edit-dialog':{open:false},
    '#family-tabs .tab.on':{dataset:{tab:'agora'}},
  };
  const topRoot = {hidden:true, contains:target => target.kind === 'top'};
  const appObject = {kind:'app',object_id:'previous-app'};
  const projectObject = {kind:'agora_task',object_id:'alpha-task'};
  const document = {
    readyState:'loading', documentElement:{lang:'es'},
    addEventListener(name, callback) {
      if (!handlers.has(name)) handlers.set(name, []);
      handlers.get(name).push(callback);
    },
    querySelector:selector => elements[selector] || null,
    querySelectorAll:() => [],
  };
  const window = {
    getSelection:() => '',
    hubNavigationView:() => ({section:state.route}),
    hubCopilotApp:{context:async() => ({active_objects:[appObject],view:{section:'apps'},pending_forms:state.appForms})},
    hubCopilotFamily:{context:async() => ({active_objects:[],view:{section:'agora'},pending_forms:state.familyForms})},
  };
  const context = vm.createContext({document,window,EventTarget,console});
  vm.runInContext(source('facets.js'), context);
  window.HubFacets.register('agora', {
    placement:'tab', _root:{contains:() => false},
    _btn:{classList:{contains:() => true},contains:() => false},
    copilot:{context:async() => ({active_objects:[projectObject],view:{section:'agora',space_id:'alpha'}})},
  });
  window.HubFacets.register('today', {
    placement:'top', _root:topRoot,
    copilot:{context:async() => {
      state.topCalls++;
      return {active_objects:[{kind:'today',object_id:'today'}],view:{section:'today'}};
    }},
  });
  vm.runInContext(source('copilot.js'), context);
  const click = kind => {
    const target = {kind,closest(selector) {
      if (kind === 'app' && selector.startsWith('.card[')) return this;
      if (kind === 'top' && selector === '.facet-top') return this;
      return null; // Sidebar links and project rows do not belong to either surface.
    }};
    for (const callback of handlers.get('pointerdown') || []) callback({target});
  };
  const navigate = route => {
    state.route = route;
    elements['#hub-apps'].hidden = route !== 'apps';
    elements['#family-panel'].hidden = route !== 'project';
    topRoot.hidden = route !== 'home';
    click('navigation');
  };
  // Normalize VM realm objects before structural assertions.
  return {state,click,navigate,collect:async() => JSON.parse(JSON.stringify(await window.hoardCopilot.context()))};
}

{
  const h = harness();
  h.click('app');
  assert.equal((await h.collect()).active_objects[0].object_id, 'previous-app');
  h.navigate('project');
  const result = await h.collect();
  assert.equal(result.view.section, 'agora');
  assert.equal(result.view.space_id, 'alpha');
  assert.deepEqual(result.active_objects, [{kind:'agora_task',object_id:'alpha-task'}]);
}

{
  const h = harness();
  h.navigate('home'); h.click('top');
  assert.equal((await h.collect()).view.section, 'today');
  assert.equal(h.state.topCalls, 1);
  h.navigate('project');
  const result = await h.collect();
  assert.equal(result.view.section, 'agora');
  assert.equal(result.view.space_id, 'alpha');
  assert.equal(h.state.topCalls, 1, 'Hidden Today must not collect context');
}

{
  const h = harness();
  const draft = {kind:'profile',fields:{name:'Unsaved group'}};
  h.state.appForms = [draft]; h.click('app');
  assert.deepEqual((await h.collect()).editor.forms, [draft]);
  h.navigate('home');
  const result = await h.collect();
  assert.equal(result.view.section, 'home');
  assert.deepEqual(result.active_objects, []);
  assert.equal(result.editor, undefined);
  assert.equal(h.state.appForms[0], draft, 'Navigation must preserve the stored draft');
}

console.log('PASS: app to project, top to project, hidden group draft on Home');
