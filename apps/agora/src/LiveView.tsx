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
  tasks: (Task & { progress?: string })[];
  messages: Message[];
  files: FileRow[];
  actors: Actor[];
  watch_error?: string;
};
type CursorPoint = { x: number; y: number; width: number; hidden: boolean };
const columns = [
  { id: "open", label: "Por hacer", states: ["open"] },
  {
    id: "working",
    label: "En curso",
    states: ["claimed", "in_progress", "changes", "blocked"],
  },
  { id: "review", label: "En revisión", states: ["review", "approved"] },
  { id: "done", label: "Finalizado", states: ["done", "dropped"] },
];
const states: Record<string, string> = {
  working: "Trabajando",
  tool: "Herramienta",
  waiting: "Esperando",
  stale: "Sin señal reciente",
  idle: "En reposo",
};
const clock = (value: number) =>
  new Date(value * 1000).toLocaleTimeString("es-ES", {
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
      aria-label={`Sesión de ${actor.name}`}
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
    task: Task;
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
    const repo = (file.repo || "Sin repositorio observado").toLocaleLowerCase();
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
      aria-label="Ágora en directo"
      data-testid="agora-live"
      style={hostTheme}
    >
      <header className="aglive-head">
        {onClose && (
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="Volver a la conversación"
          >
            <ArrowLeft size={18} />
          </button>
        )}
        <div>
          <h1>En directo</h1>
          <span>{data?.space.title || spaceId}</span>
        </div>
        <span className="aglive-connection" role="status">
          {error
            ? "Conexión interrumpida"
            : data
              ? `Actualizado ${clock(data.now)}`
              : "Conectando…"}
        </span>
        <button
          className="icon-button"
          onClick={() => setRetry((n) => n + 1)}
          aria-label="Volver a conectar"
        >
          <RefreshCw size={16} />
        </button>
      </header>
      {error && (
        <p className="aglive-error" role="alert">
          {error}. Los datos visibles corresponden a la última lectura.
        </p>
      )}
      {data?.watch_error && (
        <p className="aglive-error" role="status">
          Observador sin datos · {data.watch_error}
        </p>
      )}
      <div className="aglive-team" aria-label="Agentes en directo">
        {data?.actors.map((a) => (
          <div key={a.id}>
            <button
              key={a.id}
              className="aglive-agent"
              aria-pressed={selected === a.id}
              onClick={() => setSelected(a.id)}
              style={{ "--actor-color": a.color } as CSSProperties}
            >
              <i />
              {a.name}
              <small>
                {error || now - a.last_signal >= 60
                  ? "En reposo"
                  : states[a.state] || a.state}
              </small>
            </button>
          </div>
        ))}
        {data && !data.actors.length && (
          <p>Ningún agente activo en este servidor.</p>
        )}
      </div>
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
          <section className="aglive-board" aria-label="Pizarra">
            <h2>Pizarra</h2>
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
                              #{t.id} · {t.repo || "Equipo"}
                            </span>
                            <strong>{t.title}</strong>
                            <small>
                              {t.owner || "Sin asignar"}
                              {t.status === "blocked" ? " · Bloqueada" : ""}
                            </small>
                            {t.progress && <small>{t.progress}</small>}
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
            <section className="aglive-files" aria-label="Ficheros en uso">
              <h2>
                <FileCode2 size={17} />
                Ficheros en uso
              </h2>
              {!groups.size && (
                <p className="aglive-empty">
                  Sin bloqueos de rutas ni herramientas observadas.
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
                              ? "Bloqueo"
                              : file.source === "command"
                                ? "Comando observado"
                                : "Fichero observado"}
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
            <section className="aglive-channel" aria-label="Canal">
              <header>
                <h2>
                  <MessageSquare size={17} />
                  Canal
                </h2>
                <select
                  aria-label="Canal en directo"
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
                <p className="aglive-empty">Sin mensajes en este canal.</p>
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
                        text={m.body}
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
          aria-label={`Detalle de ${actor.name}`}
        >
          <header>
            <h2>{actor.name}</h2>
            <button
              className="icon-button"
              onClick={() => setSelected("")}
              aria-label="Cerrar detalle"
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
              ? "Dejar de seguir"
              : "Seguir a " + actor.name}
          </button>
          {actor.session ? (
            <>
              <h3>Sesión observada</h3>
              <p>{actor.session.title}</p>
              <p>
                {actor.session.engine} ·{" "}
                {actor.session.binding === "explicit"
                  ? "Identidad asignada"
                  : "Identidad inferida"}
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
            <p>Sin sesión observada. La actividad procede del Ágora.</p>
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
          aria-label="Detalle de tarea"
        >
          <header>
            <h2>#{taskDetail.task.id}</h2>
            <button
              className="icon-button"
              aria-label="Cerrar tarea"
              onClick={() => setTaskDetail(null)}
            >
              <X size={17} />
            </button>
          </header>
          <h3>{taskDetail.task.title}</h3>
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
