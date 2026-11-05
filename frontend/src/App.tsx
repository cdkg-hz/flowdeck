import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { Activity, AlertTriangle, ArrowDown, ArrowRight, ArrowUpRight, Bell, Box, Check, CheckCheck, CheckCircle2, ChevronDown, ChevronRight, Circle, CircleDot, CircleHelp, Code2, Copy, ExternalLink, Eye, EyeOff, FileCode2, FileText, FlaskConical, FolderGit2, GitBranch, GitCommitHorizontal, GitMerge, GitPullRequest, HardDrive, KeyRound, Layers3, LayoutDashboard, Loader2, LockKeyhole, LogIn, LogOut, Menu, Network, Package, Plug, Plus, RefreshCw, Search, Server, ShieldCheck, Sparkles, Terminal, Trash2, Upload, UserRound, Users, WifiOff, X, XCircle, Zap } from 'lucide-react';
import type { AdminIntegrations, AdminSkills, AgentConnection, AgentRule, AuthSession, DingTalkChannel, Integrations, Overview, PrepareRelease, Project, ProjectConnection, ProjectOptions, RecordDetail, RecordItem, TimelineItem, UploadedSkill } from './types';
import { Avatar, KIND, KindIcon, PRIORITIES, RecordsBoard, Stamp, Status, priorityTone, recordStatus, relative, workType } from './RecordsBoard';

type View = 'overview' | 'workflow' | 'releases' | 'integrations';
const STAGES: Record<string, string> = { issue: '需求处理', draft: '草稿', pr: '提交 PR', workspace: '准备代码', review: 'Agent 审查', checks: '必需检查', merge: '合并代码', release: '发布 Release', apk: '解析 APK', notes: '生成更新记录', upload: '发布安卓版本', build: '构建镜像', deploy: '更新后端', verify: '验证上线', closed: '结束', queued: '等待执行', notification: '通知', agent_task: 'Agent 任务', prepare_release: '总结并发布' };
const DINGTALK_EVENT_OPTIONS = [
  ['review_needs_attention', '审查需要处理'],
  ['review_passed', '审查通过'],
  ['checks_failed', '必需检查失败'],
  ['merged', 'PR 已合并'],
  ['job_failed', '任务失败 / 不确定 / 缺配置'],
  ['release_ready', '发版已完成'],
  ['board_column_change', '看板列切换'],
];
const JOB_KIND: Record<string, string> = { release: '发布任务', agent_task: 'Agent 任务', review: '审查任务', merge: '合并任务', prepare_release: '总结并发布' };
const RETRYABLE_JOB = new Set(['failed', 'uncertain', 'needs_configuration', 'waiting']);
const navItems: { id: View; title: string; icon: typeof Activity }[] = [{ id: 'overview', title: '项目概览', icon: LayoutDashboard }, { id: 'workflow', title: '研发流程', icon: Network }, { id: 'releases', title: '发布中心', icon: Package }, { id: 'integrations', title: '集成配置', icon: Plug }];

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin', ...init });
  if (!response.ok) {
    let detail = '';
    try {
      const body = await response.json();
      const value = body.detail || body.error || body.message || '';
      detail = Array.isArray(value) ? value.map(item => item?.msg || String(item)).join('；') : typeof value === 'string' ? value : JSON.stringify(value);
    } catch { /* use status fallback */ }
    throw new Error(detail || `服务暂时不可用（${response.status}）`);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
function jsonRequest(method: string, body?: unknown, csrfToken?: string | null): RequestInit {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
  return { method, headers, body: body === undefined ? undefined : JSON.stringify(body) };
}
function fileRequest(method: string, file: File, csrfToken?: string | null): RequestInit {
  const body = new FormData();
  body.append('file', file);
  const headers: Record<string, string> = {};
  if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
  return { method, headers, body };
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('无法读取密钥文件'));
    reader.onload = () => {
      const bytes = new Uint8Array(reader.result as ArrayBuffer);
      let binary = '';
      const chunk = 0x8000;
      for (let i = 0; i < bytes.length; i += chunk) binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
      resolve(btoa(binary));
    };
    reader.readAsArrayBuffer(file);
  });
}

function SafeLink({ url, children }: { url: string; children: React.ReactNode }) {
  try { if (!['https:', 'http:'].includes(new URL(url).protocol)) return null; } catch { return null; }
  return <a href={url} target="_blank" rel="noopener noreferrer">{children}</a>;
}

