import { Bug, CircleDot, GitPullRequest, Loader2, Package, UserRound } from 'lucide-react';
import type { RecordItem } from './types';

export const STATUS: Record<string, [string, string]> = { review_passed: ['审查通过', 'green'], waiting: ['待处理', 'neutral'], queued: ['排队中', 'blue'], running: ['进行中', 'blue'], changes_requested: ['需要修改', 'orange'], failed: ['执行失败', 'red'], uncertain: ['结果待确认', 'orange'], inconclusive: ['无法确认', 'orange'], needs_configuration: ['等待配置', 'orange'], not_connected: ['未接入', 'neutral'], merged: ['已合并', 'purple'], closed: ['已关闭', 'neutral'], deleted: ['已删除', 'neutral'], success: ['已完成', 'green'], completed: ['已完成', 'green'], superseded: ['已被新提交替代', 'neutral'] };
export const KIND: Record<string, string> = { issue: 'Issue', bug: 'Bug', pr: 'Pull Request', release: 'Release' };
const ATTENTION = new Set(['failed', 'changes_requested', 'uncertain', 'needs_configuration', 'inconclusive']);
const DONE = new Set(['merged', 'closed', 'deleted', 'success']);
const COLUMNS: { id: BoardColumn; title: string }[] = [
  { id: 'waiting', title: '待处理' },
  { id: 'active', title: '进行中' },
  { id: 'attention', title: '需要关注' },
  { id: 'done', title: '已完成' },
];

type BoardColumn = 'waiting' | 'active' | 'attention' | 'done';

export function recordStatus(record: Pick<RecordItem, 'status' | 'details'>): string {
  return record.details?.deleted ? 'deleted' : record.status;
}

export function workType(record: Pick<RecordItem, 'kind' | 'work_type' | 'labels'>): string {
  if (record.work_type) return record.work_type;
  if (record.kind === 'issue' && (record.labels || []).some(label => label.toLowerCase() === 'bug' || label === '缺陷')) return 'bug';
  return record.kind;
}

export function boardColumn(record: RecordItem): BoardColumn {
  const status = recordStatus(record);
  if (ATTENTION.has(record.status)) return 'attention';
  if (DONE.has(status) || DONE.has(record.status)) return 'done';
  if (record.status === 'running' || (record.kind === 'pr' && record.stage === 'review' && ['queued', 'running'].includes(record.status))) return 'active';
  return 'waiting';
}

export function relative(value: string) {
  const minutes = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 60000));
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时前`;
  return `${Math.floor(minutes / 1440)} 天前`;
}
export function Stamp({ value }: { value: string }) { return <time title={new Date(value).toLocaleString('zh-CN')}>{relative(value)}</time>; }
export function Status({ value, small = false }: { value: string; small?: boolean }) {
  const [label, color] = STATUS[value] || [value, 'neutral'];
  return <span className={`status ${color} ${small ? 'small' : ''}`}>{value === 'running' ? <Loader2 size={12} className="spin" /> : <i />}{label}</span>;
}
export function Avatar({ name, size = '' }: { name: string; size?: string }) { const colors = ['mint', 'peach', 'lavender', 'sky']; return <span className={`avatar ${colors[(name.charCodeAt(0) || 0) % colors.length]} ${size}`} title={name}>{name.slice(0, 1).toUpperCase()}</span>; }
export function KindIcon({ kind, size = 17 }: { kind: string; size?: number }) {
  if (kind === 'pr') return <GitPullRequest size={size} />;
  if (kind === 'release') return <Package size={size} />;
  if (kind === 'bug') return <Bug size={size} />;
  return <CircleDot size={size} />;
}

export const PRIORITIES = ['紧急', '普通', '较低'] as const;
const PRIORITY_TONE: Record<string, [string, string]> = { 紧急: ['!!!', 'urgent'], 普通: ['=', 'normal'], 较低: ['−', 'low'] };
export function priorityTone(value: string) {
  return PRIORITY_TONE[value] || ['·', 'normal'];
}

function PriorityMark({ record }: { record: RecordItem }) {
  const type = workType(record);
  if (record.priority) {
    const [mark, tone] = priorityTone(record.priority);
    return <span className={`priority ${tone}`}><span>{mark}</span>{record.priority}</span>;
  }
  if (type === 'release') return null;
  return <span className="board-missing">未设优先级</span>;
}

function OwnerMark({ record }: { record: RecordItem }) {
  if (record.assignees.length) {
    return <span className="owner"><Avatar name={record.assignees[0]} />{record.assignees[0]}{record.assignees.length > 1 ? <small>+{record.assignees.length - 1}</small> : null}</span>;
  }
  if (workType(record) === 'release') return null;
  return <span className="unassigned board-missing"><UserRound size={14}/>待指派</span>;
}

export function RecordsBoard({ records, onSelect }: { records: RecordItem[]; onSelect: (id: string) => void }) {
  const grouped = Object.fromEntries(COLUMNS.map(column => [column.id, records.filter(record => boardColumn(record) === column.id)])) as Record<BoardColumn, RecordItem[]>;
  return <div className="board-grid">
    {COLUMNS.map(column => <section key={column.id} className={`board-column ${column.id}`} aria-label={column.title}>
      <header className="board-column-heading"><h3>{column.title}</h3><span className="count-chip">{grouped[column.id].length}</span></header>
      <div className="board-column-body">
        {grouped[column.id].length ? grouped[column.id].map(record => {
          const type = workType(record);
          return <button key={record.id} type="button" className="board-card" onClick={() => onSelect(record.id)} aria-label={`查看 ${record.title}`}>
            <div className="board-card-top"><span className={`record-icon ${type} ${record.status === 'merged' ? 'merged' : ''}`}><KindIcon kind={type} size={16} /></span><Status value={recordStatus(record)} small /></div>
            <strong>{record.title}</strong>
            <small><span>{record.repo.split('/')[1]}</span><span className="meta-dot">·</span><span className="mono">{record.kind === 'release' ? record.details.tag : `#${record.number}`}</span></small>
            <div className="board-card-meta"><PriorityMark record={record} /><OwnerMark record={record} /><Stamp value={record.updated_at} /></div>
          </button>;
        }) : <div className="board-empty">暂无事项</div>}
      </div>
    </section>)}
  </div>;
}
