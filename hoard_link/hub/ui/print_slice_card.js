export function renderPrintSliceCard(report, tr) {
  const element = (tag, text) => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    return node;
  };
  const detail = element("div");
  detail.className = "print-slice";
  detail.style.overflowWrap = "anywhere";
  const numbers = new Intl.NumberFormat(tr("es-ES", "en-GB"), {maximumFractionDigits: 2});
  const unknown = tr("Sin datos", "No data");
  const metric = (n, unit = "") => n == null ? unknown : numbers.format(n) + (unit ? " " + unit : "");
  const heading = [tr("Laminado", "Slicing"), report.engine, report.version].filter(Boolean).join(" · ");
  detail.append(element("strong", heading));

  if (report.measures) {
    const m = report.measures;
    detail.append(element("p", [
      tr("Soportes", "Supports") + " · " + metric(m.support_mm3, "mm³"),
      tr("Cambios de filamento", "Filament changes") + " · " + metric(m.filament_changes),
      tr("Tiempo total estimado", "Estimated total time") + " · " + metric(m.estimated_seconds == null ? null : m.estimated_seconds / 60, "min"),
    ].join(" / ")));
    if (m.filaments.length) {
      const rows = element("ul");
      for (const f of m.filaments) {
        const material = f.material_g != null ? metric(f.material_g, "g") :
          f.material_cm3 != null ? metric(f.material_cm3, "cm³") : metric(f.material_mm, "mm");
        rows.append(element("li", [
          tr(`Filamento ${f.slot}`, `Filament ${f.slot}`) + (f.colour ? " " + f.colour : ""),
          material,
          tr("Tiempo en trazados con extrusión", "Time on extrusion paths") + " · " + metric(f.deposition_seconds, "s"),
        ].join(" · ")));
      }
      detail.append(rows);
    }
  } else {
    detail.append(element("p", tr("El laminado no generó G-code.", "Slicing did not produce G-code.")));
  }

  if (report.diagnostics.length) {
    const list = element("ul");
    list.setAttribute("aria-label", tr("Defectos del laminado", "Slicing defects"));
    for (const diagnostic of report.diagnostics) {
      const {severity, code, message, count} = diagnostic;
      const label = severity === "warning" ? tr("Aviso", "Warning") : tr("Error", "Error");
      const descriptions = {
        bed_exclusion_preview: tr("Vista de la zona de exclusión de la cama", "Preview of the bed exclusion area"),
        slicer_timeout: tr("Tiempo de laminado agotado (s)", "Slicing timed out (s)"),
        slicer_launch: tr("No se pudo iniciar el laminador", "Could not start the slicer"),
        slicer_exit: tr("Código de salida del laminador", "Slicer exit code"),
        slicer_output: tr("Número inesperado de ficheros G-code", "Unexpected G-code file count"),
        slicer_unmeasured: tr("Mediciones de laminado incompletas", "Incomplete slicing measurements"),
        slicer_result: tr("Resultado del laminador ilegible", "Unreadable slicer result"),
        filaments_missing: tr("Filamentos usados frente a los esperados", "Used versus expected filaments"),
        support_missing: tr("No se han medido los soportes requeridos", "Required supports were not measured"),
      };
      list.append(element("li", [label, descriptions[code], message, count > 1 ? `×${count}` : ""].filter(Boolean).join(" · ")));
    }
    detail.append(list);
  }
  return detail;
}
