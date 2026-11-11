// Light overlays for T-Studio clips: drawn on black, laid over the film in "screen" mode
// (the black disappears, the light adds). Palette of the "Тёплое кино" look.
import React from "react";
import { AbsoluteFill, interpolate, random, useCurrentFrame, useVideoConfig } from "remotion";

const GOLD = "#ffc46b";
const PEACH = "#ff9a6b";
const ROSE = "#ff6f91";
const TEAL = "#5ad1c8";

/** 0 -> 1 -> 0 over the overlay: a soft entry and a longer tail. */
const useEnvelope = (inShare = 0.2, outShare = 0.35) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const d = durationInFrames;
  return interpolate(frame, [0, d * inShare, d * (1 - outShare), d - 1], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: (t) => t * t * (3 - 2 * t),
  });
};

// Faces live in the middle of a wedding frame: overlays that keep it clear fade out there.
const CENTER_CLEAR =
  "radial-gradient(ellipse 34% 40% at 50% 42%, transparent 0%, transparent 60%, black 100%)";

const Black: React.FC<{ children: React.ReactNode; clearCenter?: boolean }> = ({
  children,
  clearCenter = false,
}) => (
  <AbsoluteFill style={{ backgroundColor: "black", overflow: "hidden" }}>
    <AbsoluteFill
      style={clearCenter ? { maskImage: CENTER_CLEAR, WebkitMaskImage: CENTER_CLEAR } : {}}
    >
      {children}
    </AbsoluteFill>
  </AbsoluteFill>
);

/** A warm light leak drifting across the frame. */
export const Leak: React.FC = () => {
  const frame = useCurrentFrame();
  const { durationInFrames, width, height } = useVideoConfig();
  const env = useEnvelope(0.25, 0.4);
  const t = frame / durationInFrames;
  const blobs = [
    { c: GOLD, x: -0.1 + 0.5 * t, y: 0.3, r: 0.75, a: 0.85 },
    { c: PEACH, x: 0.05 + 0.35 * t, y: 0.75, r: 0.6, a: 0.7 },
    { c: ROSE, x: -0.25 + 0.6 * t, y: 0.05, r: 0.45, a: 0.55 },
  ];
  return (
    <Black>
      {blobs.map((b, i) => (
        <div
          key={i}
          style={{
            position: "absolute",
            left: b.x * width - (b.r * width) / 2,
            top: b.y * height - (b.r * width) / 2,
            width: b.r * width,
            height: b.r * width,
            borderRadius: "50%",
            background: `radial-gradient(closest-side, ${b.c} 0%, ${b.c}88 35%, transparent 100%)`,
            opacity: env * b.a,
            filter: "blur(40px)",
          }}
        />
      ))}
    </Black>
  );
};

/** Thin light lines drawn across the frame with a glowing head. */
export const Lines: React.FC = () => {
  const frame = useCurrentFrame();
  const { durationInFrames, width, height } = useVideoConfig();
  const env = useEnvelope(0.1, 0.45);
  const lines = [0, 1, 2, 3, 4];
  return (
    <Black clearCenter>
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
        <defs>
          <linearGradient id="ln" x1="0" x2="1" y1="0" y2="0">
            <stop offset="0" stopColor={ROSE} stopOpacity="0" />
            <stop offset="0.5" stopColor={PEACH} />
            <stop offset="1" stopColor={GOLD} />
          </linearGradient>
          <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="9" result="b" />
            <feMerge>
              <feMergeNode in="b" />
              <feMergeNode in="b" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>
        {lines.map((i) => {
          // along the top and bottom edges of the frame, never across the faces
          const y0 = height * [0.07, 0.15, 0.8, 0.87, 0.94][i];
          const amp = height * (0.025 + 0.02 * random(`a${i}`));
          const delay = i * 4;
          const p = interpolate(frame - delay, [0, durationInFrames * 0.55], [0, 1], {
            extrapolateLeft: "clamp",
            extrapolateRight: "clamp",
            easing: (t) => 1 - Math.pow(1 - t, 3),
          });
          const d = `M ${-100} ${y0} C ${width * 0.3} ${y0 - amp}, ${width * 0.6} ${y0 + amp}, ${width + 100} ${y0 - amp / 2}`;
          const len = width * 1.4;
          return (
            <path
              key={i}
              d={d}
              fill="none"
              stroke="url(#ln)"
              strokeWidth={3 + 2 * (i % 2)}
              strokeLinecap="round"
              strokeDasharray={`${len * p} ${len}`}
              filter="url(#glow)"
              opacity={env * (0.75 + 0.25 * random(`o${i}`))}
            />
          );
        })}
      </svg>
    </Black>
  );
};

