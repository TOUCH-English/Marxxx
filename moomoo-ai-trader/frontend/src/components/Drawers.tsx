import { Alert, App, Button, Card, Collapse, Descriptions, Drawer, Empty, Form, Input, Modal, Radio, Space, Spin, Table, Tag, Timeline, Typography } from 'antd';
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';
import { api } from '../api/client';
import type { AICall, DecisionFull, EvidenceItem, FillRow, Intent, IntentFull, PositionRow, Trace } from '../api/types';
import { useAppState } from '../state/app';
import { useAction, useApi } from '../state/hooks';
import { fmtMoney, fmtPrice, fmtRatioPct } from '../utils/decimal';
import { fmtMyt, shortId } from '../utils/format';
import {
  CANCELLABLE_STATUSES,
  DECISION_SOURCE_LABELS,
  FEATURE_LABELS,
  INTENT_REASON_LABELS,
  MODE_LABELS,
  ORDER_STATUS_LABELS,
} from '../utils/labels';
import { ActionTag, ChecksTable, ErrorAlert, JsonBlock, KV, Money, OrderStatusTag, OutcomeTag, SideTag } from './common';

const { Text, Paragraph } = Typography;

interface TraceKey {
  decision_id?: string | null;
  intent_id?: string | null;
}

interface DrawerCtx {
  openDecision: (id: string) => void;
  openOrder: (id: string) => void;
  openTrace: (k: TraceKey) => void;
}

const Ctx = createContext<DrawerCtx | null>(null);

export function useDrawers(): DrawerCtx {
  const c = useContext(Ctx);
  if (!c) throw new Error('DrawerProvider missing');
  return c;
}

export function DrawerProvider({ children }: { children: ReactNode }) {
  const [decisionId, setDecisionId] = useState<string | null>(null);
  const [orderId, setOrderId] = useState<string | null>(null);
  const [trace, setTrace] = useState<TraceKey | null>(null);
  const openDecision = useCallback((id: string) => setDecisionId(id), []);
  const openOrder = useCallback((id: string) => setOrderId(id), []);
  const openTrace = useCallback((k: TraceKey) => setTrace(k), []);
  const v = useMemo(() => ({ openDecision, openOrder, openTrace }), [openDecision, openOrder, openTrace]);
  return (
    <Ctx.Provider value={v}>
      {children}
      <Drawer
        title="决策详情"
        width={Math.min(860, window.innerWidth)}
        open={!!decisionId}
        onClose={() => setDecisionId(null)}
        destroyOnHidden
      >
        {decisionId ? <DecisionDetail id={decisionId} /> : null}
      </Drawer>
      <Drawer title="订单详情" width={Math.min(820, window.innerWidth)} open={!!orderId} onClose={() => setOrderId(null)} destroyOnHidden>
        {orderId ? <OrderDetail id={orderId} /> : null}
      </Drawer>
      <Drawer
        title="因果链：决策 → 风控 → 订单 → 券商事件 → 成交 → 持仓"
        width={Math.min(900, window.innerWidth)}
        open={!!trace}
        onClose={() => setTrace(null)}
        destroyOnHidden
      >
        {trace ? <TraceView k={trace} /> : null}
      </Drawer>
    </Ctx.Provider>
  );
}

// ------------------------------------------------------------------ 决策详情
function conditionText(d: DecisionFull | { proposal: DecisionFull['proposal'] }) {
  const p = d.proposal;
  if (!p) return { entry: '—', exit: '—' };
  const e = p.entry_condition;
  let entry = '—';
  if (e) {
    entry = e.type === 'IMMEDIATE' ? '立即（按限价）' : e.type === 'LIMIT' || e.type === 'PRICE_BELOW' ? `价格不高于 ${fmtPrice(e.price ?? null)}` : `${e.type ?? ''} ${e.price ? fmtPrice(e.price) : ''}`;
  }
  if (p.max_buy_price) entry += `；最高买价 ${fmtPrice(p.max_buy_price)}`;
  const x = p.exit_condition;
  const parts: string[] = [];
  if (x?.stop_loss_price) parts.push(`止损 ${fmtPrice(x.stop_loss_price)}`);
  if (x?.take_profit_price) parts.push(`止盈 ${fmtPrice(x.take_profit_price)}`);
  if (x?.max_holding_days) parts.push(`最长持有 ${x.max_holding_days} 天`);
  if (p.sell_fraction) parts.push(`卖出比例 ${fmtRatioPct(p.sell_fraction)}`);
  if (p.min_sell_price) parts.push(`最低卖价 ${fmtPrice(p.min_sell_price)}`);
  return { entry, exit: parts.length ? parts.join('；') : '—' };
}

