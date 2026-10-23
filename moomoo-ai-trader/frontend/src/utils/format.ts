import dayjs from 'dayjs';
import utc from 'dayjs/plugin/utc';
import 'dayjs/locale/zh-cn';

dayjs.extend(utc);
dayjs.locale('zh-cn');

/** 马来西亚时间固定 UTC+8（无夏令时）。 */
const MYT_OFFSET_MIN = 8 * 60;

export function fmtMyt(iso: string | null | undefined, withSeconds = false): string {
  if (!iso) return '暂无数据';
  const d = dayjs(iso);
  if (!d.isValid()) return String(iso);
  return d.utcOffset(MYT_OFFSET_MIN).format(withSeconds ? 'YYYY-MM-DD HH:mm:ss' : 'YYYY-MM-DD HH:mm');
}

export function fmtMytDate(iso: string | null | undefined): string {
  if (!iso) return '暂无数据';
  const d = dayjs(iso);
  if (!d.isValid()) return String(iso);
  return d.utcOffset(MYT_OFFSET_MIN).format('YYYY-MM-DD');
}

/** 区间结束时间为「不含」（如次日 00:00），显示为前一天。 */
export function fmtMytDateEnd(iso: string | null | undefined): string {
  if (!iso) return '暂无数据';
  const d = dayjs(iso);
  if (!d.isValid()) return String(iso);
  return d.subtract(1, 'second').utcOffset(MYT_OFFSET_MIN).format('YYYY-MM-DD');
}

export function nowMytClock(): string {
  return dayjs().utcOffset(MYT_OFFSET_MIN).format('HH:mm:ss');
}

export function fmtAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '暂无数据';
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

export function fmtBool(v: unknown, yes = '是', no = '否'): string {
  return v ? yes : no;
}

/** 通用值显示：对象/数组压成可读文本。 */
export function fmtValue(v: unknown): string {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'boolean') return v ? '是' : '否';
  if (Array.isArray(v)) return v.length ? v.map((x) => fmtValue(x)).join('、') : '（空）';
  if (typeof v === 'object') {
    return Object.entries(v as Record<string, unknown>)
      .map(([k, x]) => `${k}: ${fmtValue(x)}`)
      .join('；');
  }
  return String(v);
}

export function shortId(id: string | null | undefined): string {
  if (!id) return '—';
  return id.length > 10 ? id.slice(0, 8) + '…' : id;
}
