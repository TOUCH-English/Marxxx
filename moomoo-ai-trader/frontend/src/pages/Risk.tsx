import { Alert, App, Button, Card, Checkbox, Col, Collapse, Divider, Form, Input, InputNumber, Modal, Row, Select, Space, Switch, Table, Tabs, Tag, Typography } from 'antd';
import { useEffect, useMemo, useState } from 'react';
import { api, qs } from '../api/client';
import type { ConfigDraft, Deployment, DiffRow, Items, PoolRules, RiskInfo, RiskLockRow, RiskVersion, StrategyVersion, UniverseInfo, UniversePreview, UniverseRule } from '../api/types';
import { ErrorAlert, Explain, KV, PageTitle } from '../components/common';
import { useAppState } from '../state/app';
import { useAction, useApi } from '../state/hooks';
import { fmtMoney, fmtRatioPct, shiftDecimal } from '../utils/decimal';
import { fmtMyt, fmtValue } from '../utils/format';
import { LOCK_KIND_LABELS, LOCK_NOT_RELEASABLE, MARKET_LABELS, RISK_PARAM_HELP, SEC_TYPE_LABELS, isRatioParam } from '../utils/labels';
import { MembersTable } from './Wizard';

const { Text, Paragraph } = Typography;

function paramText(key: string, v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (isRatioParam(key) && (typeof v === 'string' || typeof v === 'number')) return fmtRatioPct(String(v));
  return fmtValue(v);
}

export function DiffTable({ diff, ratioAware = true }: { diff: DiffRow[]; ratioAware?: boolean }) {
  return (
    <Table<DiffRow>
      size="small"
      rowKey="key"
      pagination={false}
      dataSource={diff}
      scroll={{ x: true }}
      locale={{ emptyText: '没有变化' }}
      columns={[
        { title: '项目', dataIndex: 'label' },
        { title: '原来', render: (_, d) => (ratioAware ? paramText(d.key, d.before) : fmtValue(d.before)) },
        { title: '改为', render: (_, d) => <Text strong>{ratioAware ? paramText(d.key, d.after) : fmtValue(d.after)}</Text> },
        { title: '影响', render: (_, d) => (d.loosens ? <Tag color="red">放宽（风险变大，需要再次输入密码）</Tag> : <Tag color="green">收紧或中性</Tag>) },
      ]}
    />
  );
}

