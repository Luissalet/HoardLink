import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import {
  ArrowLeft,
  Crosshair,
  FileCode2,
  MessageSquare,
  RefreshCw,
  X,
} from "lucide-react";
import { api, spaceId, type Task, type Message } from "./api";
import { locale, messageBody, spaceTitle, tr } from "./locale";
import { DeliveryDiff } from "./DeliveryDiff";
import { LoopPanel, LoopRing, TurnSteps, loopBrief, loopFacts, phaseLabel, type Loop } from "./LoopRing";
import "./live.css";

type Session = {
  key: string;
  state: string;
  title: string;
  engine: string;
  cwd: string;
  last_text: string;
  tool?: { name: string; input: string };
  last_action?: { name: string; input: string };
  binding?: string;
  last_activity: number;
};
type Actor = {
  id: string;
  name: string;
  color: string;
  state: string;
  last_signal: number;
  resting: boolean;
  note: string;
  target: { kind: string; id: string; at: number };
  session: Session | null;
  loop?: Loop | null;
};
type FileRow = {
  id: string;
  path: string;
  repo: string;
  source: string;
  owners: string[];
  preview: string;
  at: number;
  tool?: string;
};
type Snapshot = {
  now: number;
  space: { id: string; title: string };
  tasks: (Task & { progress?: string; loop?: Loop | null; updated?: number })[];
  messages: Message[];
  files: FileRow[];
  actors: Actor[];
  watch_error?: string;
};
type CursorPoint = { x: number; y: number; width: number; hidden: boolean };
const columns = [
  { id: "open", label: tr("Por hacer", "To do"), states: ["open"] },
  {
    id: "working",
    label: tr("En curso", "In progress"),
    states: ["claimed", "in_progress", "changes", "blocked"],
  },
  { id: "review", label: tr("En revisión", "In review"), states: ["review", "approved"] },
  { id: "done", label: tr("Finalizado", "Completed"), states: ["done", "dropped"] },
];
const states: Record<string, string> = {
  working: tr("Trabajando", "Working"),
  tool: tr("Herramienta", "Using a tool"),
  waiting: tr("Esperando", "Waiting"),
  stale: tr("Sin señal reciente", "No recent signal"),
  idle: tr("En reposo", "Idle"),
};
const clock = (value: number) =>
  new Date(value * 1000).toLocaleTimeString(locale, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

/** Only newly observed text types in. Existing history is immediately readable. */
function ObservedText({ text, fresh }: { text: string; fresh: boolean }) {
  const [length, setLength] = useState(fresh ? 0 : text.length);
  useEffect(() => {
    if (!fresh || matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setLength(text.length);
      return;
    }
    setLength(0);
    const start = performance.now();
    const timer = window.setInterval(() => {
      const count = Math.ceil((performance.now() - start) / 7);
      setLength(count);
      if (count >= text.length) clearInterval(timer);
    }, 30);
    return () => clearInterval(timer);
  }, [text, fresh]);
  return (
    <span aria-label={text}>
      <span aria-hidden="true">{text.slice(0, length)}</span>
    </span>
  );
}

// The arrow, name stripe and note appearance reuse the App Dock bridge cursor.
function AgentCursor({
  actor,
  point,
  resting,
  noteVisible,
  onSelect,
}: {
  actor: Actor;
  point: CursorPoint;
  resting: boolean;
  noteVisible: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      className="aglive-cursor"
      data-agent={actor.id}
      data-resting={resting || undefined}
      data-hidden={point.hidden || undefined}
      aria-label={tr(`Sesión de ${actor.name}`, `${actor.name} session`)}
      onClick={onSelect}
      style={
        {
          "--actor-color": actor.color,
          "--cursor-width": `${point.width}px`,
          transform: `translate(${point.x}px, ${point.y}px)`,
        } as CSSProperties
      }
    >
      <svg className="aglive-arrow" viewBox="0 0 25 30" aria-hidden="true">
        <path
          d="M2 2L2 23L8 18L12 27L17 25L13 16L22 16Z"
          fill="var(--actor-color)"
          stroke="white"
          strokeWidth="1.5"
          strokeLinejoin="round"
        />
      </svg>
      <span className="aglive-name">{actor.name}</span>
      {noteVisible && (
        <span className="aglive-note" key={actor.note}>
          {actor.note}
        </span>
      )}
    </button>
  );
}

export function LiveView({ onClose }: { onClose?: () => void }) {
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState("");
  const [following, setFollowing] = useState("");
  const [now, setNow] = useState(Date.now() / 1000);
  const [points, setPoints] = useState<Record<string, CursorPoint>>({});
  const [hostTheme, setHostTheme] = useState<CSSProperties>({});
  const [taskDetail, setTaskDetail] = useState<{
    task: Task & { commits?: string[]; submission_revision?: number };
    messages: Message[];
  } | null>(null);
  const [channel, setChannel] = useState("general");
  const [channels, setChannels] = useState<{ slug: string; title: string }[]>(
    [],
  );
  const stage = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const targets = useRef(new Map<string, HTMLElement>());
  const firstSnapshot = useRef(0);
  const clockOffset = useRef(0);
  const taskRects = useRef(new Map<string, DOMRect>());
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (window.parent === window) return;
    function receive(event: MessageEvent) {
      if (event.source !== window.parent || event.data?.type !== "aglive/theme")
        return;
      const theme: Record<string, string> = {};
      for (const name of [
        "canvas",
        "surface",
        "sunken",
        "hover",
        "text",
        "muted",
        "line",
        "strong",
        "accent",
        "focus",
        "error",
        "error-bg",
        "scheme",
      ]) {
        const value = event.data.tokens?.[name];
        if (typeof value === "string") theme[`--aglive-${name}`] = value;
      }
      setHostTheme(theme as CSSProperties);
    }
    window.addEventListener("message", receive);
    window.parent.postMessage({ type: "aglive/ready" }, "*");
    return () => window.removeEventListener("message", receive);
  }, []);
  useEffect(() => {
    firstSnapshot.current = 0;
    setData(null);
    setPoints({});
    setSelected("");
    setFollowing("");
  }, [channel]);
  useEffect(() => {
    let stopped = false;
    api("/api/agora/channels")
      .then((d) => {
        if (!stopped) setChannels(d.channels || []);
      })
      .catch(() => {});
    let timer: number;
    async function refresh() {
      try {
        const snapshot: Snapshot = await api(
          "/api/agora/live?channel=" + encodeURIComponent(channel),
        );
        if (stopped) return;
        if (!firstSnapshot.current) firstSnapshot.current = snapshot.now;
        clockOffset.current = snapshot.now - Date.now() / 1000;
        setNow(snapshot.now);
        setData(snapshot);
        setError("");
      } catch (failure) {
        if (!stopped) setError((failure as Error).message);
      } finally {
        if (!stopped)
          timer = window.setTimeout(refresh, document.hidden ? 15000 : 2500);
      }
    }
    void refresh();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [channel, retry]);
  useEffect(() => {
    const timer = window.setInterval(
      () => setNow(Date.now() / 1000 + clockOffset.current),
      1000,
    );
    return () => clearInterval(timer);
  }, []);

  const bind = (key: string) => (node: HTMLElement | null) => {
    if (node) targets.current.set(key, node);
    else targets.current.delete(key);
  };
  const noteVisible = (actor: Actor) =>
    !!actor.note &&
    !error &&
    now - actor.last_signal < 60 &&
    now - actor.target.at < 8;
  // Each actor gets a separate berth outside the target's readable content.
  // Reserving its actual height also prevents collisions in narrow columns.
  const berths = (key: string) => (
    <div className="aglive-berths" aria-hidden="true">
      {data?.actors
        .filter((a) => `${a.target.kind}:${a.target.id}` === key)
        .map((a) => (
          <div
            key={a.id}
            data-berth={a.id}
            style={{ height: noteVisible(a) ? 116 : 38 }}
          />
        ))}
    </div>
  );
  useLayoutEffect(() => {
    const viewport = stage.current,
      area = content.current;
    if (!data || !viewport || !area) return;
    let frame = 0;
    function locate() {
      const rect = area!.getBoundingClientRect();
      const next: Record<string, CursorPoint> = {};
      for (const actor of data!.actors) {
        const key = actor.target.kind + ":" + actor.target.id;
        const element =
          targets.current.get(key) || targets.current.get("rest:" + actor.id);
        if (!element) continue;
        const berth = Array.from(
          element.querySelectorAll<HTMLElement>("[data-berth]"),
        ).find((node) => node.dataset.berth === actor.id);
        const box = (berth || element).getBoundingClientRect();
        const column = element
          .closest(".aglive-column > div")
          ?.getBoundingClientRect();
        next[actor.id] = {
          x: box.left - rect.left + 2,
          y: box.top - rect.top + 4,
          width: Math.max(70, Math.min(220, box.width - 40)),
          hidden:
            !!column && (box.top < column.top || box.bottom > column.bottom),
        };
      }
      setPoints(next);
    }
    const schedule = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(locate);
    };
    locate();
    const observer = new ResizeObserver(schedule);
    observer.observe(area);
    for (const node of targets.current.values()) observer.observe(node);
    viewport.addEventListener("scroll", schedule, true);
    window.addEventListener("resize", schedule);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      viewport.removeEventListener("scroll", schedule, true);
      window.removeEventListener("resize", schedule);
    };
  }, [data, now, error]);

  // Preserve each card's old rectangle across column changes and move the real card.
  useLayoutEffect(() => {
    const previous = taskRects.current;
    const next = new Map<string, DOMRect>();
    for (const [key, node] of targets.current) {
      if (!key.startsWith("task:")) continue;
      const box = node.getBoundingClientRect();
      next.set(key, box);
      const old = previous.get(key);
      if (
        old &&
        old.x !== box.x &&
        !matchMedia("(prefers-reduced-motion: reduce)").matches
      ) {
        node.animate(
          [
            {
              transform: `translate(${old.x - box.x}px,${old.y - box.y}px)`,
              zIndex: 4,
            },
            { transform: "none", zIndex: 4 },
          ],
          { duration: 650, easing: "cubic-bezier(.16,1,.3,1)" },
        );
      }
    }
    taskRects.current = next;
  }, [data]);
  const followActor = data?.actors.find((a) => a.id === following);
  const followKey = followActor
    ? `${followActor.target.kind}:${followActor.target.id}`
    : "";
  useEffect(() => {
    if (!following || !followKey) return;
    const element =
      targets.current.get(followKey) ||
      targets.current.get("rest:" + following);
    const berth =
      element &&
      Array.from(element.querySelectorAll<HTMLElement>("[data-berth]")).find(
        (node) => node.dataset.berth === following,
      );
    const target = berth || element;
    function accompany() {
      target?.scrollIntoView({
        block: "nearest",
        inline: "nearest",
        behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "instant"
          : "smooth",
      });
    }
    accompany();
    // New observed text and folded notes can change the destination after arrival.
    const observer = new ResizeObserver(accompany);
    if (content.current) observer.observe(content.current);
    if (element) observer.observe(element);
    return () => observer.disconnect();
  }, [following, followKey, followActor?.target.at]);

  const actor = data?.actors.find((a) => a.id === selected);
  const fresh = (at: number) => at > firstSnapshot.current;
  const groups = new Map<string, FileRow[]>();
  for (const file of [...(data?.files || [])].sort((a, b) => b.at - a.at)) {
    const repo = (file.repo || tr("Sin repositorio observado", "No observed repository")).toLocaleLowerCase();
    const rows = groups.get(repo) || [];
    rows.push(file);
    groups.set(repo, rows);
  }
  async function openTask(task: Task) {
    try {
      const detail = await api(`/api/agora/tasks/${task.id}`);
      const thread = await api(`/api/agora/threads/${detail.task.thread_id}`);
      setTaskDetail({ task: detail.task, messages: thread.messages || [] });
    } catch (failure) {
      setError((failure as Error).message);
    }
  }
  return (
    <main
      className="aglive"
      aria-label={tr("Ágora en directo", "Live Agora")}
      data-testid="agora-live"
      style={hostTheme}
    >
      <header className="aglive-head">
        {onClose && (
          <button
            className="icon-button"
            onClick={onClose}
            aria-label={tr("Volver a la conversación", "Back to conversation")}
          >
            <ArrowLeft size={18} />
          </button>
        )}
        <div>
          <h1>{tr("En directo", "Live")}</h1>
          <span>{spaceTitle(spaceId, data?.space.title)}</span>
        </div>
        <span className="aglive-connection" role="status">
          {error
            ? tr("Conexión interrumpida", "Connection interrupted")
            : data
              ? tr(`Actualizado ${clock(data.now)}`, `Updated ${clock(data.now)}`)
              : tr("Conectando…", "Connecting…")}
        </span>
        <button
          className="icon-button"
          onClick={() => setRetry((n) => n + 1)}
          aria-label={tr("Volver a conectar", "Reconnect")}
        >
          <RefreshCw size={16} />
        </button>
      </header>
      {error && (
        <p className="aglive-error" role="alert">
          {error}. {tr("Los datos visibles corresponden a la última lectura.", "Showing the last available data.")}
        </p>
      )}
      {data?.watch_error && (
        <p className="aglive-error" role="status">
          {tr("Observador sin datos", "Observer data unavailable")} · {data.watch_error}
        </p>
      )}
      <div className="aglive-team" aria-label={tr("Agentes en directo", "Live agents")}>
        {data?.actors.map((a) => (
          <div key={a.id}>
            <button
              key={a.id}
              className="aglive-agent"
              aria-pressed={selected === a.id}
              onClick={() => setSelected(a.id)}
              style={{ "--actor-color": a.color } as CSSProperties}
            >
              {a.loop ? <LoopRing loop={a.loop} color={a.color} size="sm" live={!error} /> : <i />}
              {a.name}
              <small>
                {error || now - a.last_signal >= 60
                  ? states.idle
                  : states[a.state] || a.state}
              </small>
            </button>
          </div>
        ))}
        {data && !data.actors.length && (
          <p>{tr("Ningún agente activo en este servidor.", "No active agents in this server.")}</p>
        )}
      </div>
      {data?.actors.some((a) => a.loop) && (
        <div className="agloop-strip" aria-label={tr("Bucle de cada agente", "Agent loops")}>
          {data.actors
            .filter((a) => a.loop)
            .map((a) => (
              <button
                key={a.id}
                className="agloop-card"
                aria-pressed={selected === a.id}
                onClick={() => setSelected(a.id)}
                style={{ "--actor-color": a.color } as CSSProperties}
              >
                <LoopRing loop={a.loop!} color={a.color} live={!error && now - a.last_signal < 60} />
                <strong>{a.name}</strong>
                <small>{loopFacts(a.loop!, now)}</small>
              </button>
            ))}
        </div>
      )}
      <div className="aglive-stage" ref={stage}>
        <div className="aglive-content" ref={content}>
          <div className="aglive-resting-area">
            {data?.actors
              .filter((a) => a.target.kind === "rest")
              .map((a) => (
                <div key={a.id} ref={bind("rest:" + a.id)}>
                  {berths("rest:" + a.id)}
                </div>
              ))}
          </div>
          <section className="aglive-board" aria-label={tr("Pizarra", "Board")}>
            <h2>{tr("Pizarra", "Board")}</h2>
            <div className="aglive-columns">
              {columns.map((col) => (
                <section className="aglive-column" key={col.id}>
                  <h3>
                    {col.label}
                    <span>
                      {data?.tasks.filter((t) =>
                        col.states.includes(t.status || t.state),
                      ).length || 0}
                    </span>
                  </h3>
                  <div>
                    {data?.tasks
                      .filter((t) => col.states.includes(t.status || t.state))
                      .map((t) => (
                        <div
                          className="aglive-target"
                          key={t.id}
                          ref={bind("task:" + t.id)}
                        >
                          <button
                            className="aglive-task"
                            key={t.id}
                            onClick={() => void openTask(t)}
                          >
                            <span>
                              #{t.id} · {t.repo || tr("Equipo", "Team")}
                            </span>
                            <strong>{t.title}</strong>
                            <small>
                              {t.owner || tr("Sin asignar", "Unassigned")}
                              {t.status === "blocked" ? tr(" · Bloqueada", " · Blocked") : ""}
                            </small>
                            {t.progress && <small>{t.progress}</small>}
                            {t.loop && (
                              <span className="agloop-task-loop">
                                <LoopRing loop={t.loop} size="sm" live={!error} />
                                {loopBrief(t.loop)}
                              </span>
                            )}
                            {t.loop && <TurnSteps loop={t.loop} now={now} />}
                          </button>
                          {berths("task:" + t.id)}
                        </div>
                      ))}
                  </div>
                </section>
              ))}
            </div>
          </section>
          <div className="aglive-lower">
            <section className="aglive-files" aria-label={tr("Ficheros en uso", "Files in use")}>
              <h2>
                <FileCode2 size={17} />
                {tr("Ficheros en uso", "Files in use")}
              </h2>
              {!groups.size && (
                <p className="aglive-empty">
                  {tr("Sin bloqueos de rutas ni herramientas observadas.", "No path locks or observed tools.")}
                </p>
              )}
              {Array.from(groups, ([repo, files]) => (
                <div className="aglive-repo" key={repo}>
                  <h3>{repo}</h3>
                  {files.map((file) => (
                    <div
                      className="aglive-target"
                      key={file.id}
                      ref={bind("file:" + file.id)}
                    >
                      <article className="aglive-file" key={file.id}>
                        <div>
                          <strong>{file.path}</strong>
                          <span>
                            {file.source === "lock"
                              ? tr("Bloqueo", "Lock")
                              : file.source === "command"
                                ? tr("Comando observado", "Observed command")
                                : tr("Fichero observado", "Observed file")}
                          </span>
                        </div>
                        <small>
                          {file.owners.join(", ")} · {clock(file.at)}
                        </small>
                        {file.preview && (
                          <pre>
                            <ObservedText
                              key={file.at + file.preview}
                              text={file.preview}
                              fresh={fresh(file.at)}
                            />
                          </pre>
                        )}
                      </article>
                      {berths("file:" + file.id)}
                    </div>
                  ))}
                </div>
              ))}
            </section>
            <section className="aglive-channel" aria-label={tr("Canal", "Channel")}>
              <header>
                <h2>
                  <MessageSquare size={17} />
                  {tr("Canal", "Channel")}
                </h2>
                <select
                  aria-label={tr("Canal en directo", "Live channel")}
                  value={channel}
                  onChange={(e) => setChannel(e.target.value)}
                >
                  {channels.length ? (
                    channels
                      .filter((c: any) => !c.archived)
                      .map((c) => (
                        <option key={c.slug} value={c.slug}>
                          #{c.title || c.slug}
                        </option>
                      ))
                  ) : (
                    <option value="general">#general</option>
                  )}
                </select>
              </header>
              {data?.messages.length === 0 && (
                <p className="aglive-empty">{tr("Sin mensajes en este canal.", "No messages in this channel.")}</p>
              )}
              {[...(data?.messages || [])].reverse().map((m) => (
                <div
                  className="aglive-target"
                  key={m.id}
                  ref={bind("message:" + m.id)}
                >
                  <article className="aglive-message" key={m.id}>
                    <div>
                      <strong>{m.author || m.agent}</strong>
                      <time>{clock(m.created || 0)}</time>
                    </div>
                    <p>
                      <ObservedText
                        text={messageBody(m)}
                        fresh={fresh(m.created || 0)}
                      />
                    </p>
                  </article>
                  {berths("message:" + m.id)}
                </div>
              ))}
            </section>
          </div>
          <div className="aglive-cursors">
            {data?.actors.map(
              (a) =>
                points[a.id] && (
                  <AgentCursor
                    key={a.id}
                    actor={a}
                    point={points[a.id]}
                    resting={!!error || now - a.last_signal >= 60}
                    noteVisible={noteVisible(a)}
                    onSelect={() => setSelected(a.id)}
                  />
                ),
            )}
          </div>
        </div>
      </div>
      {actor && (
        <aside
          className="aglive-detail"
          aria-label={tr(`Detalle de ${actor.name}`, `${actor.name} details`)}
        >
          <header>
            <h2>{actor.name}</h2>
            <button
              className="icon-button"
              onClick={() => setSelected("")}
              aria-label={tr("Cerrar detalle", "Close details")}
            >
              <X size={17} />
            </button>
          </header>
          <p>
            {states[actor.state] || actor.state} · {clock(actor.last_signal)}
          </p>
          <button
            className="aglive-follow"
            aria-pressed={following === actor.id}
            onClick={() => setFollowing(following === actor.id ? "" : actor.id)}
          >
            <Crosshair size={16} />
            {following === actor.id
              ? tr("Dejar de seguir", "Unfollow")
              : tr(`Seguir a ${actor.name}`, `Follow ${actor.name}`)}
          </button>
          {actor.session ? (
            <>
              <h3>{tr("Sesión observada", "Observed session")}</h3>
              <p>{actor.session.title}</p>
              <p>
                {actor.session.engine} ·{" "}
                {actor.session.binding === "explicit"
                  ? tr("Identidad asignada", "Assigned identity")
                  : tr("Identidad inferida", "Inferred identity")}
              </p>
              <code>{actor.session.key}</code>
              <p>{actor.session.cwd}</p>
              {(actor.session.tool || actor.session.last_action) && (
                <pre>
                  {(actor.session.tool || actor.session.last_action)?.input}
                </pre>
              )}
              <p>{actor.session.last_text}</p>
            </>
          ) : (
            <p>{tr("Sin sesión observada. La actividad procede del Ágora.", "No observed session. Activity comes from Agora.")}</p>
          )}
          {actor.loop && (
            <>
              <h3>{tr("Bucle del agente", "Agent loop")}</h3>
              <LoopRing loop={actor.loop} color={actor.color} size="lg" name={actor.name} live={!error && now - actor.last_signal < 60} />
              <p>{loopFacts(actor.loop, now)}</p>
              <LoopPanel loop={actor.loop} />
            </>
          )}
          {data?.tasks
            .filter((t) => t.owner === actor.id)
            .map((t) => (
              <button
                key={t.id}
                className="aglive-detail-task"
                onClick={() => void openTask(t)}
              >
                #{t.id} {t.title}
              </button>
            ))}
        </aside>
      )}
      {taskDetail && (
        <aside
          className="aglive-detail aglive-task-detail"
          aria-label={tr("Detalle de tarea", "Task details")}
        >
          <header>
            <h2>#{taskDetail.task.id}</h2>
            <button
              className="icon-button"
              aria-label={tr("Cerrar tarea", "Close task")}
              onClick={() => setTaskDetail(null)}
            >
              <X size={17} />
            </button>
          </header>
          <h3>{taskDetail.task.title}</h3>
          {!!taskDetail.task.commits?.length && (
            <DeliveryDiff
              key={taskDetail.task.id}
              taskId={taskDetail.task.id}
              version={data?.tasks.find((task) => task.id === taskDetail.task.id)?.updated ??
                taskDetail.task.submission_revision}
            />
          )}
          {(() => {
            const owner = data?.actors.find((a) => a.id === taskDetail.task.owner);
            return owner?.loop ? (
              <section aria-label={tr(`Bucle de ${owner.name}`, `${owner.name} loop`)}>
                <LoopRing loop={owner.loop} color={owner.color} size="lg" name={owner.name} live={!error && now - owner.last_signal < 60} />
                <p>
                  {owner.name} · {loopFacts(owner.loop, now)}
                </p>
                <LoopPanel loop={owner.loop} />
              </section>
            ) : null;
          })()}
          {taskDetail.messages.map((m) => (
            <article key={m.id}>
              <strong>{m.author}</strong>
              <p>{m.body}</p>
            </article>
          ))}
        </aside>
      )}
    </main>
  );
}
