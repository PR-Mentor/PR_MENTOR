import { useEffect, useState } from 'react';
import { ArrowUpRight, Building2, Copy, Plus, RefreshCw, Send, Shield, UserMinus, Users } from 'lucide-react';
import {
  acceptWorkspaceInvitation, createWorkspace, getWorkspace, inviteWorkspaceMember,
  getWorkspaceLearningInsights, listWorkspaceReviews, listWorkspaces, removeWorkspaceMember, shareWorkspaceReview,
  updateWorkspaceMemberRole,
} from '../services/api';
import type { Workspace, WorkspaceRole } from '../services/api';
import type { ReviewResponse } from '../types/review';
import type { LearningInsights } from '../types/review';

interface Props { onOpenReview: (reviewId: string) => void }

type State = 'idle' | 'loading' | 'ready' | 'error';
const prettyLabel = (value: string) => value.replace(/_/g, ' ');

export default function WorkspacesPage({ onOpenReview }: Props) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [selected, setSelected] = useState('');
  const [details, setDetails] = useState<Workspace | null>(null);
  const [reviews, setReviews] = useState<ReviewResponse[]>([]);
  const [insights, setInsights] = useState<LearningInsights | null>(null);
  const [state, setState] = useState<State>('loading');
  const [error, setError] = useState('');
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<Exclude<WorkspaceRole, 'owner'>>('viewer');
  const [inviteToken, setInviteToken] = useState('');
  const [acceptToken, setAcceptToken] = useState('');
  const [reviewId, setReviewId] = useState('');
  const [message, setMessage] = useState('');

  const refresh = async (preferredId?: string) => {
    setState('loading'); setError('');
    try {
      const items = await listWorkspaces();
      setWorkspaces(items);
      const nextId = preferredId && items.some((item) => item.workspace_id === preferredId)
        ? preferredId : selected && items.some((item) => item.workspace_id === selected)
          ? selected : items[0]?.workspace_id ?? '';
      setSelected(nextId);
      if (nextId) {
        const [workspace, shared, learning] = await Promise.all([getWorkspace(nextId), listWorkspaceReviews(nextId), getWorkspaceLearningInsights(nextId)]);
        setDetails(workspace); setReviews(shared); setInsights(learning);
      } else { setDetails(null); setReviews([]); setInsights(null); }
      setState('ready');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Workspace data is unavailable.');
      setState('error');
    }
  };

  useEffect(() => { void refresh(); }, []);

  const selectWorkspace = async (workspaceId: string) => {
    setSelected(workspaceId); setError(''); setMessage('');
    if (!workspaceId) { setDetails(null); setReviews([]); setInsights(null); return; }
    setState('loading');
    try {
      const [workspace, shared, learning] = await Promise.all([getWorkspace(workspaceId), listWorkspaceReviews(workspaceId), getWorkspaceLearningInsights(workspaceId)]);
      setDetails(workspace); setReviews(shared); setInsights(learning); setState('ready');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Workspace details are unavailable.'); setState('error'); }
  };

  const create = async () => {
    try { const workspace = await createWorkspace(name); setName(''); await refresh(workspace.workspace_id); setMessage('Workspace created.'); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Workspace could not be created.'); }
  };

  const invite = async () => {
    if (!details) return;
    try {
      const invitation = await inviteWorkspaceMember(details.workspace_id, email, role);
      setInviteToken(invitation.token); setEmail(''); setMessage(`Invitation created for ${invitation.email}. Share this one-time token with them securely.`);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Invitation could not be created.'); }
  };

  const accept = async () => {
    try { const result = await acceptWorkspaceInvitation(acceptToken); setAcceptToken(''); await refresh(result.workspace_id); setMessage(`Joined workspace as ${result.role}.`); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Invitation could not be accepted.'); }
  };

  const share = async () => {
    if (!details) return;
    try { await shareWorkspaceReview(details.workspace_id, reviewId.trim()); setReviewId(''); await refresh(details.workspace_id); setMessage('Review shared with this workspace.'); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Review could not be shared.'); }
  };

  const isManager = details?.role === 'owner' || details?.role === 'admin';
  const isOwner = details?.role === 'owner';
  const canEdit = isManager || details?.role === 'editor';

  const changeRole = async (memberId: string, nextRole: Exclude<WorkspaceRole, 'owner'>) => {
    if (!details) return;
    try { await updateWorkspaceMemberRole(details.workspace_id, memberId, nextRole); await refresh(details.workspace_id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Member role could not be changed.'); }
  };

  const remove = async (memberId: string) => {
    if (!details) return;
    try { await removeWorkspaceMember(details.workspace_id, memberId); await refresh(details.workspace_id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Member could not be removed.'); }
  };

  return <section className="content-page workspace-management">
    <div className="page-heading"><div><span className="eyebrow">COLLABORATION</span><h1>Workspaces</h1><p>Share selected reviews with members you invite. Membership is checked by the backend for every shared review.</p></div><button className="secondary-button" type="button" onClick={() => void refresh()}><RefreshCw size={15}/> Refresh</button></div>
    {error && <p className="alert error-alert" role="alert">{error}</p>}{message && <p className="inline-message" role="status">{message}</p>}
    <div className="workspace-management-grid">
      <section className="workspace-panel"><h2><Building2 size={17}/> Your workspaces</h2>
        <div className="workspace-create-row"><input aria-label="New workspace name" value={name} onChange={(event) => setName(event.target.value)} placeholder="Workspace name" maxLength={100}/><button type="button" className="primary-button" disabled={!name.trim()} onClick={() => void create()}><Plus size={15}/> Create</button></div>
        <label className="workspace-field">Selected workspace<select value={selected} onChange={(event) => void selectWorkspace(event.target.value)}><option value="">Choose a workspace</option>{workspaces.map((item) => <option key={item.workspace_id} value={item.workspace_id}>{item.name} · {item.role}</option>)}</select></label>
        {!workspaces.length && state === 'ready' && <p className="workspace-empty">Create a workspace or accept an invitation to get started.</p>}
        {details && <><h3><Users size={16}/> Members</h3><ul className="workspace-member-list">{details.members?.map((member) => <li key={member.user_id}><div><strong>{member.email}</strong><small>{member.role}</small></div>{isOwner && member.role !== 'owner' && <div className="workspace-member-actions"><select aria-label={`Role for ${member.email}`} value={member.role} onChange={(event) => void changeRole(member.user_id, event.target.value as Exclude<WorkspaceRole, 'owner'>)}><option value="admin">Admin</option><option value="editor">Editor</option><option value="viewer">Viewer</option></select><button type="button" className="icon-button" onClick={() => void remove(member.user_id)} aria-label={`Remove ${member.email}`}><UserMinus size={15}/></button></div>}</li>)}</ul></>}
      </section>
      <section className="workspace-panel"><h2><Shield size={17}/> Invitations and access</h2>
        {isManager && <><div className="workspace-form"><label>Email<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="teammate@example.com"/></label><label>Role<select value={role} onChange={(event) => setRole(event.target.value as Exclude<WorkspaceRole, 'owner'>)}><option value="viewer">Viewer</option><option value="editor">Editor</option>{details?.role === 'owner' && <option value="admin">Admin</option>}</select></label><button type="button" className="secondary-button" disabled={!selected || !email.trim()} onClick={() => void invite()}><Send size={14}/> Create invite</button></div>{inviteToken && <div className="workspace-token"><code>{inviteToken}</code><button type="button" className="icon-button" aria-label="Copy invitation token" onClick={() => void navigator.clipboard?.writeText(inviteToken)}><Copy size={15}/></button></div>}</>}
        <div className="workspace-form"><label>Accept invitation token<input value={acceptToken} onChange={(event) => setAcceptToken(event.target.value)} autoComplete="off"/></label><button type="button" className="secondary-button" disabled={!acceptToken.trim()} onClick={() => void accept()}>Accept invite</button></div>
        {canEdit && <><h3>Share a review</h3><div className="workspace-form"><label>Review ID<input value={reviewId} onChange={(event) => setReviewId(event.target.value)} placeholder="Review ID"/></label><button type="button" className="secondary-button" disabled={!reviewId.trim()} onClick={() => void share()}>Share review</button></div></>}
      </section>
    </div>
    <section className="workspace-panel workspace-shared-reviews"><h2>Shared review history</h2>{state === 'loading' && <p>Loading workspace data…</p>}{state === 'ready' && !reviews.length && <p>No reviews have been shared with this workspace.</p>}{reviews.map((review) => <article key={review.review_id}><div><strong>{review.pull_request.repository_owner}/{review.pull_request.repository_name} · #{review.pull_request.pull_request_number}</strong><small>{review.pull_request.title}</small></div><button type="button" className="secondary-button" onClick={() => onOpenReview(review.review_id)}>Open review <ArrowUpRight size={14}/></button></article>)}</section>
    {details && insights && <section className="workspace-panel"><h2>Workspace learning insights</h2><p>{insights.basis}</p><p>{insights.source_review_count} shared reviews analyzed · {insights.suggested_fix_count} suggested fixes</p><p>Fix decisions: {Object.entries(insights.fix_decisions).map(([key, count]) => `${key}: ${count}`).join(' · ') || 'none recorded'}</p><p>Validation outcomes: {Object.entries(insights.validation_reports_by_status).map(([key, count]) => `${key}: ${count}`).join(' · ') || 'none recorded'}</p>{insights.suggested_topics.length > 0 && <p>Recurring topics: {insights.suggested_topics.map(prettyLabel).join(', ')}</p>}</section>}
  </section>;
}