// ------------------------------------------------------------------ 风控参数
function RiskParamsTab({ info, reload }: { info: RiskInfo; reload: () => void }) {
  const { ns } = useAppState();
  const { run, busy } = useAction();
  const base = info.active?.params ?? info.seed;
  const [values, setValues] = useState<Record<string, string | number | boolean>>({});
  const [note, setNote] = useState('');
  const [created, setCreated] = useState<{ version: RiskVersion; diff: DiffRow[]; loosens: boolean; needs_reauth: boolean } | null>(null);

  const reset = () => {
    const v: Record<string, string | number | boolean> = {};
    for (const [k, x] of Object.entries(base)) v[k] = isRatioParam(k) && typeof x === 'string' ? shiftDecimal(x, 2) : x;
    setValues(v);
  };
  useEffect(reset, [info.active?.id, ns]); // eslint-disable-line react-hooks/exhaustive-deps

  const toParams = () => {
    const out: Record<string, string | number | boolean> = {};
    for (const [k, x] of Object.entries(values)) out[k] = isRatioParam(k) && typeof x === 'string' ? shiftDecimal(x, -2) : x;
    return out;
  };

  const confirmVersion = async (id: number) => {
    const r = await run(() => api.post(`/api/${ns}/risk/versions/${id}/confirm`), { success: '已确认新的风控参数版本（从下一次运行授权起生效）' });
    if (r) {
      setCreated(null);
      reload();
    }
  };

  const keys = Object.keys(base);
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Explain>{info.note} AI 无权修改风控参数；更改会生成新版本，需要您确认，并从下一次运行授权起生效。</Explain>
      <Card size="small" title={info.active ? `当前生效版本 v${info.active.version}` : '尚未确认风控参数'}>
        {info.active ? (
          <>
            <Space wrap style={{ marginBottom: 8 }}>
              {info.active.is_test_seed ? <Tag color="gold">模拟测试种子参数（非实盘授权）</Tag> : <Tag color="green">用户已确认</Tag>}
              <Text type="secondary">确认时间 {fmtMyt(info.active.confirmed_at)}</Text>
              {info.active.note ? <Text type="secondary">· {info.active.note}</Text> : null}
            </Space>
            <Table
              size="small"
              rowKey="key"
              pagination={false}
              scroll={{ x: true }}
              dataSource={Object.entries(info.active.params).map(([k, v]) => ({
                key: k,
                label: info.labels[k] ?? k,
                value: paramText(k, v),
                hard: info.hard_limits[k] !== undefined ? paramText(k, info.hard_limits[k]) : '—',
                help: RISK_PARAM_HELP[k] ?? '',
              }))}
              columns={[
                { title: '参数', dataIndex: 'label' },
                { title: '当前值', dataIndex: 'value', width: 110 },
                { title: '产品硬上限', dataIndex: 'hard', width: 110 },
                { title: '说明', dataIndex: 'help' },
              ]}
            />
          </>
        ) : (
          <Alert type="warning" showIcon message={ns === 'live' ? '实盘风控参数必须在这里单独设置并确认（不会继承模拟参数）。下面的表单已用测试种子值预填，请逐项检查后提交。' : '尚未确认风控参数'} />
        )}
      </Card>

      <Card size="small" title="新建风控参数版本">
        <Row gutter={[12, 4]}>
          {keys.map((k) => {
            const v = values[k];
            const label = info.labels[k] ?? k;
            return (
              <Col xs={24} md={12} xl={8} key={k}>
                <Form.Item label={label} tooltip={RISK_PARAM_HELP[k]} style={{ marginBottom: 8 }} labelCol={{ span: 24 }}>
                  {typeof v === 'boolean' ? (
                    <Space>
                      <Switch checked={v} disabled />
                      <Text type="secondary">首版禁止</Text>
                    </Space>
                  ) : isRatioParam(k) ? (
                    <Space.Compact style={{ width: '100%' }}>
                      <InputNumber<string>
                        stringMode
                        min="0"
                        style={{ width: '100%' }}
                        value={v === undefined ? null : String(v)}
                        onChange={(x) => setValues((s) => ({ ...s, [k]: x ?? '' }))}
                      />
                      <Button disabled tabIndex={-1}>%</Button>
                    </Space.Compact>
                  ) : (
                    <InputNumber
                      min={0}
                      precision={0}
                      style={{ width: '100%' }}
                      value={typeof v === 'number' ? v : Number(v)}
                      onChange={(x) => setValues((s) => ({ ...s, [k]: x ?? 0 }))}
                    />
                  )}
                </Form.Item>
              </Col>
            );
          })}
        </Row>
        <Space wrap style={{ marginTop: 8 }}>
          <Input placeholder="变更说明（可选）" value={note} onChange={(e) => setNote(e.target.value)} style={{ width: 280 }} />
          <Button onClick={reset}>恢复为当前值</Button>
          <Button
            type="primary"
            loading={busy}
            onClick={async () => {
              const r = await run(() => api.post<{ version: RiskVersion; diff: DiffRow[]; loosens: boolean; needs_reauth: boolean }>(`/api/${ns}/risk/versions`, { params: toParams(), note }), {
                success: (x) => `已生成草稿版本 v${x.version.version}，请查看差异后确认`,
              });
              if (r) {
                setCreated(r);
                reload();
              }
            }}
          >
            生成新版本（草稿）
          </Button>
        </Space>
        {created ? (
          <Card size="small" type="inner" title={`草稿 v${created.version.version} 与当前版本的差异`} style={{ marginTop: 12 }}>
            {created.loosens ? <Alert type="error" showIcon style={{ marginBottom: 8 }} message="此版本放宽了风控：风险会变大。确认时需要再次输入密码。" /> : null}
            {!created.loosens && created.needs_reauth ? <Alert type="warning" showIcon style={{ marginBottom: 8 }} message="实盘风控变更确认时需要再次输入密码。" /> : null}
            <DiffTable diff={created.diff} />
            <Space style={{ marginTop: 8 }}>
              <Button type="primary" danger={created.loosens} loading={busy} onClick={() => void confirmVersion(created.version.id)}>
                确认此版本
              </Button>
              <Button onClick={() => setCreated(null)}>暂不确认</Button>
            </Space>
          </Card>
        ) : null}
      </Card>

      <Card size="small" title="版本历史">
        <Table<RiskVersion>
          size="small"
          rowKey="id"
          dataSource={info.versions}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          scroll={{ x: true }}
          expandable={{ expandedRowRender: (v) => <KV data={Object.fromEntries(Object.entries(v.params).map(([k, x]) => [info.labels[k] ?? k, paramText(k, x)]))} column={3} /> }}
          columns={[
            { title: '版本', dataIndex: 'version', render: (v: number) => `v${v}` },
            {
              title: '状态',
              render: (_, v) =>
                info.active?.id === v.id ? <Tag color="green">当前生效</Tag> : v.confirmed_by_user ? <Tag>已确认（历史）</Tag> : <Tag color="orange">草稿，未确认</Tag>,
            },
            { title: '测试种子', dataIndex: 'is_test_seed', render: (v: boolean) => (v ? '是' : '否') },
            { title: '说明', dataIndex: 'note' },
            { title: '创建', dataIndex: 'created_at_utc', render: (v: string) => fmtMyt(v) },
            { title: '确认', dataIndex: 'confirmed_at', render: (v: string | null) => fmtMyt(v) },
            {
              title: '',
              render: (_, v) =>
                !v.confirmed_by_user ? (
                  <Button size="small" loading={busy} onClick={() => void confirmVersion(v.id)}>
                    确认此版本
                  </Button>
                ) : null,
            },
          ]}
        />
      </Card>
    </Space>
  );
}