export { conditionText };

function evidenceIsNews(e: EvidenceItem) {
  return e.type === 'news' || !!e.url;
}

export function DecisionDetail({ id }: { id: string }) {
  const { ns } = useAppState();
  const { openOrder, openTrace } = useDrawers();
  const { data: d, error, loading } = useApi<DecisionFull>(`/api/${ns}/decisions/${encodeURIComponent(id)}`);
  const [raw, setRaw] = useState<AICall | null>(null);
  const [rawErr, setRawErr] = useState<string>();
  const [rawLoading, setRawLoading] = useState(false);

  if (error) return <ErrorAlert error={error} />;
  if (!d) return <Spin spinning={loading}><div style={{ minHeight: 120 }} /></Spin>;
  const cond = conditionText(d);
  const rc = d.rule_calc;
  const news = (d.evidence ?? []).filter(evidenceIsNews);
  const loadRaw = async () => {
    if (!d.ai_call_id) return;
    setRawLoading(true);
    try {
      setRaw(await api.get<AICall>(`/api/${ns}/ai-calls/${d.ai_call_id}`));
      setRawErr(undefined);
    } catch (e) {
      setRawErr(String((e as Error).message));
    } finally {
      setRawLoading(false);
    }
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card size="small" title="概要">
        <Space wrap style={{ marginBottom: 8 }}>
          <Text strong style={{ fontSize: 16 }}>{d.symbol}</Text>
          <ActionTag action={d.action} />
          <OutcomeTag outcome={d.outcome} label={d.outcome_label} />
          <Tag>{DECISION_SOURCE_LABELS[d.source] ?? d.source}</Tag>
        </Space>
        <Descriptions
          size="small"
          column={{ xs: 1, sm: 1, md: 2 }}
          items={[
            { key: 'reason', label: '执行结果', children: d.outcome_reason || '—', span: 2 },
            { key: 'asof', label: '数据时间（马来西亚）', children: fmtMyt(d.as_of) },
            { key: 'exp', label: '有效至', children: fmtMyt(d.expires_at) },
            { key: 'entry', label: '入场条件', children: cond.entry },
            { key: 'exit', label: '退出条件', children: cond.exit },
            { key: 'sv', label: '策略版本', children: d.strategy_version },
            { key: 'mode', label: '模式 / 数据', children: `${MODE_LABELS[d.mode] ?? d.mode} / ${d.data_source}` },
          ]}
        />
        <Space style={{ marginTop: 8 }}>
          <Button size="small" onClick={() => openTrace({ decision_id: d.id })}>查看因果链</Button>
        </Space>
      </Card>

      <Card size="small" title="① 事实（当时可见的数据）">
        {d.facts ? (
          <>
            {typeof d.facts.news_status === 'string' ? <Alert type="info" showIcon message={d.facts.news_status} style={{ marginBottom: 8 }} /> : null}
            {typeof d.facts.market_view === 'string' ? <Paragraph type="secondary">{d.facts.market_view}</Paragraph> : null}
            <Descriptions size="small" column={{ xs: 1, md: 2 }} items={[
              { key: 'last', label: '最新价', children: fmtPrice(d.facts.last_price as string | null) },
              { key: 'ds', label: '数据来源', children: String(d.facts.data_source ?? d.data_source) },
            ]} />
            {d.facts.features ? (
              <div style={{ marginTop: 8 }}>
                <KV data={d.facts.features as Record<string, unknown>} labels={FEATURE_LABELS} />
              </div>
            ) : null}
          </>
        ) : (
          <Text type="secondary">暂无数据</Text>
        )}
      </Card>

      <Card size="small" title="② 规则计算（程序风控，确定性）">
        {rc ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Space wrap>
              <Tag color={rc.ok ? 'green' : 'volcano'}>{rc.ok ? '风控检查全部通过' : '风控未通过'}</Tag>
              {rc.qty !== undefined ? <Tag>计算数量 {rc.qty}</Tag> : null}
              {rc.limit_price ? <Tag>限价 {fmtPrice(rc.limit_price)}</Tag> : null}
              {rc.est_fee_buy ? <Tag>预估买入费用 {fmtMoney(rc.est_fee_buy)} {d.currency}</Tag> : null}
              {rc.est_fee_sell ? <Tag>预估卖出费用 {fmtMoney(rc.est_fee_sell)} {d.currency}</Tag> : null}
            </Space>
            <ChecksTable checks={rc.checks} />
            {rc.sizing ? (
              <>
                <Text strong>数量计算过程</Text>
                <KV data={rc.sizing} />
              </>
            ) : null}
            {Object.keys(rc).some((k) => !['ok', 'checks', 'sizing', 'qty', 'limit_price', 'est_fee_buy', 'est_fee_sell'].includes(k)) ? (
              <KV
                data={Object.fromEntries(Object.entries(rc).filter(([k]) => !['ok', 'checks', 'sizing', 'qty', 'limit_price', 'est_fee_buy', 'est_fee_sell'].includes(k)))}
              />
            ) : null}
          </Space>
        ) : (
          <Text type="secondary">此决策没有进入风控计算（例如：不交易 / 继续持有 / 输出无效）。</Text>
        )}
      </Card>

      <Card size="small" title="③ 模型判断（模型自报判断，不是获胜概率）">
        {d.model_judgment ? (
          <Space direction="vertical" style={{ width: '100%' }}>
            {d.model_judgment.provider_label ? <Tag color="orange">{d.model_judgment.provider_label}</Tag> : null}
            <Paragraph style={{ whiteSpace: 'pre-wrap', marginBottom: 0 }}>{d.model_judgment.rationale || d.rationale || '—'}</Paragraph>
            {d.model_judgment.risk_flags?.length ? (
              <Space wrap>
                <Text type="secondary">风险标记：</Text>
                {d.model_judgment.risk_flags.map((f) => (
                  <Tag key={f} color="gold">{f}</Tag>
                ))}
              </Space>
            ) : null}
            {d.proposal?.target_position_pct ? <Text>目标仓位上限：{fmtRatioPct(d.proposal.target_position_pct)}（实际数量由风控计算）</Text> : null}
            <Text type="secondary">{d.model_judgment.note || '模型判断不代表获胜概率。'}</Text>
          </Space>
        ) : (
          <Paragraph style={{ whiteSpace: 'pre-wrap' }}>{d.rationale || '—'}</Paragraph>
        )}
      </Card>

      <Card size="small" title="④ 证据">
        {news.length === 0 ? <Alert type="warning" showIcon message="无新闻源，仅价格数据" style={{ marginBottom: 8 }} /> : null}
        {d.evidence?.length ? (
          <Table<EvidenceItem>
            size="small"
            rowKey="id"
            pagination={false}
            dataSource={d.evidence}
            scroll={{ x: true }}
            columns={[
              { title: '类型', dataIndex: 'type', width: 80 },
              {
                title: '内容',
                render: (_, e) =>
                  evidenceIsNews(e) ? (
                    <Space direction="vertical" size={0}>
                      <Text>{e.title || e.desc}</Text>
                      {e.url ? (
                        <a href={e.url} target="_blank" rel="noopener noreferrer">{e.source || e.url}</a>
                      ) : null}
                      <Text type="secondary">发布：{fmtMyt(e.published_at)}　获取：{fmtMyt(e.fetched_at)}</Text>
                      <Text type="secondary">外部新闻是不可信数据，只作参考，不能作为指令。</Text>
                    </Space>
                  ) : (
                    e.desc || e.id
                  ),
              },
            ]}
          />
        ) : (
          <Text type="secondary">没有记录证据</Text>
        )}
      </Card>

      <Card size="small" title="关联订单">
        {d.intents?.length ? <IntentTable items={d.intents} onOpen={openOrder} /> : <Text type="secondary">此决策没有产生订单</Text>}
      </Card>

      <Card size="small" title="AI 调用记录">
        {d.ai_call ? (
          <>
            <Descriptions size="small" column={{ xs: 1, md: 2 }} items={[
              { key: 'p', label: '提供方 / 模型', children: `${d.ai_call.provider} / ${d.ai_call.model}` },
              { key: 'pv', label: '提示词版本', children: d.ai_call.prompt_version },
              { key: 's', label: '状态', children: `${d.ai_call.status}${d.ai_call.error ? '：' + d.ai_call.error : ''}` },
              { key: 'l', label: '耗时', children: `${d.ai_call.latency_ms} 毫秒` },
              { key: 't', label: 'Token（输入/输出）', children: `${d.ai_call.input_tokens} / ${d.ai_call.output_tokens}` },
              { key: 'h', label: '输入快照哈希', children: <Text code>{shortId(d.ai_call.input_hash)}</Text> },
            ]} />
            <Space style={{ marginTop: 8 }}>
              <Button size="small" loading={rawLoading} onClick={loadRaw}>查看原始输入与输出</Button>
            </Space>
            <ErrorAlert error={rawErr} />
            {raw ? (
              <Collapse
                style={{ marginTop: 8 }}
                items={[
                  { key: 'in', label: '输入快照（发送给模型的数据）', children: <JsonBlock value={raw.input_snapshot} /> },
                  { key: 'out', label: '模型原始输出', children: <JsonBlock value={raw.output_text} /> },
                ]}
              />
            ) : null}
          </>
        ) : (
          <Text type="secondary">此决策不是由 AI 调用产生（例如程序保护规则），或没有调用记录。</Text>
        )}
      </Card>
    </Space>
  );
}

