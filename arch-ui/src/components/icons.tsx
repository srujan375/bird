/** The prototype's icons, verbatim. Stroke/fill live in board.css, so these
 *  carry geometry only. */

export const IconRename = () => (
  <svg viewBox="0 0 24 24"><path d="M4 20h4l10-10-4-4L4 16z" /><path d="M14.5 5.5l4 4" /></svg>
);
export const IconDeepen = () => (
  <svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h9" /></svg>
);
export const IconConnect = () => (
  <svg viewBox="0 0 24 24">
    <path d="M9.5 14.5l5-5" />
    <path d="M13 7l1.7-1.7a3.5 3.5 0 015 5L18 12" />
    <path d="M11 17l-1.7 1.7a3.5 3.5 0 01-5-5L6 12" />
  </svg>
);
export const IconDelete = () => (
  <svg viewBox="0 0 24 24"><path d="M5 7h14M10 7V5h4v2M8 7l1 12h6l1-12" /></svg>
);
export const IconSelect = () => (
  <svg viewBox="0 0 24 24"><path d="M5 3l14 8-6 1.6L10.5 19z" /></svg>
);
export const IconAddBox = () => (
  <svg viewBox="0 0 24 24"><rect x="3.5" y="6" width="17" height="12" rx="2.5" /><path d="M12 10v4M10 12h4" /></svg>
);
export const IconAddNote = () => (
  <svg viewBox="0 0 24 24"><path d="M4 5h16M4 10h11M4 15h16M4 20h7" /></svg>
);
export const IconMinus = () => <svg viewBox="0 0 24 24"><path d="M6 12h12" /></svg>;
export const IconPlus = () => <svg viewBox="0 0 24 24"><path d="M12 6v12M6 12h12" /></svg>;

export const IconChat = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <rect x="3.2" y="5.2" width="17.6" height="13.6" rx="2.6" />
    <path className="pane" d="M14.4 6.1h4.3c.8 0 1.4.6 1.4 1.4v9c0 .8-.6 1.4-1.4 1.4h-4.3z" />
  </svg>
);

export const IconClip = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M21.4 11.1l-9.2 9.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.9l8.5-8.4" />
  </svg>
);

export const IconWarn = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 8v5" /><path d="M12 16.5v.01" /><circle cx="12" cy="12" r="9" />
  </svg>
);

export const IconDoc = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5" />
  </svg>
);

export const IconX = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
);