// ------------------------------------------------------------------ 锁定
function LocksTab({ locks, reload }: { locks: RiskLockRow[]; reload: () => void }) {
  const { ns } = useAppState();
  const { run, busy } = useAction();
  const [target, setTarget] = useState<RiskLockRow | null>(null);
  const [note, setNote] = useState('');
  return (
    <>
      <Explain>
        风控锁定会阻止新开仓。回撤、对账差异等锁定需要您复核原因后填写说明再解除；当日亏损锁定只能由系统在马来西亚时间日界后自动解除；状态未知订单的锁定需要先在订单页核对。
        策略回退或升级都不会清除亏损锁定。
      </Explain>
      <Table<RiskLockRow>
        size="small"
        rowKey="id"
        dataSource={locks}
        pagination={{ pageSize: 15, hideOnSinglePage: true }}
        scroll={{ x: true }}
        locale={{ emptyText: '没有锁定记录' }}
        columns={[
          { title: '类型', dataIndex: 'kind', render: (k: string) => <Tag color="red">{LOCK_KIND_LABELS[k] ?? k}</Tag> },
          { title: '范围', dataIndex: 'scope', render: (s: string) => (s === 'ALL' ? '全部新开仓' : s) },
          { title: '原因', dataIndex: 'reason' },
          { title: '时间', dataIndex: 'as_of', render: (v: string) => fmtMyt(v) },
          { title: '状态', render: (_, l) => (l.active ? <Tag color="red">生效中</Tag> : <Tag>已解除</Tag>) },
          { title: '解除记录', render: (_, l) => (l.released_at ? `${fmtMyt(l.released_at)} ${l.released_by}：${l.release_note}` : '—') },
          {
            title: '操作',
            render: (_, l) =>
              !l.active ? null : LOCK_NOT_RELEASABLE[l.kind] ? (
                <Text type="secondary" style={{ fontSize: 12 }}>{LOCK_NOT_RELEASABLE[l.kind]}</Text>
              ) : (
                <Button size="small" onClick={() => { setTarget(l); setNote(''); }}>人工复核后解除</Button>
              ),
          },
        ]}
      />
      <Modal
        open={!!target}
        title="人工复核后解除锁定"
        okText="解除锁定"
        cancelText="取消"
        confirmLoading={busy}
        okButtonProps={{ disabled: note.trim().length < 4 }}
        onCancel={() => setTarget(null)}
        onOk={async () => {
          if (!target) return;
          const r = await run(() => api.post(`/api/${ns}/risk/locks/${target.id}/release`, { note }), { success: '锁定已解除（已记录复核说明）' });
          if (r) {
            setTarget(null);
            reload();
          }
        }}
      >
        <Paragraph>
          <Tag color="red">{target ? LOCK_KIND_LABELS[target.kind] ?? target.kind : ''}</Tag>
          {target?.reason}
        </Paragraph>
        <Paragraph type="secondary">请说明您核对了什么（至少 4 个字）。解除后系统可能重新允许新开仓（仍受其它风控限制）。{ns === 'live' ? '实盘需要再次输入密码。' : ''}</Paragraph>
        <Input.TextArea rows={3} value={note} onChange={(e) => setNote(e.target.value)} placeholder="例如：已核对回撤原因为单只股票跳空，持仓已按止损退出" />
      </Modal>
    </>
  );
}

