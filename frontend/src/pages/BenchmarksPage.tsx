import { useEffect, useState } from 'react';
import { Activity, BarChart3, Play, Save } from 'lucide-react';
import { createBenchmarkDataset, listBenchmarkDatasets, listBenchmarkRuns, runBenchmark, updateBenchmarkLabels } from '../services/api';
import type { BenchmarkDataset, BenchmarkRun } from '../services/api';

const example = JSON.stringify([{ case_id: 'case-1', review_id: 'LIVE_REVIEW_ID', labels_complete: false, ground_truth_signatures: [], ground_truth_outcome: null, fix_decisions: {} }], null, 2);

export default function BenchmarksPage() {
  const [datasets, setDatasets] = useState<BenchmarkDataset[]>([]);
  const [selected, setSelected] = useState('');
  const [name, setName] = useState('');
  const [casesText, setCasesText] = useState(example);
  const [runs, setRuns] = useState<BenchmarkRun[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const current = datasets.find((item) => item.dataset_id === selected);

  const refresh = async (datasetId = selected) => {
    const result = await listBenchmarkDatasets();
    setDatasets(result);
    if (datasetId && result.some((item) => item.dataset_id === datasetId)) {
      setSelected(datasetId);
      const found = result.find((item) => item.dataset_id === datasetId)!;
      setCasesText(JSON.stringify(found.cases, null, 2));
      setRuns(await listBenchmarkRuns(datasetId));
    } else if (result.length) {
      setSelected(result[0].dataset_id);
      setCasesText(JSON.stringify(result[0].cases, null, 2));
      setRuns(await listBenchmarkRuns(result[0].dataset_id));
    } else {
      setSelected(''); setRuns([]);
    }
  };

  useEffect(() => { void refresh().catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Unable to load benchmark datasets.')); }, []);

  const parseCases = () => {
    const parsed: unknown = JSON.parse(casesText);
    if (!Array.isArray(parsed)) throw new Error('Cases must be a JSON array.');
    return parsed as Array<Record<string, unknown>>;
  };
  const act = async (operation: () => Promise<unknown>) => {
    setBusy(true); setError('');
    try { await operation(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Benchmark operation failed.'); }
    finally { setBusy(false); }
  };
  const create = () => void act(async () => {
    const created = await createBenchmarkDataset(name, parseCases()); setName(''); await refresh(created.dataset_id);
  });
  const saveLabels = () => void act(async () => { if (!selected) return; await updateBenchmarkLabels(selected, parseCases()); await refresh(selected); });
  const run = () => void act(async () => { if (!selected) return; await runBenchmark(selected); await refresh(selected); });

  return <section className="content-page"><div className="page-heading"><div><div className="eyebrow">WORKSPACE / EVALUATION</div><h1>Human-labelled benchmarks</h1><p>Measure saved live reviews against explicit human labels. Runs are reproducible snapshots of stored outputs; models are not rerun.</p></div></div>
    {error && <div className="alert error-alert" role="alert">{error}</div>}
    <div className="analytics-grid"><section className="analytics-panel"><h2><BarChart3 size={17}/> Dataset</h2><label>Dataset<select value={selected} onChange={(event) => { const item = datasets.find((entry) => entry.dataset_id === event.target.value); setSelected(event.target.value); setCasesText(JSON.stringify(item?.cases ?? [], null, 2)); if (event.target.value) void listBenchmarkRuns(event.target.value).then(setRuns).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Unable to load benchmark runs.')); }}><option value="">Select dataset</option>{datasets.map((item) => <option key={item.dataset_id} value={item.dataset_id}>{item.name}</option>)}</select></label><label>Human labels and linked review IDs<textarea rows={14} spellCheck={false} value={casesText} onChange={(event) => setCasesText(event.target.value)}/></label><p>Signature format: <code>category|severity|file_path</code>. Set <code>labels_complete</code> true only after human review. An empty signature list then means a human-confirmed no-finding result.</p><div className="workspace-actions"><button className="secondary-button" type="button" disabled={busy || !selected} onClick={saveLabels}><Save size={15}/> Save labels</button><button className="primary-button" type="button" disabled={busy || !selected} onClick={run}><Play size={15}/> Run benchmark</button></div></section>
      <section className="analytics-panel"><h2><Activity size={17}/> Create dataset</h2><label>Name<input value={name} maxLength={120} onChange={(event) => setName(event.target.value)} placeholder="Regression benchmark"/></label><p>Seed cases with review IDs and add labels using the editor after dataset creation.</p><button className="secondary-button" disabled={busy || !name.trim()} onClick={create}>Create dataset</button><h2 className="benchmark-runs-title">Run history</h2>{runs.length ? runs.map((item) => <article className="benchmark-run" key={item.run_id}><strong>{item.status === 'completed' ? 'Completed' : 'Incomplete'} · {new Date(item.created_at).toLocaleString()}</strong><span>{item.labeled_case_count}/{item.case_count} labeled cases · {item.missing_prediction_case_count} missing predictions</span><span>Precision {item.metrics.precision === null ? '—' : `${(item.metrics.precision * 100).toFixed(1)}%`} · Recall {item.metrics.recall === null ? '—' : `${(item.metrics.recall * 100).toFixed(1)}%`}</span><span>Provider/model: {item.model_configurations.map((config) => `${config.provider ?? 'unknown'} / ${config.model ?? 'unknown'}`).join(', ') || 'unavailable'}</span><span>Measured model latency {item.metrics.model_latency_ms === null ? '—' : `${item.metrics.model_latency_ms} ms`} · Provider-reported tokens {item.metrics.token_usage === null ? '—' : `${item.metrics.token_usage} (${((item.metrics.token_usage_coverage ?? 0) * 100).toFixed(0)}% case coverage)`}</span><small>Provider cost unavailable. {item.limitations[0]}</small></article>) : <p>No benchmark runs recorded.</p>}</section></div>
    {!current && datasets.length === 0 && <p className="notice-strip">No benchmark datasets yet. Create one, then enter live review IDs and human labels in the JSON editor.</p>}
  </section>;
}
