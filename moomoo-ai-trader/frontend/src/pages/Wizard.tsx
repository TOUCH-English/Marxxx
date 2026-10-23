import { Alert, Button, Card, Checkbox, Col, Descriptions, Divider, Empty, InputNumber, Radio, Row, Select, Space, Steps, Switch, Table, Tag, Typography } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, qs } from '../api/client';
import type { InstrumentRow, Items, LiveGate, RiskInfo, StrategyVersion, UniverseInfo, UniverseMember, UniversePreview, WatchItem, WizardPreview } from '../api/types';
import { ChecksTable, ErrorAlert, Explain, PageTitle } from '../components/common';
import { useAppState } from '../state/app';
import { useAction, useApi } from '../state/hooks';
import { compareDecimals, fmtMoney, fmtPrice, fmtRatioPct, isDecimalString } from '../utils/decimal';
import { fmtValue } from '../utils/format';
import { MARKET_LABELS, RISK_PARAM_HELP, SEC_TYPE_LABELS, isRatioParam } from '../utils/labels';

const { Text, Paragraph, Title } = Typography;

const SIM_MODES = [
  {
    value: 'DEMO',
    title: '演示 DEMO',
    desc: '使用固定的虚构证券数据和本地模拟成交。不需要 moomoo 账户，最适合第一次了解系统。',
    need: '无需任何准备',
  },
  {
    value: 'LOCAL_PAPER',
    title: '本地纸上交易 LOCAL_PAPER',
    desc: '使用 moomoo 行情（需 OpenD 已登录）或您导入的历史数据回放，本地模拟成交，不会向券商下单。',
    need: '需要：OpenD 可连接，或已在「日志与设置 → 数据导入」导入历史数据',
  },
  {
    value: 'BROKER_PAPER',
    title: '券商模拟 BROKER_PAPER',
    desc: '在 moomoo 券商模拟账户里自动下单（虚拟资金）。',
    need: '需要：在「连接与权限诊断」检测通过并选择券商模拟账户',
  },
];

const LIVE_MODES = [
  {
    value: 'LIVE_AUTO',
    title: '真实自动交易 LIVE_AUTO（主流程）',
    desc: 'AI 在您预先授权的资金、亏损线和范围内自行判断买卖，风控通过后自动下单，不需要逐笔确认。',
    need: '需要：实盘待核实清单全部通过（默认关闭）',
  },
  {
    value: 'LIVE_REVIEW',
    title: '实盘逐笔确认 LIVE_REVIEW（可选，非主流程）',
    desc: '每笔订单需要您在网页上确认后才提交。适合想先观察的用户。',
    need: '需要：实盘待核实清单全部通过（默认关闭）',
  },
];

type Ccy = 'MYR' | 'USD';
const CCYS: Ccy[] = ['MYR', 'USD'];

interface WizardState {
  mode: string;
  useCcy: Record<Ccy, boolean>;
  capital: Record<Ccy, string>;
  loss: Record<Ccy, string>;
  scopeType: 'POOL' | 'WHITELIST';
  symbols: string[];
  overnight: boolean;
  days: number;
  strategy: 'AI_AUTONOMOUS' | 'BASELINE_MA';
}

function initState(ns: string): WizardState {
  return ns === 'live'
    ? {
        mode: 'LIVE_AUTO',
        useCcy: { MYR: true, USD: false },
        capital: { MYR: '', USD: '' },
        loss: { MYR: '', USD: '' },
        scopeType: 'POOL',
        symbols: [],
        overnight: true,
        days: 7,
        strategy: 'AI_AUTONOMOUS',
      }
    : {
        mode: 'DEMO',
        useCcy: { MYR: true, USD: true },
        capital: { MYR: '20000', USD: '5000' },
        loss: { MYR: '1000', USD: '250' },
        scopeType: 'POOL',
        symbols: [],
        overnight: true,
        days: 30,
        strategy: 'AI_AUTONOMOUS',
      };
}

function positive(v: string) {
  return isDecimalString(v) && compareDecimals(v, '0') > 0;
}

