import { useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import {
  ArrowLeft, ArrowRight, BookOpen, ChevronLeft, ChevronRight, CircleAlert, ExternalLink,
  GitBranch, GitFork, GitPullRequest, Link2, LockKeyhole, RefreshCw, Search, Star, X,
} from 'lucide-react';
import {
  getBranches, getPullRequest, getPullRequests, getRepositories, getRepository,
} from '../services/github';
import type { GitHubConnection, GitHubPullRequest, GitHubRepository } from '../services/github';
import { filterRepositories, type RepositoryOrder, type RepositoryVisibility } from '../services/repository-view';
import './repositories.css';

type Coordinates = { owner: string; repo: string };
type LoadState<T> = { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; data: T };
type ConnectionCheck = (reason: unknown) => void;

type Props = {
  coordinates: Coordinates | null;
  connection: GitHubConnection | null;
  connectionLoading: boolean;
  connectionError: string;
  onReloadConnection: () => void;
  onConnect: () => Promise<void>;
  onDisconnect: () => Promise<void>;
  onOpenRepository: (owner: string, repo: string) => void;
  onBack: () => void;
  onAnalyze: (url: string) => Promise<void>;
  manageConnection: boolean;
  onCloseManagement: () => void;
};

const PAGE_SIZE = 12;
const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' });
const numberFormat = new Intl.NumberFormat();

function readableDate(date: string | null | undefined) {
  const timestamp = date ? Date.parse(date) : NaN;
  return Number.isFinite(timestamp) ? dateFormat.format(timestamp) : 'Date unavailable';
}

function errorMessage(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message : fallback;
}

function useRemoteData<T>(load: (signal: AbortSignal, refresh: boolean) => Promise<T>, key: string, checkConnection: ConnectionCheck) {
  const callbacks = useRef({ load, checkConnection });
  const [attempt, setAttempt] = useState({ id: 0, refresh: false });
  const requestKey = `${attempt.id}:${key}`;
  const [result, setResult] = useState<{ key: string; state: LoadState<T> }>();
  useEffect(() => { callbacks.current = { load, checkConnection }; }, [load, checkConnection]);
  useEffect(() => {
    const controller = new AbortController();
    void callbacks.current.load(controller.signal, attempt.refresh).then(
      (data) => {
        if (!controller.signal.aborted) setResult({ key: requestKey, state: { status: 'ready', data } });
      },
      (reason: unknown) => {
        if (controller.signal.aborted) return;
        setResult({ key: requestKey, state: { status: 'error', message: errorMessage(reason, 'GitHub data is unavailable.') } });
        callbacks.current.checkConnection(reason);
      },
    );
    return () => controller.abort();
  }, [requestKey, attempt.refresh]);

  const state: LoadState<T> = result?.key === requestKey ? result.state : { status: 'loading' };
  return { state, reload: (refresh = false) => setAttempt(({ id }) => ({ id: id + 1, refresh })) };
}

function StatusPanel({ title, children, retry }: { title: string; children: ReactNode; retry?: () => void }) {
  const Icon = retry ? CircleAlert : BookOpen;
  return <div className="github-state" role={retry ? 'alert' : undefined}>
    <span className="github-state-icon"><Icon size={20} aria-hidden="true" /></span>
    <div><strong>{title}</strong><div className="github-state-copy">{children}</div></div>
    {retry && <button type="button" className="secondary-button" onClick={retry}><RefreshCw size={14} /> Retry</button>}
  </div>;
}

function Skeleton({ label, count = 1 }: { label: string; count?: number }) {
  return <div className={`github-skeletons${count > 1 ? ' github-card-grid' : ''}`} role="status">
    <span className="github-visually-hidden">{label}</span>
    {Array.from({ length: count }, (_, index) => <div className="github-skeleton" key={index} aria-hidden="true"><i /><i /><i /></div>)}
  </div>;
}

function Pager({ label, page, hasNext, onChange, totalPages }: {
  label: string; page: number; hasNext: boolean; onChange: (page: number) => void; totalPages?: number;
}) {
  return <nav className="github-pagination" aria-label={label}>
    <button type="button" className="secondary-button" disabled={page <= 1} onClick={() => onChange(page - 1)}><ChevronLeft size={16} /> Previous</button>
    <span aria-live="polite">Page {page}{totalPages ? ` of ${totalPages}` : ''}</span>
    <button type="button" className="secondary-button" disabled={!hasNext} onClick={() => onChange(page + 1)}>Next <ChevronRight size={16} /></button>
  </nav>;
}

function OpenOnGitHub({ href, label = 'Open on GitHub' }: { href: string; label?: string }) {
  return <a className="github-external" href={href} target="_blank" rel="noopener noreferrer">{label}<ExternalLink size={14} aria-hidden="true" /></a>;
}

function ConnectionPanel({ connection, connectionLoading, connectionError, onReloadConnection, onConnect, onDisconnect, manageConnection, onCloseManagement }: Props) {
  const [showManagement, setShowManagement] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const managementHeading = useRef<HTMLHeadingElement>(null);
  const profile = connection?.profile;
  const connected = connection?.connected === true;
  const managing = connected && (manageConnection || showManagement);

  useEffect(() => {
    if (managing) managementHeading.current?.focus();
  }, [managing]);

  const runAction = async (action: () => Promise<void>) => {
    setBusy(true);
    setActionError('');
    try { await action(); }
    catch (reason) { setActionError(errorMessage(reason, 'The GitHub connection could not be updated.')); }
    finally { setBusy(false); }
  };

  const close = () => {
    setShowManagement(false);
    setConfirmDisconnect(false);
    onCloseManagement();
  };

  if (connectionLoading && !connection) return <Skeleton label="Checking GitHub connection" />;
  if (connectionError && !connection) return <StatusPanel title="Could not check your GitHub connection" retry={onReloadConnection}>{connectionError}</StatusPanel>;

  return <div className="github-connection" aria-busy={busy}>
    <div className="github-connection-main">
      {connected && profile?.avatar_url
        ? <img className="github-connection-icon" src={profile.avatar_url} alt="" width={40} height={40} />
        : <span className="github-connection-icon"><Link2 size={20} aria-hidden="true" /></span>}
      <div className="github-connection-copy">
        <strong>{connected ? 'GitHub connected' : 'Connect your GitHub account'}</strong>
        {connected && profile
          ? <a href={profile.html_url} target="_blank" rel="noopener noreferrer">@{profile.login}</a>
          : <span>Browse repositories your GitHub account can access.</span>}
      </div>
      {connected
        ? <button type="button" className="secondary-button" aria-expanded={managing} aria-controls="github-connection-management" onClick={() => managing ? close() : setShowManagement(true)}>Manage connection</button>
        : <button type="button" className="primary-button" disabled={busy} onClick={() => void runAction(onConnect)}><Link2 size={15} /> {busy ? 'Connecting…' : 'Connect GitHub'}</button>}
    </div>
    {managing && <section id="github-connection-management" className="github-management" aria-labelledby="github-management-title">
      <div className="github-management-heading">
        <h2 id="github-management-title" ref={managementHeading} tabIndex={-1}>GitHub connection</h2>
        <button type="button" className="icon-button" aria-label="Close GitHub connection settings" onClick={close}><X size={17} /></button>
      </div>
      <p>PR Mentor reads your GitHub profile, verified email when available, and repository data for pull request reviews.</p>
      {confirmDisconnect && <p>Disconnecting stops repository access through this connection. You can reconnect at any time.</p>}
      <div className="github-actions">
        {confirmDisconnect
          ? <>
            <button type="button" className="primary-button" disabled={busy} onClick={() => void runAction(async () => { await onDisconnect(); close(); })}>{busy ? 'Disconnecting…' : 'Confirm disconnect'}</button>
            <button type="button" className="secondary-button" disabled={busy} onClick={() => setConfirmDisconnect(false)}>Cancel</button>
          </>
          : <>
            <button type="button" className="secondary-button" disabled={busy} onClick={() => void runAction(onConnect)}>Reconnect GitHub</button>
            <button type="button" className="secondary-button" onClick={() => setConfirmDisconnect(true)}>Disconnect GitHub</button>
          </>}
      </div>
    </section>}
    {(actionError || connectionError) && <p className="github-action-error" role="alert">{actionError || connectionError}</p>}
  </div>;
}

function RepositoryBadges({ repository }: { repository: GitHubRepository }) {
  const visibility = repository.visibility === 'public' ? 'Public' : repository.visibility === 'internal' ? 'Internal' : 'Private';
  return <div className="github-badges">
    <span className={`github-badge${visibility !== 'Public' ? ' github-badge-private' : ''}`}>
      {visibility !== 'Public' && <LockKeyhole size={12} aria-hidden="true" />}{visibility}
    </span>
    <span className="github-badge github-badge-language">{repository.language ?? 'No language'}</span>
  </div>;
}

function RepositoryCard({ repository, onOpen }: { repository: GitHubRepository; onOpen: Props['onOpenRepository'] }) {
  const { owner, name, description, stargazers_count, forks_count, updated_at, html_url } = repository;
  return <article className="github-card">
    <div className="github-card-top"><div className="github-owner">
      {owner.avatar_url && <img src={owner.avatar_url} alt="" width={24} height={24} loading="lazy" />}
      <span>{owner.login}</span>
    </div><RepositoryBadges repository={repository} /></div>
    <h2>{name}</h2>
    <p className="github-repository-description">{description || 'No description provided.'}</p>
    <div className="github-repository-meta">
      <span><Star size={15} aria-hidden="true" /> {numberFormat.format(stargazers_count)} {stargazers_count === 1 ? 'star' : 'stars'}</span>
      <span><GitFork size={15} aria-hidden="true" /> {numberFormat.format(forks_count)} {forks_count === 1 ? 'fork' : 'forks'}</span>
      <span>Updated {readableDate(updated_at)}</span>
    </div>
    <div className="github-card-actions">
      <button type="button" className="primary-button" aria-label={`Review PRs in ${owner.login}/${name}`} onClick={() => onOpen(owner.login, name)}>Review PRs <ArrowRight size={15} /></button>
      <OpenOnGitHub href={html_url} />
    </div>
  </article>;
}

function RepositoryIndex({ onOpen, onReconnect, checkConnection }: {
  onOpen: Props['onOpenRepository']; onReconnect: () => void; checkConnection: ConnectionCheck;
}) {
  const { state, reload } = useRemoteData((signal, refresh) => getRepositories(refresh, signal), 'repositories', checkConnection);
  const [query, setQuery] = useState('');
  const [visibility, setVisibility] = useState<RepositoryVisibility>('all');
  const [language, setLanguage] = useState('');
  const [sorting, setSorting] = useState<RepositoryOrder>('updated');
  const [page, setPage] = useState(1);

  const repositories = state.status === 'ready' ? state.data : null;
  const languages = useMemo(() => [...new Set((repositories ?? []).flatMap((repo) => repo.language ? [repo.language] : []))]
    .sort((first, second) => first.localeCompare(second)), [repositories]);
  const filtered = useMemo(() => filterRepositories(repositories ?? [], { query, visibility, language, order: sorting }),
    [query, visibility, language, sorting, repositories]);
  const totalPages = Math.max(Math.ceil(filtered.length / PAGE_SIZE), 1);
  const currentPage = Math.min(page, totalPages);
  const offset = (currentPage - 1) * PAGE_SIZE;
  const controller = (value: () => void) => { value(); setPage(1); };

  if (state.status === 'error') return <StatusPanel title="Could not load your repositories" retry={() => reload(true)}>
    <p>{state.message}</p><button type="button" className="text-action" onClick={onReconnect}>Manage connection <ArrowRight size={14} /></button>
  </StatusPanel>;

  return <>
    <div className="github-filters">
      <label className="github-search"><span>Search repositories</span><span className="github-search-field"><Search size={17} aria-hidden="true" /><input type="search" value={query} placeholder="Name or owner/repository" onChange={(event) => controller(() => setQuery(event.target.value))} /></span></label>
      <label>Visibility<select value={visibility} onChange={(event) => controller(() => setVisibility(event.target.value as RepositoryVisibility))}><option value="all">All repositories</option><option value="public">Public</option><option value="private">Private</option></select></label>
      <label>Language<select value={language} onChange={(event) => controller(() => setLanguage(event.target.value))}><option value="">All languages</option>{language && !languages.includes(language) && <option value={language}>{language}</option>}{languages.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
      <label>Sort by<select value={sorting} onChange={(event) => controller(() => setSorting(event.target.value as RepositoryOrder))}><option value="updated">Recently updated</option><option value="name">Name A–Z</option></select></label>
      <button type="button" className="secondary-button github-refresh" aria-label="Refresh repositories" disabled={state.status === 'loading'} onClick={() => reload(true)}><RefreshCw size={15} /> Refresh</button>
    </div>
    {state.status === 'loading' ? <Skeleton label="Loading repositories" count={6} /> : <>
      {!!state.data.length && <p className="github-result-count" aria-live="polite">{filtered.length === state.data.length ? `${numberFormat.format(filtered.length)} ${filtered.length === 1 ? 'repository' : 'repositories'}` : `${numberFormat.format(filtered.length)} of ${numberFormat.format(state.data.length)} repositories`}</p>}
      {!state.data.length ? <StatusPanel title="No repositories found">GitHub has no repositories available to this connection.</StatusPanel>
        : !filtered.length ? <StatusPanel title="No matching repositories"><p>Try another name or clear your filters.</p><button type="button" className="text-action" onClick={() => { setQuery(''); setVisibility('all'); setLanguage(''); setPage(1); }}>Clear filters <X size={13} /></button></StatusPanel>
          : <div className="github-card-grid">{filtered.slice(offset, offset + PAGE_SIZE).map((repository) => <RepositoryCard key={repository.id} repository={repository} onOpen={onOpen} />)}</div>}
      {totalPages > 1 && <Pager label="Repository pages" page={currentPage} totalPages={totalPages} hasNext={currentPage < totalPages} onChange={setPage} />}
    </>}
  </>;
}

function Branches({ owner, repo, checkConnection }: Coordinates & { checkConnection: ConnectionCheck }) {
  const [page, setPage] = useState(1);
  const { state, reload } = useRemoteData((signal) => getBranches(owner, repo, page, signal), `${owner}/${repo}:${page}`, checkConnection);

  return <aside className="github-branches" aria-labelledby="github-branch-title">
    <h2 id="github-branch-title"><GitBranch size={18} aria-hidden="true" /> Branches</h2>
    {state.status === 'loading' && <Skeleton label="Loading branches" />}
    {state.status === 'error' && <StatusPanel title="Branches are unavailable" retry={() => reload()}>{state.message}</StatusPanel>}
    {state.status === 'ready' && <>
      {state.data.items.length ? <ul className="github-branch-list">{state.data.items.map((branch) => <li key={branch.name}>
        <span>{branch.name}</span>{branch.protected && <span className="github-protected" aria-label="Protected branch" title="Protected branch"><LockKeyhole size={14} aria-hidden="true" /></span>}
      </li>)}</ul> : <p className="github-muted">No branches on this page.</p>}
      {(page > 1 || state.data.has_next) && <Pager label="Branch pages" page={page} hasNext={state.data.has_next} onChange={setPage} />}
    </>}
  </aside>;
}

function PullRequestDetails({ pr, onAnalyze, checkConnection }: {
  pr: GitHubPullRequest; onAnalyze: Props['onAnalyze']; checkConnection: ConnectionCheck;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const analyze = async () => {
    setBusy(true);
    setError('');
    try { await onAnalyze(pr.html_url); }
    catch (reason) {
      setError(errorMessage(reason, 'The pull request could not be analyzed.'));
      checkConnection(reason);
    }
    finally { setBusy(false); }
  };
  return <div className="github-pr-details">
    <p className="github-pr-body">{pr.body || 'No pull request description.'}</p>
    <dl className="github-pr-stats">
      <div><dt>Changed files</dt><dd>{pr.changed_files ?? 'Unavailable'}</dd></div>
      <div><dt>Additions</dt><dd className="github-additions">{pr.additions == null ? 'Unavailable' : `+${numberFormat.format(pr.additions)}`}</dd></div>
      <div><dt>Deletions</dt><dd className="github-deletions">{pr.deletions == null ? 'Unavailable' : `−${numberFormat.format(pr.deletions)}`}</dd></div>
    </dl>
    <div className="github-actions" aria-busy={busy}>
      <button type="button" className="primary-button" disabled={busy} onClick={() => void analyze()}><GitPullRequest size={15} /> {busy ? 'Analyzing PR…' : 'Analyze PR'}</button>
      <OpenOnGitHub href={pr.html_url} />
    </div>
    {error && <p role="alert" className="github-action-error">{error}</p>}
  </div>;
}

function SelectedPullRequest({ owner, repo, number, onAnalyze, checkConnection }: Coordinates & {
  number: number; onAnalyze: Props['onAnalyze']; checkConnection: ConnectionCheck;
}) {
  const { state, reload } = useRemoteData((signal) => getPullRequest(owner, repo, number, signal), `${owner}/${repo}:${number}`, checkConnection);
  if (state.status === 'loading') return <Skeleton label={`Loading pull request ${number}`} />;
  if (state.status === 'error') return <StatusPanel title="Could not load pull request details" retry={() => reload()}>{state.message}</StatusPanel>;
  return <PullRequestDetails pr={state.data} onAnalyze={onAnalyze} checkConnection={checkConnection} />;
}

function PullRequests({ owner, repo, onAnalyze, checkConnection }: Coordinates & {
  onAnalyze: Props['onAnalyze']; checkConnection: ConnectionCheck;
}) {
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<number | null>(null);
  const { state, reload } = useRemoteData((signal) => getPullRequests(owner, repo, page, signal), `${owner}/${repo}:${page}`, checkConnection);

  return <section className="github-pulls" aria-labelledby="github-pull-title">
    <h2 id="github-pull-title"><GitPullRequest size={18} aria-hidden="true" /> Open pull requests</h2>
    {state.status === 'loading' && <Skeleton label="Loading open pull requests" />}
    {state.status === 'error' && <StatusPanel title="Could not load pull requests" retry={() => reload()}>{state.message}</StatusPanel>}
    {state.status === 'ready' && <>
      {state.data.items.length ? <div className="github-pull-list">{state.data.items.map((pr) => <article className="github-pull" key={pr.number}>
        <div className="github-pull-top"><span className="github-pr-number">#{pr.number}</span><span className={`github-badge${pr.draft ? ' github-badge-language' : ' github-badge-private'}`}>{pr.draft ? 'Draft' : pr.state === 'open' ? 'Open' : pr.state}</span></div>
        <h3>{pr.title}</h3>
        <p className="github-pull-meta">By {pr.author || 'Unknown author'} · Opened {readableDate(pr.created_at)} · Updated {readableDate(pr.updated_at)}</p>
        <div className="github-branch-comparison" aria-label={`Source branch ${pr.head ?? 'unknown'}, target branch ${pr.base ?? 'unknown'}`}>
          <code>{pr.head || 'Unknown source'}</code><ArrowRight size={14} aria-hidden="true" /><code>{pr.base || 'Unknown target'}</code>
        </div>
        {selected !== pr.number && <p className="github-pr-preview">{pr.body || 'No pull request description.'}</p>}
        <div className="github-pull-actions">
          <button type="button" className="secondary-button" aria-expanded={selected === pr.number} aria-controls={selected === pr.number ? `github-pull-${pr.number}` : undefined} onClick={() => setSelected(selected === pr.number ? null : pr.number)}>{selected === pr.number ? 'Hide details' : 'View details'}</button>
          {selected !== pr.number && <OpenOnGitHub href={pr.html_url} />}
        </div>
        {selected === pr.number && <div id={`github-pull-${pr.number}`} className="github-pull-selection"><SelectedPullRequest owner={owner} repo={repo} number={pr.number} onAnalyze={onAnalyze} checkConnection={checkConnection} /></div>}
      </article>)}</div> : <StatusPanel title="No open pull requests">Open pull requests will appear here when GitHub has one for this repository.</StatusPanel>}
      {(page > 1 || state.data.has_next) && <Pager label="Pull request pages" page={page} hasNext={state.data.has_next} onChange={(next) => { setSelected(null); setPage(next); }} />}
    </>}
  </section>;
}

function RepositoryWorkspace({ owner, repo, onAnalyze, onBack, checkConnection }: Coordinates & {
  onAnalyze: Props['onAnalyze']; onBack: Props['onBack']; checkConnection: ConnectionCheck;
}) {
  const { state, reload } = useRemoteData((signal) => getRepository(owner, repo, signal), `${owner}/${repo}`, checkConnection);
  return <>
    <div className="page-heading github-heading"><div><button type="button" className="text-action github-back" onClick={onBack}><ArrowLeft size={15} /> My Repositories</button><h1>{repo}</h1><p>{owner}/{repo}</p></div>
      {state.status === 'ready' && <OpenOnGitHub href={state.data.html_url} />}
    </div>
    {state.status === 'loading' && <Skeleton label="Loading repository" />}
    {state.status === 'error' && <StatusPanel title="Could not open this repository" retry={() => reload()}>{state.message}</StatusPanel>}
    {state.status === 'ready' && <>
      <div className="github-workspace-summary"><RepositoryBadges repository={state.data} />
        <p>{state.data.description || 'No description provided.'}</p>
        <div className="github-repository-meta">
          {state.data.default_branch && <span><GitBranch size={15} aria-hidden="true" /> Default branch · {state.data.default_branch}</span>}
          <span>{numberFormat.format(state.data.open_issues_count)} {state.data.open_issues_count === 1 ? 'open issue' : 'open issues'}</span>
          <span>Updated {readableDate(state.data.updated_at)}</span>
        </div>
      </div>
      <div className="github-workspace-grid"><PullRequests owner={owner} repo={repo} onAnalyze={onAnalyze} checkConnection={checkConnection} /><Branches owner={owner} repo={repo} checkConnection={checkConnection} /></div>
    </>}
  </>;
}

export default function RepositoriesPage(props: Props) {
  const { connection, coordinates, onOpenRepository, onBack, onAnalyze } = props;
  const [manage, setManage] = useState(false);
  const [connectionNotice, setConnectionNotice] = useState('');
  const profileId = connection?.profile?.id;
  const showWorkspace = coordinates && connection?.connected;
  const checkConnection = (reason: unknown) => {
    if (reason instanceof Error && 'status' in reason && reason.status === 409) {
      setConnectionNotice(reason.message);
      props.onReloadConnection();
    }
  };
  return <section className="content-page github-repositories">
    {showWorkspace ? <RepositoryWorkspace key={`${profileId}:${coordinates.owner}/${coordinates.repo}`} {...coordinates} onBack={onBack} onAnalyze={onAnalyze} checkConnection={checkConnection} /> : <>
      <div className="page-heading github-heading"><div><h1>My Repositories</h1><p>Browse your GitHub repositories and choose a pull request to review.</p></div></div>
      {connection?.connected === false && connectionNotice && <p className="github-action-error" role="alert">{connectionNotice}</p>}
      <ConnectionPanel {...props} onConnect={async () => { setConnectionNotice(''); await props.onConnect(); }} manageConnection={props.manageConnection || manage} onCloseManagement={() => { setManage(false); props.onCloseManagement(); }} />
      {connection?.connected && <RepositoryIndex key={profileId} onOpen={onOpenRepository} onReconnect={() => setManage(true)} checkConnection={checkConnection} />}
    </>}
  </section>;
}