// ------------------------------------------------------------------ 股票池
function RulesText({ rules, labels }: { rules: PoolRules; labels: Record<string, string> }) {
  const per = (m?: Record<string, string>) => (m ? Object.entries(m).map(([k, v]) => `${MARKET_LABELS[k] ?? k} ${fmtMoney(v)}`).join('；') : '—');
  return (
    <KV
      column={2}
      data={{
        [labels.markets ?? '允许市场']: (rules.markets ?? []).map((m) => MARKET_LABELS[m] ?? m).join('、'),
        [labels.sec_types ?? '证券类型']: (rules.sec_types ?? []).map((m) => SEC_TYPE_LABELS[m] ?? m).join('、'),
        [labels.exclude_leveraged ?? '排除杠杆']: rules.exclude_leveraged ? '是' : '否',
        [labels.max_members ?? '成员上限']: rules.max_members,
        [labels.min_price ?? '最低价格']: per(rules.min_price),
        [labels.max_price ?? '最高价格']: per(rules.max_price),
        [labels.min_avg_turnover_20d ?? '成交额下限']: per(rules.min_avg_turnover_20d),
        [labels.exclude_symbols ?? '手动排除']: (rules.exclude_symbols ?? []).join('、') || '无',
        [labels.source ?? '候选来源']: rules.source === 'MOOMOO_SCAN' ? '每天用 moomoo 条件选股扫描全部美股（程序按规则筛选，不是 AI）' : '只筛自选中的证券',
        ...(rules.source === 'MOOMOO_SCAN'
          ? {
              [labels.min_market_cap_usd ?? '最低市值']: fmtMoney(String(rules.min_market_cap_usd ?? ''), 0),
              [labels.scan_top_by_turnover ?? '按成交额先取前几只']: String(rules.scan_top_by_turnover ?? '—'),
            }
          : {}),
      }}
    />
  );
}