export function IntentTable({ items, onOpen }: { items: Intent[]; onOpen: (id: string) => void }) {
  return (
    <Table<Intent>
      size="small"
      rowKey="id"
      pagination={false}
      dataSource={items}
      scroll={{ x: true }}
      onRow={(r) => ({ onClick: () => onOpen(r.id), style: { cursor: 'pointer' } })}
      columns={[
        { title: '方向', dataIndex: 'side', render: (s: string) => <SideTag side={s} /> },
        { title: '原因', dataIndex: 'reason', render: (r: string) => INTENT_REASON_LABELS[r] ?? r },
        { title: '数量', dataIndex: 'qty' },
        { title: '限价', dataIndex: 'limit_price', render: (v: string | null) => fmtPrice(v) },
        { title: '状态', render: (_, r) => <OrderStatusTag status={r.status} label={r.status_label} /> },
        { title: '已成交', render: (_, r) => `${r.filled_qty}${r.avg_fill_price ? ' @ ' + fmtPrice(r.avg_fill_price) : ''}` },
        { title: '券商订单号', dataIndex: 'broker_order_id', render: (v: string | null) => v || '—' },
      ]}
    />
  );
}

// ------------------------------------------------------------------ 订单详情
export function OrderDetail({ id }: { id: string }) {
  const { ns } = useAppState();
  const { openDecision, openTrace } = useDrawers();
  const { data: o, error, loading, reload } = useApi<IntentFull>(`/api/${ns}/orders/${encodeURIComponent(id)}`);
  const { run, busy } = useAction();
  const { modal } = App.useApp();
  const [resolveOpen, setResolveOpen] = useState(false);

  if (error) return <ErrorAlert error={error} />;
  if (!o) return <Spin spinning={loading}><div style={{ minHeight: 120 }} /></Spin>;

  const cancel = () =>
    modal.confirm({
      title: '撤销此订单？',
      content: '撤单请求会交给后台执行；撤单是否成功以券商（或本地模拟撮合器）确认为准，撤单期间仍可能成交。',
      okText: '发出撤单请求',
      cancelText: '不撤单',
      onOk: async () => {
        await run(() => api.post(`/api/${ns}/orders/${o.id}/cancel`), { success: (r: unknown) => (r as { note?: string }).note ?? '撤单请求已发出' });
        void reload();
      },
    });

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Card size="small">
        <Space wrap style={{ marginBottom: 8 }}>
          <Text strong style={{ fontSize: 16 }}>{o.symbol}</Text>
          <SideTag side={o.side} />
          <OrderStatusTag status={o.status} label={o.status_label} />
          {o.needs_manual_review ? <Tag color="magenta">需要人工核对</Tag> : null}
        </Space>
        {o.status === 'ACKNOWLEDGED' || o.status === 'SUBMITTING' ? (
          <Alert type="info" showIcon style={{ marginBottom: 8 }} message="「已提交」只表示券商已接收委托，不等于已经买入/卖出；以成交记录为准。" />
        ) : null}
        <Descriptions
          size="small"
          bordered
          column={{ xs: 1, sm: 1, md: 2 }}
          items={[
            { key: 'reason', label: '原因', children: INTENT_REASON_LABELS[o.reason] ?? o.reason },
            { key: 'qty', label: '委托数量', children: o.qty },
            { key: 'lp', label: '限价', children: fmtPrice(o.limit_price) },
            { key: 'ref', label: '参考价', children: fmtPrice(o.ref_price) },
            { key: 'fq', label: '已成交数量', children: o.filled_qty },
            { key: 'afp', label: '成交均价', children: fmtPrice(o.avg_fill_price) },
            { key: 'fee', label: '实际费用', children: <Money v={o.fees} ccy={o.currency} /> },
            { key: 'est', label: '预估费用', children: <Money v={o.est_fees} ccy={o.currency} /> },
            { key: 'bid', label: '券商订单号', children: o.broker_order_id || '尚无（未被券商接收）' },
            { key: 'sd', label: '状态说明', children: o.status_detail || '—' },
            { key: 'sp', label: '止损 / 止盈', children: `${fmtPrice(o.stop_price)} / ${fmtPrice(o.take_profit_price)}` },
            { key: 'exp', label: '有效至', children: fmtMyt(o.expires_at) },
            { key: 'asof', label: '业务时间', children: fmtMyt(o.as_of) },
            { key: 'upd', label: '最后更新', children: fmtMyt(o.updated_at_utc, true) },
            { key: 'sv', label: '策略版本', children: o.strategy_version },
            { key: 'mode', label: '模式', children: MODE_LABELS[o.mode] ?? o.mode },
          ]}
        />
        <Space wrap style={{ marginTop: 8 }}>
          {o.decision_id ? <Button size="small" onClick={() => openDecision(o.decision_id!)}>查看来源决策</Button> : null}
          <Button size="small" onClick={() => openTrace({ intent_id: o.id })}>查看因果链</Button>
          {CANCELLABLE_STATUSES.includes(o.status) ? <Button size="small" danger loading={busy} onClick={cancel}>撤单</Button> : null}
          {o.status === 'AWAITING_APPROVAL' ? <ApproveButtons id={o.id} onDone={reload} /> : null}
          {o.status === 'UNKNOWN' ? <Button size="small" type="primary" onClick={() => setResolveOpen(true)}>人工核对</Button> : null}
        </Space>
      </Card>
      <Card size="small" title="风控检查">
        {o.risk_check ? <ChecksTable checks={o.risk_check.checks} /> : <Text type="secondary">无（例如保护性退出或手动清仓）</Text>}
      </Card>
      <Card size="small" title="订单事件时间线">
        {o.events.length ? (
          <Timeline
            items={o.events.map((e) => ({
              key: e.id,
              color: e.to_status === 'FILLED' ? 'green' : e.to_status === 'REJECTED' || e.to_status === 'UNKNOWN' ? 'red' : 'blue',
              children: (
                <div>
                  <Text strong>{e.event}</Text>{' '}
                  <Text type="secondary">
                    {e.from_status ? ORDER_STATUS_LABELS[e.from_status] ?? e.from_status : '—'} → {e.to_status ? ORDER_STATUS_LABELS[e.to_status] ?? e.to_status : '—'}
                  </Text>
                  <div>{e.detail}</div>
                  <Text type="secondary" style={{ fontSize: 12 }}>{fmtMyt(e.created_at_utc, true)}（记录时间）</Text>
                </div>
              ),
            }))}
          />
        ) : (
          <Text type="secondary">暂无事件</Text>
        )}
      </Card>
      <Card size="small" title="成交明细">
        <FillsTable items={o.fills} />
      </Card>
      <ResolveModal open={resolveOpen} intent={o} onClose={() => setResolveOpen(false)} onDone={reload} />
    </Space>
  );
}

