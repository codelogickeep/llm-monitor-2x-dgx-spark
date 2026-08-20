type Health = "ok" | "warning" | "critical" | "starting" | "error";
type ViewName = "dashboard" | "stats" | "trends" | "alerts";

interface GpuMetric {
  index: number;
  name: string;
  temperature_c: number | null;
  gpu_util_pct: number | null;
  memory_util_pct: number | null;
  memory_used_mib: number | null;
  memory_total_mib: number | null;
  power_w: number | null;
  power_limit_w: number | null;
  pstate: string | null;
  sm_clock_mhz: number | null;
  status: string;
}

interface InterfaceMetric {
  name: string;
  exists: boolean;
  operstate: string;
  speed_mbps: number | null;
  rx_mbps: number;
  tx_mbps: number;
  error_delta: number;
  drop_delta: number;
  is_roce: boolean;
  health: Health;
  source?: string;
}

interface NodeSummary {
  gpu_count: number;
  cpu_used_pct?: number | null;
  mem_used_pct?: number | null;
  gpu_temp_max_c: number | null;
  gpu_util_avg_pct: number | null;
  gpu_sm_clock_avg_mhz?: number | null;
  gpu_sm_clock_pct?: number | null;
  gpu_throttle_active?: number;
  gpu_pstate_numeric?: number | null;
  power_total_w: number | null;
  cpu_soc_temp_max_c?: number | null;
  nvme_temp_max_c?: number | null;
  nic_temp_max_c?: number | null;
  roce_rx_mbps: number;
  roce_tx_mbps: number;
  warnings: string[];
  health: Health;
}

interface NodeSnapshot {
  id: string;
  name: string;
  host: string;
  status: string;
  health: Health;
  probe_latency_ms?: number;
  gpu?: GpuMetric[];
  interfaces?: InterfaceMetric[];
  memory?: {
    total_mib: number;
    used_mib: number;
    available_mib: number;
    used_pct: number;
    swap_total_mib?: number;
    swap_used_mib?: number;
    swap_used_pct?: number;
    swap_in_pages_s?: number;
    swap_out_pages_s?: number;
    pressure?: {
      some_avg10?: number;
      full_avg10?: number;
    };
  };
  disk_root?: {
    total_gib: number;
    used_gib: number;
    free_gib: number;
    used_pct: number;
  };
  summary?: NodeSummary;
  error?: string;
}

interface VllmSnapshot {
  status: string;
  health: Health;
  sample_state?: "ok" | "warmup" | "counter_reset" | "metrics_missing" | "collection_error";
  event_state?: "idle" | "active" | "completion_event" | "unknown";
  deployment_id?: string;
  interval_s?: number | null;
  counter_reset?: boolean;
  missing_metrics?: string[];
  running?: number;
  waiting?: number;
  waiting_capacity?: number;
  waiting_deferred?: number;
  kv_cache_usage_pct?: number;
  prompt_tok_s?: number;
  cached_prompt_tok_s?: number;
  uncached_prompt_tok_s?: number;
  cache_hit_ratio_pct?: number;
  generation_tok_s?: number;
  request_s?: number;
  error_s?: number;
  preemption_delta?: number;
  mtp_acceptance_pct?: number | null;
  queue_avg_s?: number | null;
  prefill_avg_s?: number | null;
  decode_avg_s?: number | null;
  itl_avg_s?: number | null;
  request_prompt_tokens_avg?: number | null;
  request_generation_tokens_avg?: number | null;
  prefill_efficiency_tok_s?: number | null;
  decode_efficiency_tok_s?: number | null;
  ttft_avg_s?: number | null;
  e2e_avg_s?: number | null;
  recent?: Record<string, { value: number; sampled_at: number; age_s: number }>;
  warnings?: string[];
  error?: string;
}

interface Snapshot {
  status: Health;
  ts: number;
  updated_at: string;
  nodes: Record<string, NodeSnapshot>;
  vllm: VllmSnapshot;
  model: {
    status: string;
    id?: string;
    root?: string;
    max_model_len?: number;
  };
  alerts: Array<{ level: Health; scope: string; message: string; signature?: string }>;
}

interface MetricStats {
  count: number;
  min: number | null;
  max: number | null;
  avg: number | null;
  p95: number | null;
  p99: number | null;
  stddev: number | null;
  percentiles_approximate?: boolean;
  percentile_samples?: number;
}

interface StatsResponse {
  window_seconds: number;
  generated_at: number;
  nodes: Record<string, Record<string, MetricStats>>;
  vllm: Record<string, MetricStats>;
  inference_sampling: {
    total_samples: number;
    collected_samples: number;
    collection_coverage_pct: number;
    active_samples: number;
    activity_ratio_pct: number;
    event_samples: number;
    states: Record<string, number>;
  };
}

interface TrendResponse {
  kind: string;
  metric: string;
  node_id: string | null;
  window_seconds: number;
  bucket_seconds: number;
  requested_bucket_seconds?: number;
  downsampled?: boolean;
  timestamps: number[];
  values: number[];
}

interface AlertsResponse {
  window_seconds: number;
  items: Array<{
    id: number;
    first_ts: number;
    last_ts: number;
    resolved_ts: number | null;
    level: Health;
    scope: string;
    message: string;
    signature: string;
    health: Health;
    status: "active" | "resolved";
  }>;
}

interface AnalysisItem {
  id: string;
  title: string;
  severity: Health | "idle" | "insufficient";
  conclusion: string;
  evidence: string;
  coverage: number;
  provisional: boolean;
  activity_ratio?: number;
  event_samples?: number;
  collected_samples?: number;
}