function UniverseTab() {
  const { ns } = useAppState();
  const { modal } = App.useApp();
  const u = useApi<UniverseInfo>(`/api/${ns}/universe`);
  const { run, busy } = useAction();
  const [previewId, setPreviewId] = useState<number | null>(null);
  const preview = useApi<UniversePreview>(previewId !== null ? `/api/${ns}/universe/preview${qs({ rule_id: previewId || undefined })}` : null, { live: false });
  const [form] = Form.useForm();
  const base = u.data?.rules[0]?.rules ?? u.data?.default_rules;

  useEffect(() => {
    if (!base) return;
    form.setFieldsValue({
      markets: base.markets,
      sec_types: base.sec_types,
      exclude_leveraged: base.exclude_leveraged,
      min_price_MY: base.min_price?.MY,
      min_price_US: base.min_price?.US,
      max_price_MY: base.max_price?.MY,
      max_price_US: base.max_price?.US,
      turn_MY: base.min_avg_turnover_20d?.MY,
      turn_US: base.min_avg_turnover_20d?.US,
      exclude_symbols: base.exclude_symbols,
      max_members: base.max_members,
    });
  }, [u.data?.rules[0]?.id, ns]); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async () => {
    const v = await form.validateFields();
    const rules: PoolRules = {
      markets: v.markets,
      sec_types: v.sec_types,
      exclude_leveraged: !!v.exclude_leveraged,
      min_price: { MY: String(v.min_price_MY ?? '0'), US: String(v.min_price_US ?? '0') },
      max_price: { MY: String(v.max_price_MY ?? '0'), US: String(v.max_price_US ?? '0') },
      min_avg_turnover_20d: { MY: String(v.turn_MY ?? '0'), US: String(v.turn_US ?? '0') },
      exclude_symbols: (v.exclude_symbols ?? []).map((x: string) => x.trim().toUpperCase()),
      max_members: v.max_members,
    };
    const r = await run(() => api.post<UniverseRule>(`/api/${ns}/universe/rules`, { rules, note: v.note ?? '' }), { success: (x) => `已生成规则草稿 v${x.version}，请预览后确认` });
    if (r) {
      void u.reload();
      setPreviewId(r.id);
    }
  };

  const confirm = async (id: number) => {
    const r = await run(() => api.post(`/api/${ns}/universe/rules/${id}/confirm`), { success: '已确认股票池规则（新授权起生效）' });
    if (r) void u.reload();
  };

  const createAuto = async () => {
    const r = await run(() => api.post<UniverseRule>(`/api/${ns}/universe/rules/auto-us`), { success: (x) => `已生成「每日自动股票池」规则草稿 v${x.version}，请预览成员后确认` });
    if (r) {
      void u.reload();
      setPreviewId(r.id);
    }
  };

  const cur = u.data?.current_run;
  const confirmedRules = (u.data?.rules ?? []).filter((r) => r.confirmed_by_user);
  const [scopeChoice, setScopeChoice] = useState<string>('');
  const scopeText = (sc?: { type: string; universe_rule_id?: number; symbols?: string[] }) => {
    if (!sc) return '—';
    if (sc.type === 'WHITELIST') return `手动白名单：${(sc.symbols ?? []).join('、')}`;
    const r = (u.data?.rules ?? []).find((x) => x.id === sc.universe_rule_id);
    return `股票池规则 v${r?.version ?? '?'}${r?.rules.source === 'MOOMOO_SCAN' ? '（每日自动扫描全部美股）' : ''}`;
  };
  const changeScope = () => {
    const isPool = scopeChoice.startsWith('rule:');
    const body = isPool
      ? { scope_type: 'POOL', universe_rule_id: Number(scopeChoice.slice(5)), confirm: true }
      : { scope_type: 'WHITELIST', symbols: u.data?.whitelist ?? [], confirm: true };
    modal.confirm({
      title: '更换当前运行的交易范围？',
      content: (
        <Space direction="vertical">
          <Text>新范围：{isPool ? scopeText({ type: 'POOL', universe_rule_id: Number(scopeChoice.slice(5)) }) : scopeText({ type: 'WHITELIST', symbols: u.data?.whitelist ?? [] })}</Text>
          <Text>不变：资金、可承受亏损、风控版本、策略、到期时间。</Text>
          <Text>已有持仓、止损和亏损记录全部保留；持仓即使不在新范围内，也照常按止损/止盈/持有期限管理。</Text>
        </Space>
      ),
      okText: '确认更换',
      cancelText: '取消',
      onOk: async () => {
        const r = await run(() => api.post<{ note: string }>(`/api/${ns}/control/scope`, body), { success: (x) => x.note });
        if (r) void u.reload();
      },
    });
  };

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <ErrorAlert error={u.error} />
      {u.data ? <Explain>{u.data.note} 范围条件改变需要您确认；范围内的例行成员更新无需逐只确认。</Explain> : null}
      {cur ? (
        <Card size="small" title="当前运行的交易范围">
          <Paragraph>现在：{scopeText(cur.scope)}（运行 #{cur.portfolio_id}，授权 #{cur.authorization_id}）</Paragraph>
          <Space wrap>
            <Select
              style={{ minWidth: 320 }}
              placeholder="选择新的交易范围"
              value={scopeChoice || undefined}
              onChange={setScopeChoice}
              options={[
                ...confirmedRules.map((r) => ({ value: `rule:${r.id}`, label: `股票池规则 v${r.version}${r.rules.source === 'MOOMOO_SCAN' ? '（每日自动扫描全部美股）' : '（只筛自选）'}` })),
                ...(u.data?.whitelist.length ? [{ value: 'whitelist', label: `手动白名单（${u.data.whitelist.join('、')}）` }] : []),
              ]}
            />
            <Button type="primary" disabled={!scopeChoice} loading={busy} onClick={changeScope}>
              更换（保留持仓）
            </Button>
          </Space>
          <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
            只能选择已确认的规则。更换不会新建运行，资金、亏损线、风控和到期时间都不变。
          </Paragraph>
        </Card>
      ) : null}
      <Card size="small" title="每日自动股票池（推荐）">
        <Paragraph>
          每天美股开盘前，程序用 moomoo 条件选股扫描<Text strong>全部美股</Text>，留下大公司、成交活跃、价格买得起的普通股（排除 ETF、场外交易和杠杆产品），
          再按近 20/60 天的强弱选出前 20 只作为当天的股票池。盘中 AI 只在这 20 只里挑选并决定买卖，最终数量由风控决定。
        </Paragraph>
        <Paragraph type="secondary">
          说明：扫描全市场是<Text strong>程序按规则</Text>完成的，不是 AI 研究整个市场；需要 moomoo OpenD 已登录。规则：价格 10–600 美元、市值 ≥ 100 亿美元、20 日平均成交额 ≥ 5,000 万美元。
        </Paragraph>
        <Button loading={busy} onClick={() => void createAuto()}>生成「每日自动股票池」规则草稿</Button>
      </Card>
      <Card size="small" title="规则版本">
        <Table<UniverseRule>
          size="small"
          rowKey="id"
          dataSource={u.data?.rules ?? []}
          pagination={false}
          scroll={{ x: true }}
          locale={{ emptyText: ns === 'live' ? '实盘默认范围为空：请在下方新建规则并确认' : '暂无规则' }}
          expandable={{ expandedRowRender: (r) => <RulesText rules={r.rules} labels={u.data?.labels ?? {}} /> }}
          columns={[
            { title: '版本', dataIndex: 'version', render: (v: number) => `v${v}` },
            { title: '状态', dataIndex: 'confirmed_by_user', render: (c: boolean) => (c ? <Tag color="green">已确认</Tag> : <Tag color="orange">未确认</Tag>) },
            { title: '说明', dataIndex: 'note' },
            { title: '创建', dataIndex: 'created_at_utc', render: (v: string) => fmtMyt(v) },
            {
              title: '操作',
              render: (_, r) => (
                <Space size={4} wrap>
                  <Button size="small" onClick={() => setPreviewId(r.id)}>预览成员</Button>
                  {!r.confirmed_by_user ? <Button size="small" type="primary" loading={busy} onClick={() => void confirm(r.id)}>确认</Button> : null}
                </Space>
              ),
            },
          ]}
        />
        {u.data?.whitelist.length ? <Paragraph style={{ marginTop: 8 }}>当前手动白名单：{u.data.whitelist.map((s) => <Tag key={s}>{s}</Tag>)}</Paragraph> : null}
      </Card>
      {previewId !== null ? (
        <Card size="small" title={`成员预览（规则 #${previewId}，数据来源 ${preview.data?.data_source ?? '…'}）`} loading={preview.loading && !preview.data} extra={<Button size="small" onClick={() => setPreviewId(null)}>关闭</Button>}>
          <ErrorAlert error={preview.error} title="无法预览" />
          {preview.data ? (
            <>
              {preview.data.disclaimer ? <Alert type="info" showIcon message={preview.data.disclaimer} style={{ marginBottom: 8 }} /> : null}
              {preview.data.stats?.matched_total !== undefined ? (
                <Alert
                  type="success"
                  showIcon
                  style={{ marginBottom: 8 }}
                  message={`符合价格/市值条件的美股共 ${preview.data.stats.matched_total} 只；按成交额取前 ${preview.data.stats.fetched} 只，券商核实 ${preview.data.stats.verified} 只，最终入选 ${preview.data.stats.members} 只`}
                  description={preview.data.stats.method}
                />
              ) : null}
              <Text strong>成员（{preview.data.members.length}）</Text>
              <MembersTable items={preview.data.members} />
              <Divider style={{ margin: '12px 0' }} />
              <Text strong>被排除（{preview.data.excluded.length}）</Text>
              <MembersTable items={preview.data.excluded} excluded />
            </>
          ) : null}
        </Card>
      ) : null}
      <Card size="small" title="新建股票池规则">
        <Form form={form} layout="vertical">
          <Row gutter={12}>
            <Col xs={24} md={8}>
              <Form.Item name="markets" label="允许市场" rules={[{ required: true, message: '至少选择一个市场' }]}>
                <Checkbox.Group options={[{ value: 'MY', label: '马股' }, { value: 'US', label: '美股' }]} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="sec_types" label="证券类型" rules={[{ required: true, message: '至少选择一种类型' }]}>
                <Checkbox.Group options={[{ value: 'STOCK', label: '股票' }, { value: 'ETF', label: '普通 ETF' }]} />
              </Form.Item>
            </Col>
            <Col xs={24} md={8}>
              <Form.Item name="exclude_leveraged" label="排除杠杆/反向产品" valuePropName="checked">
                <Switch disabled checkedChildren="排除（首版固定）" />
              </Form.Item>
            </Col>
            {(['MY', 'US'] as const).map((m) => (
              <Col xs={24} md={12} key={m}>
                <Card size="small" type="inner" title={MARKET_LABELS[m]} style={{ marginBottom: 12 }}>
                  <Row gutter={8}>
                    <Col span={8}>
                      <Form.Item name={`min_price_${m}`} label="最低价格">
                        <InputNumber<string> stringMode min="0" style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item name={`max_price_${m}`} label="最高价格">
                        <InputNumber<string> stringMode min="0" style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                    <Col span={8}>
                      <Form.Item name={`turn_${m}`} label="20日均成交额下限">
                        <InputNumber<string> stringMode min="0" style={{ width: '100%' }} />
                      </Form.Item>
                    </Col>
                  </Row>
                </Card>
              </Col>
            ))}
            <Col xs={24} md={12}>
              <Form.Item name="exclude_symbols" label="手动排除的代码">
                <Select mode="tags" tokenSeparators={[',', ' ']} placeholder="例如 US.DMOB" />
              </Form.Item>
            </Col>
            <Col xs={12} md={6}>
              <Form.Item name="max_members" label="成员上限" rules={[{ required: true }]}>
                <InputNumber min={1} max={200} style={{ width: '100%' }} />
              </Form.Item>
            </Col>
            <Col xs={12} md={6}>
              <Form.Item name="note" label="说明">
                <Input />
              </Form.Item>
            </Col>
          </Row>
          <Button type="primary" loading={busy} onClick={() => void submit()}>
            生成规则草稿
          </Button>
        </Form>
      </Card>
    </Space>
  );
}

