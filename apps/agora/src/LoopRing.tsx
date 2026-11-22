import { useId, useRef, type CSSProperties } from "react";
import { lang, locale, tr } from "./locale";
import "./loop.css";

export type LoopStep = {
  kind: "plan" | "tool" | "result" | "commit" | "submit" | "end";
  label: string;
  at: number;
  lap: number;
  ok?: boolean | null;
  title?: string;
  detail?: string;
  ms?: number;
};
export type Practice = {
  id: string;
  label: string;
  state: "ok" | "fail" | "unknown" | "pending";
  evidence: string;
};
export type WorkKind = "code" | "research" | "files" | "ops" | "chat";
export type Loop = {
  phase: "plan" | "act" | "observe" | "done" | "idle";
  since?: number;
  lap: number;
  tool: string | null;
  tool_title?: string | null;
  kind?: WorkKind;
  kind_label?: string;
  steps?: LoopStep[];
  practices?: Practice[];
  practices_summary: { ok: number; fail: number; open: number };
  tokens?: number | null;
  turn_started_at?: number | null;
  /** Tool calls of the turn so far and how many have finished. */
  calls?: number;
  calls_done?: number;
};

export const phaseLabel: Record<Loop["phase"], string> = {
  plan: tr("Planear", "Plan"),
  act: tr("Actuar", "Act"),
  observe: tr("Observar", "Observe"),
  done: tr("Hecho", "Done"),
  idle: tr("En reposo", "Idle"),
};
type RingPhase = "plan" | "act" | "observe";
/** Where each phase sits on the ring, in degrees from the x axis: plan at the top, then clockwise. */
const angle: Record<RingPhase, number> = { plan: -90, act: 30, observe: 150 };
const nodes: RingPhase[] = ["plan", "act", "observe"];
const R = 50;
const TAIL = 110;
const stepLabel: Record<LoopStep["kind"], string> = {
  plan: "Plan",
  tool: tr("Herramienta", "Tool"),
  result: tr("Resultado", "Result"),
  commit: "Commit",
  submit: tr("Entrega", "Submission"),
  end: tr("Fin", "End"),
};
const stateLabel: Record<Practice["state"], string> = {
  ok: tr("Cumplida", "Met"),
  fail: tr("Incumplida", "Unmet"),
  unknown: tr("Sin confirmar", "Unconfirmed"),
  pending: tr("Falta", "Pending"),
};
const mark: Record<Practice["state"], string> = { ok: "✓", fail: "✕", unknown: "?", pending: "·" };

const stepText = new Map([["razona", "reasoning"], ["fin del turno", "end of turn"]]);
const kindLabels: Record<WorkKind, string> = {
  code: "Code", research: "Research", files: "Files and documents", ops: "Operations", chat: "Conversation",
};
const kindLabel = (loop: Loop) => lang === "en" && loop.kind ? kindLabels[loop.kind] ?? loop.kind_label : loop.kind_label;
const practiceLabels = new Map(Object.entries({
  tests: "Tests run", browser: "App checked in a browser",
  docs: "Documentation updated (FAUSTUS.md / README / AGENTS.md)", commit: "Commit uses the required identity",
  no_evidence: "Commit contains no evidence files", agora: "Delivery recorded in Agora (submit / done)",
  sources_opened: "Sources opened and read", sources_cited: "Sources cited in the response",
  dated: "Dates checked", verified: "Claims checked against sources", language: "Response in the requester’s language",
  output_checked: "Output opened and checked", format: "Requested format used",
  ran_ok: "Commands completed successfully", checked: "Result checked after running",
  claim: "Task claimed with path locks",
}));
const toolLabels = new Map(Object.entries({
  Leer: "Read", Escribir: "Write", Editar: "Edit", "Editar cuaderno": "Edit notebook", Parche: "Patch",
  Listar: "List", "Buscar ficheros": "Find files", "Buscar en ficheros": "Search files",
  "Buscar en la web": "Search the web", "Abrir URL": "Open URL", Navegador: "Browser",
  Delegar: "Delegate", Tareas: "Tasks", Preguntar: "Ask", "Ver imagen": "View image",
}));
const toolLabel = (label: string) => lang === "en" ? toolLabels.get(label) ?? label.replace(/^Navegador · /, "Browser · ") : label;

function point(r: number, deg: number): [number, number] {
  const rad = (deg * Math.PI) / 180;
  return [60 + r * Math.cos(rad), 60 + r * Math.sin(rad)];
}

function arc(r: number, from: number, to: number) {
  const [x1, y1] = point(r, from);
  const [x2, y2] = point(r, to);
  return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 ${to - from > 180 ? 1 : 0} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
}

