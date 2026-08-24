import { Fragment, type ReactNode } from "react";

/** The prototype's two bits of inline markup, `**bold**` and `` `code` ``.
 *  The original built an HTML string and escaped by hand; React escapes text
 *  nodes for us, so this returns elements instead. */

const TOKEN = /(\*\*[^*]+?\*\*|`[^`]+?`)/g;

export function richNodes(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  const parts = String(text).split(TOKEN);
  parts.forEach((part, i) => {
    if (!part) return;
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      out.push(<strong key={i}>{part.slice(2, -2)}</strong>);
    } else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      out.push(<code className="mono" key={i}>{part.slice(1, -1)}</code>);
    } else {
      out.push(<Fragment key={i}>{part}</Fragment>);
    }
  });
  return out;
}

export function Rich({ text }: { text: string }) {
  return <>{richNodes(text)}</>;
}

/** Same, but honouring the line breaks — a board turn is a list of gestures,
 *  one per line, and running them together reads as one long sentence. */
export function RichLines({ text }: { text: string }) {
  const lines = String(text).split("\n");
  return (
    <>
      {lines.map((l, i) => (
        <span key={i}>
          {i > 0 ? <br /> : null}
          {richNodes(l)}
        </span>
      ))}
    </>
  );
}
