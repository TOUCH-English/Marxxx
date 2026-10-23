import { CloudDownloadOutlined, DeleteOutlined, PlusOutlined, UploadOutlined } from '@ant-design/icons';
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  DatePicker,
  Descriptions,
  Form,
  Input,
  InputNumber,
  List,
  Progress,
  Row,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd';
import type { Dayjs } from 'dayjs';
import { useEffect, useMemo, useState } from 'react';
import { api, qs } from '../api/client';
import type { AIUsage, AuditRow, FeeItem, FeeSchedule, FeesInfo, Items, SettingsInfo } from '../api/types';
import { DownloadLink, ErrorAlert, Explain, PageTitle } from '../components/common';
import { useDrawers } from '../components/Drawers';
import { useAppState } from '../state/app';
import { useAction, useApi } from '../state/hooks';
import { fmtMoney } from '../utils/decimal';
import { fmtMyt, shortId } from '../utils/format';
import { AUDIT_CATEGORIES, AUDIT_CATEGORY_LABELS, MARKET_LABELS } from '../utils/labels';

const { Text, Paragraph } = Typography;

// ------------------------------------------------------------------ 日志
function LogsTab() {
  const { ns } = useAppState();
  const { openTrace } = useDrawers();
  const [f, setF] = useState<{ category?: string; symbol?: string; decision_id?: string; intent_id?: string; q?: string; day?: Dayjs | null }>({});
  const [applied, setApplied] = useState(f);
  const path = `/api/${ns}/logs${qs({
    category: applied.category,
    symbol: applied.symbol?.trim().toUpperCase(),
    decision_id: applied.decision_id?.trim(),
    intent_id: applied.intent_id?.trim(),
    q: applied.q?.trim(),
    day: applied.day ? applied.day.format('YYYY-MM-DD') : undefined,
    limit: 500,
  })}`;
  const logs = useApi<Items<AuditRow>>(path);
  return (
    <>
      <Card size="small" style={{ marginBottom: 12 }}>
        <Row gutter={[8, 8]}>
          <Col xs={12} md={4}>
            <Select
              allowClear
              placeholder="类别"
              style={{ width: '100%' }}
              value={f.category}
              onChange={(v) => setF({ ...f, category: v })}
              options={AUDIT_CATEGORIES.map((c) => ({ value: c, label: AUDIT_CATEGORY_LABELS[c] ?? c }))}
            />
          </Col>
          <Col xs={12} md={4}>
            <Input allowClear placeholder="证券代码" value={f.symbol} onChange={(e) => setF({ ...f, symbol: e.target.value })} />
          </Col>
          <Col xs={12} md={4}>
            <DatePicker style={{ width: '100%' }} placeholder="日期（马来西亚）" value={f.day ?? null} onChange={(v) => setF({ ...f, day: v })} />
          </Col>
          <Col xs={12} md={4}>
            <Input allowClear placeholder="决策 ID" value={f.decision_id} onChange={(e) => setF({ ...f, decision_id: e.target.value })} />
          </Col>
          <Col xs={12} md={4}>
            <Input allowClear placeholder="订单 ID" value={f.intent_id} onChange={(e) => setF({ ...f, intent_id: e.target.value })} />
          </Col>
          <Col xs={12} md={4}>
            <Input allowClear placeholder="关键词" value={f.q} onChange={(e) => setF({ ...f, q: e.target.value })} onPressEnter={() => setApplied(f)} />
          </Col>
        </Row>
        <Space style={{ marginTop: 8 }} wrap>
          <Button type="primary" onClick={() => setApplied(f)}>查询</Button>
          <Button onClick={() => { setF({}); setApplied({}); }}>清除条件</Button>
          <DownloadLink href={`/api/${ns}/export/audit.csv`}>导出审计日志 CSV</DownloadLink>
        </Space>
      </Card>
      <ErrorAlert error={logs.error} />
      <Table<AuditRow>
        size="small"
        rowKey="id"
        loading={logs.loading && !logs.data}
        dataSource={logs.data?.items ?? []}
        pagination={{ pageSize: 50, hideOnSinglePage: true, showSizeChanger: false }}
        scroll={{ x: 1000 }}
        onRow={(r) => (r.decision_id || r.intent_id ? { onClick: () => openTrace({ decision_id: r.decision_id || undefined, intent_id: r.intent_id || undefined }), className: 'clickable-row' } : {})}
        locale={{ emptyText: '没有符合条件的日志' }}
        columns={[
          { title: '#', dataIndex: 'id', width: 70 },
          { title: '业务时间（马来西亚）', render: (_, r) => fmtMyt(r.as_of || r.created_at_utc), width: 150 },
          { title: '类别', dataIndex: 'category', render: (c: string) => <Tag>{AUDIT_CATEGORY_LABELS[c] ?? c}</Tag>, width: 90 },
          { title: '动作', dataIndex: 'action', width: 140 },
          { title: '操作者', dataIndex: 'actor', width: 80 },
          { title: '证券', dataIndex: 'symbol', render: (v: string | null) => v || '—', width: 90 },
          { title: '内容', dataIndex: 'message' },
          {
            title: '因果链',
            width: 110,
            render: (_, r) => (r.decision_id || r.intent_id ? <a>{shortId(r.decision_id || r.intent_id)}</a> : '—'),
          },
        ]}
      />
      <Text type="secondary">点击带决策/订单 ID 的行可打开因果链。系统记录时间与业务时间可能不同（回放模式下业务时间是模拟时钟）。</Text>
    </>
  );
}