/** The angle the lit arc points at after `prev`: the phase's place, reached clockwise, so it never turns back. */
export function nextAngle(prev: number | null, phase: Loop["phase"]) {
  const base = phase === "done" ? 270 : angle[phase === "idle" ? "plan" : phase];
  return prev === null ? base : prev + ((((base - prev) % 360) + 360) % 360);
}

/**
 * Plan -> act -> observe around the agent. A glowing arc travels clockwise to the phase its last transcript
 * line set, with a halo at its head while the agent is live; the lap sits in the centre. In the large ring
 * the phases are pills outside the ring and the agent's name is in the centre.
 */
export function LoopRing({
  loop,
  color,
  size = "md",
  live = true,
  name,
}: {
  loop: Loop;
  color?: string;
  size?: "sm" | "md" | "lg";
  live?: boolean;
  name?: string;
}) {
  const gid = `agloop-grad-${useId().replace(/[^\w-]/g, "")}`;
  const current = loop.phase === "done" || loop.phase === "idle" ? null : loop.phase;
  const order = current ? nodes.indexOf(current) : -1;
  const last = useRef<number | null>(null);
  const head = nextAngle(last.current, loop.phase);
  last.current = head;
  const [fx, fy] = point(R, -TAIL);
  const [tx, ty] = point(R, 0);
  return (
    <span
      className="agloop-ring"
      data-size={size}
      data-phase={loop.phase}
      data-live={live && loop.phase !== "done" ? "true" : "false"}
      role="img"
      aria-label={tr(`Bucle: ${phaseLabel[loop.phase]} · vuelta ${loop.lap}`, `Loop: ${phaseLabel[loop.phase]} · lap ${loop.lap}`)}
      style={color ? ({ "--actor-color": color } as CSSProperties) : undefined}
    >
      <svg viewBox="0 0 120 120" aria-hidden="true">
        <defs>
          <linearGradient id={gid} gradientUnits="userSpaceOnUse" x1={fx} y1={fy} x2={tx} y2={ty}>
            <stop offset="0" className="agloop-stop-from" />
            <stop offset="1" className="agloop-stop-to" />
          </linearGradient>
        </defs>
        <circle className="agloop-track" cx="60" cy="60" r={R} />
        {loop.phase === "done" ? (
          <circle className="agloop-full" cx="60" cy="60" r={R} />
        ) : (
          <g className="agloop-comet" style={{ transform: `rotate(${head}deg)` }}>
            <path className="agloop-tail" d={arc(R, -TAIL, 0)} stroke={`url(#${gid})`} />
            <circle className="agloop-halo" cx={60 + R} cy="60" r="7" />
            <circle className="agloop-head" cx={60 + R} cy="60" r="3.6" />
          </g>
        )}
        {nodes.map((id, i) => {
          const [x, y] = point(R, angle[id]);
          const state = loop.phase === "done" || order > i ? "passed" : id === current ? "active" : "ahead";
          return <circle key={id} className="agloop-node" data-state={state} cx={x} cy={y} r="4.6" />;
        })}
      </svg>
      {size !== "sm" && (
        <span className="agloop-centre">
          {size === "lg" && name && <span className="agloop-name">{name}</span>}
          <strong className="agloop-lap">{loop.phase === "done" ? "✓" : loop.lap || "·"}</strong>
          <small>{loop.phase === "done" ? phaseLabel.done : tr("Vuelta", "Lap")}</small>
        </span>
      )}
      {size === "lg" &&
        nodes.map((id) => (
          <span key={id} className="agloop-pill" data-at={id} data-active={id === current ? "true" : "false"}>
            {phaseLabel[id]}
          </span>
        ))}
    </span>
  );
}

const clock = (value: number) =>
  new Date(value * 1000).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

