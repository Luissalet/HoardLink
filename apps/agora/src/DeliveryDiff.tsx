import {useEffect, useState} from 'react';
import {api, lang} from './api';
import './delivery-diff.css';

type DiffLine = {t: 'ctx' | 'add' | 'del'; old: number | null; new: number | null; text: string};
type DiffFile = {
  path: string;
  old_path: string | null;
  status: 'added' | 'deleted' | 'modified' | 'renamed';
  additions: number;
  deletions: number;
  binary: boolean;
  truncated: boolean;
  hunks: {header: string; lines: DiffLine[]}[];
};
type Commit = {sha: string; short: string; repo: string | null; repository_candidates?: string[];
  subject?: string; error?: string; files: DiffFile[]};
type Diff = {submission_revision: number; commits: Commit[]};

const copy = {
  es: {
    title: 'Diff de la entrega', loading: 'Leyendo la entrega…', full: 'Mostrar los cambios',
    retry: 'Reintentar', unresolved: 'Repositorio por identificar', noFiles: 'Este commit no cambia ficheros.',
    unavailable: 'El diff no está disponible. Comprueba el repositorio y los hashes de la entrega.',
    commitUnavailable: 'No se pudo leer este commit.', binary: 'Fichero binario', truncated: 'Diff recortado.',
    added: 'Nuevo', deleted: 'Borrado', modified: 'Modificado', renamed: 'Renombrado',
    before: 'Línea anterior', after: 'Línea nueva', code: 'Código', previousPath: 'Antes',
  },
  en: {
    title: 'Delivery diff', loading: 'Reading the delivery…', full: 'Show changes',
    retry: 'Retry', unresolved: 'Repository to identify', noFiles: 'This commit changes no files.',
    unavailable: 'The diff is unavailable. Check the repository and commit hashes in the delivery.',
    commitUnavailable: 'This commit could not be read.', binary: 'Binary file', truncated: 'Diff truncated.',
    added: 'Added', deleted: 'Deleted', modified: 'Modified', renamed: 'Renamed',
    before: 'Old line', after: 'New line', code: 'Code', previousPath: 'Previously',
  },
}[lang];

const marks = {ctx: ' ', add: '+', del: '−'};

export function DeliveryDiff({taskId, version}: {taskId: number; version?: number}) {
  const [open, setOpen] = useState(false);
  const [full, setFull] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [diff, setDiff] = useState<Diff>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    let current = true;
    setLoading(true);
    setError('');
    api<Diff>(`/api/agora/review_diff?task_id=${taskId}${full ? '' : '&summary_only=1'}`)
      .then(result => { if (current) setDiff(result); })
      .catch(reason => {
        if (current) {
          setDiff(undefined);
          setError(reason instanceof Error ? reason.message : copy.unavailable);
        }
      })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [taskId, version, open, full, attempt]);

  const repositories = new Map<string | null, Commit[]>();
  for (const commit of diff?.commits ?? []) {
    const repository = commit.repository_candidates?.join(' / ') || commit.repo;
    const group = repositories.get(repository) ?? [];
    group.push(commit);
    repositories.set(repository, group);
  }

  return <details className="delivery-diff" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{copy.title}{diff && ` · r${diff.submission_revision}`}</summary>
    {loading && <p role="status">{copy.loading}</p>}
    {error && <div>
      <p role="alert" title={error}>{copy.unavailable}</p>
      <button type="button" onClick={() => setAttempt(value => value + 1)}>{copy.retry}</button>
    </div>}
    {diff && !loading && <>
      {!full && <button type="button" onClick={() => setFull(true)}>{copy.full}</button>}
      {Array.from(repositories, ([repo, commits]) => <section key={repo ?? ''}>
        <h3>{repo || copy.unresolved}</h3>
        {commits.map(commit => <article key={commit.sha}>
          <h4><code title={commit.sha}>{commit.short}</code> {commit.subject}</h4>
          {commit.error ? <p title={commit.error}>{copy.commitUnavailable}</p> :
            commit.files.length === 0 ? <p>{copy.noFiles}</p> :
              <ul>{commit.files.map(file => <li key={file.path}>
                {full ? <details>
                  <summary><FileSummary file={file}/></summary>
                  {file.binary && <p>{copy.binary}</p>}
                  {file.hunks.map((hunk, index) => <table key={index} aria-label={file.path}>
                    <caption>{hunk.header}</caption>
                    <thead className="delivery-diff-labels"><tr>
                      <th scope="col" aria-label={copy.before} title={copy.before}>−</th>
                      <th scope="col" aria-label={copy.after} title={copy.after}>+</th>
                      <th scope="col" aria-label={copy.code}/>
                    </tr></thead>
                    <tbody>{hunk.lines.map((line, position) => <tr className={line.t} key={position}>
                      <td>{line.old}</td><td>{line.new}</td><td><code>{marks[line.t]}{line.text}</code></td>
                    </tr>)}</tbody>
                  </table>)}
                  {file.truncated && <p>{copy.truncated}</p>}
                </details> : <FileSummary file={file}/>}
              </li>)}</ul>}
        </article>)}
      </section>)}
    </>}
  </details>;
}

function FileSummary({file}: {file: DiffFile}) {
  return <><code>{file.path}</code> <small>{copy[file.status] ?? file.status} · +{file.additions} −{file.deletions}
    {file.old_path && ` · ${copy.previousPath} ${file.old_path}`}{file.binary && ` · ${copy.binary}`}</small></>;
}
