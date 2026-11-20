type Api = (path: string) => Promise<unknown>;
type Preview = {path: string; title: string};

export type PendingDecision = {
  key: string;
  title: string;
  order: string;
  space: string;
  image: Preview | null;
  variants?: {id: number; title: string; image: Preview | null}[];
};
export type DecisionRecord = {
  id: number;
  created: number;
  title: string;
  order: string;
  variant?: string;
  decision: 'keep' | 'cancel';
  note: string;
  draft: boolean;
  decidedBy?: string;
  own: boolean;
};
export function loadPending(api: Api, space?: string): Promise<{rows: PendingDecision[]; incomplete: boolean}>;
export function loadHistory(api: Api, space?: string): Promise<DecisionRecord[]>;
export function decisionHeading(row: DecisionRecord, lang: 'es' | 'en'): string;
export function decisionUrl(row: PendingDecision): string;