// ------------------------------------------------------------------ 设置
function SettingsTab() {
  const { ns } = useAppState();
  const s = useApi<SettingsInfo>('/api/settings', { live: false });
  const usage = useApi<AIUsage>(`/api/${ns}/ai/usage`);
  const { run, busy } = useAction();
  const [backup, setBackup] = useState<{ files: string[]; dir: string; note: string } | null>(null);
  const d = s.data;
  const u = usage.data;
  const pct = (a: number, b: number) => (b > 0 ? Math.min(100, Math.round((a / b) * 100)) : 0);
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <ErrorAlert error={s.error} />
      {d ? (
        <>
          <Card size="small" title="AI 调用设置（只读）">
            <Descriptions
              size="small"
              bordered
              column={{ xs: 1, md: 2 }}
              items={[
                { key: 'p', label: '提供方 / 模型', children: `${d.ai.provider} / ${d.ai.model}` },
                { key: 'k', label: 'API 密钥', children: <Space>{d.ai.key_present ? <Tag color="green">已配置</Tag> : <Tag color="orange">未配置</Tag>}<Text type="secondary">{d.ai.key_hint}</Text></Space> },
                { key: 't', label: '超时', children: `${d.ai.timeout_s} 秒（失败重试 ${d.ai.max_retries} 次）` },
                { key: 'f', label: '同一标的最短分析间隔', children: `${d.ai.min_interval_minutes} 分钟` },
                { key: 'cl', label: '每月调用上限', children: `${d.ai.monthly_call_limit} 次` },
                { key: 'tl', label: '每月 Token 上限', children: fmtMoney(String(d.ai.monthly_token_limit), 0) },
                { key: 'b', label: '每月预算', children: `${d.ai.monthly_budget_usd} USD` },
                { key: 'pr', label: '价格（每百万 Token）', children: `输入 ${d.ai.price_input_per_mtok} / 输出 ${d.ai.price_output_per_mtok} USD` },
                { key: 'ps', label: '价格来源', children: d.ai.price_source, span: 2 },
              ]}
            />
            <Alert style={{ marginTop: 8 }} type="info" showIcon message="如何修改" description={d.how_to_change} />
            <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>页面永远不会显示密钥内容。模型 API 按量计费，与 Claude 聊天订阅无关。</Paragraph>
          </Card>
          <Card size="small" title={`本月 AI 用量（${ns === 'live' ? '实盘' : '模拟'}）`}>
            <ErrorAlert error={usage.error} />
            {u ? (
              <Row gutter={[16, 12]}>
                <Col xs={24} md={8}>
                  <Text>调用次数 {u.calls} / {u.call_limit}</Text>
                  <Progress percent={pct(u.calls, u.call_limit)} size="small" />
                </Col>
                <Col xs={24} md={8}>
                  <Text>Token {fmtMoney(String(u.input_tokens + u.output_tokens), 0)} / {fmtMoney(String(u.token_limit), 0)}</Text>
                  <Progress percent={pct(u.input_tokens + u.output_tokens, u.token_limit)} size="small" />
                </Col>
                <Col xs={24} md={8}>
                  <Text>估算费用 {u.estimated_cost_usd} / {u.budget_usd} USD</Text>
                  <Progress percent={pct(Number(u.estimated_cost_usd), Number(u.budget_usd))} size="small" status={Number(u.estimated_cost_usd) >= Number(u.budget_usd) ? 'exception' : 'normal'} />
                </Col>
                <Col span={24}><Text type="secondary">{u.price_source}。达到上限后不再调用模型，也不会产生新的 AI 订单；已有保护规则不依赖 AI。</Text></Col>
              </Row>
            ) : null}
          </Card>
          <Card size="small" title="部署配置（只读）">
            <Descriptions
              size="small"
              bordered
              column={{ xs: 1, md: 2 }}
              items={[
                { key: 'l', label: '实盘总开关', children: d.deployment.live_trading_enabled ? <Tag color="red">已开启</Tag> : <Tag>关闭（默认）</Tag> },
                { key: 'c', label: '接受客户端监控止损（实盘）', children: d.deployment.allow_client_side_stops_live ? <Tag color="orange">接受（断网/关机时不会触发）</Tag> : '不接受' },
                { key: 'a', label: '网页地址', children: d.deployment.app },
                { key: 'o', label: 'OpenD 地址', children: d.deployment.opend },
                { key: 'f', label: '券商', children: d.deployment.security_firm },
                { key: 'd', label: '数据目录', children: <Text code>{d.data_dir}</Text> },
              ]}
            />
          </Card>
          <Card size="small" title="备份">
            <Paragraph>{d.backup}</Paragraph>
            <Button
              icon={<CloudDownloadOutlined />}
              loading={busy}
              onClick={async () => {
                const r = await run(() => api.post<{ files: string[]; dir: string; note: string }>('/api/backup'), { success: '备份完成' });
                if (r) setBackup(r);
              }}
            >
              立即备份数据库
            </Button>
            {backup ? (
              <Alert
                style={{ marginTop: 8 }}
                type="success"
                showIcon
                message={`已备份到 ${backup.dir}`}
                description={
                  <>
                    <div>{backup.files.join('、')}</div>
                    <div>{backup.note}</div>
                  </>
                }
              />
            ) : null}
          </Card>
          <Card size="small" title="通知">
            <Paragraph style={{ marginBottom: 0 }}>{d.notifications}</Paragraph>
          </Card>
          <Card size="small" title="退出登录">
            <Paragraph style={{ marginBottom: 0 }}>右上角「退出」只结束网页会话；已获授权的后台运行会继续（包括持仓保护）。要停止运行，请到总览页使用「停止本次运行」。</Paragraph>
          </Card>
        </>
      ) : (
        <Card loading />
      )}
    </Space>
  );
}

