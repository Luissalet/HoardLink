"""Link inventory and probe assignments.

The 90 parent IDs and P/C/D/R classifications come from the family link
inventory. Compatibility bridges and common services retain their parent ID.
"""

from dataclasses import dataclass

from ..service_contracts import SERVICES


@dataclass(frozen=True)
class Link:
    id: str
    origin: str
    destination: str
    via: str
    probe: str
    inventory_status: str
    inventory_id: str = ""


INVENTORY = (
    Link("1.1", "Agente (Claude, Codex, Cursor, Faustus, CLI agora.cmd)", "Hub", "POST /api/agent/call y MCP stdio python -m hoard_link.hub.mcp", "hub_catalog", "P"),
    Link("1.2", "Hub", "cualquier app", "hub_call_app, hub_app_tools, hub_proxy_check → contract.call_app con el token de la app", "app_catalog:nightingale", "P"),
    Link("1.3", "Persona (Studio / UI) o app", "app destino", "POST /api/apps/<id>/call con token de familia", "app_tool:nightingale:nightingale_modules", "C"),
    Link("1.4", "Hub (comprobación de piezas, alcance del confinado)", "Nightingale", "call_app(\"nightingale\", …) (data_preview, nightingale_modules)", "app_tool:nightingale:nightingale_modules", "P"),
    Link("1.5", "Hub (referencias de la orden)", "Writer", "call_app(\"writer\", …)", "app_catalog:writer", "P"),
    Link("1.6", "Hub", "Prospero, Vulcan (y cualquier app con módulos)", "hub_module_set + start_app/stop_app", "app_catalog:prospero,vulcan", "P"),
    Link("1.7", "Hub", "Atlas", "hub_atlas_* (18) + hub_workspace → call_app(\"atlas\", …)", "app_catalog:atlas:atlas_projects", "C"),
    Link("1.8", "Hub", "Mercator", "hub_website_publish_piece → call_app(\"mercator\",\"website_publish\")", "app_catalog:mercator:website_publish", "C"),
    Link("1.9", "Hub", "Talos (Funes) y Writer (Borges)", "hub_media_import* → funes.transcribe_file, borges.library_reindex (por ids de compatibilidad)", "media_owners", "C"),
    Link("1.10", "Hub", "Hermes (Tantalus)", "hub_purchase → tantalus.watchers_match_purchase / watcher_mark_bought", "no:purchase_matching", "C"),
    Link("1.11", "Hub", "Home Hoard", "hub_purchase_open_home", "prerequisites:homehoard:opens_home", "C"),
    Link("1.12", "Hub (hub_today)", "Talos (Cassandra)", "call_app(\"cassandra\",\"svc_incidents\")", "app_catalog:cassandra:svc_incidents", "C"),
    Link("1.13", "Hub", "ComfyUI / motores de imagen", "hub_comfy_profile_register / _unregister", "backend_cache:image", "C"),
    Link("1.14", "Hub (hub_eyes_*, hub_piece_look_checked)", "Blender, Godot, 3MF, vídeo, web", "motores de «ojos» sin ventana", "no:piece_content", "R"),
    Link("1.15", "Hub", "laminador local (Orca)", "hub_print_slice", "no:slicer", "C"),
    Link("1.16", "Cliente MCP (también Faustus en la Spark)", "cualquier app con MCP stdio", "relay GET/POST/DELETE /api/mcp/apps/<id> (un hijo por sesión HTTP)", "relay", "C"),
    Link("1.17", "Hub", "ids absorbidos (22 de compatibilidad)", "manifiesto compatibility/<id>/faustus-plugin.json + bridge.json (propietario y token por módulo)", "compatibility", "C"),
    Link("1.18", "app", "Hub (como app hoardhub)", "POST /api/apps/hoardhub/call (alias hoard-hub, hub)", "no:app_to_hub_post", "C"),
    Link("1.19", "Hub", "todas las apps", "catálogo por contrato de familia (/api/agent/tools) y auditoría de cohesión", "family_catalogues", "C"),
    Link("2.1", "Hub (pasarela)", "buzón IMAP vía ayudante de Faustus", "mail_helper.py ejecutado por el Hub, <data>/mail.db, bus mail.received", "mail", "P"),
    Link("2.2", "Kafka", "pasarela del Hub", "fam_mail.MailRouter (register_interest, messages, claim)", "gateway_reader:kafka", "P"),
    Link("2.3", "Hermes · Phileas", "pasarela del Hub (en la Spark)", "MailRouter con faustus_dir y huellas escritas por la colocación", "mail_reader:phileas", "P"),
    Link("2.4", "Hermes · Tantalus", "pasarela del Hub (en la Spark)", "MailRouter(interest=…, claim_kind=\"deal\")", "fleet:tantalus", "P"),
    Link("2.5", "Ledger", "pasarela del Hub", "gemelo Node famMailRouter", "mail_reader:ledger", "R"),
    Link("2.6", "People", "pasarela del Hub", "mailsync.js + router.ensureInterest (direcciones de contactos)", "gateway_reader:people", "R"),
    Link("2.7", "JobHunter", "pasarela del Hub", "copia de la biblioteca server/hoard-link.js:643 (mailRegisterInterest)", "no:no_consumer", "D"),
    Link("2.8", "Links, Cook (copia Node)", "pasarela del Hub", "misma copia de hoard-link.js", "no:no_consumer", "D"),
    Link("2.9", "~40 copias de fam_mail.py (cada Hoard y submódulo)", "pasarela", "copia vendored de la misma regla", "mail_library", "C"),
    Link("2.10", "Faustus (ayudante de correo local)", "Hub (llamante identificado)", "data/mail_installation.json + token; el Hub valida al llamante", "mail_caller", "P"),
    Link("2.11", "Colocación (PC → Spark)", "Hermes/Faustus en la Spark", "escribe mail_installation.json y ajustes de Tantalus/Phileas antes que nada", "placement", "P"),
    Link("2.12", "Hub", "persona (correo de avisos)", "send_mail / hub_mail_compose", "no:mail_send", "C"),
    Link("2.13", "Hub (hub_mail_*, 8 herramientas)", "agentes", "hub_mail_accounts/compose/status/search/get/attention/unclaimed/fetch", "mail", "P"),
    Link("3.1", "al menos 13 apps (Cook, Cicero, Coase, Hypatia, Home, Hermes, Pygmalion, Vulcan, Vitruvius, Prospero, Silver, Writer, Faustus)", "Hub (registro de eventos)", "POST /api/events desde hoard_link.family.emit", "events", "C"),
    Link("3.2", "Bus", "motor de reglas → acciones (notificar, llamar app, copia de seguridad…)", "RuleEngine en un hilo, <data>/rules.json", "rules", "C"),
    Link("3.3", "Bus", "Cerebro de Faustus (entidades y notas)", "acción brain → cola brain_bus.db → POST /api/brain/entities", "brain", "C"),
    Link("3.4", "Faustus", "Hub (bus)", "evento faustus desde tareas externas", "faustus_events", "C"),
    Link("3.5", "apps", "persona (toast, ntfy, Telegram, correo)", "hub_notify y fam_notify con prioridad y esfera", "no:notification_content", "R"),
    Link("3.6", "apps con agenda (Home Hoard, Kafka, Ledger, People, Hypatia, Mercator)", "Hub (hub_today, hub_agenda, hub_ics_link, hub_search, hub_refs)", "cada app responde a su agenda; el Hub la reúne", "agenda", "C"),
    Link("3.7", "apps", "Hub (esferas personal/trabajo)", "spheres facet; Faustus muestra la esfera activa", "spheres", "C"),
    Link("3.8", "capa 0.4: «Mnemosyne» y «Cerberus»", "—", "—", "no:no_consumer", "D"),
    Link("3.9", "Hermes (vigilantes de Tantalus y Phileas)", "Hub (flota: último éxito, silencio, bloqueo humano)", "hub_fleet_register/report/status; Hermes publica fleet_status por su puente de compatibilidad", "fleet", "C"),
    Link("4.1", "Home Hoard", "Kafka", "_call(\"kafka\", …) por el Hub (/api/apps/kafka/call)", "prerequisites:homehoard,kafka:maintenance_mirror", "C"),
    Link("4.2", "Cook Hoard", "Talos (Funes), Kafka, Hermes (Tantalus), Ledger, Hub", "manifiesto x-links + server/hub.mjs", "prerequisites:cookhoard,funes,kafka,tantalus,ledger:cook_workflow", "C"),
    Link("4.3", "Cicero", "Nightingale", "dataset_connect kind=nightingale", "prerequisites:cicero,nightingale:dataset_workflow", "C"),
    Link("4.4", "Maxwell", "Vulcan", "directo por puerto VULCAN_URL=http://127.0.0.1:5186", "prerequisites:maxwell,vulcan:maxwell_peer", "C"),
    Link("4.5", "cualquier app (biblioteca fam_*)", "dueño del servicio", "servicios comunes del Hub: mail→hub, media→Links, stt→Funes (Talos), tts→Prospero, docs/OCR→Kafka, embed→Borges (Writer), storage→Atlas", "services", "C"),
    Link("4.6", "apps", "Hub (carriles de navegador, un sitio a la vez)", "hub_lane_acquire/release/renew/status", "lanes", "C"),
    Link("4.7", "Faustus", "Hub (salas y registro de coordinación)", "POST /api/agent/call (hub_coordination_*)", "coordination", "C"),
    Link("4.8", "Hub aislado (pruebas)", "Hoards reales", "arranque en aislado y cierre", "no:isolation_launch", "R"),
    Link("4.9", "apps (Links, Cook, JobHunter, People, Hermes)", "Hub (web compartida: robots, límite de ritmo, caché)", "hub_web_fetch/search/preview/hosts y fam_web / fam-web.js", "web", "C"),
    Link("5.1", "Hub (PC)", "Spark (3 apps)", "hub_placement_switch, SSH + git archive + tar incremental, servicios systemd de usuario", "placement", "P"),
    Link("5.2", "Apps en la Spark", "Hub del PC", "HubLink por tailnet (dirección y token declarados a la Spark)", "spark_link", "P"),
    Link("5.3", "Hub", "apps «que viven en la Spark»", "relay MCP y arranque redirigidos; el PC se niega a arrancarlas", "placement", "C"),
    Link("5.4", "Apps y Faustus", "LLM de las Sparks (GLM/DeepSeek)", "POST /api/link/chat", "backend_cache:text", "C"),
    Link("5.5", "apps", "GPU del PC", "hub_lease_* (/api/lease)", "gpu_inventory", "C"),
    Link("5.6", "Hub", "copiloto común de Faustus (PC o Spark)", "hub_copilot_request, CopilotProxy", "copilot", "C"),
    Link("5.7", "Capataz (instalación)", "Nightingale en el PC y Faustus en la Spark 3 con #3819", "hub_restart_app y hub_placement_switch", "deployment", "P"),
    Link("6.1", "Faustus (herramienta del agente)", "Ágora del Hub", "POST /api/agent/call (hub_agora_*), junto al Hub o por enlace", "agora_foreman", "P"),
    Link("6.2", "Faustus Studio (navegador)", "Ágora del Hub", "GET/POST /api/agora/x/… reenviado con el token del Hub", "no:studio_session", "C"),
    Link("6.3", "Faustus", "Hub (arrancar apps, dock, conectores)", "POST /api/apps/<id>/start, /api/apps/<id>/icon, /api/mcp/apps/<id>", "relay", "C"),
    Link("6.4", "Faustus", "Hub (esferas, paneles, servicios)", "src/hoard_hub.py:41 (8810), panel_sources.py, family_services.py", "spheres", "C"),
    Link("6.5", "Faustus (Dojo)", "Hub (/api/agent/call) y al revés hub_dojo", "exámenes y notas del Dojo", "dojo_count", "C"),
    Link("6.6", "Hub", "Faustus (arrancar, parar, estado)", "FaustusRuntime, hub_faustus_status/start/stop", "faustus", "R"),
    Link("6.7", "Hub", "Faustus (trabajadores confinados)", "hub_agora_console_worker → agora_faustus.prepare/start_pc: ámbito de ficheros y de apps, carpeta privada, modo auto", "console_orphans", "R"),
    Link("6.8", "Hub", "Faustus (chat y modelos)", "hub_link_chat, hub_chat_*", "backend_cache:text", "C"),
    Link("6.9", "Faustus ↔ Hub", "confinados leen datos del Hub (Nightingale data_query)", "file_scope.apps", "no:worker_scope", "C"),
    Link("6.10", "UI de las apps (34 de 35 manifiestos declaran copilot, todas menos Kinect-Hoard)", "Faustus (copiloto común)", "/api/copilot/info, panel.js, proxy, launcher.js de cada app → CopilotProxy del Hub → Faustus (PC o Spark); icono de Faustus como único botón", "copilot", "C"),
    Link("6.11", "Hub (hub_skill_*, hub_agent_flows)", "biblioteca de skills de Faustus y estantes por app", "skills.find_library(faustus_dir) y Shelf por app (provides.skills de los manifiestos)", "skills", "C"),
    Link("7.1", "Agentes y persona", "Ágora (tareas, hilos, canales, decisiones, revisiones)", "hub_agora_* (143 herramientas), agora.cmd, páginas de la persona", "agora_catalog", "P"),
    Link("7.2", "Consola del Ágora", "Trabajadores Codex, Claude, Cursor, Faustus", "hub_agora_console_worker (crear, continuar, parar, listar)", "console_orphans", "R"),
    Link("7.3", "Ágora", "cuota por proveedor (Codex, Claude, Cursor, Faustus)", "lectura de uso y suelos semanales", "quota_measurement", "P"),
    Link("7.4", "Ágora", "capataz (latido y aviso de silencio)", "agora_foreman.py:38, hub_agora_heartbeat", "agora_foreman", "P"),
    Link("7.5", "Ágora", "órdenes de producción (piezas, puertas, ojos, defectos, referencias)", "hub_agora_order*, hub_piece_*, hub_order_*", "no:order_content", "R"),
    Link("7.6", "Hub (Ágora)", "agentes (mención, bandeja, revisión cruzada)", "hub_agora_inbox, review, submit, done", "no:inbox_content", "P"),
    Link("7.7", "Hub", "ejecutor local, flota, integración", "hub_executor_*, hub_fleet_*, hub_integration_*", "no:integration_run", "R"),
    Link("8.1", "Drucker (production-order) → production-brief", "Coase, Nightingale, Vitruvius, Writer", "relevo de manifiesto", "no:production_relay", "D"),
    Link("8.2", "Writer (production-brief → scene-brief)", "Prospero (concept-image)", "hub_order_call", "no:production_relay", "C"),
    Link("8.3", "Prospero (concept-image → character-sheet → video-clip)", "Prospero (Lumiere)", "relevo", "no:production_relay", "P"),
    Link("8.4", "Prospero (character-sheet)", "Vulcan (reference-mesh)", "relevo + Blender", "no:production_relay", "P"),
    Link("8.5", "Vulcan (reference-mesh → articulated-mesh)", "revisión de ojos", "hub_eyes_*, hub_piece_check", "no:production_relay", "R"),
    Link("8.6", "Vulcan (reference-mesh → game-asset)", "Godot (escena jugable)", "relevo + motor jugable", "no:production_relay", "R"),
    Link("8.7", "Vulcan (posed-figure-mesh) → Phidias → Nikias", "figura y peana imprimibles (3MF)", "relevo", "no:production_relay", "R"),
    Link("8.8", "Phidias (scenery-mesh → remodelled-scenery)", "escenario de la escalera", "relevo", "no:production_relay", "C"),
    Link("8.9", "Vulcan (coloured-3mf → mesh-validation)", "validación del 3MF", "relevo", "no:production_relay", "D"),
    Link("8.10", "Nightingale (production-brief → data-table, data-chart) → Writer (editable-report)", "informe", "relevo + data_export + wh_document_*", "no:production_relay", "P"),
    Link("8.11", "Vitruvius (production-brief → interface-brief → web-page) → Mercator (published-item-url)", "publicación web", "relevo + hub_website_publish_piece", "no:production_relay", "C"),
    Link("8.12", "Cicero (editable-deck) ↔ Hypatia (rehearsal-notes, slide-defects)", "ensayo y defectos de diapositivas", "relevo", "no:production_relay", "D"),
    Link("8.13", "Writer (manuscript → reviewed-manuscript → pdf/epub)", "edición larga", "relevo", "no:production_relay", "C"),
    Link("8.14", "Hoard Hub (code_branch → integrated_code)", "integración de código", "relevo propio", "no:production_relay", "R"),
    Link("9.1", "Hub", "Baird", "registro de apps: ventana propia, sin mantenimiento automático", "excluded:baird", "C"),
)

