"""Family-wide capability discovery, with declared versus live evidence separated."""
from __future__ import annotations
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from .facets import Facet
from .contract import app_tools

ALIASES = {'voz':'voice speech tts','imagen':'image photo','video':'video','diseno':'design','documento':'document paperwork','3d':'3d mesh stl model','ia':'model training evaluation ai','agenda':'agenda calendar deadline reminder','dinero':'sales finance budget accounts','cocina':'recipe cooking pantry','pc':'service disk screen clipboard','codigo':'code api environment','leer':'library books reading','estudio':'study quiz exam','contactos':'people contacts interactions'}

def folded(value):
    return ''.join(c for c in unicodedata.normalize('NFKD',str(value).lower()) if not unicodedata.combining(c))

ALIASES.update({'crm':'commercial companies opportunities pipeline sales', 'clientes':'commercial companies contacts', 'oportunidades':'opportunities pipeline', 'propuesta':'proposals documents editor'})

RECIPES = [
    {'id':'design-motion','title':'Diseño y movimiento','stages':[['design-criterion-library','design-tokens'],['image generation'],['motion-compositions'],['video-editing'],['posts']]},
    {'id':'ai-engineering','title':'Dataset → modelo → evaluación','stages':[['ingest','quality-rules'],['fine-tuning','lineage'],['model-evaluation','regression-watch'],['service-status','gpu-timeline']]},
    {'id':'3d-production','title':'Modelo 3D → catálogo → publicación','stages':[['projects','analysis','jobs'],['3d-models','mesh-metrics'],['photo search','contact sheets'],['product-catalog','posts']]},
    {'id':'source-to-study','title':'Fuentes → investigación → estudio','stages':[['bookmarks','extraction'],['documents','ocr'],['hybrid-search','citations'],['study-guides','question-bank'],['presentation-decks']]},
    {'id':'pc-command','title':'Centro de mando del PC','stages':[['service-status','incidents'],['disk-usage'],['activity timeline','resume context'],['shared-storage','file-revisions'],['jobs','event-bus']]},
    {'id':'meeting-actions','title':'Reunión → compromisos → plazos','stages':[['audio recording','meeting minutes with action items'],['commitments_from_meetings'],['minutes-deadlines'],['family-agenda']]},
    {'id':'purchase-lifecycle','title':'Compra → envío → garantía → mantenimiento','stages':[['price-tracking','restock-watch'],['shipment-tracking'],['warranties','invoices'],['accounts','entries'],['maintenance','household-inventory']]},
    {'id':'writing-production','title':'Canon → manuscrito → voz → edición','stages':[['world-state','continuity'],['manuscripts','outline'],['text-to-speech','audiobooks'],['video-editing'],['posts']]},
    {'id':'home-menu','title':'Menú → compra → gasto','stages':[['weekly menu','pantry stock deficits'],['shopping list','recipe-cost'],['purchase-intake'],['entries','food-spending']]},
    {'id':'developer-reference','title':'Ingeniería de software con evidencia','stages':[['environments','api-lookup','code-check'],['api-lookup','localization-audit'],['shared-storage','file-revisions'],['service-status','log-search']]},
    {'id':'career','title':'Empleo → contactos → documentos','stages':[['jobs','applications','answers'],['people','conversation_brief'],['documents','editor','exports'],['travel-bookings','trips']]},
    {'id':'research-numbers','title':'Fuentes → cálculo → análisis','stages':[['library','citations'],['symbolic-math','statistics'],['charts','dashboards'],['market-data-snapshots','backtest']]},
    {'id':'measured-cad','title':'CAD medido → STL → catálogo','stages':[['parametric-cad','step-roundtrip','stl-watertight-check'],['3d-models','mesh-metrics'],['contact sheets'],['product-catalog','sales-import']]},
    {'id':'personal-continuity','title':'Actividad → recuerdo → contexto revisado','stages':[['screen-history','clipboard-history'],['activity timeline','federated recall (screen, clipboard, audio)'],['source-evidence','reviewed-updates']]},
    {'id':'games-library','title':'Biblioteca de juegos → radar → presupuesto','stages':[['games','backlog','steam-import'],['game-store-deals','release-calendar'],['budgets','subscriptions']]},
    {'id':'client-pipeline','title':'Cliente → oportunidad → propuesta → seguimiento','stages':[['people','conversation_brief'],['commercial-crm','opportunities-pipeline'],['documents','editor','exports'],['crm-followups','crm-activity-history'],['accounts','entries']]},
    {'id':'client-ai-delivery','title':'Servicio de IA → evaluación → entrega al cliente','stages':[['commercial-crm','opportunities-pipeline'],['ingest','quality-rules'],['fine-tuning','lineage'],['model-evaluation','regression-watch'],['shared-storage','file-revisions'],['crm-activity-history']]},
    {'id':'client-3d-commission','title':'Encargo 3D → modelo → catálogo → entrega','stages':[['commercial-crm','opportunities-pipeline'],['projects','analysis','jobs','parametric-cad'],['3d-models','mesh-metrics'],['product-catalog'],['shared-storage','file-revisions'],['crm-followups','crm-activity-history']]},
]

