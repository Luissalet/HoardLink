/* Domain adapter only: launcher, panel, sessions and execution belong to Faustus. */
(() => {
  'use strict';
  let surface = 'family';
  function rememberSurface(event) {
    const target = event.target;
    if (target.closest?.('.card[data-id],#group-editor,#detail-dialog,#log-dialog')) surface = 'apps';
    else if (target.closest?.('#family-panel,#edit-dialog')) surface = 'family';
  }
  document.addEventListener('pointerdown',rememberSurface,true);
  document.addEventListener('focusin',rememberSurface,true);
  function pendingForms() {
    return [...document.querySelectorAll('[data-copilot-form],.ag-compose')]
      .filter(form => !form.closest('[hidden]'))
      .map(form => ({kind:form.dataset.copilotForm || 'agora_reply',is_new:!!form.dataset.copilotForm,
        ...(form.querySelector('textarea')?.dataset.reviewRevision ? {review_submission_revision:Number(form.querySelector('textarea').dataset.reviewRevision)} : {}),
        fields:Object.fromEntries([...form.querySelectorAll('input[name],textarea[name],select[name]')]
          .filter(input => input.type !== 'password' && input.type !== 'file')
          .map(input => [input.name,input.type === 'checkbox' ? input.checked : input.value]))}));
  }
  function withoutCredentials(value) {
    if (Array.isArray(value)) return value.map(withoutCredentials);
    if (!value || typeof value !== 'object') return value;
    return Object.fromEntries(Object.entries(value)
      .filter(([key]) => !/^(?:env|environment|backend|authorization|cookie|credentials?|password|secret|token|access_token|refresh_token|api[_-]?key|.*[_-](?:key|token|secret|password))$/i.test(key.replace(/([a-z0-9])([A-Z])/g,'$1_$2')))
      .map(([key,item]) => [key,withoutCredentials(item)]));
  }
  async function context() {
    const app = await window.hubCopilotApp?.context?.() || {active_objects:[],view:{section:'apps'},pending_forms:[]};
    const family = await window.hubCopilotFamily?.context?.();
    const facet = await window.HubFacets?.copilotContext?.();
    const familyOpen = document.querySelector('#family-body') && !document.querySelector('#family-body').hidden;
    const editing = document.querySelector('#edit-dialog')?.open;
    const selected = editing ? family : app.pending_forms?.length && surface === 'apps' ? app : facet?.pending_forms?.length ? facet : surface === 'apps' || !familyOpen ? app : facet || family;
    const selection = window.getSelection()?.toString();
    const forms = [...(app.pending_forms || []),...(family?.pending_forms || []),...(facet?.pending_forms || []),...pendingForms()];
    return withoutCredentials({active_objects:selected?.active_objects || [],
      selection:selection ? {kind:'text',text:selection} : null,
      ...(forms.length ? {editor:{forms}} : {}),
      view:{...selected?.view,locale:document.documentElement.lang || 'en'}});
  }
  async function onTurnEnd() {
    await Promise.allSettled([window.hubCopilotApp?.refresh?.(),window.hubCopilotFamily?.refresh?.(),window.HubFacets?.refreshCopilot?.()]);
  }
  window.hoardCopilot = {...window.hoardCopilot,context,onTurnEnd};
})();