// ------------------------------------------------------------------ 策略
function StrategiesTab() {
  const { ns } = useAppState();
  const s = useApi<Items<StrategyVersion> & { deployments: Deployment[] }>(`/api/${ns}/strategies`);
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Explain>策略版本由系统版本化管理。AI 不能在运行中悄悄修改自己的决策政策；改进候选必须经过隔离模拟和您的确认（见「复盘与改进」）。</Explain>
      <ErrorAlert error={s.error} />
      <Card size="small" title="策略版本">
        <Table<StrategyVersion>
          size="small"
          rowKey="id"
          dataSource={s.data?.items ?? []}
          pagination={false}
          scroll={{ x: true }}
          expandable={{ expandedRowRender: (v) => <KV data={v.params} column={2} /> }}
          columns={[
            { title: '策略', dataIndex: 'key', render: (k: string) => (k === 'AI_AUTONOMOUS' ? 'AI 自主决策' : k === 'BASELINE_MA' ? '固定规则基准' : k) },
            { title: '版本', dataIndex: 'version' },
            { title: '状态', dataIndex: 'status', render: (v: string) => <Tag color={v === 'ACTIVE' ? 'green' : v === 'CANDIDATE' ? 'gold' : 'default'}>{v === 'ACTIVE' ? '生效中' : v === 'CANDIDATE' ? '候选' : v === 'INACTIVE' ? '已停用' : v}</Tag> },
            { title: '提示词版本', dataIndex: 'prompt_version', render: (v: string) => v || '—' },
            { title: '用户批准', dataIndex: 'approved_by_user', render: (v: boolean) => (v ? '是' : '否') },
            { title: '说明', dataIndex: 'note' },
            { title: '创建', dataIndex: 'created_at_utc', render: (v: string) => fmtMyt(v) },
          ]}
        />
      </Card>
      <Card size="small" title="上线 / 回退历史">
        <DeploymentsTable items={s.data?.deployments ?? []} />
      </Card>
    </Space>
  );
}

