import { createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";

const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmt = (v: number) => `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;

export function EquityChart({ points, start, height = 200 }: { points: { ts: string; equity: number }[]; start: number; height?: number }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Baseline"> | null>(null);

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, {
      height,
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: css("--muted"), fontFamily: "IBM Plex Mono, monospace", fontSize: 11 },
      grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: true, rightOffset: 4 },
      localization: { priceFormatter: fmt },
    });
    series.current = c.addBaselineSeries({
      baseValue: { type: "price", price: start },
      topLineColor: css("--pos"),
      bottomLineColor: css("--neg"),
      topFillColor1: "rgba(60,207,142,0.25)",
      topFillColor2: "rgba(60,207,142,0.02)",
      bottomFillColor1: "rgba(255,107,107,0.02)",
      bottomFillColor2: "rgba(255,107,107,0.25)",
      lineWidth: 2,
      priceFormat: { type: "custom", formatter: fmt, minMove: 0.01 },
    });
    chart.current = c;
    return () => c.remove();
  }, [height, start]);

  useEffect(() => {
    const seen = new Set<number>();
    const data = points
      .map((p) => ({ time: Math.floor(new Date(p.ts).getTime() / 1000) as UTCTimestamp, value: p.equity }))
      .filter((d) => (seen.has(d.time) ? false : (seen.add(d.time), true)));
    series.current?.setData(data);
    chart.current?.timeScale().fitContent();
  }, [points]);

  return <div ref={el} className="chart" style={{ height }} />;
}
