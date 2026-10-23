import { ApiOutlined, ReloadOutlined } from '@ant-design/icons';
import { Alert, Button, Card, Checkbox, Col, Descriptions, Form, Modal, Row, Select, Space, Steps, Table, Tag, Typography } from 'antd';
import { useMemo, useState } from 'react';
import { api } from '../api/client';
import type { Binding, Capability, ConnectionStatus, DiagAccount, DiagItem, DiagResult, LiveGate } from '../api/types';
import { ChecksTable, ErrorAlert, Explain, PageTitle } from '../components/common';
import { useAppState } from '../state/app';
import { useAction, useApi } from '../state/hooks';
import { fmtMyt } from '../utils/format';
import { CAPABILITY_STATUS, DIAG_STATUS, MARKET_LABELS, TRD_ENV_LABELS } from '../utils/labels';

const { Text, Paragraph } = Typography;

function DiagItems({ items }: { items: DiagItem[] }) {
  return (
    <Table<DiagItem>
      size="small"
      rowKey={(r, i) => `${r.key}-${i}`}
      pagination={false}
      dataSource={items}
      scroll={{ x: true }}
      columns={[
        { title: '检查项', dataIndex: 'label', width: 180 },
        {
          title: '结果',
          dataIndex: 'status',
          width: 110,
          render: (s: string) => <Tag color={DIAG_STATUS[s]?.color ?? 'default'}>{DIAG_STATUS[s]?.label ?? s}</Tag>,
        },
        {
          title: '说明',
          render: (_, it) => (
            <Space direction="vertical" size={2}>
              <Text>{it.detail}</Text>
              {it.market ? <Text type="secondary">市场：{MARKET_LABELS[it.market] ?? it.market}</Text> : null}
              {it.hint?.length ? (
                <ol className="compact-list">
                  {it.hint.map((h) => (
                    <li key={h}>{h}</li>
                  ))}
                </ol>
              ) : null}
            </Space>
          ),
        },
      ]}
    />
  );
}

interface MergedAccount extends DiagAccount {
  allMarkets: string[];
}

function mergeAccounts(list: DiagAccount[]): MergedAccount[] {
  const m = new Map<string, MergedAccount>();
  for (const a of list) {
    const k = `${a.acc_id}|${a.trd_env}`;
    const cur = m.get(k);
    if (cur) {
      cur.allMarkets = [...new Set([...cur.allMarkets, ...a.markets])];
    } else {
      m.set(k, { ...a, allMarkets: [...a.markets] });
    }
  }
  return [...m.values()];
}

function BindModal({ acc, onClose }: { acc: MergedAccount | null; onClose: () => void }) {
  const { run, busy } = useAction();
  const [markets, setMarkets] = useState<string[]>([]);
  const [agree, setAgree] = useState(false);
  const isReal = acc?.trd_env === 'REAL';
  const marketOpts = ['MY', 'US'].map((m) => ({ value: m, label: MARKET_LABELS[m], disabled: acc ? !acc.allMarkets.includes(m) : true }));
  return (
    <Modal
      open={!!acc}
      title="确认选择此账户"
      okText="确认选择此账户"
      cancelText="取消"
      okButtonProps={{ disabled: !agree || !markets.length, danger: isReal }}
      confirmLoading={busy}
      onCancel={onClose}
      afterOpenChange={(o) => {
        if (o && acc) {
          setMarkets(acc.allMarkets.filter((m) => m === 'MY' || m === 'US'));
          setAgree(false);
        }
      }}
      onOk={async () => {
        if (!acc) return;
        const r = await run(
          () => api.post<{ note?: string }>('/api/connection/bind', { acc_id: acc.acc_id, trd_env: acc.trd_env, markets, acc_type: acc.acc_type, confirm: true }),
          { success: (x) => x.note ?? '已绑定' },
        );
        if (r) onClose();
      }}
    >
      {acc ? (
        <>
          {isReal ? <Alert type="error" showIcon style={{ marginBottom: 12 }} message="这是真实账户" description="绑定真实账户需要再次输入密码。绑定不等于开启实盘：实盘仍需部署开关与全部检查通过。" /> : null}
          <Descriptions
            size="small"
            bordered
            column={1}
            items={[
              { key: 'id', label: '账户（脱敏）', children: acc.acc_id_masked },
              { key: 'env', label: '环境', children: TRD_ENV_LABELS[acc.trd_env] ?? acc.trd_env },
              { key: 't', label: '账户类型', children: acc.acc_type || '—' },
              { key: 'f', label: '券商', children: acc.security_firm || '—' },
              { key: 's', label: '状态', children: acc.status || '—' },
            ]}
          />
          <div style={{ margin: '12px 0' }}>
            <Text strong>允许系统在以下市场交易：</Text>
            <Checkbox.Group options={marketOpts} value={markets} onChange={(v) => setMarkets(v as string[])} />
          </div>
          <Checkbox checked={agree} onChange={(e) => setAgree(e.target.checked)}>
            我确认这是我要交给本系统使用的账户（系统只管理获分配的额度和自己买入的持仓）。
          </Checkbox>
        </>
      ) : null}
    </Modal>
  );
}