function PoolRuleView({ info, ruleId }: { info: UniverseInfo | undefined; ruleId: number | undefined }) {
  const r = info?.rules.find((x) => x.id === ruleId) ?? info?.rules[0];
  if (!r) return <Alert type="warning" showIcon message="还没有股票池规则" description="实盘默认范围为空。请先到「策略与风控设置 → 股票池规则」新建并确认一个规则，或改用手动白名单。" />;
  const rules = r.rules;
  const per = (m?: Record<string, string>) => (m ? Object.entries(m).map(([k, v]) => `${MARKET_LABELS[k] ?? k} ${fmtMoney(v, 2)}`).join('；') : '—');
  return (
    <Descriptions
      size="small"
      bordered
      column={{ xs: 1, md: 2 }}
      title={<Space>规则 v{r.version}{r.confirmed_by_user ? <Tag color="green">已确认</Tag> : <Tag color="orange">待确认（启动时一并确认）</Tag>}</Space>}
      items={[
        { key: 'm', label: info?.labels.markets ?? '允许市场', children: (rules.markets ?? []).map((m) => MARKET_LABELS[m] ?? m).join('、') || '—' },
        { key: 't', label: info?.labels.sec_types ?? '证券类型', children: (rules.sec_types ?? []).map((m) => SEC_TYPE_LABELS[m] ?? m).join('、') || '—' },
        { key: 'l', label: info?.labels.exclude_leveraged ?? '排除杠杆', children: rules.exclude_leveraged ? '排除杠杆/反向产品' : '不排除' },
        { key: 'mx', label: info?.labels.max_members ?? '成员上限', children: rules.max_members ?? '—' },
        { key: 'minp', label: info?.labels.min_price ?? '最低价格', children: per(rules.min_price) },
        { key: 'maxp', label: info?.labels.max_price ?? '最高价格', children: per(rules.max_price) },
        { key: 'to', label: info?.labels.min_avg_turnover_20d ?? '20日平均成交额下限', children: per(rules.min_avg_turnover_20d) },
        { key: 'ex', label: info?.labels.exclude_symbols ?? '手动排除', children: (rules.exclude_symbols ?? []).join('、') || '无' },
      ]}
    />
  );
}

