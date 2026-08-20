# vLLM 指标语义与数据模型

## 问题背景

旧版监控在写入 `vllm_metrics` 时，会把提示和生成速率的 `0` 转换成
`NULL`。与此同时，采集器也用 `0` 表示首次采样、计数器重置和指标缺失。
因此数据库中的一个 `NULL` 同时可能表示：

- 服务在线但空闲；
- 当前采样间隔没有 token 或请求完成事件；
- 监控刚启动，尚无前一个计数器样本；
- vLLM 重启后累计计数器归零；
- `/metrics` 中缺少指定指标；
- 网络或解析错误导致采集失败。

这些状态无法仅凭旧记录还原。旧数据继续保留，但不对历史 `NULL` 做推测性回填。

## v2 状态模型

每个 vLLM 采样记录两个独立状态：

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `sample_state` | `ok` | `/metrics` 可访问，核心指标完整，增量有效 |
|  | `warmup` | 首次有效采样，需等待下一次采样计算增量 |
|  | `counter_reset` | 一个或多个累计计数器减小，通常表示 vLLM 重启 |
|  | `metrics_missing` | `/metrics` 可访问，但缺少核心指标 |
|  | `collection_error` | HTTP、超时或解析失败 |
| `event_state` | `idle` | 采集有效，当前间隔没有请求活动 |
|  | `active` | 有运行、排队或 token 增量 |
|  | `completion_event` | 当前间隔有请求完成 |
|  | `unknown` | 当前采样不能可靠判断活动状态 |

`missing_metrics` 保存缺失指标名称，`counter_reset` 明确记录计数器是否重置。

## 0 与 NULL

- 实时 gauge（运行中、等待中、KV 缓存）在服务空闲时保存 `0`。
- 有效采样中的 token、请求、错误和抢占增量保存 `0`，用于计算服务活跃率。
- TTFT、端到端延迟、排队/Prefill/Decode 时长等事件指标，在当前间隔没有
  对应请求完成时保存 `NULL`。此时它们在数学上没有定义，不应伪造为 `0`。
- 首次采样、计数器重置、指标缺失和采集失败时，受影响的增量及派生指标保存
  `NULL`，并由 `sample_state` 解释原因。

## 吞吐与效率

`prompt_tok_s` 和 `generation_tok_s` 按 Prometheus 累计 token 增量除以监控采样
间隔计算。它们分别表示提示 token 入账速率和生成 token 交付速率，适合观察业务
流量，但不等同于 GPU 的纯 Prefill/Decode 计算效率。长提示、缓存命中以及请求在
某个 2.5 秒采样间隔集中结算，都可能产生很高的瞬时峰值。

生产效率使用 vLLM 原生直方图计算：

```text
prefill_efficiency_tok_s =
  delta(request_prefill_kv_computed_tokens_sum)
  / delta(request_prefill_time_seconds_sum)

decode_efficiency_tok_s =
  delta(request_generation_tokens_sum)
  / delta(request_decode_time_seconds_sum)

mtp_acceptance_pct =
  delta(spec_decode_num_accepted_tokens_total)
  / delta(spec_decode_num_draft_tokens_total) * 100
```

这些指标仍受请求长度、并发度、缓存命中和部署参数影响。性能基线应至少按
`deployment_id` 分段，并在相近提示长度、输出长度、缓存命中和并发负载下比较。

## 统计口径

- `采集覆盖率`：时间窗内成功取得核心 vLLM 指标的采样比例。
- `服务活跃率`：有效采样中处于 `active` 或 `completion_event` 的比例。
- `事件样本数`：时间窗内至少发生 token 或请求事件的采样数。
- 吞吐均值：仅对有 token 增量的采样做 `sum / count`，不会用时间窗长度作除数。
- 容量均值：总 token 增量除以有效采样间隔总时长，用于估算整段时间的实际产量。

## 迁移与兼容

SQLite 启动迁移只执行 `ALTER TABLE ADD COLUMN`，不会删除已有表或记录。旧记录的
`sample_state` 为空，分析器会根据旧字段做兼容判断；但旧版已经折叠的 `NULL` 无法
恢复成精确的空闲或异常原因。所有新语义从部署 v2 采集器之后开始生效。

监控始终保持只读：它读取 SSH 系统探针和 vLLM `/metrics`，不会自动修改或重启
推理服务，也不会根据分析结果自动调整生产参数。
