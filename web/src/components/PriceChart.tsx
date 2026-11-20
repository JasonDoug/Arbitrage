import { createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef } from "react";
import type { HistoryPoint } from "../api";

const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

export function PriceChart({ points, height = 260 }: { points: HistoryPoint[]; height?: number }) {
  const el = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Area"> | null>(null);

  useEffect(() => {
    if (!el.current) return;
    const c = createChart(el.current, {
      height,
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: css("--muted"), fontFamily: "IBM Plex Mono, monospace", fontSize: 11 },
      grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.1, bottom: 0.08 } },
      timeScale: { borderVisible: false, timeVisible: true, rightOffset: 4 },
      crosshair: { horzLine: { labelBackgroundColor: css("--accent") }, vertLine: { labelBackgroundColor: css("--accent") } },
      localization: { priceFormatter: (p: number) => `${(p * 100).toFixed(1)}¢` },
    });
    series.current = c.addAreaSeries({
      lineColor: css("--accent"),
      topColor: css("--accent-soft"),
      bottomColor: "rgba(0,0,0,0)",
      lineWidth: 2,
      priceFormat: { type: "custom", formatter: (p: number) => `${(p * 100).toFixed(1)}¢`, minMove: 0.001 },
    });
    chart.current = c;
    return () => {
      c.remove();
      chart.current = null;
    };
  }, [height]);

  useEffect(() => {
    if (!series.current) return;
    series.current.setData(points.map((p) => ({ time: (new Date(p.t).getTime() / 1000) as UTCTimestamp, value: p.close })));
    chart.current?.timeScale().fitContent();
  }, [points]);

  return <div ref={el} className="chart" style={{ height }} />;
}
