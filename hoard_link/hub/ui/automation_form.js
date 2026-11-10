/* Native form for the existing automation documents. Unknown fields round-trip unchanged. */
(() => {
  'use strict';
  const clone = value => JSON.parse(JSON.stringify(value));
  window.HubAutomationForm = {
    mount(root, value, kind, {text, apps = [], onChange}) {
      const base = clone(value); root.replaceChildren(); root.className = 'hub-automation-form';
      const node = (tag, cls, content) => { const n = document.createElement(tag); if (cls) n.className = cls; if (content !== undefined) n.textContent = content; return n; };
      const field = (parent, label, key, current, type = 'text') => {
        const lab = node('label', 'hub-form-field', label), input = node('input'); input.name = key; input.type = type; input.setAttribute('aria-label',label);
        if (type === 'checkbox') input.checked = current !== false; else input.value = current ?? '';
        lab.append(input); parent.append(lab); return input;
      };
      const select = (parent, label, options, current) => {
        const lab = node('label', 'hub-form-field', label), input = node('select'); input.setAttribute('aria-label',label);
        for (const [id, name] of options) { const o = node('option', '', name); o.value = id; input.append(o); }
        input.value = current; lab.append(input); parent.append(lab); return input;
      };
      const common = node('div', 'hub-form-grid'); root.append(common);
      const name = field(common, text('Nombre','Name'), 'name', base.name);
      const enabled = field(common, text('Activada','Enabled'), 'enabled', base.enabled, 'checkbox'); enabled.parentElement.classList.add('hub-form-checkbox');
      const whenBox = node('section', 'hub-form-section'); whenBox.append(node('h3', '', text('Cuándo se ejecuta','When it runs'))); root.append(whenBox);
      let eventType, eventPreset, affectedApp, source, cooldown, schedule, at, every, affectedChanged=false;
      const lifecycle = type => ['hub.app.started','hub.app.stopped'].includes(type);
      const triggers = [['links.watch.new',text('Se encuentra un enlace nuevo en un seguimiento','A watch finds a new link')],['hub.app.started',text('Se inicia una aplicación','An application starts')],['hub.app.stopped',text('Se para una aplicación','An application stops')],['agent.write',text('Un agente guarda un cambio','An agent saves a change')],['agent.undo',text('Un agente deshace un cambio','An agent undoes a change')]];
      if (kind === 'rules') {
        const grid = node('div','hub-form-grid'); whenBox.append(grid);
        const trigger = eventPreset = select(grid, text('Cuando…','When…'), [...triggers,['custom',text('Otro evento · configuración avanzada','Other event · advanced configuration')]], triggers.some(x=>x[0]===base.when?.type) ? base.when.type : 'custom');
        const target=base.when?.where?.['data.app'] ?? base.when?.where?.app;
        affectedApp=select(grid,text('Aplicación afectada','Affected application'),[['',text('Cualquier aplicación','Any application')],...apps.map(a=>[a.id,a.name])],typeof target==='string'?target:'');affectedApp.name='affected_app';
        if(target && !apps.some(a=>a.id===target)){const o=node('option','',typeof target==='string'?target:text('Filtro avanzado conservado','Advanced filter preserved'));o.value=typeof target==='string'?target:'__advanced__';affectedApp.append(o);affectedApp.value=o.value;}
        affectedApp.addEventListener('change',()=>{affectedChanged=true;});
        source = select(grid, text('Origen','Source'), [['',text('Cualquier aplicación','Any application')],['hub',text('El Hub','The Hub')],...apps.map(a=>[a.id,a.name])],base.when?.source || '');
        if(base.when?.source && !['hub',...apps.map(a=>a.id)].includes(base.when.source)) {const option=node('option','',base.when.source);option.value=base.when.source;source.append(option);source.value=base.when.source;}
        const expert=node('details','hub-event-expert');expert.append(node('summary','',text('Ajustes del evento','Event settings')));whenBox.append(expert);
        expert.append(source.parentElement);
        eventType = field(expert, text('Identificador del evento','Event identifier'), 'event_type', base.when?.type || '');
        cooldown = field(expert, text('Espera entre ejecuciones (segundos)','Time between runs (seconds)'), 'cooldown_s', base.cooldown_s ?? 0, 'number'); cooldown.min = '0';
        const updateTrigger=()=>{eventType.parentElement.hidden=trigger.value!=='custom';affectedApp.parentElement.hidden=!lifecycle(trigger.value);source.parentElement.hidden=lifecycle(trigger.value);if(trigger.value==='custom')expert.open=true;};
        trigger.addEventListener('change',updateTrigger);updateTrigger();
        whenBox.append(node('p','hint',text('Solo se ejecutará cuando llegue esa novedad. Las aplicaciones deben publicar sus eventos para que una regla pueda responder.','Runs only when that event arrives. Applications must publish events for a rule to respond.')));
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
      const actionTypes = [['backup',text('Crear una copia de seguridad','Create a backup')],['start_app',text('Iniciar una aplicación','Start an application')],['stop_app',text('Parar una aplicación','Stop an application')],['restart_app',text('Reiniciar una aplicación','Restart an application')],['profile_start',text('Iniciar un grupo','Start a group')],['profile_stop',text('Parar un grupo','Stop a group')],['tool',text('Herramienta de una aplicación · avanzada','Application tool · advanced')],['hub',text('Herramienta del Hub · avanzada','Hub tool · advanced')],['event',text('Publicar un evento · avanzada','Publish an event · advanced')],['brain',text('Escribir en el Cerebro · se edita en la pestaña Cerebro o en el JSON','Write to the Brain · edit it in the Brain tab or in the JSON')]];
      const actionEditors = [];
      function addAction(original) {
        const box = node('div','hub-action-editor'); list.append(box);
        const initialType = original.kind === 'hub' && original.tool === 'hub_backup_run' && !Object.keys(original.args || {}).length ? 'backup' : original.kind;
        const type = select(box,text('Acción','Action'),actionTypes,initialType || 'backup');
        const fields = node('div','hub-form-grid'); box.append(fields);
        const app = select(fields,text('Aplicación','Application'),[['',text('Elige una aplicación','Choose an application')],...apps.map(a=>[a.id,a.name])],original.app || '');app.name='app';
        if(original.app && !apps.some(a=>a.id===original.app)){const o=node('option','',original.app);o.value=original.app;app.append(o);app.value=original.app;}
        const expert=node('details','hub-action-expert');expert.append(node('summary','',text('Parámetros de la acción avanzada','Advanced action parameters')));box.append(expert);
        const tool = field(expert,text('Herramienta','Tool'),'tool',original.tool || '');
        const profile = field(fields,text('Nombre del grupo','Group name'),'group_name',original.name || '');
        const event = field(expert,text('Tipo de evento','Event type'),'type',original.type || '');
        const paramsLabel = node('label','hub-form-field',text('Parámetros (JSON)','Parameters (JSON)')); const params = node('textarea'); params.name = 'parameters'; params.rows = 4; params.spellcheck = false; params.value = JSON.stringify(original.kind === 'event' ? original.data || {} : original.args || {},null,2); paramsLabel.append(params); expert.append(paramsLabel);
        const update = () => {
          app.parentElement.hidden = !['tool','start_app','stop_app','restart_app'].includes(type.value);
          tool.parentElement.hidden = !['tool','hub'].includes(type.value); profile.parentElement.hidden = !type.value.startsWith('profile_');
          event.parentElement.hidden = type.value !== 'event'; paramsLabel.hidden = !['tool','hub','event'].includes(type.value);expert.hidden=!['tool','hub','event'].includes(type.value);
        };
        const drafts=new Map();let previousType=type.value;
        type.addEventListener('change',()=>{
          drafts.set(previousType,{app:app.value,tool:tool.value,profile:profile.value,event:event.value,params:params.value});
          const draft=drafts.get(type.value) || {app:app.value,tool:'',profile:profile.value,event:'',params:'{}'};
          app.value=draft.app;tool.value=draft.tool;profile.value=draft.profile;event.value=draft.event;params.value=draft.params;previousType=type.value;update();
        }); update();
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
          result.when = {...base.when,type:eventPreset.value==='custom'?eventType.value.trim():eventPreset.value};
          if(lifecycle(result.when.type)) {
            result.when.source='hub';
            if(affectedChanged && affectedApp.value!=='__advanced__'){
              const where={...base.when?.where};delete where.app;delete where['data.app'];
              if(affectedApp.value)where['data.app']=affectedApp.value;
              if(Object.keys(where).length)result.when.where=where;else delete result.when.where;
            }
          } else if (source.value || base.when?.source !== undefined) result.when.source = source.value.trim();
          result.cooldown_s = Number(cooldown.value);
        } else if (schedule.value === 'at') { result.at = at.value; result.every = null; }
        else { result.every = String(base.every ?? '') === every.value ? base.every : every.value; result.at = null; }
        result.then = actionEditors.map(action => action.read()); return result;
      }};
    },
  };
})();
