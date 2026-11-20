# XLSX piece checks

The `spreadsheet` profile reads an XLSX workbook and reports the selected
sheet, header names in order, data row count and the observed cell types in
each column. The first row contains headers. The active sheet is selected
unless `requirements.sheet` names another tab. The tools infer the profile
from an `.xlsx` file when the caller leaves it out.

Use `hub_piece_validate_record` with the piece ID and its space. Pass the
dataset name and version returned by the export. For example:

```json
{
  "piece_id": 12,
  "space_id": "estudio",
  "requirements": {"dataset": {"name": "venues", "version": 4}}
}
```

The Hub reads the pinned version via `data_preview` and checks its row count,
ordered names and compatible cell types against the retained XLSX. To also
verify the bytes from that export, pass its `sha256` as
`requirements.sha256`. For data from other sources, give `row_count` and
`columns: [{name, type}]` directly. If the source version is unavailable or
an expected type has no observed cells, the check reports a pending
measurement. Formulas are reported as formulas.

`hub_piece_validate_record` stores the result on the current piece revision
and returns an Orders undo token. Pass `language: "es"` or `"en"` for the
note.

## Comprobación de hojas de cálculo

El perfil `spreadsheet` reconoce los ficheros XLSX. Lee los nombres en
orden, el número de filas y los tipos de celda de la hoja activa. Con
`requirements.sheet` puedes elegir otra pestaña. Para cotejar el fichero
con su origen, indica `requirements.dataset` con el nombre y la versión
de Nightingale. Si tienes la huella de la exportación, añade
`requirements.sha256`. La comprobación registrada devuelve el token de
deshacer de Órdenes.
