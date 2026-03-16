import React from "react";
import {
  AbsoluteFill,
  Img,
  useCurrentFrame,
  useVideoConfig,
  spring,
  interpolate,
  staticFile,
  Easing,
} from "remotion";

/* ── Niche-based gradient themes ── */
const NICHE_GRADIENTS: Record<string, string> = {
  cine: "linear-gradient(135deg, #1a0a2e 0%, #16213e 50%, #0f3460 100%)",
  cinema: "linear-gradient(135deg, #1a0a2e 0%, #16213e 50%, #0f3460 100%)",
  tech: "linear-gradient(135deg, #0a192f 0%, #112240 50%, #1d3557 100%)",
  tecnologia: "linear-gradient(135deg, #0a192f 0%, #112240 50%, #1d3557 100%)",
  historia: "linear-gradient(135deg, #2c1810 0%, #3e2723 50%, #4e342e 100%)",
  history: "linear-gradient(135deg, #2c1810 0%, #3e2723 50%, #4e342e 100%)",
  ciencia: "linear-gradient(135deg, #0d1b2a 0%, #1b2838 50%, #1a535c 100%)",
  science: "linear-gradient(135deg, #0d1b2a 0%, #1b2838 50%, #1a535c 100%)",
  naturaleza: "linear-gradient(135deg, #1b3a2d 0%, #2d5a3d 50%, #1a4a2e 100%)",
  nature: "linear-gradient(135deg, #1b3a2d 0%, #2d5a3d 50%, #1a4a2e 100%)",
  gaming: "linear-gradient(135deg, #1a0033 0%, #2d1b69 50%, #11001c 100%)",
  musica: "linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #e94560 100%)",
  music: "linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #e94560 100%)",
  deporte: "linear-gradient(135deg, #0a1628 0%, #1e3a5f 50%, #2e86ab 100%)",
  sports: "linear-gradient(135deg, #0a1628 0%, #1e3a5f 50%, #2e86ab 100%)",
  terror: "linear-gradient(135deg, #0a0a0a 0%, #1a0000 50%, #2d0000 100%)",
  horror: "linear-gradient(135deg, #0a0a0a 0%, #1a0000 50%, #2d0000 100%)",
  general: "linear-gradient(135deg, #0f0c29 0%, #302b63 50%, #24243e 100%)",
};

const DEFAULT_GRADIENT = NICHE_GRADIENTS.general;

/* ── Niche accent colors for frame glow ── */
const NICHE_ACCENTS: Record<string, string> = {
  cine: "rgba(233, 69, 96, 0.4)",
  cinema: "rgba(233, 69, 96, 0.4)",
  tech: "rgba(100, 255, 218, 0.3)",
  tecnologia: "rgba(100, 255, 218, 0.3)",
  historia: "rgba(255, 183, 77, 0.35)",
  history: "rgba(255, 183, 77, 0.35)",
  ciencia: "rgba(72, 202, 228, 0.35)",
  science: "rgba(72, 202, 228, 0.35)",
  naturaleza: "rgba(76, 175, 80, 0.35)",
  nature: "rgba(76, 175, 80, 0.35)",
  gaming: "rgba(157, 78, 221, 0.4)",
  musica: "rgba(233, 69, 96, 0.4)",
  music: "rgba(233, 69, 96, 0.4)",
  deporte: "rgba(46, 134, 171, 0.4)",
  sports: "rgba(46, 134, 171, 0.4)",
  terror: "rgba(200, 0, 0, 0.4)",
  horror: "rgba(200, 0, 0, 0.4)",
  general: "rgba(139, 92, 246, 0.35)",
};

export type ImageSceneProps = {
  imagePath: string; // filename in public/ folder
  durationInFrames: number;
  niche: string; // determines background gradient theme
  orientation: "horizontal" | "vertical"; // image orientation
  imageWidth?: number;  // actual image width in px (for aspect-ratio adaptation)
  imageHeight?: number; // actual image height in px
};