const secs = (ms: number) => (ms < 10000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms / 1000)} s`);

export function loopFacts(loop: Loop, now: number) {
  const parts = [phaseLabel[loop.phase]];
  if (loop.tool) parts.push(toolLabel(loop.tool_title || loop.tool));
  parts.push(tr(`vuelta ${loop.lap || 0}`, `lap ${loop.lap || 0}`));
  if (loop.turn_started_at && loop.phase !== "done") parts.push(`${Math.max(0, Math.round(now - loop.turn_started_at))} s`);
  if (loop.tokens) parts.push(`${loop.tokens.toLocaleString(locale)} tokens`);
  const kind = kindLabel(loop);
  if (kind) parts.push(kind.toLocaleLowerCase(locale));
  return parts.join(" · ");
}

/** What a small task card says: the phase, the tool by its name and the practices that apply to the work. */
export function loopBrief(loop: Loop) {
  const s = loop.practices_summary;
  const total = s.ok + s.fail + s.open;
  const parts = [phaseLabel[loop.phase]];
  if (loop.tool) parts.push(toolLabel(loop.tool_title || loop.tool));
  const kind = kindLabel(loop);
  if (kind) parts.push(kind.toLocaleLowerCase(locale));
  if (total) parts.push(tr(`${s.ok}/${total} prácticas`, `${s.ok}/${total} practices`));
  return parts.join(" · ");
}

function StepRow({ step }: { step: LoopStep }) {
  let what;
  if (step.kind === "tool" && step.title) {
    what = (
      <span className="agloop-what">
        <strong>{toolLabel(step.title)}</strong>
        {step.detail && <span>{step.detail}</span>}
      </span>
    );
  } else if (step.kind === "result" && step.title) {
    const verdict = step.ok === false ? "✕ error" : step.ok ? "✓ ok" : tr("· terminada", "· finished");
    what = (
      <span className="agloop-what">
        <b data-ok={step.ok === false ? "false" : step.ok ? "true" : undefined}>{verdict}</b>
        <span>
          {toolLabel(step.title)}
          {step.ms !== undefined ? ` · ${secs(step.ms)}` : ""}
        </span>
      </span>
    );
  } else {
    what = <span className="agloop-what">{lang === "en" ? stepText.get(step.label) ?? step.label : step.label}</span>;
  }
  return (
    <li data-kind={step.kind} data-ok={step.ok === false ? "false" : undefined}>
      <span>{stepLabel[step.kind]}</span>
      {what}
      <time>{clock(step.at)}</time>
    </li>
  );
}

/** The steps of the observed turn and the practices of its kind of work, with the evidence of each mark. */
export function LoopPanel({ loop }: { loop: Loop }) {
  const s = loop.practices_summary;
  const practices = loop.practices ?? [];
  const kind = kindLabel(loop);
  return (
    <div className="agloop-panel" data-kind={loop.kind}>
      <h3>{tr("Pasos", "Steps")}</h3>
      {loop.steps?.length ? (
        <ol className="agloop-steps">
          {loop.steps.map((step, i) => (
            <StepRow key={i} step={step} />
          ))}
        </ol>
      ) : (
        <p className="aglive-empty">{tr("Sin pasos observados en este turno.", "No steps observed in this turn.")}</p>
      )}
      {practices.length > 0 ? (
        <>
          <h3>
            {tr("Buenas prácticas a revisar", "Practices to review")}{kind ? ` · ${kind}` : ""}
            <small>
              {tr(`${s.ok} cumplidas · ${s.fail} incumplidas · ${s.open} faltan`, `${s.ok} met · ${s.fail} unmet · ${s.open} open`)}
            </small>
          </h3>
          <ul className="agloop-practices">
            {practices.map((p) => (
              <li key={p.id} data-state={p.state} data-practice={p.id}>
                <span aria-hidden="true">{mark[p.state]}</span>
                <span>{lang === "en" ? practiceLabels.get(p.id) ?? p.label : p.label}</span>
                <small>
                  {stateLabel[p.state]}
                  {p.evidence ? ` · ${p.evidence}` : ""}
                </small>
              </li>
            ))}
          </ul>
        </>
      ) : (
        <p className="aglive-empty">{kind || tr("Conversación", "Conversation")} · {tr("Sin buenas prácticas que revisar en este turno.", "No practices to review in this turn.")}</p>
      )}
    </div>
  );
}

/** «5 pasos · 2 min» of a turn in progress, with a bar for the calls that have finished. Nothing before the first call. */
export function TurnSteps({ loop, now }: { loop: Loop; now: number }) {
  const calls = loop.calls ?? 0;
  if (!calls || loop.phase === "done" || loop.phase === "idle") return null;
  const done = Math.min(loop.calls_done ?? 0, calls);
  const minutes = loop.turn_started_at ? Math.max(0, Math.round((now - loop.turn_started_at) / 60)) : null;
  return (
    <span className="agloop-steps" data-testid="agloop-steps">
      <span className="agloop-steps__bar" role="progressbar" aria-valuemin={0} aria-valuemax={calls} aria-valuenow={done}>
        <i style={{ inlineSize: `${Math.round((done / calls) * 100)}%` }} />
      </span>
      {calls === 1 ? tr("1 paso", "1 step") : tr(`${calls} pasos`, `${calls} steps`)}
      {minutes !== null ? ` · ${minutes < 1 ? tr("menos de 1 min", "less than 1 min") : `${minutes} min`}` : ""}
    </span>
  );
}