COMPATIBILITY_OWNERS = {
    "tantalus": "hermes", "phileas": "hermes",
    "cassandra": "talos", "argus": "talos", "echo": "talos", "funes": "talos", "diskhoard": "talos",
    "galton": "pygmalion", "laplace": "nightingale", "midas": "coase",
    "borges": "writer", "gutenberg": "writer", "scheherazade": "writer", "lumiere": "prospero",
    "daedalus": "vulcan", "gepetto": "vulcan", "gulliver": "vulcan", "heron": "vulcan", "platos": "vulcan",
    "babel": "vitruvius", "theseus": "vitruvius", "durer": "daguerre",
}

# These identities publish a native module catalogue and declare the owner
# and module in their manifest. The other identities use a bridge.json list.
NATIVE_COMPATIBILITY = {
    "midas": "markets", "borges": "borges", "gutenberg": "gutenberg", "scheherazade": "fiction",
    "lumiere": "editing", "daedalus": "daedalus", "gepetto": "gepetto", "gulliver": "gulliver",
    "heron": "heron", "platos": "plato", "babel": "babel", "theseus": "theseus", "durer": "vector",
}

COMPATIBILITY_LINKS = tuple(
    Link(f"1.17.{app}", "Hub", f"{owner}/{app}",
         "Compatibility manifest + native catalogue" if app in NATIVE_COMPATIBILITY
         else "compatibility/bridge.json + GET /api/agent/tools",
         f"bridge:{app}", "C", "1.17")
    for app, owner in COMPATIBILITY_OWNERS.items()
)

SERVICE_LINKS = tuple(
    Link(f"4.5.{service}", "Family apps", owner, f"Common service {service}", f"service:{service}", "C", "4.5")
    for service, owner, _ in SERVICES
)

# Child checks run before the two parent summaries. Give the C group priority.
_PARENT_SUMMARIES = {"1.17", "4.5"}
LINKS = (
    *(row for row in INVENTORY if row.inventory_status == "C" and row.id not in _PARENT_SUMMARIES),
    *COMPATIBILITY_LINKS,
    *SERVICE_LINKS,
    *(row for row in INVENTORY if row.id in _PARENT_SUMMARIES),
    *(row for row in INVENTORY if row.inventory_status != "C"),
)
