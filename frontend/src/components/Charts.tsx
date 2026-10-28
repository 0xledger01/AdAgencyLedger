import { useRef, useState } from "react";

/** 媒体配色：与报表图例/折线保持一致；未知媒体回落到紫色。 */
export const PLATFORM_COLORS: Record<string, string> = {
  TIKTOK: "#2563eb",
  FB: "#1877f2",
  GOOGLE: "#ea4335",
  OTHER: "#8b5cf6",
};

export function platformColor(code: string): string {
  return PLATFORM_COLORS[code] || PLATFORM_COLORS.OTHER;
}

/** 表格里的迷你趋势线：只表达形状，不标刻度。 */
export function Sparkline({
  points,
  color = "var(--accent)",
  width = 92,
  height = 26,
}: {
  points: number[];
  color?: string;
  width?: number;
  height?: number;
}) {
  if (!points.length) return <span className="faint">—</span>;
  const max = Math.max(...points, 1);
  const min = Math.min(...points, 0);
  const span = max - min || 1;
  const step = points.length > 1 ? width / (points.length - 1) : 0;
  const coords = points.map((v, i) => {
    const x = i * step;
    const y = height - 2 - ((v - min) / span) * (height - 4);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  });
  return (
    <svg
      className="spark"
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      preserveAspectRatio="none"
      aria-hidden
    >
      <polyline points={coords.join(" ")} fill="none" stroke={color} strokeWidth="1.5" />
    </svg>
  );
}

export interface TrendSeries {
  platform: string;
  label: string;
  points: string[];
}

function niceStep(value: number): number {
  if (!Number.isFinite(value) || value <= 0) return 1;
  const pow = Math.pow(10, Math.floor(Math.log10(value)));
  const f = value / pow;
  const m = f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10;
  return m * pow;
}

function compact(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${(value / 1000).toFixed(0)}k`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(Math.round(value));
}

/**
 * 消耗趋势：按天、按媒体画折线。
 * 纯 SVG，不引图表库；悬停显示当天各媒体的数值。
 */
export function TrendChart({
  days,
  series,
  symbol,
  height = 280,
}: {
  days: string[];
  series: TrendSeries[];
  symbol: string;
  height?: number;
}) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [hover, setHover] = useState<number | null>(null);

  const W = 780;
  const H = height;
  const pad = { l: 56, r: 16, t: 16, b: 30 };
  const n = days.length;
  const all = series.flatMap((s) => s.points.map((v) => Number(v) || 0));
  const rawMax = Math.max(1, ...all);
  const step = niceStep(rawMax / 4);
  const max = Math.ceil(rawMax / step) * step;
  const innerW = W - pad.l - pad.r;
  const innerH = H - pad.t - pad.b;
  const stepX = n > 1 ? innerW / (n - 1) : 0;
  const x = (i: number) => pad.l + i * stepX;
  const y = (v: number) => pad.t + (1 - v / max) * innerH;
  const ticks: number[] = [];
  for (let t = 0; t <= max + step / 2; t += step) ticks.push(t);

  const labelStep = Math.max(1, Math.ceil(n / 7));
  const hoverX = hover === null ? 0 : x(hover);
  const tipRows = hover === null ? [] : series.map((s) => ({
    label: s.label,
    color: platformColor(s.platform),
    value: Number(s.points[hover] || 0),
  }));
  const tipW = 156;
  const tipH = 24 + tipRows.length * 16;
  const tipLeft = Math.min(Math.max(hoverX + 10, pad.l), W - pad.r - tipW);

  if (!n) {
    return <div className="empty">所选区间没有消耗数据</div>;
  }

  return (
    <div className="trend-wrap">
      <svg
        ref={svgRef}
        className="trend-svg"
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label="消耗趋势"
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const rect = svgRef.current?.getBoundingClientRect();
          if (!rect) return;
          const rel = ((e.clientX - rect.left) / rect.width) * W;
          const idx = Math.round((rel - pad.l) / (stepX || 1));
          setHover(Math.max(0, Math.min(n - 1, idx)));
        }}
      >
        {ticks.map((t, i) => (
          <g key={i}>
            <line
              x1={pad.l}
              x2={W - pad.r}
              y1={y(t)}
              y2={y(t)}
              stroke="var(--border)"
              strokeDasharray={i === 0 ? "0" : "3 4"}
            />
            <text x={pad.l - 8} y={y(t) + 3} textAnchor="end" className="axis-label">
              {compact(t)}
            </text>
          </g>
        ))}

        {days.map((d, i) =>
          i % labelStep === 0 || i === n - 1 ? (
            <text key={d} x={x(i)} y={H - 8} textAnchor="middle" className="axis-label">
              {d.slice(5)}
            </text>
          ) : null
        )}

        {series.map((s) => (
          <polyline
            key={s.platform}
            points={s.points.map((v, i) => `${x(i)},${y(Number(v) || 0)}`).join(" ")}
            fill="none"
            stroke={platformColor(s.platform)}
            strokeWidth="2"
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        ))}

        {hover !== null && (
          <g>
            <line
              x1={hoverX}
              x2={hoverX}
              y1={pad.t}
              y2={H - pad.b}
              stroke="var(--border-strong)"
              strokeDasharray="3 3"
            />
            {series.map((s) => (
              <circle
                key={s.platform}
                cx={hoverX}
                cy={y(Number(s.points[hover] || 0))}
                r="3.2"
                fill={platformColor(s.platform)}
                stroke="var(--panel)"
                strokeWidth="1.4"
              />
            ))}
            <g transform={`translate(${tipLeft}, ${pad.t + 6})`} pointerEvents="none">
              <rect
                width={tipW}
                height={tipH}
                rx="8"
                fill="var(--panel)"
                stroke="var(--border-strong)"
              />
              <text x="10" y="16" className="tip-title">
                {days[hover]}
              </text>
              {tipRows.map((r, i) => (
                <g key={r.label} transform={`translate(10, ${30 + i * 16})`}>
                  <circle cx="3" cy="-3" r="3" fill={r.color} />
                  <text x="12" y="0" className="tip-row">
                    {r.label}
                  </text>
                  <text x={tipW - 12} y="0" textAnchor="end" className="tip-value">
                    {symbol}
                    {r.value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                  </text>
                </g>
              ))}
            </g>
          </g>
        )}
      </svg>

      <div className="trend-legend">
        {series.map((s) => (
          <span key={s.platform} className="legend-item">
            <i style={{ background: platformColor(s.platform) }} />
            {s.label}
          </span>
        ))}
        {!series.length && <span className="faint">无数据</span>}
      </div>
    </div>
  );
}
