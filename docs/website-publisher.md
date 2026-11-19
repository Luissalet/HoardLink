# Websites started by the Hub / Webs que arrancan con el Hub

Mercator publishes website packages on this PC. Each website has a stable loopback address distinct from the Hub and Mercator's management interface. It has its own stored package and private form inbox.

In Mercator, open **Websites** and select **Start Mercator with the Hub** when publishing. The **Keep alive** setting on Mercator's Hub card shows and controls the same preference. The Hub starts Mercator when it starts. Mercator brings back its active websites on their existing addresses. A manual stop applies for the current Hub session. If a site's port is occupied, Mercator leaves it assigned and retries.

Agents can call the Mercator tools through the Hub app relay: website_publish, website_status, website_submissions, website_unpublish and website_undo. Unpublishing affects the selected website and retains its package and form inbox. The Hub preference is managed at app level.

The Hub does not provide a public endpoint for these websites. For a public destination, configure an HTTPS reverse proxy to the site's own listener, or use an external static host with a compatible form backend.

## Español

Mercator aloja paquetes web en este PC. Cada web tiene una dirección local fija distinta de las interfaces de gestión del Hub y Mercator. Sus archivos y envíos de formulario se guardan por separado.

En **Webs**, marca **Arrancar Mercator con el Hub** al publicar. La opción **Mantener viva** de la ficha de Mercator en el Hub controla el mismo ajuste. Al arrancar el Hub, Mercator recupera las publicaciones activas en sus direcciones anteriores. Una parada manual se respeta durante la sesión del Hub. Si el puerto de una web está ocupado, Mercator conserva la dirección y vuelve a intentar usarla.

Un agente puede publicar, consultar el estado y los envíos, despublicar y deshacer mediante las herramientas de Mercator que ofrece el Hub. Despublicar una web conserva su paquete y buzón. El ajuste de arranque se gestiona para toda la app.

Para un destino público, configura un proxy HTTPS hacia la dirección de la web, o un alojamiento estático externo con un servidor de formulario compatible.
