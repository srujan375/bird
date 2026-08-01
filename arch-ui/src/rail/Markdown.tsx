/**
 * The parse tree from ./md, as elements.
 *
 * Everything here is inert: no HTML is ever passed through, so the worst a
 * model can write is text that looks like markup. The one thing that leaves
 * the page is a link, and its scheme is checked first.
 */
import { Fragment, memo, useMemo, type ReactNode } from "react";
import { parseMarkdown, safeHref, type Block, type Inline } from "./md";

function inlines(kids: Inline[]): ReactNode {
  return kids.map((n, i) => {
    switch (n.t) {
      case "text": return <Fragment key={i}>{n.v}</Fragment>;
      case "code": return <code key={i}>{n.v}</code>;
      case "strong": return <strong key={i}>{inlines(n.kids)}</strong>;
      case "em": return <em key={i}>{inlines(n.kids)}</em>;
      case "del": return <del key={i}>{inlines(n.kids)}</del>;
      case "link": {
        const href = safeHref(n.href);
        if (!href) return <Fragment key={i}>{inlines(n.kids)}</Fragment>;
        return (
          <a key={i} href={href} target="_blank" rel="noreferrer noopener">
            {inlines(n.kids)}
          </a>
        );
      }
    }
  });
}

function block(b: Block, key: number, trailing?: ReactNode): ReactNode {
  switch (b.t) {
    case "p":
      return <p key={key}>{inlines(b.kids)}{trailing}</p>;
    case "h": {
      const Tag = (`h${Math.min(6, b.level + 2)}`) as "h3"; // a rail heading is never a page heading
      return <Tag key={key} data-level={b.level}>{inlines(b.kids)}{trailing}</Tag>;
    }
    case "code":
      return (
        <pre key={key} data-lang={b.lang || undefined}>
          <code>{b.code}</code>
        </pre>
      );
    case "list": {
      const items = b.items.map((blocks, i) => (
        <li key={i}>{blocks.map((child, j) => block(child, j))}</li>
      ));
      return b.ordered
        ? <ol key={key} start={b.start}>{items}</ol>
        : <ul key={key}>{items}</ul>;
    }
    case "quote":
      return <blockquote key={key}>{b.kids.map((child, i) => block(child, i))}</blockquote>;
    case "table":
      return (
        <div key={key} className="md-table">
          <table>
            <thead>
              <tr>{b.head.map((cell, i) => <th key={i}>{inlines(cell)}</th>)}</tr>
            </thead>
            <tbody>
              {b.rows.map((row, i) => (
                <tr key={i}>{row.map((cell, j) => <td key={j}>{inlines(cell)}</td>)}</tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    case "hr":
      return <hr key={key} />;
  }
}

/**
 * `trailing` rides inside the last paragraph rather than after the block, so
 * the streaming cursor sits at the end of the sentence being written instead
 * of dropping onto a line of its own.
 */
export const Markdown = memo(function Markdown({
  text,
  className = "md",
  style,
  trailing,
}: {
  text: string;
  className?: string;
  style?: React.CSSProperties;
  trailing?: ReactNode;
}) {
  const blocks = useMemo(() => parseMarkdown(text), [text]);
  const lastIsText = blocks.length > 0 && (blocks[blocks.length - 1].t === "p" || blocks[blocks.length - 1].t === "h");

  return (
    <div className={className} style={style}>
      {blocks.map((b, i) => block(b, i, trailing && lastIsText && i === blocks.length - 1 ? trailing : undefined))}
      {trailing && !lastIsText ? trailing : null}
    </div>
  );
});
