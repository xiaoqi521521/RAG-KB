import type { SourceCitation } from '@/types';

interface SourceArchiveCardProps {
  source: SourceCitation;
  active?: boolean;
  onClick?: () => void;
}

export default function SourceArchiveCard({ source, active, onClick }: SourceArchiveCardProps) {
  const location =
    source.page_number != null
      ? `第 ${source.page_number} 页`
      : source.section_title || '未标注位置';
  const scorePercent = Math.min(100, Math.max(0, Math.round(source.score * 100)));

  return (
    <div
      className={`archive-card hoverable p-3.5 cursor-pointer ${active ? 'border-seal' : ''}`}
      style={active ? { borderColor: 'var(--seal)' } : undefined}
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onClick?.();
        }
      }}
    >
      <div className="flex items-center justify-between gap-2 min-w-0">
        <div className="flex items-center gap-2 min-w-0">
          <span className="font-data text-[11px] font-semibold px-1.5 py-0.5 rounded-[2px] border border-seal bg-seal-wash text-seal shrink-0">
            {source.reference_index}
          </span>
          <span className="text-[13px] font-medium truncate" title={source.document_name}>
            {source.document_name}
          </span>
        </div>
        <span className="font-data text-[11px] text-faint shrink-0">#{source.chunk_id}</span>
      </div>

      <div className="eyebrow mt-2 !tracking-normal">
        KB{source.kb_id} · DOC{source.document_id} · CHUNK {source.chunk_index}
      </div>

      <p className="text-[12.5px] text-soft mt-2 leading-relaxed line-clamp-3">{source.excerpt}</p>

      <div className="flex items-center gap-2.5 mt-2.5">
        <span className="font-data text-[11px] text-faint shrink-0">{location}</span>
        <div className="score-track flex-1">
          <div className="score-fill" style={{ width: `${scorePercent}%` }} />
        </div>
        <span className="font-data text-[11px] text-pine shrink-0">{scorePercent}%</span>
      </div>
    </div>
  );
}