export function FillsTable({ items }: { items: FillRow[] }) {
  if (!items.length) return <Text type="secondary">暂无成交</Text>;
  return (
    <Table<FillRow>
      size="small"
      rowKey="id"
      pagination={items.length > 20 ? { pageSize: 20 } : false}
      dataSource={items}
      scroll={{ x: true }}
      columns={[
        { title: '成交时间（马来西亚）', dataIndex: 'filled_at', render: (v: string) => fmtMyt(v) },
        { title: '证券', dataIndex: 'symbol' },
        { title: '方向', dataIndex: 'side', render: (s: string) => <SideTag side={s} /> },
        { title: '数量', dataIndex: 'qty' },
        { title: '价格', dataIndex: 'price', render: (v: string) => fmtPrice(v) },
        { title: '费用', render: (_, f) => <span><Money v={f.fee} ccy={f.currency} />{f.fee_estimated ? <Tag style={{ marginLeft: 4 }}>估算</Tag> : null}</span> },
        { title: '券商', dataIndex: 'broker' },
        { title: '成交编号', dataIndex: 'broker_fill_id' },
      ]}
    />
  );
}

export function ApproveButtons({ id, onDone }: { id: string; onDone?: () => void }) {
  const { ns } = useAppState();
  const { run, busy } = useAction();
  return (
    <Space>
      <Button
        size="small"
        type="primary"
        loading={busy}
        onClick={async () => {
          await run(() => api.post(`/api/${ns}/orders/${id}/approve`), { success: '已确认，订单将提交' });
          onDone?.();
        }}
      >
        确认提交
      </Button>
      <Button
        size="small"
        danger
        loading={busy}
        onClick={async () => {
          await run(() => api.post(`/api/${ns}/orders/${id}/reject`), { success: '已拒绝此订单' });
          onDone?.();
        }}
      >
        拒绝
      </Button>
    </Space>
  );
}

