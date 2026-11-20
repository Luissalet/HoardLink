import type {PendingDecision} from './production_decisions_data.js';

export function renderDecisionCards(container: HTMLElement, rows: PendingDecision[], options: {
  lang: string;
  source(path: string, media: string): string | null | Promise<string | null>;
  orderHref?(row: PendingDecision): string | null;
  orderInPlace?: boolean;
  openOrder?(event: MouseEvent, row: PendingDecision): void;
  post(operation: string, body: Record<string, unknown>): Promise<unknown>;
  refresh(): void;
}): void;
