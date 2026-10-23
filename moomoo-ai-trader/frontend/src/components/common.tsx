import { CheckCircleFilled, CloseCircleFilled, DownloadOutlined, InfoCircleOutlined } from '@ant-design/icons';
import { Alert, Descriptions, Empty, Space, Table, Tag, Tooltip, Typography } from 'antd';
import type { ReactNode } from 'react';
import type { Check } from '../api/types';
import { fmtMoney, fmtSigned, signOf } from '../utils/decimal';
import { fmtValue } from '../utils/format';
import {
  ACTION_COLOR,
  ACTION_LABELS,
  DECISION_OUTCOME_COLOR,
  DECISION_OUTCOME_LABELS,
  ORDER_STATUS_COLOR,
  ORDER_STATUS_LABELS,
  SIDE_LABELS,
} from '../utils/labels';

const { Text, Title, Paragraph } = Typography;

export function PageTitle({ title, sub, extra }: { title: ReactNode; sub?: ReactNode; extra?: ReactNode }) {
  return (
    <div className="page-title">
      <div>
        <Title level={3} style={{ margin: 0 }}>
          {title}
        </Title>
        {sub ? (
          <Text type="secondary" className="page-sub">
            {sub}
          </Text>
        ) : null}
      </div>
      {extra ? <Space wrap>{extra}</Space> : null}
    </div>
  );
}

/** 金额（十进制字符串）显示；pnl=true 时按正负着色并带符号。 */
export function Money({ v, ccy, pnl, digits = 2 }: { v: string | null | undefined; ccy?: string; pnl?: boolean; digits?: number }) {
  if (v === null || v === undefined || v === '') return <Text type="secondary">暂无数据</Text>;
  const s = pnl ? signOf(v) : 0;
  const cls = s > 0 ? 'pnl-up' : s < 0 ? 'pnl-down' : undefined;
  return (
    <span className={cls}>
      {pnl ? fmtSigned(v, digits) : fmtMoney(v, digits)}
      {ccy ? <span className="ccy"> {ccy}</span> : null}
    </span>
  );
}

export function OrderStatusTag({ status, label }: { status: string; label?: string }) {
  return <Tag color={ORDER_STATUS_COLOR[status] ?? 'default'}>{label || ORDER_STATUS_LABELS[status] || status}</Tag>;
}

export function OutcomeTag({ outcome, label }: { outcome: string; label?: string }) {
  return <Tag color={DECISION_OUTCOME_COLOR[outcome] ?? 'default'}>{label || DECISION_OUTCOME_LABELS[outcome] || outcome}</Tag>;
}

export function ActionTag({ action }: { action: string }) {
  return <Tag color={ACTION_COLOR[action] ?? 'default'}>{ACTION_LABELS[action] ?? action}</Tag>;
}

export function SideTag({ side }: { side: string }) {
  return <Tag color={side === 'BUY' ? 'red' : 'green'}>{SIDE_LABELS[side] ?? side}</Tag>;
}

export function OkIcon({ ok }: { ok: boolean }) {
  return ok ? <CheckCircleFilled style={{ color: '#52c41a' }} /> : <CloseCircleFilled style={{ color: '#ff4d4f' }} />;
}

export function ChecksTable({ checks, size = 'small' }: { checks: Check[] | undefined | null; size?: 'small' | 'middle' }) {
  if (!checks || !checks.length) return <Empty description="暂无检查记录" image={Empty.PRESENTED_IMAGE_SIMPLE} />;
  return (
    <Table<Check>
      size={size}
      rowKey={(r, i) => `${r.name}-${i}`}
      pagination={false}
      dataSource={checks}
      scroll={{ x: true }}
      columns={[
        { title: '结果', dataIndex: 'ok', width: 96, render: (ok: boolean) => (
          <Space size={4} style={{ whiteSpace: 'nowrap' }}>
            <OkIcon ok={ok} />
            <span>{ok ? '通过' : '未通过'}</span>
          </Space>
        ) },
        { title: '检查项', dataIndex: 'name', width: 160 },
        { title: '说明', dataIndex: 'detail' },
      ]}
    />
  );
}

/** 键值对显示（labels 提供中文名，format 可自定义值显示）。 */
export function KV({
  data,
  labels,
  column = 2,
  format,
  size = 'small',
}: {
  data: Record<string, unknown> | null | undefined;
  labels?: Record<string, string>;
  column?: number;
  format?: (key: string, v: unknown) => ReactNode;
  size?: 'small' | 'middle' | 'default';
}) {
  if (!data || !Object.keys(data).length) return <Text type="secondary">暂无数据</Text>;
  return (
    <Descriptions
      size={size}
      bordered
      column={{ xs: 1, sm: 1, md: column, lg: column, xl: column, xxl: column }}
      items={Object.entries(data).map(([k, v]) => ({
        key: k,
        label: labels?.[k] ?? k,
        children: format ? format(k, v) : fmtValue(v),
      }))}
    />
  );
}

export function Explain({ children, type = 'info', title }: { children: ReactNode; type?: 'info' | 'warning' | 'error' | 'success'; title?: ReactNode }) {
  return <Alert showIcon type={type} message={title ?? children} description={title ? children : undefined} style={{ marginBottom: 12 }} />;
}

export function ErrorAlert({ error, title }: { error?: string; title?: string }) {
  if (!error) return null;
  return <Alert showIcon type="error" message={title ?? '加载失败'} description={error} style={{ marginBottom: 12 }} />;
}

export function Pre({ text, maxHeight }: { text: string | null | undefined; maxHeight?: number }) {
  if (!text) return <Text type="secondary">暂无数据</Text>;
  return (
    <pre className="pre-block" style={maxHeight ? { maxHeight, overflow: 'auto' } : undefined}>
      {text}
    </pre>
  );
}

export function JsonBlock({ value, maxHeight = 360 }: { value: unknown; maxHeight?: number }) {
  let text: string;
  if (typeof value === 'string') {
    try {
      text = JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      text = value;
    }
  } else {
    text = JSON.stringify(value, null, 2);
  }
  return <Pre text={text} maxHeight={maxHeight} />;
}

export function DownloadLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} download className="download-link">
      <DownloadOutlined /> {children}
    </a>
  );
}

export function Hint({ text }: { text: string }) {
  return (
    <Tooltip title={text}>
      <InfoCircleOutlined style={{ color: '#8c8c8c', marginLeft: 4 }} />
    </Tooltip>
  );
}

export function NoData({ text = '暂无数据' }: { text?: string }) {
  return <Empty description={text} image={Empty.PRESENTED_IMAGE_SIMPLE} />;
}

export function Para({ children }: { children: ReactNode }) {
  return <Paragraph style={{ marginBottom: 8 }}>{children}</Paragraph>;
}
