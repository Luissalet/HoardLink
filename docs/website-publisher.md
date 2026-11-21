# Websites started by the Hub / Webs que arrancan con el Hub

Mercator publishes website packages on this PC. Each website has a stable loopback address distinct from the Hub and Mercator's management interface. It has its own stored package and private form inbox.

In Mercator, open **Websites** and select **Start Mercator with the Hub** when publishing. The **Keep alive** setting on Mercator's Hub card shows and controls the same preference. The Hub starts Mercator when it starts. Mercator brings back its active websites on their existing addresses. A manual stop applies for the current Hub session. If a site's port is occupied, Mercator leaves it assigned and retries.

Agents can publish, verify forms, export hosting ZIPs and undo changes through the Hub app relay for Mercator. Unpublishing affects the selected website and retains its package and form inbox. The Hub preference is managed at app level.

For an order, use `hub_website_publish_piece` with `space_id`, `piece_id`, `revision` and `slug`. This takes the retained HTML folder or ZIP. If the order contains a separate ZIP for the server, pass its registered `package_piece_id`. The ZIP must be a material derivative of the specified web revision in the same order. Mercator verifies the package fingerprint during import and returns the local URL, provenance and `website_undo` arguments.

For a frontend with a form, pass its existing POST path and a synthetic fixture. The declaration is added to Mercator's local copy of the static site.

```json
{
  "form": {
    "path": "/api/encargos",
    "fields": {"name": "Form test", "email": "test@example.invalid", "details": "Synthetic request"},
    "marker_field": "details"
  }
}
```

Use `website_form_check {url}` through Mercator to verify a real synthetic submission and its inbox record. Capture the receipt with the publication piece or record its verdict with `hub_agora_piece_check`. Use the existing [web piece checks](piece-checks.md) on the HTML piece for browser layout and accessibility checks. The form receipt includes `website_form_check_undo` arguments.

Use Mercator's `website_export` with the published `slug`, `revision` and `provenance`. Register the returned ZIP `path` with `hub_agora_piece_add`, using kind `web-package`, the publication revision as `source_rev` and the pinned web piece as a material parent in `derives_from`. Compare the registered SHA-256 with the export receipt. The registration and `website_export_undo` have separate undo actions.

For a booking handoff, register the exported ZIP as a `web-package` derived from the `interface-brief`. Use `hub_piece_validate_record` with `profile: "web"` and the requested `viewports` to measure the public page inside that ZIP before publishing the pinned revision.

The Hub does not provide a public endpoint for these websites. For a public destination, configure an HTTPS reverse proxy to the site's own listener, or use an external static host with a compatible form backend.

## Español

Mercator aloja paquetes web en este PC. Cada web tiene una dirección local fija distinta de las interfaces de gestión del Hub y Mercator. Sus archivos y envíos de formulario se guardan por separado.

En **Webs**, marca **Arrancar Mercator con el Hub** al publicar. La opción **Mantener viva** de la ficha de Mercator en el Hub controla el mismo ajuste. Al arrancar el Hub, Mercator recupera las publicaciones activas en sus direcciones anteriores. Una parada manual se respeta durante la sesión del Hub. Si el puerto de una web está ocupado, Mercator conserva la dirección y vuelve a intentar usarla.

Un agente puede publicar, comprobar formularios, exportar ZIP de alojamiento y deshacer cambios mediante las herramientas de Mercator que ofrece el Hub. Despublicar una web conserva su paquete y buzón. El ajuste de arranque se gestiona para toda la app.

Para publicar desde una orden, usa `hub_website_publish_piece` con `space_id`, `piece_id`, `revision` y `slug`. La herramienta pasa la carpeta HTML guardada o el ZIP. Si el servidor viaja en un ZIP aparte, indica el `package_piece_id` registrado. El ZIP debe derivar como material de esa revisión web en la misma orden. Mercator verifica la huella al importar y devuelve la URL local, la procedencia y los argumentos para `website_undo`.

Si el frontal tiene formulario, añade `form` con su ruta POST (`path`), los campos sintéticos (`fields`) y el campo de texto libre que llevará la marca de prueba (`marker_field`). Mercator añade la declaración a su copia local de la web estática.

Llama a `website_form_check {url}` en Mercator para comprobar un envío sintético y su entrada en el buzón. Puedes adjuntar el recibo a la pieza publicada o guardar el veredicto con `hub_agora_piece_check`. Aplica las [comprobaciones web de pieza](piece-checks.md) al HTML para revisar maquetación y accesibilidad. El recibo del formulario incluye los argumentos para `website_form_check_undo`.

Llama a `website_export` en Mercator con `slug`, `revision` y `provenance` de la publicación. Registra la ruta `path` del ZIP mediante `hub_agora_piece_add` como `web-package`, con la revisión de la publicación en `source_rev` y la pieza web fijada como material en `derives_from`. Coteja el SHA-256 de la pieza registrada con el recibo del ZIP. El registro y `website_export_undo` se deshacen por separado.

Para un relevo de reservas, registra el ZIP exportado como `web-package` derivado del `interface-brief`. Usa `hub_piece_validate_record` con el perfil `web` y las anchuras solicitadas para medir la página del ZIP antes de publicar esa revisión.

Para un destino público, configura un proxy HTTPS hacia la dirección de la web, o un alojamiento estático externo con un servidor de formulario compatible.
