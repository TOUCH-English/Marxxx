/**
 * 金额/价格在 API 中均为十进制字符串。这里只做「显示用」的字符串处理，
 * 需要相加时使用 BigInt 定点运算，绝不用 JS 浮点数累计金额。
 */

const DEC_RE = /^([+-])?(\d*)(?:\.(\d*))?$/;

interface Parsed {
  neg: boolean;
  int: string;
  frac: string;
}

function parse(v: string): Parsed | null {
  const s = v.trim();
  const m = DEC_RE.exec(s);
  if (!m || (m[2] === '' && (m[3] === undefined || m[3] === ''))) return null;
  return { neg: m[1] === '-', int: (m[2] || '0').replace(/^0+(?=\d)/, ''), frac: m[3] ?? '' };
}

export function isDecimalString(v: unknown): v is string {
  return typeof v === 'string' && parse(v) !== null;
}

function toScaled(p: Parsed, scale: number): bigint {
  const frac = (p.frac + '0'.repeat(scale)).slice(0, scale);
  const n = BigInt(p.int + frac);
  return p.neg ? -n : n;
}

function fromScaled(n: bigint, scale: number): string {
  const neg = n < 0n;
  const abs = (neg ? -n : n).toString().padStart(scale + 1, '0');
  const int = abs.slice(0, abs.length - scale);
  const frac = scale > 0 ? abs.slice(abs.length - scale) : '';
  return (neg ? '-' : '') + int + (frac ? '.' + frac : '');
}

/** 四舍五入（远离零）到 digits 位小数，返回字符串。 */
export function roundDecimal(v: string, digits: number): string {
  const p = parse(v);
  if (!p) return v;
  const keep = p.frac.slice(0, digits).padEnd(digits, '0');
  let n = BigInt(p.int + keep);
  const next = p.frac.charAt(digits);
  if (next !== '' && Number(next) >= 5) n += 1n;
  const out = fromScaled(n, digits);
  const isZero = /^0(\.0*)?$/.test(out);
  return p.neg && !isZero ? '-' + out : out;
}

/** 把小数点左移/右移（例如 0.0025 → 0.25 用于百分比显示），纯字符串运算。 */
export function shiftDecimal(v: string, places: number): string {
  const p = parse(v);
  if (!p) return v;
  let digits = p.int + p.frac;
  let point = p.int.length + places;
  if (point < 0) {
    digits = '0'.repeat(-point) + digits;
    point = 0;
  }
  if (point > digits.length) digits = digits + '0'.repeat(point - digits.length);
  let int = digits.slice(0, point).replace(/^0+(?=\d)/, '') || '0';
  let frac = digits.slice(point).replace(/0+$/, '');
  if (int === '') int = '0';
  const out = frac ? `${int}.${frac}` : int;
  const isZero = /^0(\.0*)?$/.test(out);
  return p.neg && !isZero ? '-' + out : out;
}

/** 千分位分组（只作用于整数部分）。 */
function group(int: string): string {
  return int.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/**
 * 显示金额：保留 digits 位小数并加千分位。空值显示「暂无数据」。
 */
export function fmtMoney(v: string | number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || v === '') return '暂无数据';
  const s = typeof v === 'number' ? String(v) : v;
  const p = parse(s);
  if (!p) return s;
  const r = roundDecimal(s, digits);
  const neg = r.startsWith('-');
  const body = neg ? r.slice(1) : r;
  const [i, f] = body.split('.');
  return (neg ? '-' : '') + group(i) + (f ? '.' + f : '');
}

/** 带正负号的金额（盈亏显示用）。 */
export function fmtSigned(v: string | null | undefined, digits = 2): string {
  if (v === null || v === undefined || v === '') return '暂无数据';
  const out = fmtMoney(v, digits);
  if (out.startsWith('-') || /^0(\.0*)?$/.test(out.replace(/,/g, ''))) return out;
  return '+' + out;
}

/** 价格：保留原始精度（去掉多余的尾随 0，最多 6 位小数）。 */
export function fmtPrice(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === '') return '暂无数据';
  const s = typeof v === 'number' ? String(v) : v;
  const p = parse(s);
  if (!p) return s;
  const r = roundDecimal(s, Math.min(Math.max(p.frac.length, 2), 6));
  const [i, f] = r.replace(/^-/, '').split('.');
  const fr = f ? f.replace(/0+$/, '').padEnd(2, '0') : '';
  return (r.startsWith('-') ? '-' : '') + group(i) + (fr ? '.' + fr : '');
}

/** 比例字符串（如 "0.0025"）显示为百分比（"0.25%"）。 */
export function fmtRatioPct(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === '') return '暂无数据';
  return shiftDecimal(String(v), 2) + '%';
}

/** 已经是百分数的字符串（如 "1.28"）显示为 "1.28%"。 */
export function fmtPct(v: string | number | null | undefined, signed = false): string {
  if (v === null || v === undefined || v === '') return '暂无数据';
  const s = String(v);
  if (signed) return fmtSigned(s) + '%';
  return fmtMoney(s, 2) + '%';
}

export function signOf(v: string | null | undefined): -1 | 0 | 1 {
  if (!v) return 0;
  const p = parse(v);
  if (!p) return 0;
  if (/^0*$/.test(p.int + p.frac)) return 0;
  return p.neg ? -1 : 1;
}

/** 十进制字符串求和（BigInt 定点运算）。 */
export function sumDecimals(values: (string | null | undefined)[]): string {
  const parsed = values.filter((v): v is string => typeof v === 'string').map(parse).filter((p): p is Parsed => !!p);
  if (!parsed.length) return '0';
  const scale = Math.max(...parsed.map((p) => p.frac.length));
  const total = parsed.reduce((acc, p) => acc + toScaled(p, scale), 0n);
  return fromScaled(total, scale);
}

/** 比较两个十进制字符串：a<b → -1，a=b → 0，a>b → 1。 */
export function compareDecimals(a: string, b: string): number {
  const pa = parse(a);
  const pb = parse(b);
  if (!pa || !pb) return 0;
  const scale = Math.max(pa.frac.length, pb.frac.length);
  const x = toScaled(pa, scale);
  const y = toScaled(pb, scale);
  return x < y ? -1 : x > y ? 1 : 0;
}

/** 仅用于图表坐标（显示），不用于金额计算。 */
export function chartNumber(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}