export function ConnectionPage() {
  const { status, reloadStatus } = useAppState();
  const conn = useApi<ConnectionStatus>('/api/connection/status');
  const gate = useApi<LiveGate>('/api/live/gate');
  const { run, busy } = useAction();
  const [fresh, setFresh] = useState<DiagResult | null>(null);
  const [bindAcc, setBindAcc] = useState<MergedAccount | null>(null);
  const [form] = Form.useForm<{ acc_id?: string; trd_env?: string; symbols?: string[] }>();

  const result = fresh ?? conn.data?.last ?? null;
  const accounts = useMemo(() => mergeAccounts(fresh?.accounts ?? []), [fresh]);
  const lastAccounts = useMemo(() => mergeAccounts(conn.data?.last?.accounts ?? []), [conn.data]);
  const opendOk = status?.opend.reachable;

  const diagnose = async (withAccount: boolean) => {
    const v = withAccount ? await form.validateFields() : {};
    const r = await run(
      () =>
        api.post<DiagResult>('/api/connection/diagnose', {
          acc_id: v.acc_id || null,
          trd_env: v.trd_env || null,
          symbols: v.symbols ?? [],
        }),
      { success: (x) => (x.ok ? '只读诊断完成：通过' : '只读诊断完成：有未通过的项目') },
    );
    if (r) {
      setFresh(r);
      void conn.reload();
      void gate.reload();
      void reloadStatus();
    }
  };

  const accountColumns = (selectable: boolean) => [
    { title: '账户（脱敏）', dataIndex: 'acc_id_masked' },
    { title: '环境', dataIndex: 'trd_env', render: (v: string) => <Tag color={v === 'REAL' ? 'red' : 'blue'}>{TRD_ENV_LABELS[v] ?? v}</Tag> },
    { title: '账户类型', dataIndex: 'acc_type' },
    { title: '券商', dataIndex: 'security_firm' },
    { title: '市场权限', dataIndex: 'allMarkets', render: (v: string[]) => v.map((m) => <Tag key={m}>{MARKET_LABELS[m] ?? m}</Tag>) },
    { title: '状态', dataIndex: 'status' },
    {
      title: '操作',
      render: (_: unknown, a: MergedAccount) =>
        selectable ? (
          <Button size="small" type="primary" onClick={() => setBindAcc(a)}>
            选择此账户
          </Button>
        ) : (
          <Text type="secondary">请重新检测后选择</Text>
        ),
    },
  ];

  const bindingCols = [
    { title: '账户', dataIndex: 'acc_id_masked' },
    { title: '券商', dataIndex: 'broker' },
    { title: '环境', dataIndex: 'trd_env', render: (v: string) => TRD_ENV_LABELS[v] ?? v },
    { title: '市场', dataIndex: 'markets', render: (v: string[]) => (v ?? []).map((m) => MARKET_LABELS[m] ?? m).join('、') },
    { title: '用户确认', dataIndex: 'confirmed_by_user', render: (v: boolean) => (v ? <Tag color="green">已确认</Tag> : <Tag>未确认</Tag>) },
    { title: '最近验证', dataIndex: 'verified_at', render: (v: string | null) => (v ? fmtMyt(v) : <Text type="secondary">未验证</Text>) },
    { title: '生效', dataIndex: 'active', render: (v: boolean) => (v ? <Tag color="blue">当前使用</Tag> : <Tag>已停用</Tag>) },
  ];

  const capCols = [
    { title: '市场', dataIndex: 'market', render: (m: string) => MARKET_LABELS[m] ?? m },
    { title: '能力', dataIndex: 'item' },
    { title: '状态', dataIndex: 'status', render: (s: string) => <Tag color={CAPABILITY_STATUS[s]?.color}>{CAPABILITY_STATUS[s]?.label ?? s}</Tag> },
    { title: '说明', dataIndex: 'detail' },
    { title: '来源', dataIndex: 'source' },
    { title: '检查时间', dataIndex: 'checked_at', render: (v: string) => fmtMyt(v) },
  ];

  return (
    <div>
      <PageTitle
        title="连接与权限诊断"
        sub="只读检测 moomoo OpenD、账户列表、行情与交易权限。检测不会下单，也不会索取您的 moomoo 密码。"
        extra={
          <Button type="primary" icon={<ReloadOutlined />} loading={busy} onClick={() => void diagnose(false)} data-testid="diagnose-btn">
            重新检测（只读）
          </Button>
        }
      />
      <ErrorAlert error={conn.error} />

      <Row gutter={[12, 12]}>
        <Col xs={24} lg={10}>
          <Card size="small" title={<Space><ApiOutlined />OpenD 连接</Space>} className="card-fill">
            <Descriptions
              size="small"
              column={1}
              items={[
                { key: 'a', label: 'OpenD 地址', children: status?.opend.address ?? '暂无数据' },
                { key: 'r', label: '端口可连接', children: opendOk === undefined ? '暂无数据' : opendOk ? <Tag color="green">可连接</Tag> : <Tag>尚未连接</Tag> },
                { key: 'c', label: '最后检查', children: result?.checked_at ? fmtMyt(result.checked_at, true) : '尚未检测' },
                { key: 's', label: 'SDK 版本', children: (result?.sdk_version as string) ?? '暂无数据' },
                { key: 'f', label: '券商（.env）', children: result?.security_firm ?? '暂无数据' },
              ]}
            />
          </Card>
        </Col>
        <Col xs={24} lg={14}>
          <Card size="small" title="最少登录步骤（只在官方 OpenD 窗口输入密码）" className="card-fill">
            {!opendOk ? <Alert type="warning" showIcon message="OpenD 尚未连接" style={{ marginBottom: 8 }} /> : null}
            <Steps
              direction="vertical"
              size="small"
              current={opendOk ? (conn.data?.login_steps.length ?? 4) : 0}
              items={(conn.data?.login_steps ?? []).map((s, i) => ({ title: `第 ${i + 1} 步`, description: s }))}
            />
            <Paragraph type="secondary" style={{ marginBottom: 0 }}>
              本系统永远不会要求您在网页或聊天中提供 moomoo 密码或验证码。没有 moomoo 账户也可以使用「演示」模式。
            </Paragraph>
          </Card>
        </Col>
      </Row>

      <Card size="small" title="检测结果" style={{ marginTop: 12 }}>
        {result ? (
          <>
            <Space style={{ marginBottom: 8 }} wrap>
              <Tag color={result.ok ? 'green' : 'red'}>{result.ok ? '全部通过' : '存在未通过项目'}</Tag>
              {!fresh && conn.data?.last ? <Text type="secondary">（显示的是上一次保存的检测结果，账户 ID 已脱敏）</Text> : null}
            </Space>
            <DiagItems items={result.items} />
          </>
        ) : (
          <Text type="secondary">尚未检测。点击右上角「重新检测（只读）」。</Text>
        )}
      </Card>

      <Card size="small" title="账户列表（请明确选择，系统不会默认使用第一个账户）" style={{ marginTop: 12 }}>
        {accounts.length ? (
          <Table<MergedAccount> size="small" rowKey={(a) => `${a.acc_id_masked}-${a.trd_env}`} pagination={false} dataSource={accounts} columns={accountColumns(true)} scroll={{ x: true }} />
        ) : lastAccounts.length ? (
          <>
            <Alert type="info" showIcon style={{ marginBottom: 8 }} message="以下来自上一次保存的检测（账户 ID 已脱敏）。为确保选择正确，请先重新检测，再在新结果中选择账户。" />
            <Table<MergedAccount> size="small" rowKey={(a) => `${a.acc_id_masked}-${a.trd_env}`} pagination={false} dataSource={lastAccounts} columns={accountColumns(false)} scroll={{ x: true }} />
          </>
        ) : (
          <Text type="secondary">暂无账户数据（需要 OpenD 已登录后检测）。</Text>
        )}
      </Card>

      <Card size="small" title="带账户的只读验证（选择账户后运行，验证现金字段、交易权限与订单类型）" style={{ marginTop: 12 }}>
        <Form form={form} layout="inline" style={{ rowGap: 8 }}>
          <Form.Item name="acc_id" label="账户 ID" rules={[{ required: true, message: '请填写账户 ID' }]}>
            <Select
              style={{ minWidth: 200 }}
              placeholder="从检测结果中选择"
              options={accounts.map((a) => ({ value: a.acc_id, label: `${a.acc_id_masked}（${TRD_ENV_LABELS[a.trd_env] ?? a.trd_env}）` }))}
              notFoundContent="请先重新检测"
            />
          </Form.Item>
          <Form.Item name="trd_env" label="环境" rules={[{ required: true, message: '请选择环境' }]}>
            <Select style={{ width: 140 }} options={[{ value: 'SIMULATE', label: '券商模拟' }, { value: 'REAL', label: '真实账户' }]} />
          </Form.Item>
          <Form.Item name="symbols" label="测试行情代码">
            <Select mode="tags" style={{ minWidth: 220 }} placeholder="可选，如 US.AAPL" tokenSeparators={[',', ' ']} />
          </Form.Item>
          <Form.Item>
            <Button loading={busy} onClick={() => void diagnose(true)}>
              运行带账户的只读验证
            </Button>
          </Form.Item>
        </Form>
      </Card>

      <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
        <Col xs={24} lg={12}>
          <Card size="small" title="已绑定账户 · 模拟" className="card-fill">
            <Table<Binding> size="small" rowKey="id" pagination={false} dataSource={conn.data?.capabilities.sim_bindings ?? []} columns={bindingCols} scroll={{ x: true }} locale={{ emptyText: '暂无' }} />
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small" title="已绑定账户 · 实盘" className="card-fill">
            <Table<Binding> size="small" rowKey="id" pagination={false} dataSource={conn.data?.capabilities.live_bindings ?? []} columns={bindingCols} scroll={{ x: true }} locale={{ emptyText: '尚未选择真实账户' }} />
          </Card>
        </Col>
      </Row>

      <Card size="small" title="市场能力（按账户/市场检测，不假设马股与美股支持相同订单类型）" style={{ marginTop: 12 }}>
        <Table<Capability>
          size="small"
          rowKey={(r) => `sim-${r.id}`}
          pagination={false}
          dataSource={conn.data?.capabilities.sim ?? []}
          columns={capCols}
          scroll={{ x: true }}
          title={() => <Text strong>模拟</Text>}
          locale={{ emptyText: '尚未检测' }}
        />
        <Table<Capability>
          size="small"
          rowKey={(r) => `live-${r.id}`}
          pagination={false}
          dataSource={conn.data?.capabilities.live ?? []}
          columns={capCols}
          scroll={{ x: true }}
          title={() => <Text strong>实盘</Text>}
          locale={{ emptyText: '尚未检测' }}
          style={{ marginTop: 12 }}
        />
      </Card>

      <Card size="small" title="实盘接入前待核实清单" style={{ marginTop: 12 }} data-testid="gate-card">
        <ErrorAlert error={gate.error} />
        {gate.data ? (
          <>
            <Explain type={gate.data.passed ? 'success' : 'warning'}>{gate.data.note}</Explain>
            <ChecksTable checks={gate.data.checks} />
          </>
        ) : null}
      </Card>

      <BindModal acc={bindAcc} onClose={() => { setBindAcc(null); void conn.reload(); void gate.reload(); }} />
    </div>
  );
}