/** Coloured ribbons of light, flowing like waves. */
export const Waves: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const env = useEnvelope(0.25, 0.4);
  const ribbons = [
    { c1: GOLD, c2: PEACH, y: 0.6, amp: 0.09, k: 1.6, speed: 0.6, a: 0.7 },
    { c1: ROSE, c2: GOLD, y: 0.7, amp: 0.05, k: 2.3, speed: -0.45, a: 0.45 },
    { c1: TEAL, c2: ROSE, y: 0.78, amp: 0.04, k: 3.1, speed: 0.8, a: 0.3 },
  ];
  const steps = 48;
  return (
    <Black>
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
        <defs>
          {ribbons.map((r, i) => (
            <linearGradient key={i} id={`w${i}`} x1="0" x2="1" y1="0" y2="0">
              <stop offset="0" stopColor={r.c1} stopOpacity="0" />
              <stop offset="0.35" stopColor={r.c1} />
              <stop offset="0.7" stopColor={r.c2} />
              <stop offset="1" stopColor={r.c2} stopOpacity="0" />
            </linearGradient>
          ))}
          <filter id="soft">
            <feGaussianBlur stdDeviation="10" />
          </filter>
        </defs>
        {ribbons.map((r, i) => {
          const phase = (frame / fps) * r.speed * Math.PI;
          const top: string[] = [];
          const bottom: string[] = [];
          for (let s = 0; s <= steps; s++) {
            const x = (s / steps) * width;
            const u = (s / steps) * Math.PI * r.k + phase;
            const yc = height * (r.y + r.amp * Math.sin(u));
            const thick = height * (0.025 + 0.02 * Math.sin(u * 0.7 + 1));
            top.push(`${x},${yc - thick}`);
            bottom.unshift(`${x},${yc + thick}`);
          }
          return (
            <polygon
              key={i}
              points={[...top, ...bottom].join(" ")}
              fill={`url(#w${i})`}
              filter="url(#soft)"
              opacity={env * r.a}
            />
          );
        })}
      </svg>
    </Black>
  );
};

/** Soft out-of-focus circles of light rising slowly. */
export const Bokeh: React.FC = () => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();
  const env = useEnvelope(0.3, 0.4);
  const n = 26;
  const colors = [GOLD, PEACH, ROSE, GOLD];
  return (
    <Black clearCenter>
      {new Array(n).fill(0).map((_, i) => {
        const size = 40 + 140 * random(`s${i}`);
        const x = random(`x${i}`) * width;
        const y0 = height * (0.2 + 0.9 * random(`y${i}`));
        const rise = (frame / fps) * (15 + 30 * random(`r${i}`));
        const tw = 0.6 + 0.4 * Math.sin(frame / 9 + i);
        return (
          <div
            key={i}
            style={{
              position: "absolute",
              left: x - size / 2,
              top: y0 - rise - size / 2,
              width: size,
              height: size,
              borderRadius: "50%",
              background: `radial-gradient(closest-side, ${colors[i % 4]}cc 0%, ${colors[i % 4]}55 70%, transparent 100%)`,
              filter: `blur(${4 + 10 * random(`b${i}`)}px)`,
              opacity: env * tw * (0.35 + 0.45 * random(`o${i}`)),
            }}
          />
        );
      })}
    </Black>
  );
};

/** A quick bright diagonal sweep with streaks: the climax's first beat. */
export const Sweep: React.FC = () => {
  const frame = useCurrentFrame();
  const { durationInFrames, width, height } = useVideoConfig();
  const p = interpolate(frame, [0, durationInFrames - 1], [0, 1], {
    easing: (t) => 1 - Math.pow(1 - t, 2),
  });
  const env = useEnvelope(0.08, 0.55);
  const x = -0.4 * width + p * 1.8 * width;
  return (
    <Black>
      <div
        style={{
          position: "absolute",
          left: x,
          top: -height * 0.5,
          width: width * 0.35,
          height: height * 2,
          transform: "rotate(18deg)",
          background: `linear-gradient(90deg, transparent, ${PEACH}66, ${GOLD}, #fff6e0, ${GOLD}, ${ROSE}55, transparent)`,
          filter: "blur(28px)",
          opacity: env,
        }}
      />
      {[0, 1, 2, 3, 4, 5].map((i) => (
        <div
          key={i}
          style={{
            position: "absolute",
            left: x + (i - 2.5) * 70 - 200 * random(`d${i}`),
            top: height * (0.1 + 0.15 * i),
            width: width * (0.25 + 0.2 * random(`w${i}`)),
            height: 3,
            borderRadius: 3,
            background: `linear-gradient(90deg, transparent, ${GOLD}, transparent)`,
            filter: "blur(1.5px)",
            opacity: env * 0.8,
          }}
        />
      ))}
    </Black>
  );
};