// ------------------------------------------------------------------ 费用与汇率
const FEE_FIELDS: { key: keyof FeeItem; label: string }[] = [
  { key: 'rate', label: '费率（比例，如 0.0008）' },
  { key: 'per_share', label: '每股' },
  { key: 'min', label: '最低' },
  { key: 'max', label: '最高' },
  { key: 'max_rate_of_value', label: '最高占成交额比例' },
];

function FeesTab() {
  const { ns, overview } = useAppState();
  const fees = useApi<FeesInfo>(`/api/${ns}/fees`);
  const { run, busy } = useAction();
  const [market, setMarket] = useState<'MY' | 'US'>('MY');
  const [items, setItems] = useState<FeeItem[]>([]);
  const [source, setSource] = useState('');
  const [verified, setVerified] = useState(false);
  const [fx, setFx] = useState<{ rate: string; source: string }>({ rate: '', source: '' });

  const active = useMemo(() => (fees.data?.items ?? []).find((f) => f.market === market && f.active), [fees.data, market]);
  useEffect(() => {
    setItems((active?.params.items ?? []).map((x) => ({ ...x })));
    setSource('');
    setVerified(false);
  }, [active?.id, market]); // eslint-disable-line react-hooks/exhaustive-deps

  const setItem = (i: number, k: keyof FeeItem, v: string) =>
    setItems((arr) => arr.map((x, j) => (j === i ? (() => { const n = { ...x }; if (v === '') delete n[k]; else n[k] = v; return n; })() : x)));

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <ErrorAlert error={fees.error} />
      <Card size="small" title="需要考虑的成本">
        <List size="small" dataSource={fees.data?.cost_items ?? []} renderItem={(x) => <List.Item>{x}</List.Item>} />
        {fees.data ? (
          <Text type="secondary">
            模型价格：{fees.data.ai_pricing.model}，输入 {fees.data.ai_pricing.input_per_mtok_usd} / 输出 {fees.data.ai_pricing.output_per_mtok_usd} USD 每百万 Token（{fees.data.ai_pricing.source}）
          </Text>
        ) : null}
      </Card>
      <Card size="small" title="费用表">
        <Table<FeeSchedule>
          size="small"
          rowKey="id"
          dataSource={fees.data?.items ?? []}
          pagination={{ pageSize: 6, hideOnSinglePage: true }}
          scroll={{ x: true }}
          columns={[
            { title: '市场', dataIndex: 'market', render: (m: string) => MARKET_LABELS[m] ?? m },
            { title: '名称', render: (_, f) => f.params.label ?? '—' },
            { title: '状态', render: (_, f) => <Space size={2}>{f.active ? <Tag color="blue">生效中</Tag> : <Tag>历史</Tag>}{f.is_test ? <Tag color="orange">测试/未核对</Tag> : <Tag color="green">已核对官方费率</Tag>}</Space> },
            {
              title: '费用项',
              render: (_, f) => (
                <ul className="compact-list">
                  {(f.params.items ?? []).map((x, i) => (
                    <li key={i}>
                      {x.name}（{x.side === 'SELL' ? '仅卖出' : x.side === 'BUY' ? '仅买入' : '买卖'}）：
                      {FEE_FIELDS.filter((ff) => x[ff.key] !== undefined).map((ff) => `${ff.label.replace(/（.*）/, '')} ${String(x[ff.key])}`).join('，')}
                    </li>
                  ))}
                </ul>
              ),
            },
            { title: '来源', dataIndex: 'source' },
            { title: '时间', dataIndex: 'as_of', render: (v: string) => fmtMyt(v) },
          ]}
        />
      </Card>
      <Card size="small" title="更新费用表">
        <Explain type="warning">请按 moomoo 官网当前收费逐项核对后再勾选「已按官方费率核对」。未核对的费用表会让实盘待核实清单无法通过。{ns === 'live' ? '实盘更新需要再次输入密码。' : ''}</Explain>
        <Space wrap style={{ marginBottom: 8 }}>
          <Select value={market} onChange={setMarket} style={{ width: 120 }} options={[{ value: 'MY', label: '马股' }, { value: 'US', label: '美股' }]} />
          <Text type="secondary">以当前生效的费用表为起点修改</Text>
        </Space>
        <Table<FeeItem>
          size="small"
          rowKey={(_, i) => String(i)}
          dataSource={items}
          pagination={false}
          scroll={{ x: true }}
          columns={[
            { title: '名称', render: (_, x, i) => <Input value={x.name} onChange={(e) => setItem(i, 'name', e.target.value)} style={{ minWidth: 140 }} /> },
            {
              title: '适用',
              render: (_, x, i) => (
                <Select value={x.side ?? 'BOTH'} onChange={(v) => setItem(i, 'side', v)} style={{ width: 100 }} options={[{ value: 'BOTH', label: '买卖' }, { value: 'BUY', label: '仅买入' }, { value: 'SELL', label: '仅卖出' }]} />
              ),
            },
            ...FEE_FIELDS.map((ff) => ({
              title: ff.label,
              render: (_: unknown, x: FeeItem, i: number) => (
                <Input value={x[ff.key] !== undefined ? String(x[ff.key]) : ''} onChange={(e) => setItem(i, ff.key, e.target.value.trim())} style={{ width: 110 }} placeholder="—" />
              ),
            })),
            { title: '', render: (_, __, i) => <Button size="small" icon={<DeleteOutlined />} onClick={() => setItems((a) => a.filter((_, j) => j !== i))} /> },
          ]}
        />
        <Button size="small" icon={<PlusOutlined />} style={{ marginTop: 8 }} onClick={() => setItems((a) => [...a, { name: '新费用项', side: 'BOTH' }])}>
          添加费用项
        </Button>
        <Space wrap style={{ marginTop: 12, display: 'flex' }}>
          <Input placeholder="来源（例如：moomoo 官网收费页，2026-10-23 核对）" value={source} onChange={(e) => setSource(e.target.value)} style={{ width: 360 }} />
          <Checkbox checked={verified} onChange={(e) => setVerified(e.target.checked)}>已按官方费率核对</Checkbox>
          <Button
            type="primary"
            loading={busy}
            disabled={!source.trim() || !items.length || items.some((x) => !x.name)}
            onClick={async () => {
              const r = await run(() => api.post(`/api/${ns}/fees`, { market, items, source, verified_against_official: verified }), { success: '费用表已更新' });
              if (r) void fees.reload();
            }}
          >
            保存为新的{MARKET_LABELS[market]}费用表
          </Button>
        </Space>
      </Card>
      <Card size="small" title="汇率（USD → MYR，仅用于折算显示，不会自动换汇）">
        <Paragraph>
          当前：
          {overview?.fx ? (
            <Text strong>
              1 USD = {overview.fx.rate} MYR（{overview.fx.source}，{fmtMyt(overview.fx.as_of)}）
            </Text>
          ) : (
            <Text type="secondary">暂无数据（未录入时总览不折算合计）</Text>
          )}
        </Paragraph>
        <Space wrap>
          <InputNumber<string> stringMode min="0" step="0.01" placeholder="例如 4.20" value={fx.rate || null} onChange={(v) => setFx({ ...fx, rate: v ?? '' })} style={{ width: 140 }} />
          <Input placeholder="来源（例如：银行牌价，2026-10-23）" value={fx.source} onChange={(e) => setFx({ ...fx, source: e.target.value })} style={{ width: 300 }} />
          <Button
            loading={busy}
            disabled={!fx.rate || !fx.source.trim()}
            onClick={async () => {
              const r = await run(() => api.post(`/api/${ns}/fx`, { pair: 'USDMYR', rate: fx.rate, source: fx.source }), { success: '汇率已录入' });
              if (r) setFx({ rate: '', source: '' });
            }}
          >
            录入汇率
          </Button>
        </Space>
      </Card>
    </Space>
  );
}