interface AnalysisWindow {
  window: string;
  window_seconds: number;
  label: string;
  status: Health | "insufficient";
  coverage: number;
  summary: string;
  items: AnalysisItem[];
}

interface AnalysisResponse {
  generated_at: number;
  windows: AnalysisWindow[];
}

const nodeMetricOptions = [
  { value: "cpu_used_pct", label: "CPU 占用" },
  { value: "cpu_soc_temp_max_c", label: "CPU/SoC 温度" },
  { value: "mem_used_pct", label: "内存占用" },
  { value: "mem_available_mib", label: "可用内存" },
  { value: "swap_used_pct", label: "Swap 使用率" },
  { value: "memory_psi_some_avg10", label: "内存 PSI" },
  { value: "gpu_util_avg_pct", label: "显卡利用率" },
  { value: "gpu_temp_max_c", label: "显卡温度" },
  { value: "gpu_sm_clock_pct", label: "GPU 时钟占比" },
  { value: "power_total_w", label: "总功耗" },
  { value: "nvme_temp_max_c", label: "NVMe 温度" },
  { value: "nic_temp_max_c", label: "200G 网卡温度" },
  { value: "roce_rx_mbps", label: "RoCE 接收" },
  { value: "roce_tx_mbps", label: "RoCE 发送" },
  { value: "probe_latency_ms", label: "探针延迟" },
] as const;

const vllmMetricOptions = [
  { value: "running", label: "运行中" },
  { value: "waiting", label: "等待中" },
  { value: "kv_cache_usage_pct", label: "KV 缓存" },
  { value: "prompt_tok_s", label: "提示令牌入账速率" },
  { value: "cached_prompt_tok_s", label: "缓存提示令牌速率" },
  { value: "uncached_prompt_tok_s", label: "实际计算提示令牌速率" },
  { value: "generation_tok_s", label: "生成令牌交付速率" },
  { value: "request_s", label: "请求/秒" },
  { value: "error_s", label: "错误/秒" },
  { value: "ttft_avg_s", label: "首令牌延迟" },
  { value: "e2e_avg_s", label: "端到端延迟" },
  { value: "queue_avg_s", label: "排队时长" },
  { value: "prefill_avg_s", label: "Prefill 时长" },
  { value: "decode_avg_s", label: "Decode 时长" },
  { value: "itl_avg_s", label: "令牌间延迟" },
  { value: "cache_hit_ratio_pct", label: "缓存命中率" },
  { value: "request_prompt_tokens_avg", label: "平均提示长度" },
  { value: "request_generation_tokens_avg", label: "平均输出长度" },
  { value: "prefill_efficiency_tok_s", label: "Prefill 计算效率" },
  { value: "decode_efficiency_tok_s", label: "Decode 计算效率" },
  { value: "mtp_acceptance_pct", label: "MTP 接受率" },
  { value: "preemption_delta", label: "抢占次数" },
] as const;

const metricUnits: Record<string, string> = {
  cpu_used_pct: "%",
  cpu_soc_temp_max_c: "°C",
  mem_used_pct: "%",
  mem_available_mib: "MiB",
  swap_used_pct: "%",
  memory_psi_some_avg10: "%",
  gpu_util_avg_pct: "%",
  gpu_temp_max_c: "°C",
  gpu_sm_clock_pct: "%",
  power_total_w: "W",
  nvme_temp_max_c: "°C",
  nic_temp_max_c: "°C",
  roce_rx_mbps: "Mb/s",
  roce_tx_mbps: "Mb/s",
  probe_latency_ms: "ms",
  running: "请求",
  waiting: "请求",
  kv_cache_usage_pct: "%",
  prompt_tok_s: "tok/s",
  cached_prompt_tok_s: "tok/s",
  uncached_prompt_tok_s: "tok/s",
  generation_tok_s: "tok/s",
  request_s: "req/s",
  error_s: "req/s",
  ttft_avg_s: "s",
  e2e_avg_s: "s",
  queue_avg_s: "s",
  prefill_avg_s: "s",
  decode_avg_s: "s",
  itl_avg_s: "s",
  cache_hit_ratio_pct: "%",
  request_prompt_tokens_avg: "tok",
  request_generation_tokens_avg: "tok",
  prefill_efficiency_tok_s: "tok/s",
  decode_efficiency_tok_s: "tok/s",
  mtp_acceptance_pct: "%",
  preemption_delta: "次",
};

const trendPresets: Record<string, { label: string; window: string; bucket: string }> = {
  raw: { label: "原始", window: "24h", bucket: "raw" },
  "5m": { label: "5 分钟", window: "7d", bucket: "5m" },
  "1h": { label: "1 小时", window: "30d", bucket: "1h" },
};

const $ = <T extends HTMLElement>(id: string): T => {
  const item = document.getElementById(id);
  if (!item) throw new Error(`missing element ${id}`);
  return item as T;
};

const fmt = (value: number | null | undefined, digits = 1): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  return value.toFixed(digits);
};

const fmtInt = (value: number | null | undefined): string => {
  if (value === null || value === undefined || Number.isNaN(value)) return "--";
  return Math.round(value).toString();
};

const formatTime = (ts: number | null | undefined): string => {
  if (!ts) return "--";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(ts * 1000));
};

const setText = (id: string, text: string): void => {
  $(id).textContent = text;
};

const clamp = (value: number, min: number, max: number): number => Math.max(min, Math.min(max, value));
const escapeHtml = (value: unknown): string => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

const nodeLabels = new Map<string, string>();
let activeModelName = "vLLM model";

