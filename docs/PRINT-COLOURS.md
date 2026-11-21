# Face colours in print slicing

Nikias offers two 3MF exports for a painted figure. The standard `color` export stores one `m:colorgroup` and sets each triangle's `pid` and `p1` to its group and colour index. The colour is surface appearance under the [3MF Materials and Properties Extension](https://github.com/3MFConsortium/spec_materials/blob/1.2.1/3MF%20Materials%20Extension.md). OrcaSlicer 2.3.1 maps the colour group at object level and does not turn those triangle indices into filament painting. [OrcaSlicer 3MF importer](https://github.com/OrcaSlicer/OrcaSlicer/blob/v2.3.1/src/libslic3r/Format/bbs_3mf.cpp)

For a print job, request `result_export` with `formats: ["orca", "color"]` and register the `OrcaSlicer.3mf` file as the slicing input. Its component mesh uses `paint_color` for the selected slot on a triangle, with the object defaulting to slot 1. Its `nikias:PaintedSlots` metadata records each slot used, its intended RGB colour and the number of triangles assigned to it. Pass resolved machine and process presets and filament presets in palette order. The painter's file stores the surface assignments. The profiles supply the physical printing settings.

OrcaSlicer 2.3.1 enters its saved-project path when the preset-free paint export identifies itself as a slicer project. That path reads project-settings keys before applying the explicit presets and can exit with `0xC0000005` on Windows. Nikias identifies the `orca` output as a model in its Application metadata, retaining the triangle painting, components and placement. The `bambu` export still identifies itself as a project. [CLI source](https://github.com/OrcaSlicer/OrcaSlicer/blob/v2.3.1/src/OrcaSlicer.cpp)

`expected_filaments` checks the number of used slots. The `print_colours` module can also compare the intended slot and RGB with slots that deposit material in the model. A slot used only by support or purging cannot satisfy that comparison. The standard export can still be read for its intended colours. Print material totals include purge and supports where the slicer attributes them to the slot, so use model deposition to check small details such as a coloured edge on a prop.

## Castellano

Nikias exporta la figura pintada en dos formatos 3MF. El formato estándar `color` guarda un grupo `m:colorgroup` y asigna a cada triángulo el grupo y el índice de color con `pid` y `p1`. La extensión de materiales de 3MF define así la apariencia de la superficie. OrcaSlicer 2.3.1 asigna ese grupo a nivel de objeto. La pintura por triángulo requiere su atributo `paint_color`.

Para laminar una figura, solicita a `result_export` los formatos `["orca", "color"]` y registra el archivo `OrcaSlicer.3mf` como entrada. Su malla guarda la pintura por triángulo. La ranura 1 es el valor por defecto. El metadato `nikias:PaintedSlots` indica las ranuras con caras asignadas, el color RGB y el recuento de caras. Pasa los perfiles resueltos de máquina y proceso y los perfiles de filamento por orden de paleta.

En Windows, OrcaSlicer 2.3.1 puede terminar con `0xC0000005` al leer como proyecto un 3MF de pintura sin ajustes completos. El metadato Application de `orca` identifica el archivo como modelo y conserva la pintura, los componentes y la colocación. La exportación `bambu` se identifica como proyecto.

`expected_filaments` comprueba cuántas ranuras se usaron. El módulo `print_colours` permite cotejar la ranura y el RGB exactos con la extrusión sobre la figura. Una ranura usada solo para soporte o purga no cuenta. El cotejo también lee los colores previstos del formato estándar. El material total por ranura incluye el consumo de purga y soportes que atribuye el laminador. Para comprobar detalles pequeños, usa la extrusión de la figura.