export default function App() {
  const [view, setView] = useState<View>('overview');
  const [project, setProject] = useState('');
  const [projects, setProjects] = useState<Project[]>([]);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [records, setRecords] = useState<RecordItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  const [integrations, setIntegrations] = useState<Integrations | null>(null);
  const [kind, setKind] = useState('');
  const [filter, setFilter] = useState('');
  const [priorityFilter, setPriorityFilter] = useState('');
  const [assigneeFilter, setAssigneeFilter] = useState('');
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [connected, setConnected] = useState(false);
  const [mobile, setMobile] = useState(false);
  const [revision, setRevision] = useState(0);
  const [session, setSession] = useState<AuthSession | null>(null);
  const [loginOpen, setLoginOpen] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [prepareOpen, setPrepareOpen] = useState(false);
  const [connectionId, setConnectionId] = useState<string | null>(null);
  const [loginIntent, setLoginIntent] = useState<'create' | 'connection' | null>(null);
  const [deletingProject, setDeletingProject] = useState<Project | null>(null);
  const canRead = Boolean(session && (!session.require_login || session.authenticated));
  function handleProjectDeleted(deletedRepo: string) { if (project === deletedRepo) setProject(''); void refresh(); }
  const closeDetail = useCallback(() => setSelected(null), []);
  const requestNumber = useRef(0);
  useEffect(() => { const timer = setTimeout(() => setQuery(search), 220); return () => clearTimeout(timer); }, [search]);
  const refresh = useCallback(async () => {
    const request = ++requestNumber.current;
    const pageSize = view === 'overview' ? 200 : 50;
    const params = new URLSearchParams({ limit: String(pageSize), offset: String(view === 'overview' ? 0 : page * pageSize) });
    if (project) params.set('repo', project);
    if (query) params.set('q', query);
    if (view === 'releases') params.set('kind', 'release'); else if (kind) params.set('kind', kind);
    if (filter) params.set('status', filter);
    if (view === 'overview' && priorityFilter) params.set('priority', priorityFilter);
    try {
      const [p, o, r, i] = await Promise.all([api<Project[]>('/api/projects'), api<Overview>(`/api/overview${project ? `?repo=${encodeURIComponent(project)}` : ''}`), api<{ items: RecordItem[]; total: number }>(`/api/records?${params}`), api<Integrations>('/api/integrations')]);
      if (request !== requestNumber.current) return;
      setProjects(p); setOverview(o); setRecords(r.items); setTotal(r.total); setIntegrations(i); setError(''); setRevision(x => x + 1);
    } catch (e) { if (request === requestNumber.current) setError((e as Error).message); }
    finally { if (request === requestNumber.current) setLoading(false); }
  }, [project, query, view, kind, filter, priorityFilter, page]);
  useEffect(() => { setPage(0); }, [project, query, view, kind, filter, priorityFilter]);
  useEffect(() => { let active = true; api<AuthSession>('/api/auth/session').then(value => { if (active) setSession(value); }).catch(() => { if (active) setSession({ authenticated: false, csrf_token: null, username: null, admin_configured: true, require_login: true }); }); return () => { active = false; }; }, []);
  useEffect(() => {
    if (!canRead) {
      setError('');
      setConnected(false);
      return;
    }
    setLoading(true);
    void refresh();
  }, [refresh, canRead]);
  useEffect(() => {
    if (!canRead) return;
    const source = new EventSource('/api/stream');
    source.onopen = () => setConnected(true);
    source.onmessage = () => { void refresh(); };
    source.onerror = () => setConnected(false);
    return () => source.close();
  }, [refresh, canRead]);
  function navigate(next: View) { setView(next); setKind(''); setFilter(''); setPriorityFilter(''); setAssigneeFilter(''); setSearch(''); setMobile(false); }
  function requestCreate() { setMobile(false); if (session?.authenticated) setCreateOpen(true); else { setLoginIntent('create'); setLoginOpen(true); } }
  function requestConnection(id: string) { if (session?.authenticated) setConnectionId(id); else { setConnectionId(id); setLoginIntent('connection'); setLoginOpen(true); } }
  async function logout() {
    if (!session?.csrf_token) return;
    try { await api<unknown>('/api/auth/logout', jsonRequest('POST', undefined, session.csrf_token)); setSession(await api<AuthSession>('/api/auth/session')); setConnectionId(null); } catch (e) { setError((e as Error).message); }
  }
  const currentProject = projects.find(p => p.repo === project);
  const demo = integrations?.mode === 'demo';
  const prepareProjects = projects.filter(item => !item.demo && item.prepare_release);
  const canPrepare = !!session?.authenticated && !demo && (currentProject ? !!currentProject.prepare_release : prepareProjects.length > 0);
  const attention = records.filter(r => ['failed', 'changes_requested', 'uncertain', 'needs_configuration'].includes(r.status));
  const release = records.find(r => r.kind === 'release' && r.status === 'running') || records.find(r => r.kind === 'release');
  const assigneeOptions = [...new Set(records.flatMap(item => item.assignees))].sort((a, b) => a.localeCompare(b));
  const visibleRecords = assigneeFilter === 'none' ? records.filter(item => !item.assignees.length) : assigneeFilter ? records.filter(item => item.assignees.includes(assigneeFilter)) : records;

  if (!session) {
    return <div className="login-gate-screen"><div className="login-gate-card"><div className="loading-state"><Loader2 className="spin" />正在加载…</div></div></div>;
  }
  if (session.require_login && !session.authenticated) {
    return <LoginGate session={session} onAuthenticated={next => { setSession(next); setError(''); }} />;
  }

  return <div className="app-shell">
    {mobile && <button className="sidebar-scrim" onClick={() => setMobile(false)} aria-label="收起导航" />}
    <aside className={`sidebar ${mobile ? 'open' : ''}`}>
      <button className="brand" onClick={() => { setProject(''); navigate('overview'); }}><span className="brand-mark"><Layers3 size={23} strokeWidth={2.4} /></span><span>Flowdeck<span className="brand-caption">研发工作流</span></span></button>
      <div className="workspace-label"><span className="workspace-avatar">W</span><div>研发工作空间<small>Development workspace</small></div><ChevronDown size={15} /></div>
      <span className="nav-caption">工作台</span>
      <nav aria-label="主导航">{navItems.map(item => <button key={item.id} className={`nav-item ${view === item.id ? 'active' : ''}`} onClick={() => navigate(item.id)}><item.icon size={19} /><span>{item.title}</span>{item.id === 'overview' && !!overview?.counts.attention && <b>{overview.counts.attention}</b>}</button>)}</nav>
      <div className="nav-caption project-caption">项目 <span>{projects.length}</span><button className="sidebar-add" onClick={requestCreate} aria-label="创建项目" title="创建项目"><Plus size={15}/></button></div>
      <nav className="project-nav" aria-label="项目筛选"><button className={!project ? 'selected' : ''} onClick={() => { setProject(''); setMobile(false); }}><FolderGit2 size={16} />全部项目</button>{projects.map((p, idx) => <button key={p.id} className={project === p.repo ? 'selected' : ''} onClick={() => { setProject(p.repo); setMobile(false); }}><i className={`project-dot color-${idx % 3}`} /><span>{p.name}</span><small>{p.record_count}</small></button>)}</nav>
      <div className="sidebar-bottom"><div className="architecture-note"><GitBranch size={19} /><strong>从需求，到上线。</strong><p>让每一次交接都有迹可循。</p></div><div className="team-footer"><span className="team-icon"><Users size={18} /></span><div>研发团队<small>内网 · 只读看板</small></div><CircleHelp size={17} aria-hidden="true" /></div></div>
    </aside>
    <div className="main-shell">
      <header className="topbar"><div className="breadcrumb"><button className="icon-button mobile-toggle" onClick={() => setMobile(true)} aria-label="展开导航"><Menu size={20} /></button><span>工作空间</span><ChevronRight size={14} /><strong>{navItems.find(x => x.id === view)?.title}</strong>{currentProject && <><ChevronRight size={14} /><span>{currentProject.name}</span></>}</div><div className="topbar-right"><span className={`connection ${connected ? 'online' : ''}`}>{connected ? <i /> : <WifiOff size={13} />}{connected ? '实时更新' : '正在连接'}</span><span className="topbar-divider"/><button className="icon-button" title="查看待处理事项" aria-label="查看待处理事项" onClick={() => { setView('overview'); setFilter('attention'); }}><Bell size={18} />{!!overview?.counts.attention && <span className="notification-dot" />}</button>{session?.authenticated ? <div className="admin-session"><span className="viewer-avatar">{session.username?.slice(0, 1).toUpperCase() || '管'}</span><span>{session.username}</span><button className="icon-button" onClick={() => void logout()} title="退出管理" aria-label="退出管理"><LogOut size={16}/></button></div> : <button className="admin-login" onClick={() => { setLoginIntent(null); setLoginOpen(true); }}><LogIn size={15}/>管理员登录</button>}</div></header>
      <main>
        {demo && <div className="demo-notice"><FlaskConical size={15} /><span>演示环境</span><p>当前为示例数据，不会触发审查或发布。</p><button onClick={() => navigate('integrations')}>查看接入方式 <ArrowRight size={14} /></button></div>}
        <div className="page-heading"><div><div className="eyebrow">{view === 'overview' ? 'WORKSPACE OVERVIEW' : view === 'releases' ? 'RELEASE CENTER' : view === 'workflow' ? 'HOW WORK FLOWS' : 'CONNECTED SERVICES'}</div><h1>{view === 'overview' ? (currentProject ? currentProject.name : '项目概览') : view === 'releases' ? '发布中心' : view === 'workflow' ? '研发流程' : '集成配置'}</h1><p>{view === 'overview' ? '跟踪每个需求、每次审查和每一个发布。' : view === 'releases' ? '从 GitHub Release 到更新记录，查看每一步执行结果。' : view === 'workflow' ? '谁来处理、何时交接、出现问题怎么办。' : '查看数据来源、执行器与项目接入状态。'}</p></div><div className="heading-actions"><span className="date-label">{new Date().toLocaleDateString('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' })}</span>{(view === 'overview' || view === 'releases') && canPrepare && <button className="button primary" onClick={() => setPrepareOpen(true)}><Package size={15}/>总结并发布</button>}<button className="button secondary" onClick={() => { setLoading(true); void refresh(); }}><RefreshCw size={15} className={loading ? 'spin' : ''} />刷新</button></div></div>
        {error && <div className="error-banner" role="alert"><XCircle size={18}/>{error}<button onClick={() => void refresh()}>重试</button></div>}
        {!overview && loading ? <div className="loading-state"><Loader2 className="spin" />正在加载工作空间…</div> : <>
          {view === 'workflow' ? <WorkflowPage /> : view === 'integrations' ? <IntegrationsPage integrations={integrations} projects={projects} session={session} authenticated={!!session?.authenticated} onCreate={requestCreate} onConnection={requestConnection} onDelete={setDeletingProject} /> : <>
            {view === 'overview' && <>
              <section className="stats-grid" aria-label="工作概况">{[{ title: '未完成事项', value: overview?.counts.active, icon: Layers3, color: 'blue', note: '需求、PR 与发布任务' }, { title: '审查中', value: overview?.counts.reviewing, icon: Sparkles, color: 'purple', note: '排队与正在分析的 PR' }, { title: '需要关注', value: overview?.counts.attention, icon: Zap, color: 'orange', note: '异常、修改与待指派事项' }, { title: '已完成发布', value: overview?.counts.released, icon: CheckCheck, color: 'green', note: '已完成的 GitHub Release' }].map((s, i) => <button className={`stat-card ${i === 2 ? 'attention-stat' : ''}`} key={s.title} onClick={() => { if (i === 2) setFilter('attention'); else if (i === 3) { setView('releases'); setFilter('success'); } else { setKind(i === 1 ? 'pr' : ''); setFilter(i === 1 ? 'reviewing' : 'active'); } }}><div className="stat-top"><span>{s.title}</span><span className={`stat-icon ${s.color}`}><s.icon size={18} /></span></div><strong>{s.value ?? '—'}<small>项</small></strong><p>{s.note}<ArrowUpRight size={14}/></p></button>)}</section>
              <section className="flow-strip"><div><span className="tiny-label">标准工作流</span><strong>每一步，都有明确的下一步</strong></div><div className="mini-flow">{[{ title: '提出需求', icon: CircleDot }, { title: '分支开发', icon: GitBranch }, { title: '代码审查', icon: Sparkles }, { title: '合并代码', icon: GitMerge }, { title: '发布上线', icon: Package }].map((s, i) => <div className="mini-step" key={s.title}><span className={i === 2 ? 'featured' : ''}><s.icon size={17}/></span><small>{s.title}</small>{i < 4 && <ChevronRight size={13} className="mini-arrow"/>}</div>)}</div><button className="icon-button flow-more" onClick={() => navigate('workflow')} aria-label="查看完整研发流程"><ArrowUpRight size={19}/></button></section>
            </>}
            <div className={view === 'overview' ? 'content-grid' : 'releases-content'}><div className={`records-panel panel ${view === 'overview' ? 'board-panel' : ''}`}><div className="panel-heading"><div><h2>{view === 'releases' ? '版本发布记录' : '工作事项'} <span className="count-chip">{assigneeFilter ? visibleRecords.length : total}</span></h2></div><label className="search"><Search size={16}/><input placeholder={view === 'releases' ? '搜索版本…' : '搜索事项…'} value={search} onChange={e => setSearch(e.target.value)} aria-label="搜索事项" />{search && <button className="icon-button" onClick={() => setSearch('')} aria-label="清除搜索"><X size={13}/></button>}</label></div>
              <div className="table-controls"><div className="tabs">{(view === 'releases' ? [['', '全部版本'], ['running', '进行中'], ['attention', '需要关注'], ['success', '已完成']] : [['', '全部事项'], ['issue', 'Issue'], ['bug', 'Bug'], ['pr', 'Pull Request'], ['release', 'Release']]).map(([key, label]) => <button key={key} className={(view === 'releases' ? filter : kind) === key ? 'active' : ''} onClick={() => { if (view === 'releases') setFilter(key); else { setKind(key); setFilter(''); setAssigneeFilter(''); } }}>{label}</button>)}</div>{view === 'overview' && <div className="board-filters"><select className="status-filter" value={priorityFilter} onChange={e => setPriorityFilter(e.target.value)} aria-label="按优先级筛选"><option value="">全部优先级</option>{PRIORITIES.map(level => <option key={level} value={level}>{level}</option>)}<option value="none">未设置</option></select><select className="status-filter" value={assigneeFilter} onChange={e => setAssigneeFilter(e.target.value)} aria-label="按负责人筛选"><option value="">全部负责人</option><option value="none">待指派</option>{assigneeOptions.map(name => <option key={name} value={name}>{name}</option>)}</select></div>}</div>
              {!visibleRecords.length ? <EmptyState filtered={!!(query || filter || kind || priorityFilter || assigneeFilter)} /> : view === 'overview' ? <RecordsBoard records={visibleRecords} onSelect={setSelected} /> : <div className="table-wrap"><table><thead><tr><th>事项</th><th>状态</th><th className="priority-column">优先级</th><th>负责人</th><th className="updated-column">更新</th><th aria-label="查看详情" /></tr></thead><tbody>{visibleRecords.map(r => <tr key={r.id} tabIndex={0} onClick={() => setSelected(r.id)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelected(r.id); } }} aria-label={`查看 ${r.title}`}><td><div className="record-cell"><span className={`record-icon ${workType(r)} ${r.status === 'merged' ? 'merged' : ''}`}><KindIcon kind={workType(r)}/></span><div><strong>{r.title}</strong><small><span>{r.repo.split('/')[1]}</span><span className="meta-dot">·</span><span className="mono">{r.kind === 'release' ? r.details.tag : `#${r.number}`}</span></small></div></div></td><td><Status value={recordStatus(r)}/></td><td className="priority-column">{r.priority ? <span className={`priority ${priorityTone(r.priority)[1]}`}><span>{priorityTone(r.priority)[0]}</span>{r.priority}{r.details.priority_conflict && <CircleHelp size={12}/>}</span> : <span className="muted">—</span>}</td><td>{r.assignees.length ? <div className="owner"><Avatar name={r.assignees[0]} /><span>{r.assignees[0]}</span>{r.assignees.length > 1 && <small>+{r.assignees.length - 1}</small>}</div> : <span className="unassigned"><UserRound size={14}/>待指派</span>}</td><td className="updated-column"><Stamp value={r.updated_at}/></td><td><ChevronRight size={14} className="row-chevron"/></td></tr>)}</tbody></table></div>}
              <div className="table-footer"><span>{view === 'overview' ? (total > records.length ? `共 ${total} 条 · 仅显示最近 ${records.length} 条` : `共 ${assigneeFilter ? visibleRecords.length : total} 条`) : `共 ${total} 条 · 第 ${page + 1} 页`}</span>{view === 'releases' && <div className="pagination"><button disabled={page === 0} onClick={() => setPage(p => p - 1)}>上一页</button><button disabled={(page + 1) * 50 >= total} onClick={() => setPage(p => p + 1)}>下一页</button></div>}<span><span className="live-dot"/>{demo ? '示例工作空间' : '来自 GitHub 与执行服务'}</span></div>
            </div>
            {view === 'overview' && <aside className="context-column"><section className="panel release-spotlight"><div className="panel-heading"><h2><Package size={17}/>发布动态</h2><div>{canPrepare && <button className="text-button" onClick={() => setPrepareOpen(true)}>总结并发布</button>}<button className="text-button" onClick={() => navigate('releases')}>全部 <ArrowRight size={13}/></button></div></div>{release ? <><div className="release-summary"><div className="release-package"><Box size={27}/></div><div><small>{release.repo.split('/')[1]}</small><h3>{release.details.tag || release.title}</h3></div><Status value={release.status} small/></div><div className="release-checklist">{['notes'].map(step => { const state = release.details.steps?.[step] || 'waiting'; return <div key={step} className={state}><StepIcon status={state}/><span>{STAGES[step]}</span>{state === 'running' && <small>执行中</small>}</div>; })}</div><button className="release-detail" onClick={() => setSelected(release.id)}>查看发布详情 <ArrowUpRight size={15}/></button></> : <div className="small-empty">尚无发布记录</div>}</section>
              <section className="panel attention-panel"><div className="panel-heading"><h2><Zap size={17}/>需要处理</h2><span className="count-chip orange-chip">{attention.length}</span></div>{attention.length ? attention.slice(0, 2).map(r => <button className="attention-item" key={r.id} onClick={() => setSelected(r.id)}><span className={`attention-marker ${r.status === 'failed' ? 'red' : ''}`} /><div><strong>{r.status === 'changes_requested' ? '审查发现待修改问题' : '执行过程需要关注'}</strong><p>{r.title}</p><small>{r.repo.split('/')[1]} <span>·</span> <Stamp value={r.updated_at}/></small></div><ChevronRight size={14}/></button>) : <div className="small-empty"><CheckCircle2 size={20}/>当前没有待处理异常</div>}</section>
            </aside>}</div>
            {view === 'overview' && <section className="activity-panel panel"><div className="panel-heading"><h2><Activity size={17}/>最近动态</h2><span className="subtle">事件时间线</span></div><div className="activity-grid">{overview?.activity.slice(0, 4).map(a => <button key={a.id} onClick={() => setSelected(a.record_id)} className="activity-item"><span className={`activity-icon ${a.status === 'failed' || a.status === 'changes_requested' ? 'warn' : ''}`}>{a.stage === 'review' || a.stage === 'notes' ? <Sparkles size={16}/> : a.stage === 'deploy' ? <Server size={16}/> : <GitCommitHorizontal size={17}/>}</span><div><strong>{a.title}</strong><p>{a.record_title}</p><small>{a.actor} <span>·</span> <Stamp value={a.occurred_at}/></small></div></button>)}</div>{!overview?.activity.length && <div className="small-empty">接入项目后，操作与执行记录将显示在这里。</div>}</section>}
          </>}
        </>}
        <footer className="page-footer"><span><Layers3 size={13}/> FLOWDECK</span><p>需求有归属，过程可追踪，发布有结果。</p></footer>
      </main>
    </div>
    {selected && <DetailPanel id={selected} revision={revision} session={session} onClose={closeDetail} />}
    {loginOpen && <LoginDialog session={session} onClose={() => { setLoginOpen(false); setLoginIntent(null); if (loginIntent === 'connection' && !session?.authenticated) setConnectionId(null); }} onAuthenticated={next => { setSession(next); setLoginOpen(false); if (loginIntent === 'create') setCreateOpen(true); setLoginIntent(null); }} />}
    {createOpen && session?.authenticated && <ProjectWizard session={session} onClose={() => setCreateOpen(false)} onCreated={connection => { setCreateOpen(false); setConnectionId(connection.id); void refresh(); }} />}
    {prepareOpen && session?.authenticated && <PrepareReleaseDialog session={session} projects={prepareProjects} selectedRepo={project} onClose={() => setPrepareOpen(false)} onQueued={recordId => { setPrepareOpen(false); setView('releases'); setSelected(recordId); void refresh(); }} />}
    {connectionId && session?.authenticated && <ConnectionDialog projectId={connectionId} session={session} onClose={() => setConnectionId(null)} onChanged={() => void refresh()} onDeleted={handleProjectDeleted} />}
    {deletingProject && session?.authenticated && <DeleteProjectDialog project={deletingProject} session={session} onClose={() => setDeletingProject(null)} onDeleted={handleProjectDeleted} />}
  </div>;
}

function StepIcon({ status }: { status: string }) { return status === 'success' || status === 'completed' || status === 'merged' ? <CheckCircle2 size={16}/> : status === 'running' ? <Loader2 size={16} className="spin"/> : ['failed', 'changes_requested'].includes(status) ? <XCircle size={16}/> : <Circle size={16}/>; }
function EmptyState({ filtered }: { filtered: boolean }) { return <div className="empty-state"><FolderGit2 size={35}/><h3>{filtered ? '没有匹配的事项' : '等待第一条项目记录'}</h3><p>{filtered ? '尝试调整搜索词或筛选条件。' : '配置 GitHub 仓库并启动执行服务后，记录将自动出现在这里。'}</p></div>; }

