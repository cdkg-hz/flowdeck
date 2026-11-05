export type RecordItem = {
  id: string; repo: string; kind: 'issue' | 'pr' | 'release'; work_type?: 'issue' | 'bug' | 'pr' | 'release';
  external_id: string; number: number | null;
  title: string; body: string; url: string; status: string; stage: string; priority: string | null;
  assignees: string[]; labels: string[]; details: Record<string, any>; created_at: string; updated_at: string; demo: boolean;
};
export type TimelineItem = { id: string; record_id: string; stage: string; status: string; title: string; message: string; actor: string; occurred_at: string; record_title?: string; repo?: string };
export type Project = {
  id: string; repo: string; name: string; description: string; record_count: number;
  last_synced_at: string | null; sync_error: string | null; demo: boolean;
  status?: 'pending' | 'active' | 'demo'; storage_status?: 'pending' | 'ready' | 'error';
  api_status?: 'missing' | 'pending' | 'ready' | 'error'; webhook_status?: 'pending' | 'ready';
  prepare_release?: boolean;
};
export type Overview = { counts: { total: number; active: number; attention: number; reviewing: number; released: number }; activity: TimelineItem[]; mode: string };
export type RecordDetail = RecordItem & { timeline: TimelineItem[]; jobs: { id: string; kind: string; status: string; step: string; attempt: number; error: string | null }[]; linked_records: RecordItem[] };
export type Integrations = { mode: string; read_only_ui?: boolean; events?: { pending: number; failed: number; last_error: string | null }; github: { configured: boolean; webhook_path: string; sync_interval: number }; agent: { configured: boolean; protocol: string }; dingtalk?: { configured: boolean }; projects: { repo: string; review: boolean; merge: boolean; release: boolean; required_checks: string[]; managers: string[] }[] };

export type AuthSession = { authenticated: boolean; csrf_token: string | null; username: string | null; admin_configured: boolean; require_login: boolean };
export type ProjectEvent = { value: string; label: string; description: string };
export type ProjectOptions = { events: ProjectEvent[]; default_events: string[]; storage_roots: string[]; webhook_public_base_url: string; mode: string; management_enabled: boolean };
export type PrepareRelease = {
  enabled: boolean;
  agent_id: string;
  prompt: string;
  skill_ids: string[];
};
export type AgentRule = {
  id: string;
  name: string;
  event: 'issues' | 'pull_request' | 'release';
  agent_id: string;
  prompt: string;
  include_repository: boolean;
  include_github_credentials: boolean;
  skill_ids: string[];
  enabled: boolean;
};
export type RepositorySkill = { id: string; label: string };
export type UploadedSkill = { id: string; slug: string; label: string; files: string[]; updated_at: string };
export type ProjectConnection = {
  id: string; name: string; repo: string; description: string; status: 'pending' | 'active' | 'demo'; events: string[];
  storage_path: string; repository_path: string; workspaces_path: string; storage_locked: boolean;
  storage_status: 'pending' | 'ready' | 'error'; storage_error: string | null;
  api_status: 'missing' | 'pending' | 'ready' | 'error'; api_error: string | null;
  webhook_status: 'pending' | 'ready'; webhook_url: string | null; webhook_id?: string; webhook_path?: string; github_settings_url: string;
  github_hook_id?: string | null; webhook_registered?: boolean; webhook_error?: string | null;
  token_configured: boolean; last_delivery_at: string | null; last_synced_at: string | null; sync_error: string | null;
  automation: { review: boolean; merge: boolean; release: boolean };
  automation_config: {
    agent_id: string;
    agent_rules: AgentRule[];
    prepare_release: PrepareRelease;
    review_enabled?: boolean;
    merge_enabled?: boolean;
    release_enabled?: boolean;
    required_checks?: string[];
    merge_method?: string;
    base_branches?: string[];
    priority_labels?: Record<string, string>;
    managers?: string[];
    previous_tag?: string;
    review_rules?: string;
  };
  skills?: RepositorySkill[];
  public_url_configured: boolean;
};
export type AgentConnection = { id: string; name: string; base_url: string; api_key_configured: boolean; model: string; reasoning_effort: string; timeout_seconds: number; enabled: boolean; updated_at: string };
export type DingTalkChannel = {
  id: string; name: string; enabled: boolean; events: string[]; project_ids: string[];
  webhook_configured: boolean; sign_configured: boolean; updated_at: string;
};
export type AdminIntegrations = { agents: AgentConnection[]; dingtalk_channels: DingTalkChannel[] };
export type AdminSkills = { skills: UploadedSkill[] };