// ------------------------------------------------------------------ 数据导入
interface InstRow {
  symbol: string;
  market: string;
  name: string;
  sec_type: string;
  currency: string;
  lot_size: number;
}

function HistoryImport() {
  const { ns } = useAppState();
  const { run, busy } = useAction();
  const [csv, setCsv] = useState('');
  const [fileName, setFileName] = useState('');
  const [rows, setRows] = useState<InstRow[]>([]);
  const [err, setErr] = useState<string>();
  const [note, setNote] = useState('');

  const onFile = (f: File | undefined) => {
    if (!f) return;
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result ?? '').replace(/^﻿/, '');
      const lines = text.split(/\r?\n/).filter((l) => l.trim());
      const header = (lines[0] ?? '').split(',').map((h) => h.trim());
      const need = ['symbol', 'date', 'open', 'high', 'low', 'close', 'volume'];
      const missing = need.filter((n) => !header.includes(n));
      if (missing.length) {
        setErr(`CSV 缺少列：${missing.join('、')}（需要 ${need.join(', ')}）`);
        setCsv('');
        setRows([]);
        return;
      }
      setErr(undefined);
      const si = header.indexOf('symbol');
      const syms = [...new Set(lines.slice(1).map((l) => l.split(',')[si]?.trim()).filter(Boolean))];
      setRows(
        syms.map((s) => {
          const market = s.startsWith('US.') ? 'US' : 'MY';
          return { symbol: s, market, name: s, sec_type: 'STOCK', currency: market === 'US' ? 'USD' : 'MYR', lot_size: market === 'US' ? 1 : 100 };
        }),
      );
      setCsv(text);
      setFileName(f.name);
    };
    reader.readAsText(f, 'utf-8');
  };

  const upd = (i: number, k: keyof InstRow, v: string | number) => setRows((r) => r.map((x, j) => (j === i ? { ...x, [k]: v } : x)));

  if (ns !== 'sim') return <Alert type="info" showIcon message="历史数据只用于模拟（本地纸上交易的历史回放）。请切换到「自动模拟」。" />;
  return (
    <Card size="small" title="导入历史日K（用于本地纸上交易的历史回放）">
      <Explain>
        只导入您有权使用的数据。CSV 需要列：symbol, date, open, high, low, close, volume（日期格式 YYYY-MM-DD，代码如 MY.1155、US.AAPL）。导入后在启动向导选择「本地纸上交易」即使用历史回放（非实时）。
      </Explain>
      <Space wrap>
        <label className="ant-btn ant-btn-default" style={{ display: 'inline-flex', alignItems: 'center', gap: 6, cursor: 'pointer', padding: '4px 15px' }}>
          <UploadOutlined /> 选择 CSV 文件
          <input type="file" accept=".csv,text/csv" style={{ display: 'none' }} onChange={(e) => onFile(e.target.files?.[0])} />
        </label>
        {fileName ? <Text>{fileName}（{csv.split(/\r?\n/).filter((l) => l.trim()).length - 1} 行）</Text> : null}
      </Space>
      {err ? <Alert type="error" showIcon message={err} style={{ marginTop: 8 }} /> : null}
      {rows.length ? (
        <>
          <Paragraph style={{ marginTop: 12 }}>请填写/核对每只证券的资料：</Paragraph>
          <Table<InstRow>
            size="small"
            rowKey="symbol"
            pagination={false}
            dataSource={rows}
            scroll={{ x: true }}
            columns={[
              { title: '代码', dataIndex: 'symbol' },
              { title: '名称', render: (_, r, i) => <Input value={r.name} onChange={(e) => upd(i, 'name', e.target.value)} /> },
              { title: '市场', render: (_, r, i) => <Select value={r.market} style={{ width: 90 }} onChange={(v) => { upd(i, 'market', v); upd(i, 'currency', v === 'US' ? 'USD' : 'MYR'); }} options={[{ value: 'MY', label: '马股' }, { value: 'US', label: '美股' }]} /> },
              { title: '类型', render: (_, r, i) => <Select value={r.sec_type} style={{ width: 100 }} onChange={(v) => upd(i, 'sec_type', v)} options={[{ value: 'STOCK', label: '股票' }, { value: 'ETF', label: 'ETF' }]} /> },
              { title: '币种', dataIndex: 'currency' },
              { title: '每手', render: (_, r, i) => <InputNumber min={1} value={r.lot_size} onChange={(v) => upd(i, 'lot_size', Number(v) || 1)} style={{ width: 90 }} /> },
            ]}
          />
          <Space wrap style={{ marginTop: 8 }}>
            <Input placeholder="数据来源/授权说明" value={note} onChange={(e) => setNote(e.target.value)} style={{ width: 320 }} />
            <Button
              type="primary"
              loading={busy}
              onClick={() => run(() => api.post<{ rows: number }>('/api/sim/imports', { instruments: rows, daily_csv: csv, note }), { success: (r) => `已导入 ${r.rows} 行历史数据` })}
            >
              导入
            </Button>
          </Space>
        </>
      ) : null}
    </Card>
  );
}