export function DeploymentsTable({ items }: { items: Deployment[] }) {
  return (
    <Table<Deployment>
      size="small"
      rowKey="id"
      dataSource={items}
      pagination={false}
      scroll={{ x: true }}
      locale={{ emptyText: '暂无上线或回退记录（当前为随软件交付的初始版本）' }}
      columns={[
        { title: '时间', dataIndex: 'created_at_utc', render: (v: string) => fmtMyt(v) },
        { title: '策略', dataIndex: 'strategy_key' },
        { title: '动作', dataIndex: 'action', render: (a: string) => (a === 'ACTIVATE' ? <Tag color="green">上线</Tag> : a === 'ROLLBACK' ? <Tag color="purple">回退</Tag> : a) },
        { title: '版本', render: (_, d) => `#${d.from_version_id ?? '—'} → #${d.to_version_id}` },
        { title: '候选', dataIndex: 'candidate_id', render: (v: number | null) => (v ? `#${v}` : '—') },
        { title: '操作者', dataIndex: 'actor' },
        { title: '说明', dataIndex: 'note' },
      ]}
    />
  );
}

// ------------------------------------------------------------------ 中文草稿
function DraftsTab() {
  const { ns } = useAppState();
  const drafts = useApi<Items<ConfigDraft>>(`/api/${ns}/drafts`);
  const { run, busy } = useAction();
  const { modal } = App.useApp();
  const [text, setText] = useState('');
  const [latest, setLatest] = useState<ConfigDraft | null>(null);

  const decide = async (d: ConfigDraft, action: 'confirm' | 'discard') => {
    const r = await run(() => api.post<{ note?: string }>(`/api/${ns}/drafts/${d.id}/${action}`), {
      success: (x) => (action === 'confirm' ? x.note ?? '已确认草稿' : '已放弃草稿'),
    });
    if (r) {
      setLatest(null);
      void drafts.reload();
    }
  };

  const pending = useMemo(() => (drafts.data?.items ?? []).filter((d) => d.status === 'DRAFT'), [drafts.data]);

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Explain>
        可以用中文提出设置要求，例如「单笔风险改成 0.2%」「最多持有 2 只」「每天最多 2 笔」「排除 US.DMOB」「只做马股」。系统只生成草稿并显示前后差异，必须由您确认后才保存；放宽风控或实盘变更需要再次输入密码。AI 无权修改这些设置。
      </Explain>
      <Card size="small" title="用中文提出修改">
        <Input.TextArea rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder="例如：单笔风险改成 0.2%，最多持有 2 只" />
        <Button
          type="primary"
          style={{ marginTop: 8 }}
          loading={busy}
          disabled={!text.trim()}
          onClick={async () => {
            const r = await run(() => api.post<ConfigDraft>(`/api/${ns}/drafts`, { text }), { success: '已生成草稿，请查看差异' });
            if (r) {
              setLatest(r);
              void drafts.reload();
            }
          }}
        >
          生成草稿
        </Button>
      </Card>
      {latest ? (
        <Card size="small" type="inner" title={`草稿 #${latest.id}：「${latest.request_text}」`}>
          {latest.loosens_risk ? <Alert type="error" showIcon style={{ marginBottom: 8 }} message="此草稿放宽了风控（风险变大），确认需要再次输入密码。" /> : null}
          <DiffTable diff={latest.diff} />
          <Space style={{ marginTop: 8 }}>
            <Button type="primary" danger={latest.loosens_risk} loading={busy} onClick={() => void decide(latest, 'confirm')}>确认保存</Button>
            <Button loading={busy} onClick={() => void decide(latest, 'discard')}>放弃</Button>
          </Space>
        </Card>
      ) : null}
      <Card size="small" title="草稿记录">
        <Table<ConfigDraft>
          size="small"
          rowKey="id"
          dataSource={drafts.data?.items ?? []}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          scroll={{ x: true }}
          locale={{ emptyText: '暂无草稿' }}
          expandable={{ expandedRowRender: (d) => <DiffTable diff={d.diff} /> }}
          columns={[
            { title: '#', dataIndex: 'id' },
            { title: '要求', dataIndex: 'request_text' },
            { title: '放宽风控', dataIndex: 'loosens_risk', render: (v: boolean) => (v ? <Tag color="red">是</Tag> : '否') },
            { title: '状态', dataIndex: 'status', render: (s: string) => <Tag color={s === 'CONFIRMED' ? 'green' : s === 'DRAFT' ? 'orange' : 'default'}>{s === 'CONFIRMED' ? '已确认' : s === 'DRAFT' ? '待确认' : s === 'DISCARDED' ? '已放弃' : s}</Tag> },
            { title: '创建', dataIndex: 'created_at_utc', render: (v: string) => fmtMyt(v) },
            {
              title: '',
              render: (_, d) =>
                d.status === 'DRAFT' ? (
                  <Space size={4}>
                    <Button
                      size="small"
                      type="primary"
                      onClick={() =>
                        modal.confirm({
                          title: `确认草稿 #${d.id}？`,
                          content: <DiffTable diff={d.diff} />,
                          width: Math.min(720, window.innerWidth),
                          okText: '确认保存',
                          cancelText: '取消',
                          onOk: () => decide(d, 'confirm'),
                        })
                      }
                    >
                      确认
                    </Button>
                    <Button size="small" onClick={() => void decide(d, 'discard')}>放弃</Button>
                  </Space>
                ) : null,
            },
          ]}
        />
        {pending.length ? <Text type="secondary">有 {pending.length} 个草稿待处理。</Text> : null}
      </Card>
    </Space>
  );
}