class CapabilitiesFacet(Facet):
    id='capabilities'
    ui_scripts=('capabilities.js',)

    def inventory(self):
        apps={app.id:app for app in self.hub.apps}
        config=getattr(self.hub,'config',None)
        if config:
            from .registry import scan
            for app in scan(config.roots,exclude_ids=config.exclude_ids,include_mcp_only=True):
                apps.setdefault(app.id,app)
        return list(apps.values())

    def find(self,args):
        query=folded(args.get('query','')).strip()[:200]
        limit=max(1,min(50,int(args.get('limit',20))))
        words=re.findall(r'[a-z0-9]+',query)
        terms=set(words)
        for word in words:
            terms.update(ALIASES.get(word,'').split())
        candidates=[]
        for app in self.inventory():
            hay=folded(' '.join([app.id,app.name,app.purpose,*app.capabilities]))
            tokens=set(re.findall(r'[a-z0-9]+',hay))
            score=sum(term in tokens for term in terms)
            if query and any(folded(cap)==query for cap in app.capabilities):
                score+=10
            if query and not score:
                continue
            candidates.append((score,app))
        candidates.sort(key=lambda pair:(-pair[0],pair[1].id))
        chosen=candidates[:limit]
        live={}
        # Bounded probes never start an application or treat a manifest as a successful connection.
        if args.get('check') is True:
            probe=[app for _,app in chosen[:8] if app.agent_contract]
            with ThreadPoolExecutor(max_workers=4) as pool:
                for app, answer in zip(probe,pool.map(lambda a:app_tools(a,timeout=2),probe)):
                    live[app.id]=answer
        rows=[]
        for score,app in chosen:
            row={'app':app.id,'name':app.name,'purpose':app.purpose,'url':app.url,'kind':app.kind,'capabilities':app.capabilities,'evidence':'manifest','manifest':app.manifest_path,'agent_contract_declared':app.agent_contract,'score':score}
            if app.id in live:
                answer=live[app.id]
                row['checked_at']=time.time();row['reachable']=answer.get('ok',False);row['contract']=answer.get('contract');row['error']=answer.get('error')
                if answer.get('ok'):
                    tools=answer.get('tools',[])
                    filtered=[t for t in tools if isinstance(t,dict) and (not terms or any(term in folded(t.get('name','')+' '+t.get('description','')) for term in terms))]
                    row['tools']=[{'name':t.get('name'),'description':str(t.get('description',''))[:500],'inputSchema':t.get('inputSchema',{}),'annotations':t.get('annotations',{})} for t in filtered[:30]]
                    row['tools_total']=len(tools);row['tools_truncated']=len(filtered)>30;row['evidence']='live-catalogue'
            rows.append(row)
        return {'ok':True,'query':args.get('query',''),'apps':rows,'matched':len(candidates),'truncated':len(candidates)>limit,'probe_limit':8,'note':'Capabilities are declarations. Live catalogues verify tool availability, not successful execution or model readiness.'}

    def recipes(self,args):
        wanted=args.get('id')
        choices=[r for r in RECIPES if not wanted or r['id']==wanted]
        if wanted and not choices:
            return {'ok':False,'error':'Unknown recipe id'}
        rows=[]
        inventory=self.inventory()
        for recipe in choices:
            stages=[]
            for i,capabilities in enumerate(recipe['stages']):
                providers=[]
                for app in inventory:
                    matches=[cap for cap in capabilities if cap in app.capabilities]
                    if matches:
                        providers.append({'app':app.id,'name':app.name,'matches':matches,'coverage':len(matches)/len(capabilities),'url':app.url})
                providers.sort(key=lambda p:(-p['coverage'],p['app']))
                stages.append({'stage':i+1,'needs':capabilities,'providers':providers,'missing':[cap for cap in capabilities if not any(cap in p['matches'] for p in providers)]})
            rows.append({**recipe,'stages':stages})
        return {'ok':True,'recipes':rows,'note':'These are capability plans, not executed workflows. Inspect live tools, bind real input IDs, preserve originals, and verify each stage before downstream work.'}

    def get(self,req):
        if req.path=='/api/capabilities':
            return self.find({'query':req.q('query',''),'limit':req.q_int('limit',20),'check':req.q_bool('check')})
        if req.path=='/api/capabilities/recipes':
            return self.recipes({'id':req.q('id')})
        return None

    @classmethod
    def tools(cls):
        return [
          {'name':'hub_capability_find','description':'Find capabilities across ALL installed Hoards, optionally inspect live tool schemas (at most 8 probes). Buscar herramientas y capacidades de toda la familia con evidencia; no arranca apps.','inputSchema':{'type':'object','properties':{'query':{'type':'string','maxLength':200},'limit':{'type':'integer','minimum':1,'maximum':50},'check':{'type':'boolean'}},'additionalProperties':False},'annotations':{'readOnlyHint':True}},
          {'name':'hub_recipe_plan','description':'Plan cross-Hoard design, AI, 3D, research, meetings, purchases, writing, PC and career workflows using declared capabilities. Ver proveedores y carencias por etapa; no afirma ejecución.','inputSchema':{'type':'object','properties':{'id':{'type':'string'}},'additionalProperties':False},'annotations':{'readOnlyHint':True}},
        ]

    def handlers(self):
        return {'hub_capability_find':self.find,'hub_recipe_plan':self.recipes}
