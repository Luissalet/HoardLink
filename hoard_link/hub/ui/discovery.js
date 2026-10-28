/* Task vocabulary for discovery. This describes catalog entries, never starts them. */
(() => {
  'use strict';
  const entries = {
    writer:['crear','Escribe novelas, organiza manuscritos y desarrolla tus historias.','Write novels, organize manuscripts and develop stories.','novela escribir escritura libro manuscrito ficción personaje historia guion'],
    cookhoard:['vida','Guarda recetas y organiza comidas e ingredientes.','Save recipes and organize meals and ingredients.','receta recetas cocina cocinar comida menú ingrediente'],
    prospero:['crear','Crea y edita vídeo, audio y proyectos audiovisuales.','Create and edit video, audio and audiovisual projects.','video vídeo audio película editar montaje multimedia'],
    daguerre:['crear','Organiza y trabaja con fotografías e imágenes.','Organize and work with photographs and images.','foto fotografía imagen imágenes álbum galería'],
    vulcan:['crear','Crea y trabaja con modelos y escenas en 3D.','Create and work with 3D models and scenes.','3d modelo modelado escena objeto malla'],
    vitruvius:['crear','Desarrolla software y organiza proyectos de diseño y código.','Develop software and organize design and source projects.','software desarrollo desarrollar código diseño diseñar documentación api'],
    pygmalion:['analizar','Prepara datos, desarrolla y evalúa modelos de inteligencia artificial.','Prepare datasets, develop and evaluate AI models.','modelo modelos entrenar entrenamiento evaluación evaluar dataset lora pruebas comparación'],
    scheherazade:['crear','Desarrolla historias y experiencias narrativas.','Develop stories and narrative experiences.','historia narración narrativa cuento ficción'],
    atlas:['organizar','Organiza archivos y carpetas compartidos de tus proyectos.','Organize shared project files and folders.','archivo archivos ficheros carpeta carpetas proyecto compartido revisión versiones'],
    nightingale:['analizar','Analiza datos, tablas y hojas de cálculo.','Analyze data, tables and spreadsheets.','datos tabla tablas excel hoja cálculo estadística csv'],
    hypatia:['analizar','Resuelve y explora problemas de matemáticas.','Solve and explore mathematics problems.','matemática matemáticas ecuación función cálculo álgebra'],
    galton:['analizar','Evalúa modelos desde el estudio de Pygmalion.','Evaluate models through the Pygmalion studio.','modelo modelos evaluación evaluar pruebas comparación'],
    coase:['analizar','Investiga empresas y prepara informes empresariales.','Research companies and prepare business reports.','empresa empresas informe informes negocio análisis'],
    mercator:['organizar','Organiza contactos comerciales, ventas y seguimiento.','Organize business contacts, sales and follow-up.','ventas venta cliente clientes comercial crm calendario'],
    ledger:['vida','Lleva tus cuentas, movimientos y finanzas personales.','Track accounts, transactions and personal finances.','dinero gastos ingreso finanzas cuenta presupuesto movimiento'],
    homehoard:['vida','Organiza la casa y las tareas del hogar.','Organize your home and household tasks.','casa hogar tarea mantenimiento doméstico'],
    people:['vida','Organiza tus contactos y relaciones personales.','Organize contacts and personal relationships.','persona personas contacto contactos relación agenda'],
    dorian:['vida','Consulta tu contexto personal y revisa reflexiones con sus fuentes.','Consult personal context and review reflections with their sources.','contexto personal reflexión reflexiones perfil diario fuentes'],
    jobhunter:['vida','Busca empleo y sigue tus candidaturas.','Find jobs and track applications.','empleo trabajo oferta ofertas candidatura currículum'],
    gamerhoard:['vida','Organiza tu colección de videojuegos.','Organize your video game collection.','juego juegos videojuego videojuegos colección jugar'],
    silver:['vida','Consulta y organiza tus proyectos de Pokémon.','Explore and organize your Pokémon projects.','pokemon pokémon criatura equipo juego'],
    talos:['sistema','Consulta y organiza la actividad de tu equipo.','Review and organize your computer activity.','equipo ordenador disco archivo actividad sistema'],
    prometheus:['sistema','Gestiona los modelos y servicios de inteligencia artificial.','Manage AI models and services.','modelo modelos motor motores ia inteligencia artificial spark gpu'],
    hermes:['organizar','Organiza comunicaciones y automatizaciones.','Organize communications and automations.','correo mensaje comunicación automatizar calendario'],
    kafka:['organizar','Guarda facturas, contratos y pólizas y avisa de sus plazos.','Keep invoices, contracts and policies and track their deadlines.','papeles facturas contratos seguros polizas plazos vencimientos pdf documentos tramites'],
    links:['organizar','Guarda enlaces, lecturas y descargas de vídeo.','Save links, reading and video downloads.','enlaces links lecturas descargas videos marcadores'],
    'kinect-hoard':['crear','Captura cámara, profundidad y cuerpo con un Kinect.','Capture camera, depth and body tracking with a Kinect.','kinect camara profundidad cuerpo esqueleto audio'],
        cicero:['crear','Trabaja con documentos y presentaciones.','Work with documents and presentations.','presentación presentaciones documento diapositiva discurso'],
  };
  const normalize = value => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase();
  const words = value => normalize(value).split(/[^a-z0-9]+/).filter(w => w.length > 2 && !['una','uno','los','las','del','con','para','quiero','necesito','como','hacer','mis','puedo','the','and','with','want'].includes(w)).map(w=>w.length>5 && /(?:ar|er|ir)$/.test(w)?w.slice(0,-2):w);
  const entry = app => entries[app.id] || null;
  window.HubDiscovery = {
    description(app, spanish) { const e=entry(app); return e ? e[spanish ? 1 : 2] : app.purpose || app.name; },
    launchMessage(app, spanish) {
      const missing=/manifest.*launch|no.*launch hint|no process launch/i.test(app.launch_reason || '');
      return spanish ? (missing?'El inicio desde el Hub no está configurado. Abre Gestionar → Detalles para consultar la configuración.':'No se puede iniciar con la configuración actual. Abre Gestionar → Detalles para consultar el motivo.') : (missing?'Starting from the Hub is not configured. Open Manage → Details to review the configuration.':'This app cannot start with its current configuration. Open Manage → Details to review the reason.');
    },
    category(app) { return entry(app)?.[0] || 'otras'; },
    matches(app, query) {
      const tokens=words(query); if (!tokens.length) return !normalize(query).trim() || normalize(app.name).includes(normalize(query));
      const hay=normalize([app.name,app.id,app.purpose,...(entry(app) || []),...(app.capabilities || [])].join(' '));
      return tokens.every(w => hay.includes(w) || w.length>5 && hay.includes(w.replace(/(?:es|os|as)$/,'')));
    },
    categories(spanish) { return spanish ? [['todas','Todas'],['crear','Crear y escribir'],['organizar','Organizar y consultar'],['analizar','Analizar'],['vida','Vida cotidiana'],['sistema','Equipo y modelos'],['otras','Otras herramientas']] : [['todas','All'],['crear','Create and write'],['organizar','Organize and find'],['analizar','Analyze'],['vida','Everyday life'],['sistema','Computer and models'],['otras','Other tools']]; },
  };
})();