function ResolveModal({ open, intent, onClose, onDone }: { open: boolean; intent: Intent; onClose: () => void; onDone: () => void }) {
  const { ns } = useAppState();
  const { run, busy } = useAction();
  const [form] = Form.useForm<{ resolution: string; broker_order_id?: string; note: string }>();
  const resolution = Form.useWatch('resolution', form);
  return (
    <Modal
      open={open}
      title={`人工核对状态未知的订单（${intent.symbol}）`}
      okText="提交核对结果"
      cancelText="取消"
      confirmLoading={busy}
      onCancel={onClose}
      onOk={async () => {
        const v = await form.validateFields();
        const r = await run(() => api.post<{ status: string }>(`/api/${ns}/orders/${intent.id}/resolve`, v), {
          success: (x) => `已记录核对结果，订单状态：${ORDER_STATUS_LABELS[x.status] ?? x.status}`,
        });
        if (r) {
          onClose();
          onDone();
        }
      }}
    >
      <Paragraph type="secondary">
        请先在 moomoo 官方 App 或 OpenD 中查看是否存在这笔委托。本操作只记录核对结果，<Text strong>不会重新发送订单</Text>。
      </Paragraph>
      <Form form={form} layout="vertical" initialValues={{ resolution: 'NOT_SUBMITTED', note: '' }}>
        <Form.Item name="resolution" label="核对结果" rules={[{ required: true }]}>
          <Radio.Group>
            <Space direction="vertical">
              <Radio value="NOT_SUBMITTED">券商那边没有这笔委托（未提交）</Radio>
              <Radio value="ATTACH">券商那边有这笔委托，关联券商订单号</Radio>
            </Space>
          </Radio.Group>
        </Form.Item>
        {resolution === 'ATTACH' ? (
          <Form.Item name="broker_order_id" label="券商订单号" rules={[{ required: true, message: '请填写在 moomoo 中看到的订单号' }]}>
            <Input placeholder="在 moomoo 订单记录中查到的订单号" />
          </Form.Item>
        ) : null}
        <Form.Item name="note" label="核对说明" rules={[{ required: true, message: '请简单说明核对方式' }]}>
          <Input.TextArea rows={2} placeholder="例如：已在 moomoo App 订单记录中核对，没有这笔委托" />
        </Form.Item>
      </Form>
    </Modal>
  );
}