function updateNodeOptions(nodes: Record<string, NodeSnapshot>): void {
  const select = $("trendNode") as HTMLSelectElement;
  const selected = select.value;
  for (const [id, node] of Object.entries(nodes)) nodeLabels.set(id, node.name || id);
  const ids = Object.keys(nodes);
  if (!ids.length) return;
  select.innerHTML = ids
    .map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(nodeLabels.get(id) || id)}</option>`)
    .join("");
  select.value = ids.includes(selected) ? selected : ids[0];
}

function metricBar(
  label: string,
  value: number | null | undefined,
  unit: string,
  maximum: number,
  warning: number,
  critical: number,
  tone = "",
): string {
  const safe = value ?? 0;
  const pct = clamp((safe / Math.max(1, maximum)) * 100, 0, 100);
  const cls = safe >= critical ? "bad" : safe >= warning ? "warn" : "";
  return `
    <div class="bar-line">
      <span class="muted">${label}</span>
      <span class="bar ${tone} ${cls}"><i style="width:${pct}%"></i></span>
      <span>${fmt(value, 1)}${unit}</span>
    </div>
  `;
}

function nodeMetric(value: string, label: string): string {
  return `<div class="node-kpi"><span>${label}</span><strong>${value}</strong></div>`;
}

function renderAlertsBanner(snapshot: Snapshot): void {
  const root = $("alertsBanner");
  const counter = $("alertsCount");
  root.innerHTML = "";
  root.classList.toggle("is-clear", snapshot.alerts.length === 0);
  counter.textContent = String(snapshot.alerts.length);
  counter.classList.toggle("has-alerts", snapshot.alerts.length > 0);
  if (!snapshot.alerts.length) {
    return;
  }
  for (const alert of snapshot.alerts.slice(0, 8)) {
    const item = document.createElement("div");
    item.className = `alert ${alert.level}`;
    item.textContent = `${alert.scope}：${alert.message}`;
    root.appendChild(item);
  }
}

function renderGpuList(gpus: GpuMetric[]): string {
  if (!gpus.length) return `<p class="muted">暂无 GPU 数据</p>`;
  return gpus
    .map((gpu) => {
      const powerLimit = gpu.power_limit_w || 140;
      return `
        <div class="gpu-item">
          <h3>显卡 ${gpu.index} · ${escapeHtml(gpu.name || "NVIDIA")}</h3>
          ${metricBar("SM 利用率", gpu.gpu_util_pct, "%", 100, 85, 96)}
          ${metricBar("温度", gpu.temperature_c, "°C", 100, 82, 86, "secondary")}
          ${metricBar("功耗", gpu.power_w, "W", powerLimit, powerLimit * 0.85, powerLimit * 0.95, "secondary")}
        </div>
      `;
    })
    .join("");
}

function renderInterfaces(items: InterfaceMetric[]): string {
  if (!items.length) return `<p class="muted">暂无链路数据</p>`;
  return items
    .map((iface) => {
      const badge = iface.is_roce ? "RoCE" : "Wi-Fi";
      const stateText = iface.operstate === "up" ? "在线" : iface.operstate || "未知";
      const errorCount = iface.error_delta + iface.drop_delta;
      return `
        <div class="iface">
          <strong>${escapeHtml(iface.name)}</strong>
          <span>${badge}</span>
          <span class="link-${iface.health}">${stateText}</span>
          <span>↓ ${fmt(iface.rx_mbps, 1)} / ↑ ${fmt(iface.tx_mbps, 1)}</span>
          <span>${errorCount === 0 ? "0" : `${errorCount} 项`}</span>
        </div>
      `;
    })
    .join("");
}

function renderNodes(nodes: Record<string, NodeSnapshot>): void {
  const root = $("nodes");
  root.innerHTML = Object.values(nodes)
    .map((node) => {
      const summary = node.summary;
      const health = node.health || "error";
      const roceInterfaces = (node.interfaces || []).filter((item) => item.is_roce);
      const roceSpeed = Math.max(0, ...roceInterfaces.map((item) => item.speed_mbps || 0));
      const roceLabel = roceSpeed >= 1000 ? `${fmt(roceSpeed / 1000, 0)} Gb/s RoCE` : "RoCE 链路";
      const memory = node.memory || ({} as NonNullable<NodeSnapshot["memory"]>);
      const pressure = memory.pressure || {};
      return `
        <article class="surface node-panel">
          <div class="node-head">
            <div class="node-identity">
              <span class="node-code">${escapeHtml(node.id.toUpperCase())}</span>
              <div>
                <h2>${escapeHtml(node.name)}</h2>
                <p>${escapeHtml(node.host)} · 探针 ${fmt(node.probe_latency_ms, 0)} ms</p>
              </div>
            </div>
            <span class="badge ${health}">${health === "ok" ? "正常" : health === "warning" ? "警告" : health === "critical" ? "严重" : health}</span>
          </div>
          ${
            node.status !== "ok"
              ? `<div class="alert critical">${escapeHtml(node.error || "节点离线")}</div>`
              : `
                <div class="node-kpis node-kpis-primary">
                  ${nodeMetric(`${fmt(summary?.gpu_util_avg_pct, 0)}%`, "GPU 利用率")}
                  ${nodeMetric(`${fmt(summary?.gpu_temp_max_c, 0)}°C`, "GPU 温度")}
                  ${nodeMetric(`${fmt(summary?.power_total_w, 1)} W`, "GPU 功耗")}
                  ${nodeMetric(`${fmt(summary?.cpu_used_pct, 0)}%`, "CPU 占用")}
                  ${nodeMetric(`${fmt(summary?.mem_used_pct, 0)}%`, "统一内存占用")}
                  ${nodeMetric(`↓${fmt(summary?.roce_rx_mbps, 1)} ↑${fmt(summary?.roce_tx_mbps, 1)}`, "RoCE Mb/s")}
                </div>
                <div class="node-detail">
                  <section class="subsection health-metrics-section">
                    <div class="subsection-head"><h3>核心硬件健康</h3><span>实时只读传感器</span></div>
                    <div class="health-metrics">
                      ${nodeMetric(`${fmt(summary?.cpu_soc_temp_max_c, 1)}°C`, "CPU/SoC 温度")}
                      ${nodeMetric(`${fmt(summary?.nvme_temp_max_c, 1)}°C`, "NVMe 温度")}
                      ${nodeMetric(`${fmt(summary?.nic_temp_max_c, 1)}°C`, "200G 网卡温度")}
                      ${nodeMetric(`${fmt(memory.swap_used_pct, 1)}%`, "Swap 使用率")}
                      ${nodeMetric(fmt(pressure.some_avg10, 2), "内存 PSI avg10")}
                      ${nodeMetric(`${fmt((memory.available_mib || 0) / 1024, 1)} GiB`, "可用统一内存")}
                      ${nodeMetric(`${fmt(summary?.gpu_sm_clock_avg_mhz, 0)} MHz`, "GPU SM 时钟")}
                      ${nodeMetric(summary?.gpu_pstate_numeric === null || summary?.gpu_pstate_numeric === undefined ? "--" : `P${fmt(summary.gpu_pstate_numeric, 0)}`, "GPU P-State")}
                    </div>
                  </section>
                  <section class="subsection">
                    <div class="subsection-head"><h3>GPU 实时状态</h3><span>${summary?.gpu_count || 0} 块 NVIDIA GPU</span></div>
                    <div class="gpu-list">${renderGpuList(node.gpu || [])}</div>
                  </section>
                  <section class="subsection">
                    <div class="subsection-head"><h3>高速互联与网络</h3><span>${roceLabel}</span></div>
                    <div class="iface-scroll">
                      <div class="iface-table">
                        <div class="iface iface-head"><span>接口</span><span>类型</span><span>状态</span><span>接收 / 发送 Mb/s</span><span>错误/丢弃</span></div>
                        ${renderInterfaces(node.interfaces || [])}
                      </div>
                    </div>
                  </section>
                </div>
              `
          }
        </article>
      `;
    })
    .join("");
}

const severityText: Record<string, string> = {
  ok: "正常",
  warning: "关注",
  critical: "严重",
  idle: "待机",
  insufficient: "积累中",
};

let analysisData: AnalysisResponse | null = null;
let currentAnalysisWindow = 900;

function renderAnalysis(): void {
  const root = $("analysisPanel");
  const selected = analysisData?.windows.find((item) => item.window_seconds === currentAnalysisWindow);
  if (!selected) {
    root.innerHTML = `<div class="analysis-loading">分析数据暂不可用</div>`;
    return;
  }
  root.innerHTML = `
    <div class="analysis-summary ${selected.status}">
      <div>
        <span class="analysis-status-dot"></span>
        <div><strong>${escapeHtml(selected.label)} · ${escapeHtml(severityText[selected.status] || selected.status)}</strong><p>${escapeHtml(selected.summary)}</p></div>
      </div>
      <span>时间覆盖率 ${fmt(selected.coverage, 1)}%</span>
    </div>
    <div class="analysis-grid">
      ${selected.items.map((item) => `
        <article class="analysis-item ${item.severity}">
          <div class="analysis-item-head">
            <h3>${escapeHtml(item.title)}</h3>
            <span>${escapeHtml(severityText[item.severity] || item.severity)}</span>
          </div>
          <strong>${escapeHtml(item.conclusion)}</strong>
          <p>${escapeHtml(item.evidence)}</p>
          <small>${item.id === "inference"
            ? `采集覆盖 ${fmt(item.coverage, 1)}% · 活跃 ${fmt(item.activity_ratio, 1)}% · 事件样本 ${item.event_samples ?? 0}`
            : `有效覆盖 ${fmt(item.coverage, 1)}%${item.provisional ? " · 临时观察" : " · 正式结论"}`}</small>
        </article>
      `).join("")}
    </div>
  `;
}

async function refreshAnalysis(): Promise<void> {
  analysisData = await fetchJson<AnalysisResponse>("/api/analysis");
  renderAnalysis();
}

function drawSeriesChart(
  canvas: HTMLCanvasElement,
  series: Array<{
    label: string;
    color: string;
    values: number[];
    timestamps?: number[];
    axis?: "left" | "right";
    dash?: number[];
    lineWidth?: number;
    opacity?: number;
    maxGapSeconds?: number;
  }>,
  title: string,
  options: { unit?: string; windowSeconds?: number } = {},
): void {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(280, Math.floor(rect.width));
  const height = Math.max(160, Math.floor(rect.height));
  const pixelRatio = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.floor(width * pixelRatio);
  canvas.height = Math.floor(height * pixelRatio);
  ctx.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
  canvas.setAttribute("aria-label", title);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#121614";
  ctx.fillRect(0, 0, width, height);

  const axisSeries = {
    left: series.filter((item) => (item.axis || "left") === "left"),
    right: series.filter((item) => item.axis === "right"),
  };
  const observedMax = {
    left: Math.max(0, ...axisSeries.left.flatMap((item) => item.values).filter(Number.isFinite)),
    right: Math.max(0, ...axisSeries.right.flatMap((item) => item.values).filter(Number.isFinite)),
  };
  const niceAxisMax = (maximum: number): number => {
    const magnitude = 10 ** Math.floor(Math.log10(Math.max(1, maximum)));
    const normalizedMax = maximum / magnitude;
    const niceFactor = normalizedMax <= 1 ? 1 : normalizedMax <= 2 ? 2 : normalizedMax <= 5 ? 5 : 10;
    return Math.max(1, niceFactor * magnitude);
  };
  const axisMax = {
    left: niceAxisMax(observedMax.left),
    right: niceAxisMax(observedMax.right),
  };
  const hasRightAxis = axisSeries.right.length > 0;
  const plot = {
    left: width <= 420 ? 43 : 50,
    right: hasRightAxis ? (width <= 420 ? 43 : 50) : 12,
    top: 45,
    bottom: 29,
  };
  const innerW = width - plot.left - plot.right;
  const innerH = height - plot.top - plot.bottom;
  const allTimestamps = series.flatMap((item) => item.timestamps || []).filter(Number.isFinite);
  const latestTimestamp = Math.max(...allTimestamps);
  const earliestTimestamp = Math.min(...allTimestamps);
  const hasTimeAxis = Number.isFinite(earliestTimestamp) && latestTimestamp > earliestTimestamp;
  const yTickCount = 5;

  const formatAxisValue = (value: number, maximum: number): string => {
    if (maximum < 2) return value.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
    if (maximum < 10) return value.toFixed(1).replace(/\.0$/, "");
    return Math.round(value).toLocaleString("zh-CN");
  };

  const formatAxisTime = (timestamp: number, includeDate: boolean): string => {
    const date = new Date(timestamp * 1000);
    const time = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
    if (!includeDate) return time;
    const day = new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" })
      .format(date)
      .replaceAll("/", "-");
    return `${day} ${time}`;
  };

  ctx.strokeStyle = "#2a332f";
  ctx.lineWidth = 1;
  ctx.font = "10px system-ui";
  ctx.textBaseline = "middle";
  for (let i = 0; i < yTickCount; i += 1) {
    const ratio = i / (yTickCount - 1);
    const y = plot.top + innerH * ratio;
    ctx.beginPath();
    ctx.moveTo(plot.left, y);
    ctx.lineTo(width - plot.right, y);
    ctx.stroke();
    ctx.fillStyle = hasRightAxis ? (axisSeries.left[0]?.color || "#87938d") : "#87938d";
    ctx.textAlign = "right";
    ctx.fillText(formatAxisValue(axisMax.left * (1 - ratio), axisMax.left), plot.left - 7, y);
    if (hasRightAxis) {
      ctx.fillStyle = axisSeries.right[0]?.color || "#87938d";
      ctx.textAlign = "left";
      ctx.fillText(formatAxisValue(axisMax.right * (1 - ratio), axisMax.right), width - plot.right + 7, y);
    }
  }

  if (options.unit) {
    ctx.textBaseline = "alphabetic";
    if (hasRightAxis) {
      ctx.fillStyle = axisSeries.left[0]?.color || "#87938d";
      ctx.textAlign = "left";
      ctx.fillText(`提示 ${options.unit}`, 4, 16);
      ctx.fillText(`峰值 ${observedMax.left.toFixed(1)}`, plot.left, 34);
      ctx.fillStyle = axisSeries.right[0]?.color || "#87938d";
      ctx.textAlign = "right";
      ctx.fillText(`生成 ${options.unit}`, width - 4, 16);
      ctx.fillText(`峰值 ${observedMax.right.toFixed(1)}`, width - plot.right, 34);
    } else {
      ctx.fillStyle = "#87938d";
      ctx.textAlign = "left";
      ctx.fillText(options.unit, 4, 34);
    }
  }

  series.forEach((item) => {
    if (!item.values.length) return;
    ctx.strokeStyle = item.color;
    ctx.lineWidth = item.lineWidth ?? 2;
    ctx.globalAlpha = item.opacity ?? 1;
    ctx.setLineDash(item.dash || []);
    ctx.beginPath();
    item.values.forEach((value, index) => {
      const timestamp = item.timestamps?.[index] ?? Number.NaN;
      const x = hasTimeAxis && Number.isFinite(timestamp)
        ? plot.left + (innerW * (timestamp - earliestTimestamp)) / (latestTimestamp - earliestTimestamp)
        : plot.left + (innerW * index) / Math.max(1, item.values.length - 1);
      const itemAxisMax = item.axis === "right" ? axisMax.right : axisMax.left;
      const y = plot.top + innerH - (clamp(value, 0, itemAxisMax) / itemAxisMax) * innerH;
      const previousTimestamp = item.timestamps?.[index - 1] ?? Number.NaN;
      const exceedsGap = item.maxGapSeconds !== undefined
        && Number.isFinite(timestamp)
        && Number.isFinite(previousTimestamp)
        && timestamp - previousTimestamp > item.maxGapSeconds;
      if (index === 0 || exceedsGap) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
  ctx.globalAlpha = 1;
  ctx.setLineDash([]);

  if (hasTimeAxis) {
    const xTickCount = width <= 420 ? 3 : 5;
    ctx.fillStyle = "#87938d";
    ctx.textBaseline = "alphabetic";
    for (let i = 0; i < xTickCount; i += 1) {
      const ratio = i / (xTickCount - 1);
      const x = plot.left + innerW * ratio;
      const timestamp = earliestTimestamp + (latestTimestamp - earliestTimestamp) * ratio;
      ctx.textAlign = i === 0 ? "left" : i === xTickCount - 1 ? "right" : "center";
      ctx.fillText(formatAxisTime(timestamp, i === 0 || i === xTickCount - 1), x, height - 7);
    }
  }

  if (!hasRightAxis) {
    ctx.fillStyle = "#87938d";
    ctx.textAlign = "right";
    ctx.fillText(`峰值 ${observedMax.left.toFixed(1)}${options.unit ? ` ${options.unit}` : ""}`, width - plot.right, 34);
  }
}

function updateDashboard(snapshot: Snapshot): void {
  const dot = $("liveDot");
  dot.className = `dot ${snapshot.status}`;
  setText("overallStatus", snapshot.status === "ok" ? "正常" : snapshot.status === "warning" ? "警告" : snapshot.status === "critical" ? "严重" : snapshot.status);
  setText("updatedAt", snapshot.updated_at || "--");
  activeModelName = snapshot.model.id || "vLLM model";
  setText("modelLine", activeModelName);
  setText("modelContext", `上下文 ${snapshot.model.max_model_len || "--"}`);
  updateNodeOptions(snapshot.nodes || {});

  const v = snapshot.vllm || ({} as VllmSnapshot);
  const sampleStateText: Record<string, string> = {
    ok: "采集正常",
    warmup: "计数器预热",
    counter_reset: "检测到计数器重置",
    metrics_missing: "核心指标缺失",
    collection_error: "采集失败",
  };
  const eventStateText: Record<string, string> = {
    idle: "服务空闲",
    active: "请求处理中",
    completion_event: "请求已完成",
    unknown: "活动未知",
  };
  setText("metricState", `${sampleStateText[v.sample_state || ""] || "等待采集"} · ${eventStateText[v.event_state || ""] || "状态未知"}`);
  const recentValue = (key: string): number | null | undefined => v.recent?.[key]?.value;
  const markRecentSample = (id: string, key: string): void => {
    const sample = v.recent?.[key];
    $(id).title = sample ? `最近一次有效采样，${Math.round(sample.age_s)} 秒前` : "最近 15 分钟暂无有效请求样本";
  };
  setText("running", fmtInt(v.running));
  setText("waiting", fmtInt(v.waiting));
  setText("kvCache", `${fmt(v.kv_cache_usage_pct, 1)}%`);
  setText("promptRate", fmt(recentValue("prompt_tok_s"), 1));
  setText("decodeRate", fmt(v.generation_tok_s, 1));
  setText("requestRate", fmt(recentValue("request_s"), 2));
  const recentTtft = recentValue("ttft_avg_s");
  const recentE2e = recentValue("e2e_avg_s");
  setText("ttft", recentTtft === null || recentTtft === undefined ? "--" : `${fmt(recentTtft, 2)}s`);
  setText("e2e", recentE2e === null || recentE2e === undefined ? "--" : `${fmt(recentE2e, 2)}s`);
  const recentCacheHit = recentValue("cache_hit_ratio_pct");
  setText("cacheHit", recentCacheHit === null || recentCacheHit === undefined ? "--" : `${fmt(recentCacheHit, 1)}%`);
  const recentPrefillEfficiency = recentValue("prefill_efficiency_tok_s");
  const recentDecodeEfficiency = recentValue("decode_efficiency_tok_s");
  const recentMtpAcceptance = recentValue("mtp_acceptance_pct");
  setText("prefillEfficiency", fmt(recentPrefillEfficiency, 1));
  setText("decodeEfficiency", fmt(recentDecodeEfficiency, 1));
  setText("mtpAcceptance", recentMtpAcceptance === null || recentMtpAcceptance === undefined ? "--" : `${fmt(recentMtpAcceptance, 1)}%`);
  for (const [id, key] of [
    ["promptRate", "prompt_tok_s"],
    ["requestRate", "request_s"],
    ["ttft", "ttft_avg_s"],
    ["e2e", "e2e_avg_s"],
    ["cacheHit", "cache_hit_ratio_pct"],
    ["prefillEfficiency", "prefill_efficiency_tok_s"],
    ["decodeEfficiency", "decode_efficiency_tok_s"],
    ["mtpAcceptance", "mtp_acceptance_pct"],
  ]) markRecentSample(id, key);
  renderAlertsBanner(snapshot);
  renderNodes(snapshot.nodes || {});
  const nodes = Object.values(snapshot.nodes || {});
  const online = nodes.filter((node) => node.status === "ok").length;
  const maxTemp = Math.max(0, ...nodes.map((node) => node.summary?.gpu_temp_max_c || 0));
  setText("nodeSummaryLine", `${online}/${nodes.length || 2} 节点在线 · 最高 ${fmt(maxTemp, 0)}°C`);
}

function metricStatsRow(metric: string, label: string, stats: MetricStats | undefined): string {
  return `
    <tr>
      <td>${label}</td>
      <td>${fmt(stats?.min, 2)}</td>
      <td>${fmt(stats?.avg, 2)}</td>
      <td>${stats?.percentiles_approximate ? "≈" : ""}${fmt(stats?.p95, 2)}</td>
      <td>${stats?.percentiles_approximate ? "≈" : ""}${fmt(stats?.p99, 2)}</td>
      <td>${fmt(stats?.max, 2)}</td>
      <td>${stats?.count ?? 0}</td>
    </tr>
  `;
}

function renderStats(stats: StatsResponse): void {
  const container = $("statsContent");
  const nodeSections = Object.entries(stats.nodes || {}).map(([nodeId, metrics]) => {
    const rows = nodeMetricOptions
      .map((item) => metricStatsRow(item.value, item.label, metrics[item.value]))
      .join("");
    return `
      <article class="panel table-panel">
        <div class="panel-head">
          <h3>节点 ${escapeHtml(nodeLabels.get(nodeId) || nodeId)}</h3>
          <span class="muted">窗口 ${fmt(stats.window_seconds / 3600, 1)} 小时</span>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>指标</th><th>最小</th><th>平均</th><th>P95</th><th>P99</th><th>最大</th><th>样本</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </article>
    `;
  });

  const vllmRows = vllmMetricOptions
    .map((item) => metricStatsRow(item.value, item.label, stats.vllm[item.value]))
    .join("");

  container.innerHTML = `
    <article class="panel sampling-summary">
      <div><span>采集覆盖率</span><strong>${fmt(stats.inference_sampling?.collection_coverage_pct, 1)}%</strong></div>
      <div><span>服务活跃率</span><strong>${fmt(stats.inference_sampling?.activity_ratio_pct, 1)}%</strong></div>
      <div><span>有效采样</span><strong>${stats.inference_sampling?.collected_samples ?? 0}</strong></div>
      <div><span>事件样本</span><strong>${stats.inference_sampling?.event_samples ?? 0}</strong></div>
    </article>
    ${nodeSections.join("")}
    <article class="panel table-panel">
      <div class="panel-head">
        <h3>推理服务 ${escapeHtml(activeModelName)}</h3>
        <span class="muted">窗口 ${fmt(stats.window_seconds / 3600, 1)} 小时</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>指标</th><th>最小</th><th>平均</th><th>P95</th><th>P99</th><th>最大</th><th>样本</th>
          </tr>
        </thead>
        <tbody>${vllmRows}</tbody>
      </table>
    </article>
  `;
}

function renderTrend(trend: TrendResponse): void {
  const metricLabel = currentTrendLabel();
  setText("trendMeta", `窗口 ${formatWindowLabel(trend.window_seconds)} · 粒度 ${formatBucketLabel(trend.bucket_seconds)} · ${trend.timestamps.length} 个点${trend.downsampled ? " · 已自动限点" : ""}`);
  setText("trendTitle", `${metricLabel}`);
  drawSeriesChart(
    $("trendChart") as HTMLCanvasElement,
    [
      {
        label: metricLabel,
        color: "#61c7dc",
        values: trend.values || [],
        timestamps: trend.timestamps || [],
      },
    ],
    metricLabel,
    { unit: metricUnits[trend.metric] || "", windowSeconds: trend.window_seconds },
  );
}

function renderAlertsTable(alerts: AlertsResponse): void {
  const rows = alerts.items
    .map((item) => {
      const levelText = item.level === "ok" ? "正常" : item.level === "warning" ? "警告" : item.level === "critical" ? "严重" : item.level;
      const statusText = item.status === "active" ? "持续中" : "已恢复";
      return `
        <tr>
          <td>${formatTime(item.first_ts)}</td>
          <td><span class="pill ${item.level}">${levelText}</span></td>
          <td>${escapeHtml(item.scope)}</td>
          <td>${escapeHtml(item.message)}</td>
          <td>${statusText}</td>
        </tr>
      `;
    })
    .join("");
  $("alertsContent").innerHTML = `
    <article class="panel table-panel">
      <div class="panel-head">
        <h3>告警历史</h3>
        <span class="muted">共 ${alerts.items.length} 条</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>首次出现</th><th>级别</th><th>范围</th><th>消息</th><th>状态</th>
          </tr>
        </thead>
        <tbody>${rows || `<tr><td colspan="5">暂无告警</td></tr>`}</tbody>
      </table>
    </article>
  `;
}

function formatWindowLabel(seconds: number): string {
  if (seconds >= 86400) return `${Math.round(seconds / 86400)} 天`;
  if (seconds >= 3600) return `${Math.round(seconds / 3600)} 小时`;
  return `${seconds} 秒`;
}

function formatBucketLabel(seconds: number): string {
  if (seconds <= 0) return "原始";
  if (seconds >= 3600) return `${Math.round(seconds / 3600)} 小时`;
  if (seconds >= 60) return `${Math.round(seconds / 60)} 分钟`;
  return `${seconds} 秒`;
}

function currentTrendLabel(): string {
  const scope = ($("trendScope") as HTMLSelectElement).value;
  const metric = ($("trendMetric") as HTMLSelectElement).value;
  const node = ($("trendNode") as HTMLSelectElement).value;
  const option = (scope === "node" ? nodeMetricOptions : vllmMetricOptions).find((item) => item.value === metric);
  const scopeLabel = scope === "node" ? (nodeLabels.get(node) || node) : "推理服务";
  return `${scopeLabel} · ${option?.label || metric}`;
}

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`请求失败 ${response.status}`);
  }
  return (await response.json()) as T;
}

async function refreshSnapshot(): Promise<void> {
  const snapshot = await fetchJson<Snapshot>("/api/snapshot");
  updateDashboard(snapshot);
}

async function refreshDashboardTrend(): Promise<void> {
  const [promptRaw, promptAverage, generationRaw, generationAverage] = await Promise.all([
    fetchJson<TrendResponse>("/api/trends?kind=vllm&metric=prompt_tok_s&window=24h&bucket=raw"),
    fetchJson<TrendResponse>("/api/trends?kind=vllm&metric=prompt_tok_s&window=24h&bucket=15m"),
    fetchJson<TrendResponse>("/api/trends?kind=vllm&metric=generation_tok_s&window=24h&bucket=raw"),
    fetchJson<TrendResponse>("/api/trends?kind=vllm&metric=generation_tok_s&window=24h&bucket=15m"),
  ]);
  drawSeriesChart(
    $("dashboardChart") as HTMLCanvasElement,
    [
      {
        label: "提示入账 / 原始（左轴）",
        color: "#22d3ee",
        values: promptRaw.values || [],
        timestamps: promptRaw.timestamps || [],
        axis: "left",
        lineWidth: 1.25,
        opacity: 0.62,
        maxGapSeconds: 30,
      },
      {
        label: "提示入账 / 采样均值（左轴）",
        color: "#f7c948",
        values: promptAverage.values || [],
        timestamps: promptAverage.timestamps || [],
        axis: "left",
        dash: [7, 5],
        lineWidth: 2.25,
        maxGapSeconds: 1800,
      },
      {
        label: "生成交付 / 原始（右轴）",
        color: "#ff7b72",
        values: generationRaw.values || [],
        timestamps: generationRaw.timestamps || [],
        axis: "right",
        lineWidth: 1.25,
        opacity: 0.72,
        maxGapSeconds: 30,
      },
      {
        label: "生成交付 / 采样均值（右轴）",
        color: "#b28dff",
        values: generationAverage.values || [],
        timestamps: generationAverage.timestamps || [],
        axis: "right",
        dash: [7, 5],
        lineWidth: 2.25,
        maxGapSeconds: 1800,
      },
    ],
    "近 24 小时令牌入账与交付速率",
    { unit: "tok/s", windowSeconds: 86400 },
  );
}

async function refreshStats(): Promise<void> {
  const windowValue = (document.getElementById("statsWindow") as HTMLSelectElement).value;
  const stats = await fetchJson<StatsResponse>(`/api/stats?window=${encodeURIComponent(windowValue)}`);
  renderStats(stats);
}

function updateTrendMetricOptions(): void {
  const scope = ($("trendScope") as HTMLSelectElement).value;
  const metricSelect = $("trendMetric") as HTMLSelectElement;
  const options = scope === "node" ? nodeMetricOptions : vllmMetricOptions;
  metricSelect.innerHTML = options
    .map((item, index) => `<option value="${item.value}"${index === 0 ? " selected" : ""}>${item.label}</option>`)
    .join("");
  $("trendNodeWrap").style.display = scope === "node" ? "inline-block" : "none";
}

async function refreshTrend(): Promise<void> {
  const scope = ($("trendScope") as HTMLSelectElement).value;
  const metric = ($("trendMetric") as HTMLSelectElement).value;
  const node = ($("trendNode") as HTMLSelectElement).value;
  const windowValue = ($("trendWindow") as HTMLSelectElement).value;
  const bucketValue = ($("trendBucket") as HTMLSelectElement).value;
  const params = new URLSearchParams({ kind: scope, metric, window: windowValue, bucket: bucketValue });
  if (scope === "node") params.set("node", node);
  const trend = await fetchJson<TrendResponse>(`/api/trends?${params.toString()}`);
  renderTrend(trend);
}

async function refreshAlerts(): Promise<void> {
  const windowValue = (document.getElementById("alertsWindow") as HTMLSelectElement).value;
  const alerts = await fetchJson<AlertsResponse>(`/api/alerts?window=${encodeURIComponent(windowValue)}`);
  renderAlertsTable(alerts);
}

async function refreshActiveView(): Promise<void> {
  const active = currentView;
  if (active === "stats") {
    await refreshStats();
  } else if (active === "trends") {
    await refreshTrend();
  } else if (active === "alerts") {
    await refreshAlerts();
  }
}

let currentView: ViewName = "dashboard";
let wsBackoffMs = 1000;

function setView(view: ViewName): void {
  currentView = view;
  for (const button of Array.from(document.querySelectorAll<HTMLButtonElement>(".tab"))) {
    button.classList.toggle("active", button.dataset.view === view);
  }
  for (const section of Array.from(document.querySelectorAll<HTMLElement>(".view"))) {
    section.classList.toggle("active", section.id === `${view}View`);
  }
  if (view === "trends") {
    updateTrendMetricOptions();
  }
  void refreshActiveView();
}

function connectWebSocket(): void {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (event: MessageEvent<string>) => {
    const snapshot = JSON.parse(event.data) as Snapshot;
    updateDashboard(snapshot);
  };
  ws.onopen = () => {
    wsBackoffMs = 1000;
  };
  ws.onclose = () => {
    setTimeout(connectWebSocket, wsBackoffMs);
    wsBackoffMs = Math.min(30000, wsBackoffMs * 2);
  };
  ws.onerror = () => {
    ws.close();
  };
}

function wireEvents(): void {
  for (const button of Array.from(document.querySelectorAll<HTMLButtonElement>(".tab"))) {
    button.addEventListener("click", () => {
      const view = button.dataset.view as ViewName | undefined;
      if (view) setView(view);
    });
  }

  const trendControls = ["trendScope", "trendNode", "trendMetric", "trendWindow", "trendBucket"] as const;
  for (const id of trendControls) {
    $(id).addEventListener("change", () => {
      if (id === "trendScope") updateTrendMetricOptions();
      void refreshTrend();
    });
  }
  $("statsWindow").addEventListener("change", () => void refreshStats());
  $("alertsWindow").addEventListener("change", () => void refreshAlerts());
  for (const button of Array.from(document.querySelectorAll<HTMLButtonElement>(".window-button"))) {
    button.addEventListener("click", () => {
      currentAnalysisWindow = Number(button.dataset.analysisWindow || 900);
      for (const item of Array.from(document.querySelectorAll<HTMLButtonElement>(".window-button"))) {
        item.classList.toggle("active", item === button);
      }
      renderAnalysis();
    });
  }
}

async function boot(): Promise<void> {
  wireEvents();
  updateTrendMetricOptions();
  connectWebSocket();
  void refreshSnapshot().catch(console.error);
  void refreshDashboardTrend().catch(console.error);
  void refreshStats().catch(console.error);
  void refreshAlerts().catch(console.error);
  void refreshTrend().catch(console.error);
  void refreshAnalysis().catch(console.error);
  setInterval(() => {
    void refreshDashboardTrend().catch(console.error);
  }, 60000);
  setInterval(() => {
    void refreshActiveView().catch(console.error);
  }, 60000);
  setInterval(() => {
    void refreshAnalysis().catch(console.error);
  }, 60000);
}

void boot();