export function RiskPage() {
  const { ns } = useAppState();
  const risk = useApi<RiskInfo>(`/api/${ns}/risk`);
  const activeLocks = (risk.data?.locks ?? []).filter((l) => l.active).length;
  return (
    <div>
      <PageTitle title="策略与风控设置" sub={`${ns === 'live' ? '实盘' : '模拟'}参数单独保存；实盘参数必须单独设置并确认，不会继承模拟参数。所有变更都有版本与审计记录。`} />
      <Alert type="warning" showIcon style={{ marginBottom: 12 }} message="AI 不能修改这里的任何设置" description="风控参数、股票池规则和策略版本只能由您确认更改；更改从下一次运行授权起生效，当前授权仍使用批准时的版本。" />
      <ErrorAlert error={risk.error} />
      <Tabs
        destroyOnHidden
        items={[
          { key: 'risk', label: '风控参数', children: risk.data ? <RiskParamsTab info={risk.data} reload={() => void risk.reload()} /> : <Card loading /> },
          { key: 'locks', label: `风控锁定${activeLocks ? `（${activeLocks}）` : ''}`, children: <LocksTab locks={risk.data?.locks ?? []} reload={() => void risk.reload()} /> },
          { key: 'universe', label: '股票池规则', children: <UniverseTab /> },
          { key: 'strategies', label: '策略版本', children: <StrategiesTab /> },
          { key: 'drafts', label: '用中文提出修改', children: <DraftsTab /> },
        ]}
      />
      <Collapse
        style={{ marginTop: 12 }}
        items={[{ key: 'h', label: '产品硬上限（任何版本都不能超过）', children: risk.data ? <KV data={Object.fromEntries(Object.entries(risk.data.hard_limits).map(([k, v]) => [risk.data!.labels[k] ?? k, paramText(k, v)]))} column={2} /> : null }]}
      />
    </div>
  );
}
