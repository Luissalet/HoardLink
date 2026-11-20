type Api = (path: string) => Promise<unknown>;
type Preview = {path: string; title: string};
type Artifact = {title: string; media: string; path: string | null; image: Preview | null; url: string | null};

export type PendingDecision = {
  key: string;
  title: string;
  order: string;
  space: string;
  image: Preview | null;
  artifacts?: Artifact[];
  variants?: {id: number; title: string; image: Preview | null; artifacts?: Artifact[]; signature: string}[];
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
