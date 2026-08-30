import { useMemo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';

const CITE_PATTERN = /\[参考(\d{1,2})\]/g;

interface CitationTextProps {
  content: string;
  onCite?: (index: number) => void;
  highlight?: number | null;
}

/** 将回答中的 [参考N] 标记转换为受控 Markdown 链接，再渲染为引用印章。 */
function transformCitations(text: string): string {
  return text.replace(
    CITE_PATTERN,
    (_match, index: string) =>
      `[${index}](#cite-${index})`,
  );
}

export default function CitationText({ content, onCite, highlight }: CitationTextProps) {
  const transformed = useMemo(() => transformCitations(content), [content]);

  return (
    <div className="markdown-body text-[14px]">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            const citeMatch = /^#cite-(\d+)$/.exec(href || '');
            if (!citeMatch) {
              return <a href={href}>{children}</a>;
            }
            const index = Number(citeMatch[1]);
            return (
              <span
                className={highlight === index ? 'cite-seal active' : 'cite-seal'}
                role="button"
                tabIndex={0}
                aria-label={`查看参考来源 ${index}`}
                onClick={() => onCite?.(index)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    onCite?.(index);
                  }
                }}
              >
                {index}
              </span>
            );
          },
          code({ className, children, ...props }) {
            const match = /language-(\w+)/.exec(className || '');
            if (!match) {
              return (
                <code className={className} {...props}>
                  {children}
                </code>
              );
            }
            return (
              <SyntaxHighlighter style={oneDark} language={match[1]} PreTag="div">
                {String(children).replace(/\n$/, '')}
              </SyntaxHighlighter>
            );
          },
        }}
      >
        {transformed}
      </ReactMarkdown>
    </div>
  );
}
