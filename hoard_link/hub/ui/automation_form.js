/* Native form for the existing automation documents. Unknown fields round-trip unchanged. */
(() => {
  'use strict';
  const clone = value => JSON.parse(JSON.stringify(value));
  window.HubAutomationForm = {
    mount(root, value, kind, {text, apps = [], onChange}) {
      const base = clone(value); root.replaceChildren(); root.className = 'hub-automation-form';
      const node = (tag, cls, content) => { const n = document.createElement(tag); if (cls) n.className = cls; if (content !== undefined) n.textContent = content; return n; };
      const field = (parent, label, key, current, type = 'text') => {
        const lab = node('label', 'hub-form-field', label), input = node('input'); input.name = key; input.type = type;
        if (type === 'checkbox') input.checked = current !== false; else input.value = current ?? '';
        lab.append(input); parent.append(lab); return input;
      };
      const select = (parent, label, options, current) => {
        const lab = node('label', 'hub-form-field', label), input = node('select');
        for (const [id, name] of options) { const o = node('option', '', name); o.value = id; input.append(o); }
        input.value = current; lab.append(input); parent.append(lab); return input;
      };
      const common = node('div', 'hub-form-grid'); root.append(common);
      const name = field(common, text('Nombre','Name'), 'name', base.name);
      const enabled = field(common, text('Activada','Enabled'), 'enabled', base.enabled, 'checkbox'); enabled.parentElement.classList.add('hub-form-checkbox');
      const whenBox = node('section', 'hub-form-section'); whenBox.append(node('h3', '', text('Cuándo se ejecuta','When it runs'))); root.append(whenBox);
      let eventType, source, cooldown, schedule, at, every;
      if (kind === 'rules') {
        const grid = node('div','hub-form-grid'); whenBox.append(grid);
        eventType = field(grid, text('Evento que inicia la regla','Event that starts the rule'), 'event_type', base.when?.type || ''); eventType.placeholder = 'writer.export.done';
        source = field(grid, text('Aplicación de origen (opcional)','Source application (optional)'), 'event_source', base.when?.source || '');
        cooldown = field(grid, text('Espera entre ejecuciones (segundos)','Time between runs (seconds)'), 'cooldown_s', base.cooldown_s ?? 0, 'number'); cooldown.min = '0';
      } else {
        const grid = node('div','hub-form-grid'); whenBox.append(grid);
        schedule = select(grid, text('Frecuencia','Frequency'), [['at',text('A una hora del día','At a time of day')],['every',text('Cada cierto tiempo','At an interval')]], base.every ? 'every' : 'at');
        at = field(grid, text('Hora local','Local time'), 'at', base.at || '04:00', 'time');
        every = field(grid, text('Intervalo (por ejemplo, 6h o 30m)','Interval (for example, 6h or 30m)'), 'every', base.every || '6h');
        const displaySchedule = () => { at.parentElement.hidden = schedule.value !== 'at'; every.parentElement.hidden = schedule.value !== 'every'; };
        schedule.addEventListener('change', displaySchedule); displaySchedule();
      }
      const actionSection = node('section','hub-form-section'); actionSection.append(node('h3','',text('Qué debe hacer','What it should do'))); root.append(actionSection);
      const list = node('div','hub-action-list'); actionSection.append(list);
      const actionTypes = [['backup',text('Crear una copia de seguridad','Create a backup')],['start_app',text('Iniciar una aplicación','Start an application')],['stop_app',text('Parar una aplicación','Stop an application')],['restart_app',text('Reiniciar una aplicación','Restart an application')],['profile_start',text('Iniciar un grupo','Start a group')],['profile_stop',text('Parar un grupo','Stop a group')],['tool',text('Usar una herramienta de una aplicación','Use an application tool')],['hub',text('Usar una herramienta del Hub','Use a Hub tool')],['event',text('Publicar un evento','Publish an event')]];
      const actionEditors = [];
      function addAction(original) {
        const box = node('div','hub-action-editor'); list.append(box);
        const initialType = original.kind === 'hub' && original.tool === 'hub_backup_run' && !Object.keys(original.args || {}).length ? 'backup' : original.kind;
        const type = select(box,text('Acción','Action'),actionTypes,initialType || 'backup');
        const fields = node('div','hub-form-grid'); box.append(fields);
        const app = field(fields,text('Aplicación (identificador)','Application (ID)'),'app',original.app || '');
        const appOptions = node('datalist'); appOptions.id = 'hub-action-apps-' + Math.random().toString(36).slice(2); app.setAttribute('list',appOptions.id);
        for (const item of apps) { const o = node('option','',item.name); o.value = item.id; appOptions.append(o); } fields.append(appOptions);
        const tool = field(fields,text('Herramienta','Tool'),'tool',original.tool || '');
        const profile = field(fields,text('Nombre del grupo','Group name'),'group_name',original.name || '');
        const event = field(fields,text('Tipo de evento','Event type'),'type',original.type || '');
        const paramsLabel = node('label','hub-form-field',text('Parámetros (JSON)','Parameters (JSON)')); const params = node('textarea'); params.name = 'parameters'; params.rows = 4; params.spellcheck = false; params.value = JSON.stringify(original.kind === 'event' ? original.data || {} : original.args || {},null,2); paramsLabel.append(params); fields.append(paramsLabel);
        const update = () => {
          app.parentElement.hidden = !['tool','start_app','stop_app','restart_app'].includes(type.value);
          tool.parentElement.hidden = !['tool','hub'].includes(type.value); profile.parentElement.hidden = !type.value.startsWith('profile_');
          event.parentElement.hidden = type.value !== 'event'; paramsLabel.hidden = !['tool','hub','event'].includes(type.value);
        };
        type.addEventListener('change',update); update();
        const remove = node('button','ghost danger',text('Quitar acción','Remove action')); remove.type = 'button'; box.append(remove);
        const editor = {read() {
          const same = type.value === initialType;
          const action = same ? clone(original) : {};
          if (type.value === 'backup') return {...(same ? action : {}),kind:'hub',tool:'hub_backup_run',args:{}};
          action.kind = type.value;
          if (['tool','start_app','stop_app','restart_app'].includes(type.value)) action.app = app.value.trim();
          if (['tool','hub'].includes(type.value)) action.tool = tool.value.trim();
          if (type.value.startsWith('profile_')) action.name = profile.value.trim();
          if (type.value === 'event') action.type = event.value.trim();
          if (['tool','hub','event'].includes(type.value)) action[type.value === 'event' ? 'data' : 'args'] = JSON.parse(params.value || '{}');
          return action;
        }};
        actionEditors.push(editor); remove.onclick = () => { actionEditors.splice(actionEditors.indexOf(editor),1); box.remove(); onChange?.(); };
      }
      for (const action of base.then || []) addAction(action);
      const add = node('button','ghost',text('Añadir acción','Add action')); add.type = 'button'; add.onclick = () => { addAction({kind:'hub',tool:'hub_backup_run',args:{}}); onChange?.(); }; actionSection.append(add);
      root.oninput = root.onchange = () => onChange?.();
      return {read() {
        const result = clone(base); result.name = name.value; result.enabled = enabled.checked;
        if (kind === 'rules') {
          result.when = {...base.when,type:eventType.value.trim()};
          if (source.value || base.when?.source !== undefined) result.when.source = source.value.trim();
          result.cooldown_s = Number(cooldown.value);
        } else if (schedule.value === 'at') { result.at = at.value; result.every = null; }
        else { result.every = String(base.every ?? '') === every.value ? base.every : every.value; result.at = null; }
        result.then = actionEditors.map(action => action.read()); return result;
      }};
    },
  };
})();