export const ImageScene: React.FC<ImageSceneProps> = ({
  imagePath,
  niche,
  orientation,
  imageWidth,
  imageHeight,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  const gradient = NICHE_GRADIENTS[niche.toLowerCase()] || DEFAULT_GRADIENT;
  const accent = NICHE_ACCENTS[niche.toLowerCase()] || NICHE_ACCENTS.general;

  // ── Entrance animation (first 0.8s) ──
  const entranceSpring = spring({
    frame,
    fps,
    config: { damping: 14, stiffness: 120 },
  });

  const entranceScale = interpolate(entranceSpring, [0, 1], [0.75, 1]);
  const entranceOpacity = interpolate(frame, [0, Math.min(12, durationInFrames)], [0, 1], {
    extrapolateRight: "clamp",
    extrapolateLeft: "clamp",
  });

  // ── Ken Burns subtle zoom during hold ──
  const kenBurnsScale = interpolate(
    frame,
    [0, durationInFrames],
    [1.0, 1.04],
    { extrapolateRight: "clamp", extrapolateLeft: "clamp" }
  );

  // ── Exit animation (last 0.5s) ──
  const exitStart = Math.max(0, durationInFrames - Math.round(0.5 * fps));
  const exitProgress = interpolate(
    frame,
    [exitStart, durationInFrames],
    [0, 1],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" }
  );
  const exitScale = interpolate(exitProgress, [0, 1], [1, 0.85]);
  const exitOpacity = interpolate(exitProgress, [0, 1], [1, 0]);

  // Combined transforms
  const totalScale = entranceScale * kenBurnsScale * exitScale;
  const totalOpacity = entranceOpacity * exitOpacity;

  // ── Background image blur scale (slightly larger to avoid edges) ──
  const bgKenBurns = interpolate(
    frame,
    [0, durationInFrames],
    [1.15, 1.22],
    { extrapolateRight: "clamp", extrapolateLeft: "clamp" }
  );

  // ── Image frame dimensions — SMALLER so blurred background is visible ──
  // Clamp aspect ratio to avoid extreme shapes (ultra-wide collages, etc.)
  // Standard frame: ~1300x730 (16:9-ish), adapts moderately to actual image AR
  const MAX_W = 1300;
  const MAX_H = 730;
  const MIN_W = 420;   // never narrower than this
  const MIN_H = 500;   // never shorter than this (avoids thin strips)

  let frameWidth: number;
  let frameHeight: number;

  if (imageWidth && imageHeight && imageWidth > 0 && imageHeight > 0) {
    // Clamp aspect ratio between 0.5 (2:1 vertical) and 2.2 (landscape max)
    // This prevents extreme shapes like ultra-wide collages or ultra-tall strips
    const rawAr = imageWidth / imageHeight;
    const ar = Math.max(0.5, Math.min(rawAr, 2.2));

    if (ar >= 1) {
      // Horizontal or square — fit within MAX_W × MAX_H
      frameWidth = MAX_W;
      frameHeight = Math.round(MAX_W / ar);
      if (frameHeight > MAX_H) {
        frameHeight = MAX_H;
        frameWidth = Math.round(MAX_H * ar);
      }
      // Enforce minimums
      if (frameHeight < MIN_H) {
        frameHeight = MIN_H;
        frameWidth = Math.round(MIN_H * ar);
        if (frameWidth > MAX_W) frameWidth = MAX_W;
      }
    } else {
      // Vertical — fit within MIN_W...MAX_W × MAX_H
      frameHeight = MAX_H;
      frameWidth = Math.round(MAX_H * ar);
      if (frameWidth < MIN_W) {
        frameWidth = MIN_W;
        frameHeight = Math.round(MIN_W / ar);
        if (frameHeight > MAX_H) frameHeight = MAX_H;
      }
    }
  } else {
    // Fallback: standard 16:9 frame
    frameWidth = orientation === "vertical" ? MIN_W : MAX_W;
    frameHeight = orientation === "vertical" ? MAX_H : MAX_H;
  }

  const borderRadius = 12;

  // ── Shadow pulse animation ──
  const shadowPulse = interpolate(
    frame,
    [0, durationInFrames / 2, durationInFrames],
    [0.6, 1, 0.6],
    { extrapolateRight: "clamp", extrapolateLeft: "clamp" }
  );

  return (
    <AbsoluteFill style={{ background: "#000" }}>
      {/* Layer 1: Blurred background image */}
      <AbsoluteFill
        style={{
          transform: `scale(${bgKenBurns})`,
          filter: "blur(40px) brightness(0.4) saturate(1.3)",
        }}
      >
        <Img
          src={staticFile(imagePath)}
          style={{
            width: "100%",
            height: "100%",
            objectFit: "cover",
          }}
        />
      </AbsoluteFill>

      {/* Layer 2: Niche gradient overlay */}
      <AbsoluteFill
        style={{
          background: gradient,
          opacity: 0.65,
          mixBlendMode: "overlay",
        }}
      />

      {/* Layer 3: Vignette */}
      <AbsoluteFill
        style={{
          background:
            "radial-gradient(ellipse at center, transparent 40%, rgba(0,0,0,0.7) 100%)",
        }}
      />

      {/* Layer 4: Framed image — centered */}
      <AbsoluteFill
        style={{
          justifyContent: "center",
          alignItems: "center",
        }}
      >
        <div
          style={{
            width: frameWidth,
            height: frameHeight,
            borderRadius: borderRadius + 4,
            transform: `scale(${totalScale})`,
            opacity: totalOpacity,
            boxShadow: `0 20px 60px rgba(0,0,0,${0.5 * shadowPulse}), 0 0 80px ${accent}`,
            position: "relative",
          }}
        >
          {/* Thin subtle border frame */}
          <div
            style={{
              position: "absolute",
              inset: -3,
              borderRadius: borderRadius + 4,
              border: "1px solid rgba(255,255,255,0.12)",
            }}
          />

          {/* The image itself */}
          <div
            style={{
              width: "100%",
              height: "100%",
              borderRadius,
              overflow: "hidden",
              position: "relative",
            }}
          >
            <Img
              src={staticFile(imagePath)}
              style={{
                width: "100%",
                height: "100%",
                objectFit: "cover",
                transform: `scale(${kenBurnsScale})`,
              }}
            />

            {/* Subtle inner shadow for depth */}
            <div
              style={{
                position: "absolute",
                inset: 0,
                borderRadius,
                boxShadow: "inset 0 0 30px rgba(0,0,0,0.15)",
                pointerEvents: "none",
              }}
            />
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};