function AgentActivityBanner({ record }: { record: RecordDetail }) {
  const activity = record.details?.agent_activity;
  const live = ['queued', 'running'].includes(record.status) || record.jobs.some(job => ['queued', 'running'].includes(job.status));
  if (!live || !activity?.text) return null;
  const recent = Array.isArray(activity.recent) ? activity.recent.filter((item: unknown) => typeof item === 'string') : [];
  return <div className="detail-callout agent-activity"><Loader2 size={18} className="spin"/><div><strong>{activity.title || 'Agent 正在工作'}</strong><p>{activity.text}</p>{recent.length > 0 && <ul className="agent-activity-recent">{recent.map((line: string, i: number) => <li key={i}>{line}</li>)}</ul>}</div></div>;
}
function DetailPanel({ id, onClose, revision, session }: { id: string; onClose: () => void; revision: number; session: AuthSession | null }) {
  const [record, setRecord] = useState<RecordDetail | null>(null);
  const [error, setError] = useState('');
  const [tab, setTab] = useState('timeline');
  const [retrying, setRetrying] = useState('');
  const panel = useRef<HTMLDivElement>(null);
  useEffect(() => { let active = true; api<RecordDetail>(`/api/records/${id}`).then(r => { if (active) { setRecord(r); setError(''); } }).catch(e => { if (active) setError(e.message); }); return () => { active = false; }; }, [id, revision]);
  async function retryJob(jobId: string) {
    if (!session?.csrf_token) return;
    setRetrying(jobId);
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}/retry`, jsonRequest('POST', undefined, session.csrf_token));
      setRecord(await api<RecordDetail>(`/api/records/${id}`));
      setError('');
    } catch (e) { setError((e as Error).message); }
    finally { setRetrying(''); }
  }
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    panel.current?.focus();
    function key(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
      if (e.key === 'Tab') { const elements = panel.current?.querySelectorAll<HTMLElement>('button, a[href], input, [tabindex="0"]'); if (!elements?.length) return; const first = elements[0], last = elements[elements.length - 1]; if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { e.preventDefault(); last.focus(); } else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); } }
    }
    document.addEventListener('keydown', key);
    return () => { document.body.style.overflow = overflow; document.removeEventListener('keydown', key); previous?.focus(); };
  }, [onClose]);
  return <div className="detail-overlay"><button className="detail-backdrop" onClick={onClose} aria-label="关闭详情"/><div ref={panel} className="detail-panel" role="dialog" aria-modal="true" aria-labelledby="detail-title" tabIndex={-1}><div className="detail-toolbar"><span><KindIcon kind={record ? workType(record) : 'issue'}/>{record ? KIND[workType(record)] : '事项详情'} {record?.number && <b>#{record.number}</b>}</span><div>{record?.url && !record.details?.deleted && <SafeLink url={record.url}>在 GitHub 查看 <ExternalLink size={14}/></SafeLink>}<button className="icon-button" onClick={onClose} aria-label="关闭详情"><X size={20}/></button></div></div>{error ? <div className="error-banner">{error}</div> : !record ? <div className="loading-state"><Loader2 className="spin"/>加载详情…</div> : <div className="detail-body"><span className="detail-repo"><FolderGit2 size={15}/>{record.repo}{record.demo && <span className="demo-chip">示例</span>}</span><h2 id="detail-title">{record.title}</h2><div className="detail-meta"><Status value={recordStatus(record)}/><span>{STAGES[record.stage] || record.stage}</span><span className="meta-dot">·</span><Stamp value={record.updated_at}/></div><div className="detail-properties"><div><span>负责人</span><strong>{record.assignees.length ? record.assignees.map(a => <span className="owner" key={a}><Avatar name={a}/>{a}</span>) : '待指派'}</strong></div><div><span>紧急程度</span><strong>{record.priority || '未设置'}{record.details.priority_conflict && <small className="warning-text">多个优先级标签</small>}</strong></div>{record.details.head_sha && <div><span>审查提交</span><code>{record.details.head_sha.slice(0, 12)}</code></div>}{record.details.head_ref && <div><span>分支</span><code>{record.details.head_ref} → {record.details.base_ref}</code></div>}</div>
      {record.details?.deleted && <div className="detail-callout"><Trash2 size={18}/><div><strong>GitHub 上已删除</strong><p>仓库里的 Issue 或 Bug 已被删除。本地保留这条记录，并同步为已删除。</p></div></div>}
      {record.status === 'not_connected' && <div className="detail-callout"><Plug size={18}/><div><strong>此自动化环节尚未接入</strong><p>GitHub 记录已保存。配置相应执行器后，才会产生实际执行结果。</p></div></div>}
      {record.details.error && <div className="detail-callout error"><XCircle size={18}/><div><strong>执行需要处理</strong><p>{record.details.error}</p></div></div>}
      <div className="detail-tabs tabs"><button className={tab === 'timeline' ? 'active' : ''} onClick={() => setTab('timeline')}>进度记录</button>{record.kind === 'pr' && <button className={tab === 'review' ? 'active' : ''} onClick={() => setTab('review')}>审查结果</button>}{record.kind === 'release' && <button className={tab === 'version' ? 'active' : ''} onClick={() => setTab('version')}>版本信息</button>}{!!record.details.agent_results && <button className={tab === 'agent' ? 'active' : ''} onClick={() => setTab('agent')}>Agent 结果</button>}<button className={tab === 'description' ? 'active' : ''} onClick={() => setTab('description')}>说明</button></div>
      {tab === 'timeline' && <><AgentActivityBanner record={record}/><div className="detail-timeline">{[...record.timeline].reverse().map((e: TimelineItem, i) => <div className={`timeline-event ${i === 0 ? 'latest' : ''}`} key={e.id}><span className={`timeline-icon ${e.status}`}><StepIcon status={e.status}/></span><div><div className="timeline-heading"><strong>{e.title}</strong><Stamp value={e.occurred_at}/></div><small>{e.actor} · {STAGES[e.stage] || e.stage}</small>{e.message && <p>{e.message}</p>}</div></div>)}</div>{!record.timeline.length && <div className="small-empty">还没有执行事件。</div>}{!!record.jobs.length && <div className="job-section"><h3>执行任务</h3>{record.jobs.map(job => <div className="job-item" key={job.id}><Terminal size={15}/><code>{job.id.slice(0, 10)}</code><span>{JOB_KIND[job.kind] || job.kind} · {STAGES[job.step] || job.step}</span><Status value={job.status} small/>{session?.authenticated && RETRYABLE_JOB.has(job.status) && <button className="button secondary" disabled={!!retrying} onClick={() => void retryJob(job.id)}>{retrying === job.id ? <Loader2 className="spin" size={14}/> : <RefreshCw size={14}/>}重试</button>}{job.error && <p>{job.error}</p>}</div>)}</div>}</>}
      {tab === 'review' && <>{record.details.review ? <><div className="review-summary"><ShieldCheck size={21}/><p>{record.details.review.summary}</p></div>{record.details.review.findings?.map((f: any, i: number) => <article className="finding" key={i}><div><span className={`finding-severity ${f.severity}`}>{({ critical: '严重', high: '高', medium: '中', low: '低' } as Record<string, string>)[f.severity]}</span><h3>{f.title}</h3></div><code>{f.file}:{f.line}</code><p>{f.explanation}</p>{f.suggestion && <div className="suggestion"><strong>修改建议</strong><p>{f.suggestion}</p></div>}</article>)}</> : <div className="small-empty"><Sparkles size={25}/>尚无对应当前提交的审查结论。</div>}{record.details.checks && <div className="checks"><h3>CI 检查</h3>{record.details.checks.map((c: any) => <div key={c.name}><StepIcon status={c.state}/><span>{c.name}</span><small>{({success: "通过", failure: "失败", pending: "等待中"} as Record<string, string>)[c.state] || c.state}</small></div>)}</div>}</>}
      {tab === 'version' && <><div className="version-grid">{[['Release Tag', record.details.tag], ['版本名称', record.details.version_name], ['版本号', record.details.version_code], ['应用包名', record.details.package_name], ['APK 文件', record.details.apk_name], ['镜像版本', record.details.image]].map(([label, value]) => <div key={label}><small>{label}</small><strong>{value || '等待获取'}</strong></div>)}</div><h3 className="section-title">用户更新说明</h3><div className="notes-content">{record.details.public_notes || '更新说明尚未生成。'}</div><h3 className="section-title">研发版本记录</h3><div className="notes-content">{record.details.technical_notes || '研发记录尚未生成。'}</div></>}
      {tab === 'description' && <div className="notes-content">{record.body || '暂无说明。'}</div>}
      {tab === 'agent' && <AgentResults details={record.details.agent_results || {}} />}
      {!!record.linked_records.length && <div className="linked-records"><h3>GitHub 明确关联的事项</h3>{record.linked_records.map(x => <p key={x.id}>#{x.number} {x.title}</p>)}</div>}
    </div>}</div></div>;
}

const development = [
  { id: 'issue', title: '提出需求', role: '提出人 / 管理人', icon: CircleDot, text: '创建 GitHub Issue，设置 Assignees 与「紧急 / 普通 / 较低」标签。@提及用于通知，不替代正式指派。', output: '有负责人和紧急程度的 Issue', failure: '未指派或未设置优先级时，在看板明确提示。' },
  { id: 'pr', title: '分支开发 · 提交 PR', role: '开发者', icon: GitBranch, text: '在新分支完成修复或功能开发，提交 PR，并通过 GitHub 的关联机制连接 Issue。', output: '固定基线与当前提交 SHA 的 PR', failure: '草稿 PR 等待就绪，新提交触发新一轮审查。' },
  { id: 'review', title: 'Agent 代码审查', role: '执行服务 → Agent', icon: Sparkles, text: '执行服务准备独立代码工作区。本地 Copilot CLI 运行时阅读差异、相关实现和测试，返回有依据的问题与结论。不需要 Copilot 订阅。', output: '绑定本次提交的结构化审查报告', failure: '发现问题发钉钉；结果格式异常或分析未完成时不视为通过。' },
  { id: 'checks', title: '检查合并条件', role: '执行服务 / GitHub', icon: ShieldCheck, text: '验证当前提交的 Agent 结论、所有配置的必需检查，以及 GitHub 合并条件。', output: '当前提交满足全部合并条件', failure: '检查未完成则等待，失败则发钉钉；新提交使旧结论失效。' },
  { id: 'merge', title: '合并代码', role: '执行服务 → GitHub', icon: GitMerge, text: '启用自动合并的项目，在全部条件满足后调用 GitHub API，并携带预期 SHA。', output: 'GitHub 确认 PR 已合并', failure: '不绕过分支保护；合并失败保存原因，等待处理。' },
];
const publishing = [
  { id: 'release', title: '总结并发布', role: '管理员 / 执行服务', icon: Package, text: '管理员在项目接入启用总结并发布后，在看板填写 tag 并点击按钮。Agent 按该功能的任务说明与已选 Skill 改文件（无 git 权限），Flowdeck 提交并推到默认主分支，再打 tag 并创建 GitHub Release。', output: '主分支新提交、Git Tag 与 GitHub Release', failure: '未启用总结并发布、远程 tag 已存在或默认分支受保护无法推送时停止。' },
  { id: 'notes', title: '分析变更 · 生成记录', role: '执行服务 → Agent', icon: FileText, text: '对比上次正式发布与本次提交，由本地 Copilot CLI 运行时结合代码和 PR 生成用户更新说明及研发记录，并更新 GitHub 的专用生成区段。镜像构建与后端部署由仓库自己的 GitHub Actions 完成，不在本程序执行。', output: '两份可追溯到代码的版本记录', failure: '没有明确基线或结果不符合约定时停止。' },
];
function WorkflowPage() {
  const [section, setSection] = useState('development');
  const [selected, setSelected] = useState(0);
  const steps = section === 'development' ? development : publishing;
  const current = steps[selected];
  return <div className="workflow-page"><div className="workflow-switch"><button className={section === 'development' ? 'active' : ''} onClick={() => { setSection('development'); setSelected(0); }}><GitPullRequest size={17}/>开发协作</button><button className={section === 'publishing' ? 'active' : ''} onClick={() => { setSection('publishing'); setSelected(0); }}><Package size={17}/>版本发布</button></div><section className="panel workflow-canvas"><div className="workflow-canvas-header"><div><span className="tiny-label">{section === 'development' ? 'ISSUE → MERGE' : 'RELEASE → NOTES'}</span><h2>{section === 'development' ? '从一个问题，到一次可靠的合并' : '从一个 GitHub Release，到一份可追溯的更新记录'}</h2></div><span className="flow-legend"><i/>正常流程 <i className="exception"/>异常交接</span></div><div className="workflow-nodes">{steps.map((step, i) => <div key={step.id} className="workflow-node-wrap"><button className={`workflow-node ${selected === i ? 'selected' : ''}`} onClick={() => setSelected(i)}><span className="node-number">0{i + 1}</span><span className="node-icon"><step.icon size={23}/></span><strong>{step.title}</strong><small>{step.role}</small></button>{i < steps.length - 1 && <ArrowRight size={20} className="node-arrow"/>}</div>)}</div><div className="exception-path"><ArrowDown size={16}/><span>{section === 'development' ? '审查或检查发现问题 → 发钉钉通知 → 开发者修改 → 重新审查' : '任一步骤失败 → 停止后续步骤 → 发钉钉通知 → 核对结果后处理'}</span></div></section><section className="flow-explanation panel"><div className="flow-detail-heading"><span className="stat-icon green"><current.icon size={24}/></span><div><span className="tiny-label">STEP 0{selected + 1}</span><h2>{current.title}</h2></div><span className="role-tag">{current.role}</span></div><p>{current.text}</p><div className="flow-output-grid"><div><CheckCircle2 size={18}/><h3>进入下一步的依据</h3><p>{current.output}</p></div><div><Bell size={18}/><h3>异常如何处理</h3><p>{current.failure}</p></div></div></section><div className="ownership-grid">{[{ icon: Network, title: '程序管理流程', text: '接收事件、准备代码、派发任务、保存结果并执行规则。' }, { icon: Sparkles, title: 'Agent 分析代码', text: '本地 Agent 运行时按需审查 PR、理解新增功能，返回带依据的分析结果。' }, { icon: Terminal, title: '本程序写回 GitHub', text: '把更新记录写进 GitHub Release；镜像与部署由仓库的 GitHub Actions 负责。' }].map(c => <div className="panel" key={c.title}><c.icon size={20}/><h3>{c.title}</h3><p>{c.text}</p></div>)}</div></div>;
}

function IntegrationsPage({ integrations, projects, session, authenticated, onCreate, onConnection, onDelete }: { integrations: Integrations | null; projects: Project[]; session: AuthSession | null; authenticated: boolean; onCreate: () => void; onConnection: (id: string) => void; onDelete: (project: Project) => void }) {
  const [copied, setCopied] = useState(false);
  const webhookPath = integrations?.github.webhook_path || '/api/webhooks/github/{webhook_id}';
  async function copy() { try { await navigator.clipboard.writeText(webhookPath); setCopied(true); setTimeout(() => setCopied(false), 2000); } catch { setCopied(false); } }
  const services = [{ name: 'GitHub', icon: GitBranch, configured: integrations?.github.configured, text: 'Issue、PR、检查和 Release 的数据源', detail: '事件实时接收，API 定期补漏' }, { name: 'Agent', icon: Sparkles, configured: integrations?.agent?.configured, text: '代码审查、发布分析与事件任务', detail: '本地 Copilot CLI 运行时调用你配置的 Chat Completions 接口' }, { name: '钉钉', icon: Bell, configured: integrations?.dingtalk?.configured, text: '按渠道把审查、合并和失败发到群机器人', detail: '自定义机器人 Webhook + 加签，可选事件和项目' }];
  return <div className="integrations-page">
    {!!integrations?.events?.failed && <div className="error-banner"><WifiOff size={18}/><div><strong>{integrations.events.failed} 条事件需要处理</strong><p>{integrations.events.last_error}</p></div></div>}
    {integrations?.mode === 'demo' && <div className="management-notice"><FlaskConical size={19}/><div><strong>这是接入流程预览</strong><p>管理员可以登录查看步骤，但演示环境不会创建项目、目录或 GitHub Webhook。</p></div></div>}
    <div className="integration-grid">{services.map(s => <section key={s.name} className="panel integration-card"><div className="integration-top"><span className="integration-logo"><s.icon size={26}/></span><span className={`status ${s.configured ? 'green' : 'neutral'}`}><i/>{s.configured ? '已配置' : '未配置'}</span></div><h2>{s.name}</h2><p>{s.text}</p><small>{s.detail}</small>{s.configured && <div className="config-note">配置状态不代表连接已验证，以实际任务结果为准。</div>}</section>)}</div>
    {authenticated && session && <AgentConnections session={session} />}
    {authenticated && session && <DingTalkChannels session={session} projects={projects.filter(p => !p.demo)} />}
    {authenticated && session && <SkillLibrary session={session} />}
    <section className="panel webhook-info"><div><h2><Network size={19}/>统一事件入口</h2><p>每个项目获得独立的 Webhook 地址与 Secret。程序据此验证来源，并决定何时启动 Agent 任务和发布流程。</p></div><div className="endpoint"><span>POST</span><code>{webhookPath}</code><button className="icon-button" onClick={() => void copy()} aria-label="复制 Webhook 路径">{copied ? <Check size={16}/> : <Copy size={16}/>}</button></div><div className="webhook-foot"><ShieldCheck size={15}/>公网仅转发事件入口，网页和查询接口留在内网。</div></section>
    <section className="panel project-configs"><div className="panel-heading"><div><h2>项目接入状态</h2><span className="subtle">仓库、工作区与 Webhook 就绪情况</span></div><button className="button primary" onClick={onCreate}><Plus size={16}/>{authenticated ? '创建项目' : '登录后创建'}</button></div>{projects.length ? projects.map(p => { const cfg = integrations?.projects.find(x => x.repo === p.repo); return <div className="project-config" key={p.id}><FolderGit2 size={20}/><div><strong>{p.name}</strong><small>{p.repo}</small></div><span>{p.demo ? '演示项目' : cfg?.review ? '审查已启用' : '审查未启用'}</span><span>{p.demo ? '示例数据' : p.sync_error ? '同步异常' : p.status === 'pending' ? '正在接入' : p.last_synced_at ? `同步于 ${relative(p.last_synced_at)}` : '等待首次同步'}</span><div className="project-actions"><button className="text-button connection-link" onClick={() => p.demo ? onCreate() : onConnection(p.id)}>{p.demo ? (authenticated ? '预览接入' : '登录后预览') : authenticated ? '接入详情' : '管理员登录'} <ChevronRight size={14}/></button>{authenticated && !p.demo && <button className="text-button danger" onClick={() => onDelete(p)} title="删除项目"><Trash2 size={13}/>删除</button>}</div>{p.sync_error && <p className="warning-text">{p.sync_error}</p>}</div>; }) : <div className="empty-state"><FileCode2 size={30}/><h3>尚未接入仓库</h3><p>使用管理员入口创建第一个项目，系统会准备独立目录和 Webhook 配置。</p><button className="button primary" onClick={onCreate}><Plus size={16}/>创建项目</button></div>}</section>
    <div className="integration-help"><Code2 size={18}/><p>代码与任务记录由本程序管理。审查/合并仍可由服务端 YAML 配置。</p></div>
  </div>;
}

function Modal({ title, subtitle, onClose, children, wide = false }: { title: string; subtitle?: string; onClose: () => void; children: React.ReactNode; wide?: boolean }) {
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const timer = window.setTimeout(() => dialog.current?.querySelector<HTMLElement>('input, button, [tabindex="0"]')?.focus(), 0);
    function key(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
      if (e.key !== 'Tab') return;
      const items = dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]');
      if (!items?.length) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
    document.addEventListener('keydown', key);
    return () => { window.clearTimeout(timer); document.body.style.overflow = overflow; document.removeEventListener('keydown', key); previous?.focus(); };
  }, [onClose]);
  return <div className="modal-layer"><button className="modal-backdrop" onClick={onClose} aria-label="关闭对话框"/><div ref={dialog} className={`modal-card ${wide ? 'wide' : ''}`} role="dialog" aria-modal="true" aria-labelledby="modal-title"><header className="modal-header"><div><h2 id="modal-title">{title}</h2>{subtitle && <p>{subtitle}</p>}</div><button className="icon-button" onClick={onClose} aria-label="关闭"><X size={20}/></button></header>{children}</div></div>;
}

function LoginFields({ session, onAuthenticated, actions }: { session: AuthSession | null; onAuthenticated: (session: AuthSession) => void; actions: ReactNode }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(e: React.FormEvent) {
    e.preventDefault(); setBusy(true); setError('');
    try { onAuthenticated(await api<AuthSession>('/api/auth/login', jsonRequest('POST', { username, password }))); }
    catch (reason) { setError((reason as Error).message); setBusy(false); }
  }
  return <form className="auth-form" onSubmit={submit}>
    {session && !session.admin_configured && <div className="inline-alert"><AlertTriangle size={18}/><div><strong>管理员账号尚未配置</strong><p>请先在服务端设置管理员用户名和密码，再返回登录。</p></div></div>}
    {error && <div className="form-error" role="alert"><XCircle size={16}/>{error}</div>}
    <label className="field"><span>用户名</span><input value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" required disabled={busy || session?.admin_configured === false} autoFocus /></label>
    <label className="field"><span>密码</span><div className="secret-input"><input type={visible ? 'text' : 'password'} value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" required disabled={busy || session?.admin_configured === false}/><button type="button" className="icon-button" onClick={() => setVisible(x => !x)} aria-label={visible ? '隐藏密码' : '显示密码'}>{visible ? <EyeOff size={17}/> : <Eye size={17}/>}</button></div></label>
    <div className="modal-actions">{actions}<button type="submit" className="button primary" disabled={busy || !username || !password || session?.admin_configured === false}>{busy ? <Loader2 className="spin" size={16}/> : <LockKeyhole size={16}/>}登录</button></div>
  </form>;
}

function LoginGate({ session, onAuthenticated }: { session: AuthSession; onAuthenticated: (session: AuthSession) => void }) {
  return <div className="login-gate-screen">
    <div className="login-gate-card">
      <div className="login-gate-header">
        <span className="brand-mark login-brand-mark"><Layers3 size={23} strokeWidth={2.4} /></span>
        <h1>Flowdeck</h1>
        <p>研发工作流与发布管理</p>
        <span className="gate-subtitle">未登录不能访问任何界面</span>
      </div>
      <LoginFields session={session} onAuthenticated={onAuthenticated} actions={null} />
      <div className="login-gate-footer"><LockKeyhole size={13}/>请使用管理员账号登录</div>
    </div>
  </div>;
}

function LoginDialog({ session, onClose, onAuthenticated }: { session: AuthSession | null; onClose: () => void; onAuthenticated: (session: AuthSession) => void }) {
  return <Modal title="管理员登录" subtitle="登录后可接入和验证项目。" onClose={onClose}><div className="modal-body">
    <LoginFields session={session} onAuthenticated={onAuthenticated} actions={<button type="button" className="button secondary" onClick={onClose}>取消</button>} />
  </div></Modal>;
}

const fallbackEvents = [
  ['issues', 'Issues', '需求、Bug 标签与变更'], ['pull_request', 'Pull requests', 'PR 创建、提交与合并状态'], ['pull_request_review', 'Pull request reviews', '代码审查结论'], ['release', 'Releases', '正式版本发布'],
  ['check_run', 'Check runs', '单项检查结果'], ['check_suite', 'Check suites', '检查套件状态'], ['status', 'Commit statuses', '提交状态变化'], ['workflow_run', 'Workflow runs', 'Actions 工作流结果'],
].map(([value, label, description]) => ({ value, label, description }));

function ProjectWizard({ session, onClose, onCreated }: { session: AuthSession; onClose: () => void; onCreated: (connection: ProjectConnection) => void }) {
  const [options, setOptions] = useState<ProjectOptions | null>(null);
  const [loadError, setLoadError] = useState('');
  const [step, setStep] = useState(0);
  const [name, setName] = useState(''); const [description, setDescription] = useState(''); const [repo, setRepo] = useState('');
  const [storagePath, setStoragePath] = useState(''); const [events, setEvents] = useState<string[]>([]); const [token, setToken] = useState(''); const [tokenVisible, setTokenVisible] = useState(false);
  const [dockerUser, setDockerUser] = useState(''); const [dockerPassword, setDockerPassword] = useState(''); const [dockerVisible, setDockerVisible] = useState(false);
  const [keystoreFile, setKeystoreFile] = useState<File | null>(null); const [keyAlias, setKeyAlias] = useState('');
  const [keystorePassword, setKeystorePassword] = useState(''); const [keyPassword, setKeyPassword] = useState('');
  const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  useEffect(() => { let active = true; api<ProjectOptions>('/api/project-options').then(value => { if (!active) return; setOptions(value); setEvents(value.default_events); }).catch(reason => { if (active) setLoadError((reason as Error).message); }); return () => { active = false; }; }, []);
  const eventOptions = options?.events.length ? options.events : fallbackEvents;
  const repoValid = /^[^/\s]+\/[^/\s]+$/.test(repo.trim());
  const storageValid = /^(?:[A-Za-z]:[\\/]|\\\\|\/)/.test(storagePath.trim());
  const basicsValid = !!name.trim() && repoValid && storageValid;
  const repoDirectory = repo.trim().split('/').pop()?.replace(/[^A-Za-z0-9._-]/g, '-') || 'project';
  const storageSuggestions = options?.storage_roots.map(root => `${root.replace(/[\\/]$/, '')}${root.includes('\\') ? '\\' : '/'}${repoDirectory}`) || [];
  const ciFilled = !!(dockerUser.trim() || dockerPassword.trim() || keystoreFile || keyAlias.trim() || keystorePassword.trim() || keyPassword.trim());
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (step === 0) { if (basicsValid) { setError(''); setStep(1); } return; }
    if (step === 1) {
      if (!events.length) { setError('请至少选择一个 GitHub 事件。'); return; }
      if (!token.trim()) { setError('请填写 GitHub PAT，接入仓库需要它。'); return; }
      setError(''); setStep(2); return;
    }
    if ((dockerUser.trim() || dockerPassword.trim()) && (!dockerUser.trim() || !dockerPassword.trim())) { setError('镜像仓库用户名和密码需要同时填写。'); return; }
    const androidAny = !!(keystoreFile || keyAlias.trim() || keystorePassword.trim() || keyPassword.trim());
    const androidAll = !!(keystoreFile && keyAlias.trim() && keystorePassword.trim() && keyPassword.trim());
    if (androidAny && !androidAll) { setError('安卓签名需要同时提供密钥文件、别名和两个密码。'); return; }
    setBusy(true); setError('');
    try {
      const payload: Record<string, unknown> = { name: name.trim(), description: description.trim(), repo: repo.trim(), storage_path: storagePath.trim(), events, github_token: token.trim() };
      if (dockerUser.trim()) { payload.docker_registry_username = dockerUser.trim(); payload.docker_registry_password = dockerPassword; }
      if (keystoreFile) {
        payload.android_keystore_base64 = await fileToBase64(keystoreFile);
        payload.android_key_alias = keyAlias.trim();
        payload.android_keystore_password = keystorePassword;
        payload.android_key_password = keyPassword;
      }
      onCreated(await api<ProjectConnection>('/api/projects', jsonRequest('POST', payload, session.csrf_token)));
    } catch (reason) { setError((reason as Error).message); setBusy(false); }
  }
  const primaryDisabled = busy || !options || !!loadError || options?.management_enabled === false || (step === 0 ? !basicsValid : step === 1 ? !events.length || !token.trim() : false);
  return <Modal title="创建项目" subtitle="连接 GitHub 仓库，并为执行服务准备独立工作目录。" onClose={onClose} wide><form className="modal-body wizard" onSubmit={submit}>
    <div className="stepper" aria-label={`第 ${step + 1} 步，共 3 步`}><span className={step >= 0 ? 'active' : ''}><b>1</b>项目与存储</span><i/><span className={step >= 1 ? 'active' : ''}><b>2</b>GitHub 连接</span><i/><span className={step === 2 ? 'active' : ''}><b>3</b>CI 凭据（选填）</span></div>
    {loadError && <div className="form-error" role="alert"><XCircle size={16}/>{loadError}</div>}
    {step === 1 && <div className="management-notice"><ShieldCheck size={19}/><div><strong>GitHub Token 权限</strong><p>建议使用只授权当前仓库的 Fine-grained Token。默认流程需要 Metadata、Contents、Issues、Pull requests、Checks、Commit statuses 的读取权限，以及 <strong>Webhooks 读写</strong>（自动创建并订阅事件）。下一步的镜像仓库和安卓签名都是选填；只有要写入这些 Actions Secrets 时，才需要 <strong>Secrets 读写</strong>。审查回写、合并和发布的写权限按服务端启用的功能另行配置。</p></div></div>}
    {options?.management_enabled === false && <div className="inline-alert">{options.mode === 'demo' ? <FlaskConical size={18}/> : <AlertTriangle size={18}/>}<div><strong>{options.mode === 'demo' ? '当前环境只提供流程预览' : '凭据加密尚未配置'}</strong><p>{options.mode === 'demo' ? '你可以浏览全部接入步骤；演示模式不会写入项目配置、创建目录或连接 GitHub。' : '当前是正式环境，但服务端尚未配置凭据加密主密钥。配置后才能保存项目、Token 和 Webhook Secret。'}</p></div></div>}
    {step === 0 && <div className="form-grid"><label className="field"><span>项目名称 <em>必填</em></span><input value={name} onChange={e => setName(e.target.value)} placeholder="例如：移动端应用" required/></label><label className="field"><span>GitHub 仓库 <em>必填</em></span><input value={repo} onChange={e => setRepo(e.target.value)} placeholder="owner/repository" required aria-describedby="repo-help" aria-invalid={!!repo && !repoValid}/><small id="repo-help">填写仓库完整名，例如 acme/mobile-app。</small></label><label className="field full"><span>项目说明</span><textarea value={description} onChange={e => setDescription(e.target.value)} placeholder="这套自动化服务于什么项目？" rows={3}/></label><label className="field full"><span>项目存放目录 <em>必填</em></span><div className="input-icon"><HardDrive size={16}/><input value={storagePath} onChange={e => setStoragePath(e.target.value)} placeholder="选择允许根目录下的项目子目录" list="storage-paths" required aria-invalid={!!storagePath && !storageValid}/></div><datalist id="storage-paths">{storageSuggestions.map(path => <option value={path} key={path}/>)}</datalist><small>{storagePath && !storageValid ? '请输入绝对路径，例如 G:\\workspaces\\mobile-app 或 /srv/workspaces/mobile-app。' : '系统将在此目录准备仓库与隔离工作区；首次拉取代码后目录将锁定。'}</small>{!!options?.storage_roots.length && <div className="allowed-roots"><span>允许的根目录</span>{options.storage_roots.map(root => <code key={root}>{root}</code>)}</div>}</label></div>}
    {step === 1 && <><div className="credential-explainer"><div><KeyRound size={19}/><p><strong>Personal Access Token（PAT）</strong>让 Flowdeck 调用 GitHub API 并自动创建 Webhook。只有填写了镜像或安卓凭据时，才会把它们写入仓库的 Actions Secrets。界面不会再次显示已保存的 Token。</p></div><div><ShieldCheck size={19}/><p><strong>Webhook Secret</strong>用于验证 GitHub 发来的事件，由 Flowdeck 生成并在自动创建 Webhook 时写入 GitHub，一般不必再手动复制。</p></div></div><label className="field"><span>GitHub PAT <em>必填</em></span><div className="secret-input"><input type={tokenVisible ? 'text' : 'password'} value={token} onChange={e => setToken(e.target.value)} placeholder="github_pat_…" autoComplete="off" required={step === 1}/><button type="button" className="icon-button" onClick={() => setTokenVisible(x => !x)} aria-label={tokenVisible ? '隐藏 Token' : '显示 Token'}>{tokenVisible ? <EyeOff size={17}/> : <Eye size={17}/>}</button></div></label><fieldset className="event-picker"><legend>订阅事件 <em>至少选择一个</em></legend><p>默认事件覆盖需求、PR、检查、状态与发布流程。</p><div>{eventOptions.map(event => <label key={event.value} className={events.includes(event.value) ? 'checked' : ''}><input type="checkbox" checked={events.includes(event.value)} onChange={e => setEvents(current => e.target.checked ? [...current, event.value] : current.filter(value => value !== event.value))}/><span><strong>{event.label}</strong><small>{event.description}</small></span></label>)}</div></fieldset></>}
    {step === 2 && <><div className="management-notice"><Upload size={19}/><div><strong>镜像与安卓凭据都是选填</strong><p>不需要推送镜像或打包 APK 的项目可全部留空，点「跳过并创建」。两组互相独立：只推镜像就只填仓库账号，只打 APK 就只填签名。填写后由 Flowdeck 写入当前仓库的 GitHub Actions Secrets，密钥只写到 GitHub，不会再从本界面读回。</p></div></div><div className="ci-secret-groups"><fieldset className="ci-secret-group"><legend>阿里云镜像 <em className="optional">选填</em></legend><div className="form-grid"><label className="field"><span>镜像仓库用户名 <em className="optional">选填</em></span><input value={dockerUser} onChange={e => setDockerUser(e.target.value)} placeholder="不推镜像可留空" autoComplete="off"/></label><label className="field"><span>镜像仓库密码 <em className="optional">选填</em></span><div className="secret-input"><input type={dockerVisible ? 'text' : 'password'} value={dockerPassword} onChange={e => setDockerPassword(e.target.value)} placeholder="不推镜像可留空" autoComplete="new-password"/><button type="button" className="icon-button" onClick={() => setDockerVisible(x => !x)} aria-label={dockerVisible ? '隐藏密码' : '显示密码'}>{dockerVisible ? <EyeOff size={17}/> : <Eye size={17}/>}</button></div></label></div></fieldset><fieldset className="ci-secret-group"><legend>安卓签名 <em className="optional">选填</em></legend><div className="form-grid"><label className="field full"><span>安卓签名密钥文件 <em className="optional">选填</em></span><input type="file" accept=".jks,.keystore" onChange={e => setKeystoreFile(e.target.files?.[0] || null)}/><small>{keystoreFile ? `已选择 ${keystoreFile.name}` : '不打包 APK 可跳过。选择 .jks 或 .keystore 后，将作为 ANDROID_KEYSTORE_BASE64 写入 GitHub。'}</small></label><label className="field"><span>密钥别名 <em className="optional">选填</em></span><input value={keyAlias} onChange={e => setKeyAlias(e.target.value)} placeholder="不打包 APK 可留空" autoComplete="off"/></label><label className="field"><span>密钥库密码 <em className="optional">选填</em></span><input type="password" value={keystorePassword} onChange={e => setKeystorePassword(e.target.value)} placeholder="不打包 APK 可留空" autoComplete="new-password"/></label><label className="field"><span>Key 密码 <em className="optional">选填</em></span><input type="password" value={keyPassword} onChange={e => setKeyPassword(e.target.value)} placeholder="不打包 APK 可留空" autoComplete="new-password"/></label></div></fieldset></div></>}
    {error && <div className="form-error" role="alert"><XCircle size={16}/>{error}</div>}
    <div className="modal-actions"><button type="button" className="button secondary" onClick={step === 0 ? onClose : () => { setStep(current => current - 1); setError(''); }}>{step === 0 ? '取消' : '上一步'}</button><button className="button primary" disabled={primaryDisabled}>{busy ? <Loader2 className="spin" size={16}/> : step < 2 ? <ArrowRight size={16}/> : <Plus size={16}/>} {step < 2 ? '下一步' : options?.management_enabled === false ? '当前环境不可创建' : ciFilled ? '创建并查看接入' : '跳过并创建'}</button></div>
  </form></Modal>;
}

function AgentResults({ details }: { details: Record<string, any> }) {
  const items = Object.entries(details);
  if (!items.length) return <div className="small-empty"><Sparkles size={25}/>还没有 Agent 结构化结果。</div>;
  return <div className="agent-results">{items.map(([id, value]) => {
    const actions = value?.executed || value?.verified;
    const pull = actions?.pull_request;
    const docs = actions?.release_docs;
    return <article className="agent-result-card" key={id}>
      <div className="timeline-heading"><strong>{value?.name || 'Agent 任务'}</strong><Status value={value?.outcome || 'waiting'} small/></div>
      {value?.summary && <p>{value.summary}</p>}
      {value?.error && <p className="warning-text">{value.error}</p>}
      {pull && <p>PR #{pull.number}：{pull.merged ? '已由系统合并' : pull.message || 'GitHub 显示未合并'}</p>}
      {docs && <p>说明文档：{docs.paths?.length ? `${docs.paths.join('、')}` : '无'}{docs.pushed ? `，已推送到 ${docs.branch}` : ''}</p>}
    </article>;
  })}</div>;
}

function AgentConnections({ session }: { session: AuthSession }) {
  const empty = { name: '', base_url: '', api_key: '', model: '', reasoning_effort: '', timeout_seconds: 1800, enabled: true };
  const [agents, setAgents] = useState<AgentConnection[]>([]);
  const [form, setForm] = useState(empty);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [keyVisible, setKeyVisible] = useState(false);
  const load = useCallback(async () => {
    const value = await api<AdminIntegrations>('/api/admin/integrations');
    setAgents(value.agents);
  }, []);
  useEffect(() => { void load().catch(reason => setError((reason as Error).message)); }, [load]);
  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy('save'); setError(''); setNotice('');
    try {
      const path = editing ? `/api/admin/agents/${encodeURIComponent(editing)}` : '/api/admin/agents';
      await api<AgentConnection>(path, jsonRequest(editing ? 'PUT' : 'POST', form, session.csrf_token));
      setForm(empty); setEditing(null); setKeyVisible(false); await load();
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(''); }
  }
  async function test(id: string) {
    setBusy('test-' + id); setError(''); setNotice('');
    try {
      const result = await api<{ ok: boolean; message?: string }>(`/api/admin/agents/${encodeURIComponent(id)}/test`, jsonRequest('POST', undefined, session.csrf_token));
      setNotice(result.message || '已验证模型接口、凭据与模型');
    }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(''); }
  }
  async function remove(id: string) {
    if (!window.confirm('删除这个模型接口？已绑定的事件规则会失败，直到改选其他接口。')) return;
    setBusy('del'); setError(''); setNotice('');
    try { await api(`/api/admin/agents/${encodeURIComponent(id)}`, jsonRequest('DELETE', undefined, session.csrf_token)); if (editing === id) { setEditing(null); setForm(empty); } await load(); }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(''); }
  }
  return <section className="panel agent-pool">
    <div className="panel-heading"><div><h2>模型接口</h2><span className="subtle">审查、发布分析和事件任务都由本地 Copilot CLI 运行时执行，不需要 Copilot 订阅。这里配置它要调用的 Chat Completions 地址、API Key 与模型名。DeepSeek 等 Chat Completions 服务可直连。再到项目接入详情里写「GitHub 发生什么事时让 Agent 做什么」。</span></div></div>
    {error && <div className="form-error" role="alert"><XCircle size={16}/>{error}</div>}
    {notice && <div className="form-ok" role="status"><CheckCircle2 size={16}/>{notice}</div>}
    {agents.map(item => <div className="agent-row" key={item.id}><Sparkles size={16}/><div><strong>{item.name}</strong><small>{item.base_url} · {item.model}{item.reasoning_effort ? ` · ${item.reasoning_effort}` : ''}</small></div><span className={`status ${item.enabled ? 'green' : 'neutral'}`}><i/>{item.enabled ? '启用' : '停用'}</span><button className="text-button" disabled={!!busy} onClick={() => void test(item.id)}>{busy === 'test-' + item.id ? <Loader2 className="spin" size={14}/> : '测试'}</button><button className="text-button" onClick={() => { setEditing(item.id); setForm({ name: item.name, base_url: item.base_url, api_key: '', model: item.model || '', reasoning_effort: item.reasoning_effort || '', timeout_seconds: item.timeout_seconds, enabled: item.enabled }); }}>编辑</button><button className="text-button danger" disabled={!!busy} onClick={() => void remove(item.id)}>删除</button></div>)}
    <form className="form-grid publisher-grid" onSubmit={save}>
      <label className="field"><span>名称</span><input value={form.name} onChange={e => setForm(v => ({ ...v, name: e.target.value }))} placeholder="release-reviewer" required/></label>
      <label className="field"><span>模型接口地址</span><input value={form.base_url} onChange={e => setForm(v => ({ ...v, base_url: e.target.value }))} placeholder="https://api.openai.com/v1" required/><small>写入 Copilot CLI 的第三方 provider 配置，必须提供 /chat/completions。</small></label>
      <label className="field"><span>模型名</span><input value={form.model} onChange={e => setForm(v => ({ ...v, model: e.target.value }))} placeholder="gpt-5 或网关模型别名" required/><small>必须指定模型名。</small></label>
      <label className="field"><span>推理力度</span><select value={form.reasoning_effort} onChange={e => setForm(v => ({ ...v, reasoning_effort: e.target.value }))}><option value="">模型默认</option><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="xhigh">xhigh</option><option value="max">max</option></select></label>
      <label className="field"><span>API Key {editing ? '' : <em>必填</em>}</span><div className="secret-input"><input type={keyVisible ? 'text' : 'password'} value={form.api_key} onChange={e => setForm(v => ({ ...v, api_key: e.target.value }))} placeholder={editing ? '留空则保持已保存的 Key' : 'sk-…'} autoComplete="off" required={!editing}/><button type="button" className="icon-button" onClick={() => setKeyVisible(x => !x)} aria-label="显示 API Key">{keyVisible ? <EyeOff size={17}/> : <Eye size={17}/>}</button></div></label>
      <label className="field"><span>超时（秒）</span><input type="number" min={15} max={3600} value={form.timeout_seconds} onChange={e => setForm(v => ({ ...v, timeout_seconds: Number(e.target.value) || 1800 }))}/></label>
      <label className="switch-row full"><input type="checkbox" checked={form.enabled} onChange={e => setForm(v => ({ ...v, enabled: e.target.checked }))}/><span>启用此模型接口</span></label>
      <div className="toolbar-actions full"><button className="button primary" disabled={!!busy || !form.name.trim()}>{busy === 'save' ? <Loader2 className="spin" size={15}/> : <Plus size={15}/>}{editing ? '保存模型接口' : '添加模型接口'}</button>{editing && <button type="button" className="button secondary" onClick={() => { setEditing(null); setForm(empty); }}>取消编辑</button>}</div>
    </form>
  </section>;
}

function DingTalkChannels({ session, projects }: { session: AuthSession; projects: Project[] }) {
  const empty = { name: '', webhook: '', sign_secret: '', enabled: true, events: [] as string[], project_ids: [] as string[] };
  const [channels, setChannels] = useState<DingTalkChannel[]>([]);
  const [form, setForm] = useState(empty);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [webhookVisible, setWebhookVisible] = useState(false);
  const [secretVisible, setSecretVisible] = useState(false);
  const load = useCallback(async () => {
    const value = await api<AdminIntegrations>('/api/admin/integrations');
    setChannels(value.dingtalk_channels);
  }, []);
  useEffect(() => { void load().catch(reason => setError((reason as Error).message)); }, [load]);
  function resetForm() {
    setForm(empty); setEditing(null); setWebhookVisible(false); setSecretVisible(false);
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy('save'); setError(''); setNotice('');
    try {
      const path = editing ? `/api/admin/dingtalk-channels/${encodeURIComponent(editing)}` : '/api/admin/dingtalk-channels';
      await api<DingTalkChannel>(path, jsonRequest(editing ? 'PUT' : 'POST', {
        name: form.name, webhook: form.webhook, sign_secret: form.sign_secret,
        enabled: form.enabled, events: form.events, project_ids: form.project_ids,
      }, session.csrf_token));
      resetForm(); await load();
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(''); }
  }
  async function test(id: string) {
    setBusy('test-' + id); setError(''); setNotice('');
    try {
      const result = await api<{ ok: boolean; message?: string }>(`/api/admin/dingtalk-channels/${encodeURIComponent(id)}/test`, jsonRequest('POST', undefined, session.csrf_token));
      setNotice(result.message || '已向该渠道发送测试消息');
    }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(''); }
  }
  async function remove(id: string) {
    if (!window.confirm('删除这个钉钉渠道？')) return;
    setBusy('del'); setError(''); setNotice('');
    try { await api(`/api/admin/dingtalk-channels/${encodeURIComponent(id)}`, jsonRequest('DELETE', undefined, session.csrf_token)); if (editing === id) resetForm(); await load(); }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(''); }
  }
  const silent = !form.events.length || !form.project_ids.length;
  return <section className="panel agent-pool">
    <div className="panel-heading"><div><h2>钉钉渠道</h2><span className="subtle">把审查、合并、失败和看板变化发到钉钉群自定义机器人。Webhook 与加签密钥加密保存；空事件或空项目的渠道不会发出消息。</span></div></div>
    {error && <div className="form-error" role="alert"><XCircle size={16}/>{error}</div>}
    {notice && <div className="form-ok" role="status"><CheckCircle2 size={16}/>{notice}</div>}
    {channels.map(item => {
      const eventLabels = DINGTALK_EVENT_OPTIONS.filter(([id]) => item.events.includes(id)).map(([, label]) => label);
      const projectNames = projects.filter(project => item.project_ids.includes(project.id)).map(project => project.name);
      return <div className="agent-row" key={item.id}><Bell size={16}/><div><strong>{item.name}</strong><small>{eventLabels.join('、') || '未选事件'} · {projectNames.join('、') || '未选项目'}</small></div><span className={`status ${item.enabled ? 'green' : 'neutral'}`}><i/>{item.enabled ? '启用' : '停用'}</span><button className="text-button" disabled={!!busy} onClick={() => void test(item.id)}>{busy === 'test-' + item.id ? <Loader2 className="spin" size={14}/> : '测试'}</button><button className="text-button" onClick={() => { setEditing(item.id); setForm({ name: item.name, webhook: '', sign_secret: '', enabled: item.enabled, events: item.events, project_ids: item.project_ids }); setWebhookVisible(false); setSecretVisible(false); }}>编辑</button><button className="text-button danger" disabled={!!busy} onClick={() => void remove(item.id)}>删除</button></div>;
    })}
    <form className="form-grid publisher-grid" onSubmit={save}>
      <label className="field"><span>名称</span><input value={form.name} onChange={e => setForm(v => ({ ...v, name: e.target.value }))} placeholder="开发群" required/></label>
      <label className="field"><span>Webhook {editing ? '' : <em>必填</em>}</span><div className="secret-input"><input type={webhookVisible ? 'text' : 'password'} value={form.webhook} onChange={e => setForm(v => ({ ...v, webhook: e.target.value }))} placeholder={editing ? '留空则保持已保存的 Webhook' : 'https://oapi.dingtalk.com/robot/send?access_token=…'} autoComplete="off" required={!editing}/><button type="button" className="icon-button" onClick={() => setWebhookVisible(x => !x)} aria-label={webhookVisible ? '隐藏 Webhook' : '显示 Webhook'}>{webhookVisible ? <EyeOff size={17}/> : <Eye size={17}/>}</button></div></label>
      <label className="field"><span>SEC {editing ? '' : <em>必填</em>}</span><div className="secret-input"><input type={secretVisible ? 'text' : 'password'} value={form.sign_secret} onChange={e => setForm(v => ({ ...v, sign_secret: e.target.value }))} placeholder={editing ? '留空则保持已保存的密钥' : 'SEC…'} autoComplete="off" required={!editing}/><button type="button" className="icon-button" onClick={() => setSecretVisible(x => !x)} aria-label={secretVisible ? '隐藏 SEC' : '显示 SEC'}>{secretVisible ? <EyeOff size={17}/> : <Eye size={17}/>}</button></div></label>
      <label className="switch-row full"><input type="checkbox" checked={form.enabled} onChange={e => setForm(v => ({ ...v, enabled: e.target.checked }))}/><span>启用此渠道</span></label>
      <fieldset className="event-picker full"><legend>事件</legend><div>{DINGTALK_EVENT_OPTIONS.map(([id, label]) => <label key={id} className={form.events.includes(id) ? 'checked' : ''}><input type="checkbox" checked={form.events.includes(id)} onChange={e => setForm(v => ({ ...v, events: e.target.checked ? [...v.events, id] : v.events.filter(item => item !== id) }))}/><span><strong>{label}</strong></span></label>)}</div></fieldset>
      <fieldset className="event-picker full"><legend>项目</legend><div>{projects.map(project => <label key={project.id} className={form.project_ids.includes(project.id) ? 'checked' : ''}><input type="checkbox" checked={form.project_ids.includes(project.id)} onChange={e => setForm(v => ({ ...v, project_ids: e.target.checked ? [...v.project_ids, project.id] : v.project_ids.filter(item => item !== project.id) }))}/><span><strong>{project.name}</strong><small>{project.repo}</small></span></label>)}</div>{!projects.length && <p className="muted-note">暂无非演示项目可选。</p>}</fieldset>
      {silent && <p className="muted-note full">当前不会发出任何消息</p>}
      <div className="toolbar-actions full"><button className="button primary" disabled={!!busy || !form.name.trim()}>{busy === 'save' ? <Loader2 className="spin" size={15}/> : <Plus size={15}/>}{editing ? '保存渠道' : '添加渠道'}</button>{editing && <button type="button" className="button secondary" onClick={resetForm}>取消编辑</button>}</div>
    </form>
  </section>;
}

function SkillLibrary({ session }: { session: AuthSession }) {
  const [skills, setSkills] = useState<UploadedSkill[]>([]);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const uploadRef = useRef<HTMLInputElement>(null);
  const replaceRefs = useRef<Record<string, HTMLInputElement | null>>({});
  const load = useCallback(async () => {
    const value = await api<AdminSkills>('/api/admin/skills');
    setSkills(value.skills);
  }, []);
  useEffect(() => { void load().catch(reason => setError((reason as Error).message)); }, [load]);
  async function upload(file: File, skillId?: string) {
    setBusy(skillId ? 'put-' + skillId : 'upload'); setError(''); setNotice('');
    try {
      const path = skillId ? `/api/admin/skills/${encodeURIComponent(skillId)}` : '/api/admin/skills';
      await api<UploadedSkill>(path, fileRequest(skillId ? 'PUT' : 'POST', file, session.csrf_token));
      setNotice(skillId ? '已替换 skill zip' : '已上传 skill');
      await load();
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(''); }
  }
  async function remove(id: string) {
    if (!window.confirm('删除这个 skill？已勾选它的项目需要先取消勾选。')) return;
    setBusy('del'); setError(''); setNotice('');
    try { await api(`/api/admin/skills/${encodeURIComponent(id)}`, jsonRequest('DELETE', undefined, session.csrf_token)); await load(); }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(''); }
  }
  return <section className="panel agent-pool">
    <div className="panel-heading"><div><h2>Skill</h2><span className="subtle">上传包含 SKILL.md 的 zip（一层目录，或 zip 根就是 SKILL.md）。接入详情里的「总结并发布」和「事件任务」只从这份列表勾选；Worker 只注入已勾选的正文。</span></div></div>
    {error && <div className="form-error" role="alert"><XCircle size={16}/>{error}</div>}
    {notice && <div className="form-ok" role="status"><CheckCircle2 size={16}/>{notice}</div>}
    {skills.map(item => <div className="agent-row" key={item.id}><FileText size={16}/><div><strong>{item.label}</strong><small>{item.slug}{item.files?.length ? ` · ${item.files.join('、')}` : ''}</small></div>
      <input ref={node => { replaceRefs.current[item.id] = node; }} type="file" accept=".zip,application/zip" hidden onChange={e => { const file = e.target.files?.[0]; e.target.value = ''; if (file) void upload(file, item.id); }}/>
      <button className="text-button" disabled={!!busy} onClick={() => replaceRefs.current[item.id]?.click()}>{busy === 'put-' + item.id ? <Loader2 className="spin" size={14}/> : '替换'}</button>
      <button className="text-button danger" disabled={!!busy} onClick={() => void remove(item.id)}>删除</button>
    </div>)}
    {!skills.length && <p className="muted-note">尚未上传 skill。把 skill 目录打成 zip 后上传。</p>}
    <div className="toolbar-actions">
      <input ref={uploadRef} type="file" accept=".zip,application/zip" hidden onChange={e => { const file = e.target.files?.[0]; e.target.value = ''; if (file) void upload(file); }}/>
      <button className="button primary" type="button" disabled={!!busy} onClick={() => uploadRef.current?.click()}>{busy === 'upload' ? <Loader2 className="spin" size={15}/> : <Upload size={15}/>}上传 zip</button>
    </div>
  </section>;
}

function blankAgentRule(agentId = ''): AgentRule {
  return { id: '', name: '', event: 'pull_request', agent_id: agentId, prompt: '', include_repository: false, include_github_credentials: false, skill_ids: [], enabled: true };
}

function agentOptionLabel(item: AgentConnection) {
  return item.model ? `${item.name} · ${item.model}` : item.name;
}

function blankPrepareRelease(agentId = ''): PrepareRelease {
  return { enabled: false, agent_id: agentId, prompt: '按已选 Skill 总结当前功能并改文件。完成后列出变更路径并请求推送。', skill_ids: [] };
}

function splitNames(value: string) {
  return value.split(/[\n,]+/).map(item => item.trim()).filter(Boolean);
}

function AgentRuleSettings({ connection, session, busy, disabled, onBusy, onError, onSaved }: {
  connection: ProjectConnection; session: AuthSession; busy: string; disabled: boolean;
  onBusy: (value: string) => void; onError: (value: string) => void; onSaved: (value: ProjectConnection) => void;
}) {
  const cfg = connection.automation_config;
  const savedKey = JSON.stringify(cfg?.agent_rules || []);
  const savedPrepare = JSON.stringify(cfg?.prepare_release || blankPrepareRelease(cfg?.agent_id || ''));
  const savedPolicy = JSON.stringify({
    review_enabled: !!cfg?.review_enabled,
    merge_enabled: !!cfg?.merge_enabled,
    required_checks: cfg?.required_checks || [],
    merge_method: cfg?.merge_method || 'squash',
    managers: cfg?.managers || [],
    previous_tag: cfg?.previous_tag || '',
  });
  const [agentId, setAgentId] = useState(cfg?.agent_id || '');
  const [reviewEnabled, setReviewEnabled] = useState(!!cfg?.review_enabled);
  const [mergeEnabled, setMergeEnabled] = useState(!!cfg?.merge_enabled);
  const [requiredChecks, setRequiredChecks] = useState((cfg?.required_checks || []).join('\n'));
  const [mergeMethod, setMergeMethod] = useState(cfg?.merge_method || 'squash');
  const [managers, setManagers] = useState((cfg?.managers || []).join(', '));
  const [previousTag, setPreviousTag] = useState(cfg?.previous_tag || '');
  const [rules, setRules] = useState<AgentRule[]>(cfg?.agent_rules || []);
  const [prepare, setPrepare] = useState<PrepareRelease>(cfg?.prepare_release || blankPrepareRelease(cfg?.agent_id || ''));
  const [agents, setAgents] = useState<AgentConnection[]>([]);
  useEffect(() => { setAgentId(cfg?.agent_id || ''); }, [cfg?.agent_id]);
  useEffect(() => {
    const policy = JSON.parse(savedPolicy) as { review_enabled: boolean; merge_enabled: boolean; required_checks: string[]; merge_method: string; managers: string[]; previous_tag: string };
    setReviewEnabled(policy.review_enabled);
    setMergeEnabled(policy.merge_enabled);
    setRequiredChecks(policy.required_checks.join('\n'));
    setMergeMethod(policy.merge_method);
    setManagers(policy.managers.join(', '));
    setPreviousTag(policy.previous_tag);
  }, [savedPolicy]);
  useEffect(() => { setRules(JSON.parse(savedKey) as AgentRule[]); }, [savedKey]);
  useEffect(() => { setPrepare(JSON.parse(savedPrepare) as PrepareRelease); }, [savedPrepare]);
  useEffect(() => { api<AdminIntegrations>('/api/admin/integrations').then(value => setAgents(value.agents.filter(item => item.enabled))).catch(() => setAgents([])); }, []);
  const skills = connection.skills || [];
  const needsReviewAgent = reviewEnabled || prepare.enabled;
  function addRule() {
    setRules(current => [...current, blankAgentRule(agentId || agents[0]?.id || '')]);
  }
  function patch(index: number, update: Partial<AgentRule>) {
    setRules(current => current.map((item, i) => i === index ? { ...item, ...update } : item));
  }
  function patchPrepare(update: Partial<PrepareRelease>) {
    setPrepare(current => ({ ...current, ...update }));
  }
  async function save(event: React.FormEvent) {
    event.preventDefault();
    onBusy('agent-rules'); onError('');
    try {
      const saved = await api<ProjectConnection>(`/api/projects/${encodeURIComponent(connection.id)}/automation`, jsonRequest('PUT', {
        agent_id: agentId,
        review_enabled: reviewEnabled,
        merge_enabled: reviewEnabled && mergeEnabled,
        required_checks: splitNames(requiredChecks),
        merge_method: mergeMethod,
        managers: splitNames(managers),
        previous_tag: previousTag,
        prepare_release: {
          ...prepare,
          agent_id: prepare.agent_id || agentId || agents[0]?.id || '',
        },
        agent_rules: rules.map(rule => ({
          ...rule,
          include_repository: true,
          include_github_credentials: rule.event !== 'issues',
        })),
      }, session.csrf_token));
      onSaved(saved);
    } catch (reason) { onError((reason as Error).message); }
    finally { onBusy(''); }
  }
  return <form className="connection-section" onSubmit={save}>
    <div className="connection-section-title"><div><span className="tiny-label">AGENT</span><h3>Agent</h3></div></div>
    <p>模型接口和 API Key 在「集成配置」维护。GitHub PAT 只给 Flowdeck 用，不会注入 Agent 运行时。</p>
    {!agents.length && <p className="warning-text">请先在集成配置中添加并启用模型接口。</p>}
    <div className="automation-card-block">
      <div className="automation-card-header"><div><h4>内置审查</h4><p>固定功能，不能删除。启用后，收到 PR 会自动审查；不必为此添加事件任务。</p></div><span className={`status ${reviewEnabled ? 'green' : 'orange'}`}><i/>{reviewEnabled ? '已启用' : '未启用'}</span></div>
      <label className="switch-row"><input type="checkbox" checked={reviewEnabled} onChange={e => { const on = e.target.checked; setReviewEnabled(on); if (on && !agentId) setAgentId(agents[0]?.id || ''); if (!on) setMergeEnabled(false); }} disabled={disabled}/><span><strong>启用内置审查</strong><small>项目已订阅 pull_request 时，新开或更新的非草稿 PR 会排队审查。</small></span></label>
      {reviewEnabled && <div className="form-grid">
        <label className="field full"><span>模型接口</span><select value={agentId} onChange={e => setAgentId(e.target.value)} required aria-label="内置审查使用的模型接口"><option value="">选择模型接口</option>{agents.map(item => <option value={item.id} key={item.id}>{agentOptionLabel(item)}</option>)}</select></label>
        {needsReviewAgent && !agentId && <p className="warning-text">启用内置审查后必须选择模型接口，否则任务会停在等待配置。</p>}
        <label className="field full"><span>钉钉通知人</span><input value={managers} onChange={e => setManagers(e.target.value)} placeholder="手机号，逗号分隔，可空" aria-label="钉钉通知人"/></label>
        <label className="switch-row full"><input type="checkbox" checked={mergeEnabled} onChange={e => setMergeEnabled(e.target.checked)} disabled={disabled}/><span><strong>审查通过后自动合并</strong><small>没有 GitHub CI 时把检查名留空，审查通过就会合并。</small></span></label>
        {mergeEnabled && <label className="field"><span>必需的 GitHub 检查（可空）</span><textarea rows={3} value={requiredChecks} onChange={e => setRequiredChecks(e.target.value)} placeholder="没有 CI 请留空。若有，填写 PR 页 Checks 名称，多个换行或逗号分隔"/></label>}
        {mergeEnabled && <label className="field"><span>合并方式</span><select value={mergeMethod} onChange={e => setMergeMethod(e.target.value)} aria-label="合并方式"><option value="squash">squash</option><option value="merge">merge</option><option value="rebase">rebase</option></select></label>}
      </div>}
    </div>
    <div className="automation-card-block">
      <div className="automation-card-header"><div><h4>总结并发布</h4><p>固定功能，不能删除。启用后看板才会出现「总结并发布」按钮。</p></div><span className={`status ${prepare.enabled ? 'green' : 'orange'}`}><i/>{prepare.enabled ? '已启用' : '未启用'}</span></div>
      <label className="switch-row"><input type="checkbox" checked={prepare.enabled} onChange={e => patchPrepare({ enabled: e.target.checked, agent_id: prepare.agent_id || agentId || agents[0]?.id || '' })} disabled={disabled}/><span><strong>启用总结并发布</strong><small>Agent 按下方说明和技能改仓库；Flowdeck 提交、推送到默认主分支、打 tag 并创建 GitHub Release。</small></span></label>
      {prepare.enabled && <div className="form-grid">
        <label className="field full"><span>模型接口</span><select value={prepare.agent_id} onChange={e => patchPrepare({ agent_id: e.target.value })} required><option value="">选择模型接口</option>{agents.map(item => <option value={item.id} key={item.id}>{agentOptionLabel(item)}</option>)}</select></label>
        <label className="field full"><span>任务说明</span><textarea rows={4} value={prepare.prompt} onChange={e => patchPrepare({ prompt: e.target.value })} placeholder="按已选 Skill 总结当前功能并改文件。完成后列出变更路径并请求推送。" required/></label>
        <label className="field"><span>首次发版对比 Tag</span><input value={previousTag} onChange={e => setPreviousTag(e.target.value)} placeholder="仓库还没有正式 Release 时填写，例如 v1.0.0"/></label>
      </div>}
      {prepare.enabled && <fieldset className="event-picker"><legend>附带的 Skill</legend>
        {skills.length ? <div>{skills.map(item => <label key={item.id} className={(prepare.skill_ids || []).includes(item.id) ? 'checked' : ''}><input type="checkbox" checked={(prepare.skill_ids || []).includes(item.id)} onChange={e => patchPrepare({ skill_ids: e.target.checked ? [...(prepare.skill_ids || []), item.id] : (prepare.skill_ids || []).filter(id => id !== item.id) })} disabled={disabled}/><span><strong>{item.label}</strong></span></label>)}</div> : <p>请先在集成配置上传 skill zip</p>}
      </fieldset>}
    </div>
    <div className="automation-card-block">
      <div className="automation-card-header"><div><h4>事件任务</h4><p>额外的 Agent 任务。内置审查请用上方固定功能，不要靠添加 PR 事件来开启。</p></div><button type="button" className="button secondary" disabled={disabled} onClick={addRule}><Plus size={14}/>添加任务</button></div>
      {rules.map((rule, index) => <div className="agent-rule-card" key={rule.id || `new-${index}`}>
        <div className="form-grid">
        <label className="field"><span>名称</span><input value={rule.name} onChange={e => patch(index, { name: e.target.value })} placeholder="审查后合并" required/></label>
        <label className="field"><span>GitHub 事件</span><select value={rule.event} onChange={e => patch(index, { event: e.target.value as AgentRule['event'] })}><option value="issues">Issue</option><option value="pull_request">Pull request</option><option value="release">Release</option></select></label>
        <label className="field full"><span>模型接口</span><select value={rule.agent_id} onChange={e => patch(index, { agent_id: e.target.value })} required><option value="">选择模型接口</option>{agents.map(item => <option value={item.id} key={item.id}>{agentOptionLabel(item)}</option>)}</select></label>
        <label className="field full"><span>任务说明</span><textarea rows={4} value={rule.prompt} onChange={e => patch(index, { prompt: e.target.value })} placeholder="审查这个 PR。通过则合并。" required/></label>
        </div>
        {!(connection.events || []).includes(rule.event) && rule.event !== 'release' && <p className="warning-text">当前项目 Webhook 未订阅「{rule.event}」，这条规则不会收到事件。</p>}
        <p className="muted-note">{rule.event === 'issues' ? 'Issue 任务只做分析，不会改 GitHub。' : rule.event === 'pull_request' ? '这是额外任务，不会代替上方的内置审查。若 Agent 请求合并，由 Flowdeck 用项目 PAT 执行。' : 'GitHub Release 事件到来时额外跑 Agent。看板「总结并发布」请用上方固定功能。'}</p>
        <fieldset className="event-picker"><legend>附带的 Skill</legend>
          {skills.length ? <div>{skills.map(item => <label key={item.id} className={(rule.skill_ids || []).includes(item.id) ? 'checked' : ''}><input type="checkbox" checked={(rule.skill_ids || []).includes(item.id)} onChange={e => patch(index, { skill_ids: e.target.checked ? [...(rule.skill_ids || []), item.id] : (rule.skill_ids || []).filter(id => id !== item.id) })} disabled={disabled}/><span><strong>{item.label}</strong></span></label>)}</div> : <p>请先在集成配置上传 skill zip</p>}
        </fieldset>
        <label className="switch-row"><input type="checkbox" checked={rule.enabled} onChange={e => patch(index, { enabled: e.target.checked })} disabled={disabled}/><span>启用这条规则</span></label>
        <button type="button" className="text-button danger" onClick={() => setRules(current => current.filter((_, i) => i !== index))}>删除规则</button>
      </div>)}
      {!rules.length && <p className="muted-note">尚未添加事件任务。</p>}
    </div>
    <div className="toolbar-actions"><button className="button primary" disabled={disabled || !!busy}>{busy === 'agent-rules' ? <Loader2 className="spin" size={15}/> : <Check size={15}/>}保存 Agent 配置</button></div>
  </form>;
}

function ConnectionDialog({ projectId, session, onClose, onChanged, onDeleted }: { projectId: string; session: AuthSession; onClose: () => void; onChanged: () => void; onDeleted?: (repo: string) => void }) {
  const [connection, setConnection] = useState<ProjectConnection | null>(null); const [error, setError] = useState(''); const [busy, setBusy] = useState('');
  const [secret, setSecret] = useState(''); const [secretVisible, setSecretVisible] = useState(false); const [copied, setCopied] = useState('');
  const [token, setToken] = useState(''); const [storagePath, setStoragePath] = useState(''); const initialized = useRef(false);
  const [deleteFiles, setDeleteFiles] = useState(true);
  async function deleteCurrentProject() { if (!window.confirm(`确定要彻底删除项目「${connection?.name || projectId}」吗？此操作无法撤销。${deleteFiles ? ' 将同时物理清理服务器存储目录。' : ''}`)) return; setBusy('delete'); setError(''); try { await api<{ ok: boolean; message: string }>(`/api/projects/${encodeURIComponent(projectId)}?delete_files=${deleteFiles}`, jsonRequest('DELETE', undefined, session.csrf_token)); onDeleted?.(connection?.repo || ''); onClose(); } catch (reason) { setError((reason as Error).message); setBusy(''); } }
  const load = useCallback(async () => { try { const value = await api<ProjectConnection>(`/api/projects/${encodeURIComponent(projectId)}/connection`); setConnection(value); setError(''); if (!initialized.current) { setStoragePath(value.storage_path); initialized.current = true; } } catch (reason) { setError((reason as Error).message); } }, [projectId]);
  useEffect(() => { void load(); const timer = window.setInterval(() => void load(), 3000); return () => window.clearInterval(timer); }, [load]);
  async function reveal() { setBusy('secret'); setError(''); try { const value = await api<{ secret: string }>(`/api/projects/${encodeURIComponent(projectId)}/webhook-secret/reveal`, jsonRequest('POST', undefined, session.csrf_token)); setSecret(value.secret); setSecretVisible(true); } catch (reason) { setError((reason as Error).message); } finally { setBusy(''); } }
  async function copy(value: string, key: string) { try { await navigator.clipboard.writeText(value); setCopied(key); window.setTimeout(() => setCopied(''), 1800); } catch { setError('浏览器未允许复制，请手动选择文本。'); } }
  async function saveToken(e: React.FormEvent) { e.preventDefault(); if (!token) return; setBusy('token'); setError(''); try { setConnection(await api<ProjectConnection>(`/api/projects/${encodeURIComponent(projectId)}/github-token`, jsonRequest('PUT', { github_token: token }, session.csrf_token))); setToken(''); onChanged(); } catch (reason) { setError((reason as Error).message); } finally { setBusy(''); } }
  async function saveStorage(e: React.FormEvent) { e.preventDefault(); if (!storagePath) return; setBusy('storage'); setError(''); try { setConnection(await api<ProjectConnection>(`/api/projects/${encodeURIComponent(projectId)}/storage`, jsonRequest('PUT', { storage_path: storagePath }, session.csrf_token))); onChanged(); } catch (reason) { setError((reason as Error).message); } finally { setBusy(''); } }
  async function verify() { setBusy('verify'); setError(''); try { setConnection(await api<ProjectConnection>(`/api/projects/${encodeURIComponent(projectId)}/verify`, jsonRequest('POST', undefined, session.csrf_token))); onChanged(); } catch (reason) { setError((reason as Error).message); } finally { setBusy(''); } }
  const readiness = connection ? [{ label: '工作目录已准备', ready: connection.storage_status === 'ready', detail: connection.storage_error }, { label: 'GitHub API 可访问', ready: connection.api_status === 'ready', detail: connection.api_error || (connection.api_status === 'missing' ? '等待配置 PAT' : null) }, { label: 'Webhook ping 已验证', ready: connection.webhook_status === 'ready', detail: connection.last_delivery_at ? `GitHub ping 接收于 ${new Date(connection.last_delivery_at).toLocaleString('zh-CN')}` : '等待 GitHub 自动发送 ping' }] : [];
  return <Modal title={connection?.name || '项目接入详情'} subtitle={connection?.repo || '正在读取接入状态…'} onClose={onClose} wide><div className="modal-body connection-body">
    {error && <div className="form-error" role="alert"><XCircle size={16}/>{error}</div>}
    {!connection ? <div className="loading-state"><Loader2 className="spin"/>加载接入详情…</div> : <>
      <div className="connection-section-title"><span className={`status ${connection.status === 'active' ? 'green' : 'orange'}`}><i/>{connection.status === 'active' ? '已激活' : '等待接入完成'}</span><span className="subtle">{connection.status === 'active' ? '事件通道与仓库访问已就绪' : '完成下方三项验证后自动激活'}</span></div>
      {connection.status === 'demo' && <div className="inline-alert"><FlaskConical size={18}/><div><strong>演示项目</strong><p>此页面用于预览接入步骤，管理操作不会在演示环境执行。</p></div></div>}
      <section className="readiness"><div className="connection-section-title"><div><span className="tiny-label">READINESS</span><h3>接入准备度</h3></div><button className="button secondary" onClick={() => void verify()} disabled={!!busy || connection.status === 'demo'}>{busy === 'verify' ? <Loader2 className="spin" size={15}/> : <RefreshCw size={15}/>}重新验证</button></div><div className="readiness-grid">{readiness.map(item => <div className={item.ready ? 'ready' : ''} key={item.label}>{item.ready ? <CheckCircle2 size={20}/> : <Circle size={20}/>}<strong>{item.label}</strong><small>{item.ready ? '已就绪' : item.detail || '正在准备'}</small></div>)}</div><p className="readiness-note">工作目录、GitHub API 和 GitHub 自动发送的一次有效 ping 测试事件全部就绪后，项目才算完成接入。</p></section>
      {!connection.public_url_configured && <div className="inline-alert"><AlertTriangle size={18}/><div><strong>尚未配置公网 Webhook 基础地址</strong><p>请由服务管理员配置公网入口，之后重新验证此项目。</p></div></div>}
      <section className="connection-section"><div className="connection-section-title"><div><span className="tiny-label">GITHUB WEBHOOK</span><h3>{connection.webhook_registered ? '已自动注册 Webhook' : 'Webhook'}</h3></div>{connection.github_settings_url && <SafeLink url={connection.github_settings_url}>打开仓库 Webhooks <ExternalLink size={14}/></SafeLink>}</div>{connection.webhook_registered ? <p>Flowdeck 已用 PAT 在 GitHub 创建 Webhook，并订阅本项目勾选的事件。GitHub 会自动发送 ping；三项验证通过后项目激活。一般不必再去 GitHub 手动填写。</p> : <ol className="setup-checklist"><li><span>1</span><p>在 GitHub 仓库的 <strong>Settings → Webhooks → Add webhook</strong> 中粘贴 Payload URL。</p></li><li><span>2</span><p>Content type 选择 <code>application/json</code>，粘贴下方 Webhook Secret，并启用 SSL 验证。</p></li><li><span>3</span><p>选择 “Let me select individual events”，勾选本项目列出的事件并保存；GitHub 会自动发送 ping 测试事件完成验证。</p></li></ol>}<CopyRow label="Payload URL" value={connection.webhook_url || '等待公网地址配置'} disabled={!connection.webhook_url} copied={copied === 'url'} onCopy={() => connection.webhook_url && void copy(connection.webhook_url, 'url')}/><div className="secret-row"><div><span>Webhook Secret</span><small>由 Flowdeck 生成，用于验证事件来源；它不是 GitHub PAT。</small></div>{secret ? <div className="secret-value"><code>{secretVisible ? secret : '•'.repeat(Math.min(28, secret.length))}</code><button className="icon-button" onClick={() => setSecretVisible(x => !x)} aria-label={secretVisible ? '隐藏 Secret' : '显示 Secret'}>{secretVisible ? <EyeOff size={16}/> : <Eye size={16}/>}</button><button className="icon-button" onClick={() => void copy(secret, 'secret')} aria-label="复制 Secret">{copied === 'secret' ? <Check size={16}/> : <Copy size={16}/>}</button></div> : <button className="button secondary" onClick={() => void reveal()} disabled={busy === 'secret' || connection.status === 'demo'}>{busy === 'secret' ? <Loader2 className="spin" size={15}/> : <Eye size={15}/>}显示 Secret</button>}</div><div className="event-summary"><strong>已订阅的事件</strong><div>{connection.events.map(event => <code key={event}>{event}</code>)}</div></div></section>
      <section className="connection-section two-column"><form onSubmit={saveToken}><div className="connection-section-title"><div><span className="tiny-label">GITHUB API</span><h3>Personal Access Token</h3></div><span className={`status ${connection.token_configured ? 'green' : 'orange'}`}><i/>{connection.token_configured ? '已保存' : '待配置'}</span></div><p>供 Flowdeck 访问 GitHub API、注册 Webhook，以及 Agent 任务结束后合并 PR / 推送文档。不会交给 Agent 运行时。保存后只能替换，不能从界面读回。</p><div className="inline-form"><input type="password" value={token} onChange={e => setToken(e.target.value)} placeholder={connection.token_configured ? '输入新 Token 以替换' : 'github_pat_…'} autoComplete="off" aria-label="新的 GitHub PAT"/><button className="button secondary" disabled={!token || !!busy || connection.status === 'demo'}>{busy === 'token' ? <Loader2 className="spin" size={15}/> : <KeyRound size={15}/>}保存</button></div></form><form onSubmit={saveStorage}><div className="connection-section-title"><div><span className="tiny-label">STORAGE</span><h3>项目存放目录</h3></div>{connection.storage_locked && <span className="lock-label"><LockKeyhole size={13}/>已锁定</span>}</div><p>仓库目录：<code>{connection.repository_path || '正在准备'}</code><br/>工作区目录：<code>{connection.workspaces_path || '正在准备'}</code></p><div className="inline-form"><input value={storagePath} onChange={e => setStoragePath(e.target.value)} disabled={connection.storage_locked} aria-label="项目存放目录"/><button className="button secondary" disabled={!storagePath || connection.storage_locked || !!busy}>{busy === 'storage' ? <Loader2 className="spin" size={15}/> : <HardDrive size={15}/>}保存</button></div></form></section>
      {session.authenticated && connection.status !== 'demo' && <AgentRuleSettings connection={connection} session={session} busy={busy} disabled={!!busy} onBusy={setBusy} onError={setError} onSaved={value => { setConnection(value); onChanged(); }}/>}
      {connection.status !== 'demo' && session.authenticated && <section className="connection-section danger-zone"><div className="connection-section-title"><div><span className="tiny-label danger">DANGER ZONE</span><h3>删除项目</h3></div></div><div className="danger-box"><AlertTriangle size={18}/><div><strong>警告：此操作不可逆</strong><p>删除后将永久清理该项目在 Flowdeck 中的所有数据，包括任务执行记录、时间线与接入凭据。</p></div></div><label className="confirm-check"><input type="checkbox" checked={deleteFiles} onChange={e => setDeleteFiles(e.target.checked)}/><span><strong>同时删除本地存储目录与工作区文件</strong><small>勾选后将物理删除服务器存储路径（{connection.storage_path || '未指定'}）及隔离工作区。</small></span></label><div className="toolbar-actions"><button type="button" className="button danger" disabled={busy === 'delete'} onClick={() => void deleteCurrentProject()}>{busy === 'delete' ? <Loader2 className="spin" size={15}/> : <Trash2 size={15}/>}删除此项目</button></div></section>}
      <div className="modal-actions"><span className="auto-refresh"><RefreshCw size={13}/>打开期间每 3 秒更新状态</span><button className="button primary" onClick={onClose}>完成</button></div>
    </>}
  </div></Modal>;
}

function CopyRow({ label, value, disabled, copied, onCopy }: { label: string; value: string; disabled?: boolean; copied: boolean; onCopy: () => void }) {
  return <div className="copy-row"><span>{label}</span><code>{value}</code><button className="icon-button" disabled={disabled} onClick={onCopy} aria-label={`复制${label}`}>{copied ? <Check size={16}/> : <Copy size={16}/>}</button></div>;
}


function PrepareReleaseDialog({ session, projects, selectedRepo, onClose, onQueued }: {
  session: AuthSession; projects: Project[]; selectedRepo: string;
  onClose: () => void; onQueued: (recordId: string) => void;
}) {
  const initial = projects.find(item => item.repo === selectedRepo)?.id || (projects.length === 1 ? projects[0].id : '');
  const [projectId, setProjectId] = useState(initial);
  const [tag, setTag] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!projectId) { setError('请选择项目'); return; }
    setBusy(true); setError('');
    try {
      const created = await api<{ id: string; record_id: string; status: string }>(
        `/api/projects/${encodeURIComponent(projectId)}/prepare-release`,
        jsonRequest('POST', { tag: tag.trim() }, session.csrf_token),
      );
      onQueued(created.record_id);
    } catch (reason) {
      setError((reason as Error).message);
      setBusy(false);
    }
  }
  return <Modal title="总结并发布" subtitle="Agent 按项目接入中的总结并发布配置改仓库，随后推到默认主分支并创建 GitHub Release。" onClose={onClose}>
    <form className="modal-body" onSubmit={submit}>
      {error && <div className="form-error" role="alert"><XCircle size={16}/>{error}</div>}
      <label className="field"><span>项目</span>
        <select value={projectId} onChange={e => setProjectId(e.target.value)} required disabled={busy || projects.length === 1}>
          <option value="">选择项目</option>
          {projects.map(item => <option key={item.id} value={item.id}>{item.name} · {item.repo}</option>)}
        </select>
      </label>
      <label className="field"><span>Tag</span><input value={tag} onChange={e => setTag(e.target.value)} placeholder="v1.2.3" required pattern="[A-Za-z0-9._-]{1,64}" title="只能使用字母、数字、点、下划线和短横线" disabled={busy} autoFocus /></label>
      <p className="muted-note">Agent 没有 git 权限；提交、推送、打 tag 和创建 Release 由 Flowdeck 完成。</p>
      {!projects.length && <p className="warning-text">没有已启用总结并发布的正式项目。</p>}
      <div className="modal-actions"><button type="button" className="button secondary" onClick={onClose} disabled={busy}>取消</button><button type="submit" className="button primary" disabled={busy || !projects.length || !projectId || !tag.trim()}>{busy ? <Loader2 className="spin" size={16}/> : <Package size={16}/>}开始</button></div>
    </form>
  </Modal>;
}

function DeleteProjectDialog({ project, session, onClose, onDeleted }: { project: Project; session: AuthSession; onClose: () => void; onDeleted: (repo: string) => void }) {
  const [deleteFiles, setDeleteFiles] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function remove() {
    setBusy(true); setError('');
    try {
      await api<{ ok: boolean; message: string }>(`/api/projects/${encodeURIComponent(project.id)}?delete_files=${deleteFiles}`, jsonRequest('DELETE', undefined, session.csrf_token));
      onDeleted(project.repo);
      onClose();
    } catch (reason) {
      setError((reason as Error).message);
      setBusy(false);
    }
  }
  return <Modal title="删除项目" subtitle={`确定要删除项目「${project.name}」(${project.repo}) 吗？`} onClose={onClose}><div className="modal-body">
    {error && <div className="form-error" role="alert"><XCircle size={16}/>{error}</div>}
    <div className="danger-box"><AlertTriangle size={18}/><div><strong>警告：此操作不可逆</strong><p>删除后将永久清除该项目在 Flowdeck 中的所有数据，包括任务执行记录、时间线与接入凭据。</p></div></div>
    <label className="confirm-check"><input type="checkbox" checked={deleteFiles} onChange={e => setDeleteFiles(e.target.checked)}/><span><strong>同时删除本地存储目录与工作区文件</strong><small>勾选后将物理删除该项目在服务器上的存储目录及隔离工作区。</small></span></label>
    <div className="modal-actions"><button type="button" className="button secondary" onClick={onClose} disabled={busy}>取消</button><button type="button" className="button danger" onClick={() => void remove()} disabled={busy}>{busy ? <Loader2 className="spin" size={16}/> : <Trash2 size={16}/>}确认删除</button></div>
  </div></Modal>;
}
