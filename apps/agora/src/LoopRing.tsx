import type { CSSProperties } from "react";
import "./loop.css";

export type LoopStep = {
  kind: "plan" | "tool" | "result" | "commit" | "submit" | "end";
  label: string;
  at: number;
  lap: number;
  ok?: boolean | null;
};
export type Practice = {
  id: string;
  label: string;
  state: "ok" | "fail" | "unknown" | "pending";
  evidence: string;
};
export type Loop = {
  phase: "plan" | "act" | "observe" | "done" | "idle";
  since: number;
  lap: number;
  tool: string | null;
  steps?: LoopStep[];
  practices?: Practice[];
  practices_summary: { ok: number; fail: number; open: number };
  tokens?: number | null;
  turn_started_at?: number | null;
};

export const phaseLabel: Record<Loop["phase"], string> = {
  plan: "Planear",
  act: "Actuar",
  observe: "Observar",
  done: "Terminado",
  idle: "En reposo",
};
const segments = [
  { id: "plan", label: "Planear", from: -90 },
  { id: "act", label: "Actuar", from: 30 },
  { id: "observe", label: "Observar", from: 150 },
] as const;
const stepLabel: Record<LoopStep["kind"], string> = {
  plan: "Plan",
  tool: "Herramienta",
  result: "Resultado",
  commit: "Commit",
  submit: "Entrega",
  end: "Fin",
};
const stateLabel: Record<Practice["state"], string> = {
  ok: "Cumplida",
  fail: "Incumplida",
  unknown: "Sin confirmar",
  pending: "Falta",
};
const mark: Record<Practice["state"], string> = { ok: "✓", fail: "✕", unknown: "?", pending: "·" };

function point(r: number, deg: number): [number, number] {
  const rad = (deg * Math.PI) / 180;
  return [60 + r * Math.cos(rad), 60 + r * Math.sin(rad)];
}

function arc(r: number, from: number, to: number) {
  const [x1, y1] = point(r, from);
  const [x2, y2] = point(r, to);
  return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 0 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

/** Plan -> act -> observe around the agent; the lit segment is the phase its last transcript line set. */
export function LoopRing({
  loop,
  color,
  size = "md",
  live = true,
}: {
  loop: Loop;
  color?: string;
  size?: "sm" | "md";
  live?: boolean;
}) {
  return (
    <span
      className="agloop-ring"
      data-size={size}
      data-phase={loop.phase}
      data-live={live && loop.phase !== "done" ? "true" : "false"}
      role="img"
      aria-label={`Bucle: ${phaseLabel[loop.phase]} · vuelta ${loop.lap}`}
      style={color ? ({ "--actor-color": color } as CSSProperties) : undefined}
    >
      <svg viewBox={size === "md" ? "-26 -4 172 128" : "0 0 120 120"} aria-hidden="true">
        {segments.map((seg) => {
          const d = arc(44, seg.from + 3, seg.from + 117);
          const [lx, ly] = point(58, seg.from + 60);
          const active = seg.id === loop.phase;
          return (
            <g key={seg.id} data-active={active ? "true" : "false"}>
              <path className="agloop-track" d={d} />
              {active && <path className="agloop-sweep" d={d} pathLength={100} />}
              {size === "md" && (
                <text className="agloop-label" x={lx} y={ly + 3} textAnchor="middle">
                  {seg.label}
                </text>
              )}
            </g>
          );
        })}
        <circle className="agloop-core" cx="60" cy="60" r={size === "md" ? 24 : 30} />
        <text className="agloop-lap" x="60" y={size === "md" ? 66 : 71} textAnchor="middle">
          {loop.phase === "done" ? "✓" : loop.lap || "·"}
        </text>
      </svg>
    </span>
  );
}

const clock = (value: number) =>
  new Date(value * 1000).toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit", second: "2-digit" });

export function loopFacts(loop: Loop, now: number) {
  const parts = [phaseLabel[loop.phase]];
  if (loop.tool) parts.push(loop.tool);
  parts.push(`vuelta ${loop.lap || 0}`);
  if (loop.turn_started_at && loop.phase !== "done") parts.push(`${Math.max(0, Math.round(now - loop.turn_started_at))} s`);
  if (loop.tokens) parts.push(`${loop.tokens.toLocaleString("es-ES")} tokens`);
  return parts.join(" · ");
}

/** The steps of the observed turn and the practice checklist, with the evidence of each mark. */
export function LoopPanel({ loop }: { loop: Loop }) {
  const s = loop.practices_summary;
  return (
    <div className="agloop-panel">
      <h3>Pasos</h3>
      {loop.steps?.length ? (
        <ol className="agloop-steps">
          {loop.steps.map((step, i) => (
            <li key={i} data-kind={step.kind} data-ok={step.ok === false ? "false" : undefined}>
              <span>{stepLabel[step.kind]}</span>
              <span>{step.label}</span>
              <time>{clock(step.at)}</time>
            </li>
          ))}
        </ol>
      ) : (
        <p className="aglive-empty">Sin pasos observados en este turno.</p>
      )}
      <h3>
        Buenas prácticas a revisar
        <small>
          {s.ok} cumplidas · {s.fail} incumplidas · {s.open} faltan
        </small>
      </h3>
      <ul className="agloop-practices">
        {loop.practices?.map((p) => (
          <li key={p.id} data-state={p.state}>
            <span aria-hidden="true">{mark[p.state]}</span>
            <span>{p.label}</span>
            <small>
              {stateLabel[p.state]}
              {p.evidence ? ` · ${p.evidence}` : ""}
            </small>
          </li>
        ))}
      </ul>
    </div>
  );
}
