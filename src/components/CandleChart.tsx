import { useEffect, useMemo, useRef } from 'react';
import { CandlestickSeries, ColorType, HistogramSeries, createChart, type IChartApi, type ISeriesApi, type Time, type UTCTimestamp } from 'lightweight-charts';

export type Candle = { time: number; open: number; high: number; low: number; close: number; volume: number };

export default function CandleChart({ candles, interval }: { candles: Candle[]; interval: number }) {
  const element = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const price = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volume = useRef<ISeriesApi<'Histogram'> | null>(null);
  const fitted = useRef(false);
  const grouped = useMemo(() => {
    const output: Candle[] = [];
    for (const candle of candles) {
      const time = Math.floor(candle.time / (interval * 60)) * interval * 60;
      const last = output[output.length - 1];
      if (last?.time === time) {
        last.high = Math.max(last.high, candle.high);
        last.low = Math.min(last.low, candle.low);
        last.close = candle.close;
        last.volume += candle.volume || 0;
      } else output.push({ ...candle, time, volume: candle.volume || 0 });
    }
    return output;
  }, [candles, interval]);

  useEffect(() => {
    if (!element.current) return;
    const instance = createChart(element.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: '#111318' }, textColor: '#777e8d', fontFamily: 'Inter, Microsoft YaHei, sans-serif', fontSize: 10 },
      grid: { vertLines: { color: '#1c1f26' }, horzLines: { color: '#1c1f26' } },
      rightPriceScale: { borderColor: '#262a33', scaleMargins: { top: 0.12, bottom: 0.22 } },
      timeScale: { borderColor: '#262a33', timeVisible: true, secondsVisible: false, rightOffset: 6, barSpacing: 7, tickMarkFormatter: (time: Time) => new Date(Number(time) * 1000).toLocaleTimeString('en-GB', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit' }) },
      localization: { timeFormatter: (time: Time) => new Date(Number(time) * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) },
      crosshair: { mode: 0, vertLine: { color: '#8b91a0', labelBackgroundColor: '#303640' }, horzLine: { color: '#8b91a0', labelBackgroundColor: '#303640' } },
      handleScroll: true, handleScale: true,
    });
    const series = instance.addSeries(CandlestickSeries, { upColor: '#35b992', downColor: '#ed6876', borderVisible: false, wickUpColor: '#35b992', wickDownColor: '#ed6876', priceFormat: { type: 'price', precision: 2, minMove: 0.01 } });
    const volumes = instance.addSeries(HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: 'volume', lastValueVisible: false, priceLineVisible: false });
    instance.priceScale('volume').applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
    chart.current = instance; price.current = series; volume.current = volumes; fitted.current = false;
    return () => { instance.remove(); chart.current = null; price.current = null; volume.current = null; };
  }, []);

  useEffect(() => { fitted.current = false; }, [interval]);
  useEffect(() => {
    if (!price.current || !volume.current) return;
    price.current.setData(grouped.map(c => ({ time: c.time as UTCTimestamp, open: c.open, high: c.high, low: c.low, close: c.close })));
    volume.current.setData(grouped.map(c => ({ time: c.time as Time, value: c.volume, color: c.close >= c.open ? '#35b99235' : '#ed687635' })));
    if (!fitted.current && grouped.length) { chart.current?.timeScale().fitContent(); fitted.current = true; }
  }, [grouped]);

  return <div className="candle-chart" ref={element} role="img" aria-label={`${interval} 分钟模拟或浏览器观测K线图，可拖动和缩放`} />;
}