function NewsImport() {
  const { ns } = useAppState();
  const { run, busy } = useAction();
  const [form] = Form.useForm();
  return (
    <Card size="small" title="导入新闻（不可信的外部数据）">
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: 12 }}
        message="新闻是不可信的外部数据"
        description="新闻只会作为参考数据交给 AI，系统不会把其中的任何文字（例如「忽略规则」「买入某股」）当作指令。必须提供来源链接与发布时间。"
      />
      <Form form={form} layout="vertical">
        <Row gutter={12}>
          <Col xs={24} md={6}>
            <Form.Item name="symbol" label="证券代码" rules={[{ required: true, message: '必填' }]}>
              <Input placeholder="例如 MY.1155" />
            </Form.Item>
          </Col>
          <Col xs={24} md={18}>
            <Form.Item name="title" label="标题" rules={[{ required: true, message: '必填' }]}>
              <Input />
            </Form.Item>
          </Col>
          <Col xs={24} md={12}>
            <Form.Item name="url" label="来源链接" rules={[{ required: true, message: '必填' }, { type: 'url', message: '请输入完整链接（https://…）' }]}>
              <Input placeholder="https://" />
            </Form.Item>
          </Col>
          <Col xs={24} md={6}>
            <Form.Item name="source" label="来源名称" rules={[{ required: true, message: '必填' }]}>
              <Input placeholder="例如 The Edge Malaysia" />
            </Form.Item>
          </Col>
          <Col xs={24} md={6}>
            <Form.Item name="published_at" label="发布时间" rules={[{ required: true, message: '必填' }]}>
              <DatePicker showTime style={{ width: '100%' }} />
            </Form.Item>
          </Col>
          <Col span={24}>
            <Form.Item name="summary" label="摘要（可选）">
              <Input.TextArea rows={2} />
            </Form.Item>
          </Col>
        </Row>
        <Button
          type="primary"
          loading={busy}
          onClick={async () => {
            const v = await form.validateFields();
            const pub = (v.published_at as Dayjs).format();
            const r = await run(
              () =>
                api.post<{ imported: number }>(`/api/${ns}/news/import`, {
                  items: [{ symbol: String(v.symbol).trim().toUpperCase(), title: v.title, url: v.url, source: v.source, published_at: pub, fetched_at: new Date().toISOString(), summary: v.summary ?? '' }],
                }),
              { success: (x) => `已导入 ${x.imported} 条新闻（标记为不可信外部数据）` },
            );
            if (r) form.resetFields();
          }}
        >
          导入这条新闻
        </Button>
      </Form>
    </Card>
  );
}

export function LogsPage() {
  return (
    <div>
      <PageTitle title="日志与设置" sub="按日期、股票、决策或订单查询完整日志与因果链；查看 AI 预算、费用、备份与数据导入。" />
      <Tabs
        destroyOnHidden
        items={[
          { key: 'logs', label: '日志', children: <LogsTab /> },
          { key: 'settings', label: '系统设置与备份', children: <SettingsTab /> },
          { key: 'fees', label: '费用与汇率', children: <FeesTab /> },
          {
            key: 'import',
            label: '数据导入',
            children: (
              <Space direction="vertical" size="middle" style={{ width: '100%' }}>
                <HistoryImport />
                <NewsImport />
              </Space>
            ),
          },
        ]}
      />
    </div>
  );
}