// ------------------------------------------------------------------ 因果链
export function TraceView({ k }: { k: TraceKey }) {
  const { ns } = useAppState();
  const { openDecision, openOrder } = useDrawers();
  const q = new URLSearchParams();
  if (k.decision_id) q.set('decision_id', k.decision_id);
  if (k.intent_id) q.set('intent_id', k.intent_id);
  const { data: t, error, loading } = useApi<Trace>(`/api/${ns}/trace?${q.toString()}`);
  if (error) return <ErrorAlert error={error} />;
  if (!t) return <Spin spinning={loading}><div style={{ minHeight: 120 }} /></Spin>;
  const d = t.decision;
  const positions = t.positions.filter((p): p is PositionRow => !!p);
  return (
    <Timeline
      items={[
        {
          key: 'd',
          color: 'blue',
          children: (
            <Card size="small" title="1. 决策">
              {d ? (
                <Space direction="vertical" style={{ width: '100%' }}>
                  <Space wrap>
                    <Text strong>{d.symbol}</Text>
                    <ActionTag action={d.action} />
                    <OutcomeTag outcome={d.outcome} label={d.outcome_label} />
                    <Text type="secondary">{fmtMyt(d.as_of)}</Text>
                  </Space>
                  <Text>{d.rationale}</Text>
                  <Button size="small" onClick={() => openDecision(d.id)}>打开决策详情</Button>
                </Space>
              ) : (
                <Text type="secondary">没有关联决策（例如程序保护或手动清仓产生的订单）</Text>
              )}
            </Card>
          ),
        },
        {
          key: 'r',
          color: d?.rule_calc?.ok === false ? 'red' : 'green',
          children: (
            <Card size="small" title="2. 风控">
              {d?.rule_calc ? <ChecksTable checks={d.rule_calc.checks} /> : <Text type="secondary">无风控计算记录</Text>}
            </Card>
          ),
        },
        {
          key: 'o',
          color: 'blue',
          children: (
            <Card size="small" title="3. 订单意图与券商事件">
              {t.intents.length ? (
                t.intents.map((i) => (
                  <div key={i.id} style={{ marginBottom: 12 }}>
                    <Space wrap>
                      <SideTag side={i.side} />
                      <Text>{i.symbol} × {i.qty} @ {fmtPrice(i.limit_price)}</Text>
                      <OrderStatusTag status={i.status} label={i.status_label} />
                      <Button size="small" onClick={() => openOrder(i.id)}>订单详情</Button>
                    </Space>
                    <ul className="compact-list">
                      {i.events.map((e) => (
                        <li key={e.id}>
                          {e.event}：{e.from_status ? ORDER_STATUS_LABELS[e.from_status] ?? e.from_status : '—'} → {e.to_status ? ORDER_STATUS_LABELS[e.to_status] ?? e.to_status : '—'} {e.detail}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))
              ) : (
                <Text type="secondary">没有产生订单</Text>
              )}
            </Card>
          ),
        },
        {
          key: 'f',
          color: 'green',
          children: (
            <Card size="small" title="4. 成交">
              <FillsTable items={t.intents.flatMap((i) => i.fills)} />
            </Card>
          ),
        },
        {
          key: 'p',
          color: 'gray',
          children: (
            <Card size="small" title="5. 持仓">
              {positions.length ? (
                positions.map((p) => (
                  <Descriptions
                    key={p.id}
                    size="small"
                    column={{ xs: 1, md: 3 }}
                    items={[
                      { key: 's', label: '证券', children: p.symbol },
                      { key: 'st', label: '状态', children: p.status === 'OPEN' ? '持有中' : '已平仓' },
                      { key: 'q', label: '数量', children: p.qty },
                      { key: 'c', label: '成本', children: fmtPrice(p.avg_cost) },
                      { key: 'r', label: '已实现盈亏', children: <Money v={p.realized_pnl} ccy={p.currency} pnl /> },
                      { key: 'stop', label: '止损/止盈', children: `${fmtPrice(p.stop_price)} / ${fmtPrice(p.take_profit_price)}` },
                    ]}
                  />
                ))
              ) : (
                <Text type="secondary">没有关联持仓</Text>
              )}
            </Card>
          ),
        },
        {
          key: 'a',
          color: 'gray',
          children: (
            <Card size="small" title="审计记录">
              {t.audit.length ? (
                <ul className="compact-list">
                  {t.audit.map((a) => (
                    <li key={a.id}>
                      <Text type="secondary">{fmtMyt(a.as_of || a.created_at_utc)}</Text> [{a.category}/{a.action}] {a.message}
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无审计记录" />
              )}
            </Card>
          ),
        },
      ]}
    />
  );
}
