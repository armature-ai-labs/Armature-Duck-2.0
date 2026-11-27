import { h } from './vendor/preact-htm.module.js';

/**
 * Animated 8-segment Armature commutator mark.
 * Renders a crisp inline SVG with rotating/pulsing amber-cream segments.
 */
export function ArmatureMark({ size = 24, spin = false, className = '' }) {
  const segs = [];
  const cx = 16;
  const cy = 16;
  const rInner = 6.2;
  const rOuter = 13.0;
  for (let i = 0; i < 8; i++) {
    const a0 = (i * Math.PI) / 4 - Math.PI / 2 + 0.14;
    const a1 = ((i + 1) * Math.PI) / 4 - Math.PI / 2 - 0.14;
    const x0 = cx + rOuter * Math.cos(a0);
    const y0 = cy + rOuter * Math.sin(a0);
    const x1 = cx + rOuter * Math.cos(a1);
    const y1 = cy + rOuter * Math.sin(a1);
    const x2 = cx + rInner * Math.cos(a1);
    const y2 = cy + rInner * Math.sin(a1);
    const x3 = cx + rInner * Math.cos(a0);
    const y3 = cy + rInner * Math.sin(a0);
    const d = [
      `M ${x0.toFixed(2)} ${y0.toFixed(2)}`,
      `A ${rOuter} ${rOuter} 0 0 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`,
      `L ${x2.toFixed(2)} ${y2.toFixed(2)}`,
      `A ${rInner} ${rInner} 0 0 0 ${x3.toFixed(2)} ${y3.toFixed(2)}`,
      'Z',
    ].join(' ');
    const hot = i === 0 || i === 4;
    segs.push(h('path', {
      key: i,
      d,
      fill: hot ? 'var(--accent)' : 'currentColor',
      opacity: hot ? '1' : (i % 2 === 0 ? '0.82' : '0.42'),
    }));
  }

  return h('svg', {
    className: `arm-mark ${spin ? 'arm-mark-spin' : ''} ${className}`.trim(),
    width: size,
    height: size,
    viewBox: '0 0 32 32',
    fill: 'none',
    'aria-hidden': 'true',
  },
    ...segs,
    h('circle', { cx, cy, r: 2.3, fill: 'var(--saffron)' }),
  );
}
