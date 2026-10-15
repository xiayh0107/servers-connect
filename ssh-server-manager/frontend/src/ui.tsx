import {useEffect, useId, useRef, type ButtonHTMLAttributes, type ReactNode} from 'react';

export function Button({variant = 'secondary', className = '', ...props}: ButtonHTMLAttributes<HTMLButtonElement> & {variant?: 'primary'|'secondary'|'danger'|'ghost'}) {
  return <button type="button" className={`button ${variant} ${className}`} {...props}/>;
}
const paths: Record<string, ReactNode> = {
  hosts: <><rect x="3" y="4" width="18" height="7" rx="1.5"/><rect x="3" y="13" width="18" height="7" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/></>,
  terminal: <><path d="M5 8l4 4-4 4M12 16h7"/></>,
  files: <path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>,
  folder: <path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/>,
  file: <><path d="M5 3h10l4 4v14H5zM15 3v5h4M9 12h6M9 16h4"/></>,
  tags: <><path d="M3 3h10l8 8-10 10-8-8z"/><circle cx="8" cy="8" r="1"/></>,
  skills: <><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/></>,
  credentials: <><circle cx="8" cy="9" r="5"/><path d="M12 13l8 8M16 17l3-3M18 19l3-3"/></>,
  search: <><circle cx="10" cy="10" r="6"/><path d="M15 15l6 6"/></>,
  plus: <path d="M12 5v14M5 12h14"/>,
  close: <path d="M6 6l12 12M6 18L18 6"/>,
  check: <path d="M3 12h4l3-7 5 14 3-7h3"/>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 6a8 8 0 0114 6M18 18a8 8 0 01-14-6"/></>,
  more: <><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></>,
  arrow: <path d="M9 5l7 7-7 7"/>,
  up: <path d="M5 12l7-7 7 7M12 5v15"/>,
  download: <path d="M12 3v12M7 10l5 5 5-5M4 17v4h16v-4"/>,
  upload: <path d="M12 16V4M7 9l5-5 5 5M4 17v4h16v-4"/>,
  link: <><path d="M10 14l4-4M8 16l-2 2a4 4 0 01-6-6l4-4M16 8l2-2a4 4 0 016 6l-4 4"/></>,
  edit: <path d="M4 20h4L20 8l-4-4L4 16zM14 6l4 4"/>,
  copy: <><rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/></>,
  lock: <><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 018 0v3"/></>,
  globe: <><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a17 17 0 010 18M12 3a17 17 0 000 18"/></>,
};
export function Icon({name, size = 16}: {name: string; size?: number}) {
  return <svg aria-hidden="true" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">{paths[name] || paths.file}</svg>;
}
export function Modal({title, onClose, children}: {title: string; onClose: () => void; children: ReactNode}) {
  const ref = useRef<HTMLDivElement>(null), id = useId(), close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const node = ref.current!;
    const focusables = () => [...node.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),a[href],[tabindex="0"]')];
    (node.querySelector<HTMLElement>('[data-initial-focus]') || focusables()[0] || node).focus();
    const key = (event: KeyboardEvent) => {
      const dialogs=document.querySelectorAll('[role="dialog"]');
      if(dialogs[dialogs.length-1]!==node)return;
      if (event.key === 'Escape') {event.preventDefault(); close.current();}
      if (event.key === 'Tab') {
        const nodes = focusables(), first = nodes[0], last = nodes.at(-1);
        if (!first) {event.preventDefault(); node.focus();}
        else if (event.shiftKey && (document.activeElement === first || document.activeElement === node)) {event.preventDefault(); last?.focus();}
        else if (!event.shiftKey && document.activeElement === last) {event.preventDefault(); first.focus();}
      }
    };
    document.addEventListener('keydown', key);
    return () => {document.removeEventListener('keydown', key); if (previous?.isConnected) previous.focus();};
  }, []);
  return <div className="modal-backdrop" onMouseDown={event => {if (event.target === event.currentTarget) onClose();}}><div role="dialog" aria-modal="true" aria-labelledby={id} ref={ref} tabIndex={-1} className="modal"><header><h2 id={id}>{title}</h2><Button variant="ghost" aria-label="Close" onClick={onClose}><Icon name="close"/></Button></header>{children}</div></div>;
}
export function ErrorNotice({error}: {error: unknown}) {return error ? <div role="alert" className="notice danger">{error instanceof Error ? error.message : String(error)}</div> : null;}
export function EmptyState({title, children}: {title: string; children?: ReactNode}) {return <div className="empty-state"><Icon name="hosts" size={30}/><h2>{title}</h2>{children}</div>;}
export function PageHeader({title, description, children}: {title: string; description?: string; children?: ReactNode}) {return <header className="page-header"><div><h1>{title}</h1>{description && <p>{description}</p>}</div><div className="actions">{children}</div></header>;}
export function Field({label, children}: {label: string; children: ReactNode}) {return <label className="field"><span>{label}</span>{children}</label>;}
export function Badge({children, tone = ''}: {children: ReactNode; tone?: string}) {return <span className={`badge ${tone}`}>{children}</span>;}
export const formatDate = (value?: string | null) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : '—';