const pct = (v: number | null | undefined) => (v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}%`);

export function MembersTable({ items, excluded }: { items: UniverseMember[]; excluded?: boolean }) {
  const scanned = items.some((x) => x.market_cap_usd !== undefined);
  return (
    <Table<UniverseMember>
      size="small"
      rowKey="symbol"
      dataSource={items}
      pagination={items.length > 10 ? { pageSize: 10 } : false}
      scroll={{ x: true }}
      locale={{ emptyText: excluded ? '没有被排除的证券' : '没有符合规则的证券' }}
      columns={[
        { title: '代码', dataIndex: 'symbol' },
        { title: '名称', dataIndex: 'name' },
        { title: '市场', dataIndex: 'market', render: (m: string) => MARKET_LABELS[m] ?? m },
        { title: '类型', dataIndex: 'sec_type', render: (m: string) => SEC_TYPE_LABELS[m] ?? m },
        { title: '最近收盘', dataIndex: 'last_close', render: (v: string | null) => fmtPrice(v) },
        { title: '20日平均成交额', dataIndex: 'avg_turnover_20d', render: (v: string | null) => fmtMoney(v, 0) },
        ...(scanned
          ? [
              ...(!excluded ? [{ title: '排名', dataIndex: 'pool_rank' }] : []),
              { title: '市值（美元）', dataIndex: 'market_cap_usd', render: (v: string | null) => fmtMoney(v, 0) },
              { title: '20日涨跌', dataIndex: 'change_20d_pct', render: (v: number | null) => pct(v) },
              { title: '60日涨跌', dataIndex: 'change_60d_pct', render: (v: number | null) => pct(v) },
            ]
          : []),
        ...(excluded ? [{ title: '排除原因', dataIndex: 'reasons', render: (r: string[] | undefined) => (r ?? []).map((x) => <Tag key={x} color="orange">{x}</Tag>) }] : []),
      ]}
    />
  );
}

export function WizardPage() {
  const { ns, overview } = useAppState();
  const nav = useNavigate();
  const { run, busy } = useAction();
  const [step, setStep] = useState(0);
  const [st, setSt] = useState<WizardState>(() => initState(ns));
  const [preview, setPreview] = useState<WizardPreview | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewErr, setPreviewErr] = useState<string>();
  const [agree, setAgree] = useState(false);

  useEffect(() => {
    setSt(initState(ns));
    setStep(0);
    setPreview(null);
    setAgree(false);
  }, [ns]);

  const universe = useApi<UniverseInfo>(`/api/${ns}/universe`, { live: false });
  const ruleId = universe.data?.rules[0]?.id;
  const poolPreview = useApi<UniversePreview>(step === 3 && st.scopeType === 'POOL' ? `/api/${ns}/universe/preview${qs({ rule_id: ruleId })}` : null, { live: false });
  const instruments = useApi<Items<InstrumentRow> & { note?: string; data_source?: string }>(step === 3 ? `/api/${ns}/instruments` : null, { live: false });
  const watch = useApi<Items<WatchItem>>(`/api/${ns}/watchlist`, { live: false });
  const risk = useApi<RiskInfo>(`/api/${ns}/risk`, { live: false });
  const strategies = useApi<Items<StrategyVersion>>(`/api/${ns}/strategies`, { live: false });
  const gate = useApi<LiveGate>(ns === 'live' ? '/api/live/gate' : null, { live: false });

  useEffect(() => {
    // 白名单默认带上自选中已标记「加入白名单」的证券
    if (watch.data && st.symbols.length === 0) {
      const wl = watch.data.items.filter((w) => w.in_whitelist).map((w) => w.symbol);
      if (wl.length) setSt((s) => ({ ...s, symbols: wl }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watch.data]);

  const ccys = CCYS.filter((c) => st.useCcy[c]);
  const body = useMemo(
    () => ({
      mode: st.mode,
      capital: Object.fromEntries(ccys.map((c) => [c, st.capital[c]])),
      max_planned_loss: Object.fromEntries(ccys.map((c) => [c, st.loss[c]])),
      scope_type: st.scopeType,
      symbols: st.scopeType === 'WHITELIST' ? st.symbols : [],
      universe_rule_id: st.scopeType === 'POOL' ? ruleId ?? null : null,
      overnight: st.overnight,
      days: st.days,
      strategy_key: st.strategy,
    }),
    [st, ccys, ruleId],
  );

  const capitalErr = !ccys.length ? '请至少选择一种币种' : ccys.find((c) => !positive(st.capital[c])) ? '请填写大于 0 的可使用资金' : '';
  const lossErr = ccys.find((c) => !positive(st.loss[c]))
    ? '请填写大于 0 的可承受亏损'
    : ccys.find((c) => positive(st.capital[c]) && compareDecimals(st.loss[c], st.capital[c]) > 0)
      ? '可承受亏损不能大于可使用资金'
      : '';
  const scopeErr = st.scopeType === 'WHITELIST' && !st.symbols.length ? '手动白名单至少选择一只证券' : st.scopeType === 'POOL' && !ruleId ? '没有股票池规则' : '';

  const stepErr = [st.mode ? '' : '请选择模式', capitalErr, lossErr, scopeErr, st.days > 0 ? '' : '运行期限必须大于 0', ''][step];

  const doPreview = async () => {
    setPreviewBusy(true);
    setPreviewErr(undefined);
    setAgree(false);
    try {
      setPreview(await api.post<WizardPreview>(`/api/${ns}/wizard/preview`, body));
    } catch (e) {
      setPreview(null);
      setPreviewErr((e as Error).message);
    } finally {
      setPreviewBusy(false);
    }
  };

  useEffect(() => {
    if (step === 5) void doPreview();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  const start = async () => {
    const r = await run(() => api.post<{ ok: boolean; portfolio_id: number; data_source?: string }>(`/api/${ns}/wizard/start`, { ...body, confirm: true }), {
      success: '已确认授权，自动运行已启动',
    });
    if (r) nav('/overview');
  };

  const modes = ns === 'live' ? LIVE_MODES : SIM_MODES;
  const activeRisk = risk.data?.active?.params;
  const stratParams = strategies.data?.items.find((s) => s.key === st.strategy && s.status === 'ACTIVE');

  const instrumentOptions = useMemo(() => {
    const m = new Map<string, string>();
    for (const i of instruments.data?.items ?? []) if (!i.is_leveraged) m.set(i.symbol, `${i.symbol} ${i.name}`);
    for (const w of watch.data?.items ?? []) if (!m.has(w.symbol)) m.set(w.symbol, w.symbol);
    return [...m.entries()].map(([value, label]) => ({ value, label }));
  }, [instruments.data, watch.data]);

  const steps = [
    { title: '选择模式' },
    { title: '可使用资金' },
    { title: '可承受亏损' },
    { title: '交易范围' },
    { title: '隔夜与期限' },
    { title: '确认授权' },
  ];

  return (
    <div data-testid="wizard">
      <PageTitle
        title={`启动向导 · ${ns === 'live' ? '真实自动交易' : '自动模拟'}`}
        sub="只需设置资金、可承受亏损和交易范围。确认一次授权后，AI 在范围内自动分析并买卖，程序风控计算数量并把关，不需要逐笔确认。"
      />
      {overview?.portfolio?.running && overview.auth?.status === 'ACTIVE' ? (
        <Explain type="warning">
          当前已有运行中的{ns === 'live' ? '实盘' : '模拟'}（{overview.portfolio.mode_label}）。启动同一模式的新运行会替换它（旧授权失效，记录保留）。
        </Explain>
      ) : null}
      {ns === 'live' ? (
        <Explain type="error" title="这是真实资金操作">
          实盘交付时默认关闭。只有部署配置开启、账户已绑定并通过只读验证、实盘风控参数已单独确认、费用表已核对、保护方案已验证后，才能启动。确认时需要再次输入密码。
        </Explain>
      ) : null}

      <Card>
        <Steps current={step} items={steps} size="small" responsive onChange={(i) => (i < step ? setStep(i) : undefined)} style={{ marginBottom: 24 }} />

        {step === 0 ? (
          <>
            <Title level={5}>第 1 步：选择运行模式</Title>
            <Row gutter={[12, 12]}>
              {modes.map((m) => (
                <Col xs={24} md={ns === 'live' ? 12 : 8} key={m.value}>
                  <Card
                    size="small"
                    className={`mode-card ${st.mode === m.value ? 'selected' : ''}`}
                    onClick={() => setSt((s) => ({ ...s, mode: m.value }))}
                    data-testid={`mode-${m.value}`}
                  >
                    <Radio checked={st.mode === m.value}>
                      <Text strong>{m.title}</Text>
                    </Radio>
                    <Paragraph style={{ margin: '8px 0 4px' }}>{m.desc}</Paragraph>
                    <Text type="secondary" style={{ fontSize: 12 }}>{m.need}</Text>
                  </Card>
                </Col>
              ))}
            </Row>
            {ns === 'live' ? (
              <Card size="small" title="实盘接入前待核实清单" style={{ marginTop: 12 }}>
                <ErrorAlert error={gate.error} />
                {gate.data ? (
                  <>
                    <Tag color={gate.data.passed ? 'green' : 'red'} style={{ marginBottom: 8 }}>{gate.data.passed ? '全部通过' : '尚未全部通过，无法启动实盘'}</Tag>
                    <ChecksTable checks={gate.data.checks.map((c) => ({ name: c.name, ok: c.ok, detail: c.detail }))} />
                  </>
                ) : null}
              </Card>
            ) : null}
          </>
        ) : null}

        {step === 1 ? (
          <>
            <Title level={5}>第 2 步：AI 可以使用多少资金？</Title>
            <Paragraph type="secondary">
              这是交给系统管理的上限，按币种分别设置（马股用令吉 MYR，美股用美元 USD）。系统不会自动换汇，也不会动用账户里的其他资金或持仓。
              {ns === 'live' ? '建议先用小额尝试；小额交易受费用影响更大，下一步会显示估算。' : '模拟用的是虚拟资金。'}
            </Paragraph>
            {CCYS.map((c) => (
              <Space key={c} wrap style={{ marginBottom: 12, display: 'flex' }}>
                <Checkbox checked={st.useCcy[c]} onChange={(e) => setSt((s) => ({ ...s, useCcy: { ...s.useCcy, [c]: e.target.checked } }))}>
                  <Text strong style={{ display: 'inline-block', width: 120 }}>{c === 'MYR' ? '马股（MYR）' : '美股（USD）'}</Text>
                </Checkbox>
                <Space.Compact>
                  <InputNumber<string>
                    stringMode
                    min="0"
                    step="100"
                    style={{ width: 200 }}
                    disabled={!st.useCcy[c]}
                    value={st.capital[c] || null}
                    placeholder="例如 1000"
                    onChange={(v) => setSt((s) => ({ ...s, capital: { ...s.capital, [c]: v ?? '' } }))}
                    data-testid={`capital-${c}`}
                  />
                  <Button disabled tabIndex={-1}>{c}</Button>
                </Space.Compact>
              </Space>
            ))}
          </>
        ) : null}

        {step === 2 ? (
          <>
            <Title level={5}>第 3 步：最多可以承受多少计划亏损？</Title>
            <Paragraph type="secondary">
              当这次运行的累计亏损达到这条「停止线」时，系统停止开新仓。
              <Text strong>停止线不保证最大实际亏损</Text>：遇到跳空、停牌或无法成交时，实际亏损可能更大。
            </Paragraph>
            {ccys.map((c) => (
              <Space key={c} wrap style={{ marginBottom: 12, display: 'flex' }}>
                <Text strong style={{ display: 'inline-block', width: 120 }}>{c === 'MYR' ? '马股（MYR）' : '美股（USD）'}</Text>
                <Space.Compact>
                  <InputNumber<string>
                    stringMode
                    min="0"
                    step="50"
                    style={{ width: 200 }}
                    value={st.loss[c] || null}
                    onChange={(v) => setSt((s) => ({ ...s, loss: { ...s.loss, [c]: v ?? '' } }))}
                    data-testid={`loss-${c}`}
                  />
                  <Button disabled tabIndex={-1}>{c}</Button>
                </Space.Compact>
                <Text type="secondary">可使用资金 {fmtMoney(st.capital[c])} {c}</Text>
              </Space>
            ))}
          </>
        ) : null}

        {step === 3 ? (
          <>
            <Title level={5}>第 4 步：AI 可以在哪些股票里交易？</Title>
            <Radio.Group value={st.scopeType} onChange={(e) => setSt((s) => ({ ...s, scopeType: e.target.value }))} style={{ marginBottom: 12 }}>
              <Space direction="vertical">
                <Radio value="POOL">
                  <Text strong>授权股票池（推荐）</Text>
                  <Text type="secondary">：程序按规则（市场、类型、流动性、价格、排除项）每天确定成员，AI 只在池内挑选，无需您逐只选股。</Text>
                </Radio>
                <Radio value="WHITELIST">
                  <Text strong>手动白名单</Text>
                  <Text type="secondary">：只允许您挑选的几只证券。</Text>
                </Radio>
              </Space>
            </Radio.Group>
            {st.scopeType === 'POOL' ? (
              <Space direction="vertical" style={{ width: '100%' }}>
                <Explain>AI 只能在程序筛选出的成员中选择，不能自行扩大范围；范围条件改变需要您确认，范围内的例行成员更新无需逐只确认。{universe.data?.note}</Explain>
                <PoolRuleView info={universe.data} ruleId={ruleId} />
                <Card size="small" title={`按当前数据预览（${poolPreview.data?.data_source ?? '…'}）`} loading={poolPreview.loading && !poolPreview.data}>
                  <ErrorAlert error={poolPreview.error} title="无法预览股票池" />
                  {poolPreview.data ? (
                    <>
                      {poolPreview.data.disclaimer ? <Alert type="info" showIcon message={poolPreview.data.disclaimer} style={{ marginBottom: 8 }} /> : null}
                      <Text strong>成员（{poolPreview.data.members.length}）</Text>
                      <MembersTable items={poolPreview.data.members} />
                      <Divider style={{ margin: '12px 0' }} />
                      <Text strong>被排除（{poolPreview.data.excluded.length}）</Text>
                      <MembersTable items={poolPreview.data.excluded} excluded />
                    </>
                  ) : null}
                </Card>
              </Space>
            ) : (
              <Space direction="vertical" style={{ width: '100%' }}>
                {instruments.data?.note ? <Alert type="info" showIcon message={instruments.data.note} /> : null}
                <Select
                  mode="tags"
                  style={{ width: '100%' }}
                  placeholder="选择或输入代码，例如 US.AAPL、MY.1155"
                  value={st.symbols}
                  options={instrumentOptions}
                  onChange={(v: string[]) => setSt((s) => ({ ...s, symbols: v.map((x) => x.trim().toUpperCase()).filter(Boolean) }))}
                  data-testid="whitelist-select"
                />
                <Text type="secondary">杠杆/反向产品不能加入。可以在「自选股与股票详情」中核对名称和证券类型后加入白名单。</Text>
              </Space>
            )}
          </>
        ) : null}

        {step === 4 ? (
          <>
            <Title level={5}>第 5 步：隔夜、运行期限与策略</Title>
            <Space direction="vertical" size="middle" style={{ width: '100%' }}>
              <Space wrap>
                <Text strong style={{ width: 120, display: 'inline-block' }}>允许隔夜持仓</Text>
                <Switch checked={st.overnight} onChange={(v) => setSt((s) => ({ ...s, overnight: v }))} checkedChildren="允许" unCheckedChildren="不允许" />
                <Text type="secondary">{st.overnight ? '持仓可以过夜，由止损/止盈/持有期限保护。' : '收市前尽量卖出当天的持仓（可能无法全部成交）。'}</Text>
              </Space>
              <Space wrap>
                <Text strong style={{ width: 120, display: 'inline-block' }}>运行期限</Text>
                <Space.Compact>
                  <InputNumber min={1} max={365} value={st.days} onChange={(v) => setSt((s) => ({ ...s, days: Number(v) || 0 }))} data-testid="days" />
                  <Button disabled tabIndex={-1}>天</Button>
                </Space.Compact>
                <Text type="secondary">到期或您撤销后停止开新仓；已有持仓继续按保护规则自动退出。</Text>
              </Space>
              <div>
                <Text strong>决策策略</Text>
                <Radio.Group value={st.strategy} onChange={(e) => setSt((s) => ({ ...s, strategy: e.target.value }))} style={{ display: 'block', marginTop: 8 }}>
                  <Space direction="vertical">
                    <Radio value="AI_AUTONOMOUS">
                      <Text strong>AI 自主决策（默认）</Text>
                      <Text type="secondary">：AI 在授权范围内自行选择买哪只、何时买卖；数量由程序风控计算。</Text>
                    </Radio>
                    <Radio value="BASELINE_MA">
                      <Text strong>固定规则基准</Text>
                      <Text type="secondary">：MA20/MA60 均线交叉，仅作为对照比较用。</Text>
                    </Radio>
                  </Space>
                </Radio.Group>
              </div>
              <Card size="small" title="预设参数（只读，无需填写）">
                <Paragraph type="secondary">
                  以下风控与策略参数是已确认的预设，AI 无权修改。如需调整，请到「策略与风控设置」新建版本并确认，新参数从下一次授权起生效。
                  {risk.data?.active?.is_test_seed ? ' 当前为模拟测试种子参数（非实盘授权）。' : ''}
                </Paragraph>
                {activeRisk ? (
                  <Table
                    size="small"
                    pagination={false}
                    rowKey="key"
                    scroll={{ x: true }}
                    dataSource={Object.entries(activeRisk)
                      .filter(([k]) => RISK_PARAM_HELP[k])
                      .map(([k, v]) => ({ key: k, label: risk.data?.labels[k] ?? k, value: isRatioParam(k) ? fmtRatioPct(String(v)) : fmtValue(v), help: RISK_PARAM_HELP[k] }))}
                    columns={[
                      { title: '参数', dataIndex: 'label' },
                      { title: '预设值', dataIndex: 'value', width: 100 },
                      { title: '含义', dataIndex: 'help' },
                    ]}
                  />
                ) : (
                  <Alert type="warning" showIcon message="尚未确认风控参数" description={ns === 'live' ? '实盘必须单独设置并确认风控参数（不会继承模拟参数）。请到「策略与风控设置」。' : undefined} />
                )}
                {stratParams ? (
                  <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
                    策略版本 {stratParams.key} {stratParams.version}：{stratParams.note}
                  </Paragraph>
                ) : null}
              </Card>
            </Space>
          </>
        ) : null}

        {step === 5 ? (
          <>
            <Title level={5}>第 6 步：阅读授权摘要并确认</Title>
            <ErrorAlert error={previewErr} title="无法生成授权摘要" />
            {previewBusy && !preview ? <Card loading /> : null}
            {preview ? (
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                {preview.problems.map((p) => (
                  <Alert key={p} type="error" showIcon message={p} />
                ))}
                <Card size="small" title="授权摘要" data-testid="auth-summary">
                  {preview.summary ? <Paragraph style={{ whiteSpace: 'pre-wrap', fontSize: 15, marginBottom: 0 }}>{preview.summary}</Paragraph> : <Empty description="存在问题，无法生成摘要" />}
                </Card>
                <Alert type="warning" showIcon message="重要提示" description={preview.warning} />
                <Card size="small" title="费用对小额交易的影响（示例估算）">
                  {preview.fee_impact.length ? (
                    <ul className="compact-list">
                      {preview.fee_impact.map((f) => (
                        <li key={f.currency}>
                          <Tag>{f.currency}</Tag>
                          {f.note}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <Text type="secondary">暂无费用估算（缺少费用表或风控参数）</Text>
                  )}
                </Card>
                {preview.gate ? (
                  <Card size="small" title="实盘接入前待核实清单">
                    <ChecksTable checks={preview.gate} />
                  </Card>
                ) : null}
                {st.scopeType === 'POOL' && !preview.pool_rule_confirmed && ns === 'sim' ? (
                  <Text type="secondary">确认后会一并确认股票池规则 v{preview.pool_rule?.version}。</Text>
                ) : null}
                <Checkbox checked={agree} onChange={(e) => setAgree(e.target.checked)} disabled={!!preview.problems.length || !preview.summary} data-testid="wizard-agree">
                  我已阅读以上授权摘要：AI 将在此范围内自动买卖，不再逐笔询问；停止线不保证最大实际亏损。
                  {ns === 'live' ? '这是真实资金。' : ''}
                </Checkbox>
                <Space wrap>
                  <Button
                    type="primary"
                    size="large"
                    danger={ns === 'live'}
                    disabled={!agree || !!preview.problems.length || !preview.summary}
                    loading={busy}
                    onClick={start}
                    data-testid="wizard-start"
                  >
                    确认授权并启动{ns === 'live' ? '真实自动交易' : '自动模拟'}
                  </Button>
                  <Button onClick={() => void doPreview()} loading={previewBusy}>重新生成摘要</Button>
                </Space>
              </Space>
            ) : null}
          </>
        ) : null}

        <Divider />
        <Space wrap>
          {step > 0 ? <Button onClick={() => setStep(step - 1)}>上一步</Button> : null}
          {step < 5 ? (
            <Button type="primary" disabled={!!stepErr} onClick={() => setStep(step + 1)} data-testid="wizard-next">
              下一步
            </Button>
          ) : null}
          {stepErr ? <Text type="danger">{stepErr}</Text> : null}
        </Space>
      </Card>
    </div>
  );
}
