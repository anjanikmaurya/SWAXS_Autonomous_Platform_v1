// SWAXS Platform icon set.
// 24x24 canvas, 2.0px primary stroke (1.5px for dense detail), round caps and
// joins, >=1px canvas padding, >=2px inner gaps. Colour comes from `currentColor`.
//
// 1D traces are the sphere form factor P(q) = [3(sin x - x cos x)/x^3]^2, x = qR,
// drawn as log I vs q with a small incoherent background so the minima stay finite.
//
//   <ReductionIcon size={22} style={{ color: "var(--swaxs-reduction)" }} />
//
// strokeWidth scales as 1/12 of size; pass absolute={true} to keep it fixed.

const Svg = ({ size = 24, title, absolute = false, children, ...rest }) => (
  <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor"
       strokeWidth={absolute ? 2.0 : (size / 12)}
       strokeLinecap="round" strokeLinejoin="round"
       role={title ? "img" : "presentation"} aria-label={title}
       aria-hidden={title ? undefined : true} {...rest}>
    {title ? <title>{title}</title> : null}
    {children}
  </svg>
);

export const CalibrationIcon = (p) => (
  <Svg {...p}>
  <circle cx="12" cy="12" r="5.2" />
  <circle cx="12" cy="12" r="9.0" strokeWidth={1.5} opacity={0.55} strokeDasharray="10 3.6" strokeDashoffset={5} />
  <circle cx="12" cy="12" r="1.2" fill="currentColor" stroke="none" />
  </Svg>
);

export const ReductionIcon = (p) => (
  <Svg {...p}>
  <circle cx="6.2" cy="7.4" r="3.2" />
  <circle cx="6.2" cy="7.4" r="1.05" fill="currentColor" stroke="none" />
  <path d="M10.8 5.6L11.7 5.8L12.5 6.5L13.3 7.6L14.1 9.1L14.7 10.7L15.3 12.6L16.6 18.9L16.9 19.9L17.1 20.0L17.2 19.9L18.2 18.2L18.5 18.0L18.9 17.9L19.5 18.2L20.8 19.6L21.4 20.0" />
  </Svg>
);

export const VisualisationIcon = (p) => (
  <Svg {...p}>
  <path d="M2.8 3.6L4.3 3.8L5.8 4.2L7.2 5.0L8.5 6.0L9.6 7.2L10.5 8.5L11.4 10.0L12.9 12.8L13.4 13.5L13.7 13.6L14.0 13.5L15.1 12.7L15.7 12.3L16.8 12.1L17.9 12.3L20.1 13.3L21.2 13.6" strokeWidth={1.5} opacity={0.55} />
  <path d="M2.8 10.0L4.3 10.2L5.8 10.6L7.2 11.4L8.5 12.4L9.6 13.6L10.5 14.9L11.4 16.4L12.9 19.2L13.4 19.9L13.7 20.0L14.0 19.9L15.1 19.1L15.7 18.7L16.8 18.5L17.9 18.7L20.1 19.7L21.2 20.0" strokeWidth={1.5} opacity={0.55} />
  <path d="M2.8 6.8L4.3 7.0L5.8 7.4L7.2 8.2L8.5 9.2L10.5 11.7L12.9 16.0L13.4 16.7L14.0 16.7L15.7 15.5L16.8 15.3L17.9 15.5L20.1 16.5L21.2 16.8" />
  </Svg>
);

export const SubtractionIcon = (p) => (
  <Svg {...p}>
  <path d="M2.8 3.4L4.3 3.6L5.8 4.2L7.2 5.1L8.5 6.5L9.5 7.9L10.4 9.5L12.5 14.8L13.0 15.5L13.4 15.5L15.0 13.9L16.0 13.5L17.3 13.8L19.8 15.4L20.4 15.6L21.2 15.5" />
  <path d="M3 15.8c5.2.5 9.4 2.2 18 3.8" strokeWidth={1.5} opacity={0.55} strokeDasharray="2.6 2.4" />
  <path d="M16.4 4.6h4.6" />
  </Svg>
);

export const QualityGateIcon = (p) => (
  <Svg {...p}>
  <path d="M2.6 4.2L3.3 4.3L3.9 4.5L4.5 5.0L5.1 5.5L6.0 6.9L7.0 9.4L7.3 9.8L7.6 9.8L8.3 9.1L8.8 9.0L9.3 9.2L10.5 9.7L11.2 9.8" strokeWidth={1.5} />
  <path d="M14.6 6.4 16.4 8.2l3.6-4" />
  <path d="m2.6 17.6 1.9-2.8 1.7 3.4 1.9-3.2 1.9 2.6" strokeWidth={1.5} />
  <path d="m15.2 14.8 4.6 4.6M19.8 14.8l-4.6 4.6" />
  </Svg>
);

export const AnalysisIcon = (p) => (
  <Svg {...p}>
  <path d="M4.8 3.4v16.4h16.4" strokeWidth={1.5} opacity={0.55} />
  <path d="M6.4 6 20.4 15.6" />
  <circle cx="8.2" cy="7.3" r="1.35" fill="currentColor" stroke="none" />
  <circle cx="12" cy="9.9" r="1.35" fill="currentColor" stroke="none" />
  <circle cx="15.8" cy="12.5" r="1.35" fill="currentColor" stroke="none" />
  <circle cx="19.6" cy="15.1" r="1.35" fill="currentColor" stroke="none" />
  </Svg>
);

export const AutoFitIcon = (p) => (
  <Svg {...p}>
  <path d="M2.8 4.4L4.4 4.6L5.9 5.3L7.4 6.4L8.7 7.9L9.9 9.6L10.9 11.4L13.3 17.8L14.0 18.9L14.5 18.9L16.3 17.4L17.5 17.0L18.8 17.3L21.2 18.7" />
  <circle cx="3.9" cy="3.5" r="1.25" fill="currentColor" stroke="none" />
  <circle cx="6.5" cy="4.1" r="1.25" fill="currentColor" stroke="none" />
  <circle cx="9.0" cy="8.0" r="1.25" fill="currentColor" stroke="none" />
  <circle cx="11.6" cy="13.7" r="1.25" fill="currentColor" stroke="none" />
  <circle cx="14.4" cy="20.4" r="1.25" fill="currentColor" stroke="none" />
  <circle cx="17.3" cy="17.2" r="1.25" fill="currentColor" stroke="none" />
  <circle cx="20.1" cy="17.4" r="1.25" fill="currentColor" stroke="none" />
  </Svg>
);

export const SynthesisIcon = (p) => (
  <Svg {...p}>
  <path d="M3.2 5.4h2.8M3.2 9.6h2.8M3.2 13.8h2.8M3.2 18h2.8" strokeWidth={1.5} opacity={0.55} />
  <path d="M6 5.4v12.6" />
  <path d="M6 11.7h13.4" strokeWidth={1.5} opacity={0.55} />
  <path d="m18.4 9.1 2.6 2.6-2.6 2.6" />
  <circle cx="10.6" cy="11.7" r="1.5" fill="currentColor" stroke="none" />
  <circle cx="15.4" cy="11.7" r="1.5" fill="currentColor" stroke="none" />
  </Svg>
);

export const GuinierIcon = (p) => (
  <Svg {...p}>
  <rect x="3.4" y="4.2" width="17.2" height="11.6" rx="2.8" />
  <path d="M8.8 15.8 8 20.6l4.6-4.8" />
  <path d="M6.4 7.2L7.8 7.3L9.2 7.6L10.5 8.1L11.7 8.7L12.6 9.4L13.4 10.3L14.0 11.2L14.8 12.8" strokeWidth={1.5} />
  <path d="M17.4 6.3c.28 1.5.83 2.05 2.29 2.33-1.46.28-2.01.83-2.29 2.33-.28-1.5-.83-2.05-2.29-2.33 1.46-.28 2.01-.83 2.29-2.33z" fill="currentColor" strokeWidth={.9} />
  </Svg>
);

export const AutoWatchIcon = (p) => (
  <Svg {...p}>
  <path d="M12 4a8 8 0 1 1-5.66 2.34" />
  <path d="M9.8 2.3 12 4l-1.7 2.3" />
  <path d="M6.8 12.2h2.6l1.8-3.6 2.3 6 1.6-2.4h2.1" strokeWidth={1.5} />
  </Svg>
);

export const ReductionAltBIcon = (p) => (
  <Svg {...p}>
  <path d="M5.70 7.58A5.4 5.4 0 0 1 5.70 16.42" strokeWidth={2.0} />
  <path d="M8.89 5.01A9.4 9.4 0 0 1 8.89 18.99" strokeWidth={1.5} opacity={0.55} />
  <circle cx="2.6" cy="12" r="1" fill="currentColor" stroke="none" />
  <path d="M13.0 4.6L13.7 4.8L14.4 5.5L15.1 6.6L15.8 8.1L16.7 11.7L17.9 18.1L18.2 19.3L18.4 19.4L18.5 19.3L19.3 17.9L19.6 17.6L19.8 17.5L20.1 17.6L20.5 17.8L21.4 18.9" />
  </Svg>
);

export const ReductionAltCIcon = (p) => (
  <Svg {...p}>
  <circle cx="12" cy="12" r="4.8" strokeWidth={1.5} opacity={0.55} />
  <circle cx="12" cy="12" r="9" strokeWidth={1.5} opacity={0.55} />
  <path d="M14.2 13.4 21.2 17.7M14.2 10.6 21.2 6.3" />
  <circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" />
  </Svg>
);

export const UiMarkIcon = (p) => (
  <Svg {...p}>
  <rect x="2.8" y="2.8" width="18.4" height="18.4" rx="4.4" />
  <path d="M6.8 7.6L7.7 7.7L8.6 8.1L9.5 8.7L10.3 9.5L11.0 10.4L11.6 11.4L13.2 15.1L13.6 15.7L13.8 15.8L14.1 15.8L15.0 15.1L15.7 14.9L16.4 14.9L17.2 15.3" strokeWidth={1.5} />
  </Svg>
);

export const UiMarkBareIcon = (p) => (
  <Svg {...p}>
  <path d="M4.2 4.8L5.5 5.0L6.8 5.7L8.0 6.8L9.2 8.3L10.1 9.9L10.9 11.7L11.7 13.9L12.9 17.9L13.4 18.9L13.7 19.0L13.9 18.9L14.9 17.7L15.4 17.2L15.9 17.0L16.4 16.9L17.3 17.2L19.3 18.6L20.2 19.0" />
  <circle cx="4.2" cy="4.8" r="1.6" fill="currentColor" stroke="none" />
  </Svg>
);

export const UiBusIcon = (p) => (
  <Svg {...p}>
  <path d="M3 16.4h18" />
  <path d="M6.2 16.4v-3.8M12 16.4v-3.8M17.8 16.4v-3.8" strokeWidth={1.5} />
  <circle cx="6.2" cy="10.8" r="1.7" fill="currentColor" stroke="none" />
  <circle cx="12" cy="10.8" r="1.7" fill="currentColor" stroke="none" />
  <circle cx="17.8" cy="10.8" r="1.7" fill="currentColor" stroke="none" />
  </Svg>
);

export const UiFolderIcon = (p) => (
  <Svg {...p}>
  <path d="M3.2 18.4V6.2A1.8 1.8 0 0 1 5 4.4h4.3l2.1 2.6h7.6a1.8 1.8 0 0 1 1.8 1.8v9.6a1.8 1.8 0 0 1-1.8 1.8H5a1.8 1.8 0 0 1-1.8-1.8z" />
  </Svg>
);

export const UiFolderChangeIcon = (p) => (
  <Svg {...p}>
  <path d="M3.4 9.2h13.2l-3-3" />
  <path d="M20.6 14.8H7.4l3 3" />
  </Svg>
);

export const UiStopAllIcon = (p) => (
  <Svg {...p}>
  <circle cx="12" cy="12" r="8.8" />
  <rect x="8.5" y="8.5" width="7" height="7" rx="1.8" strokeWidth={1.5} />
  </Svg>
);

export const UiPortsIcon = (p) => (
  <Svg {...p}>
  <path d="M9 3.4v4.2M15 3.4v4.2" strokeWidth={1.5} />
  <path d="M6.2 7.6h11.6v3.2a5.8 5.8 0 0 1-11.6 0z" />
  <path d="M12 16.6v4" />
  </Svg>
);

export const UiStartIcon = (p) => (
  <Svg {...p}>
  <path d="M8.6 5.4 19 12 8.6 18.6z" />
  </Svg>
);

export const UiStopIcon = (p) => (
  <Svg {...p}>
  <rect x="6" y="6" width="12" height="12" rx="2.4" />
  </Svg>
);

export const UiOpenIcon = (p) => (
  <Svg {...p}>
  <path d="M14.2 3.8h6v6" />
  <path d="M20.2 3.8 11.4 12.6" />
  <path d="M18 14v5a1.8 1.8 0 0 1-1.8 1.8H5.6A1.8 1.8 0 0 1 3.8 19V8.4a1.8 1.8 0 0 1 1.8-1.8h5" />
  </Svg>
);

export const UiStatusRunningIcon = (p) => (
  <Svg {...p}>
  <circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none" />
  <path d="M17.4 7.1a6.9 6.9 0 0 1 0 9.8M6.6 16.9a6.9 6.9 0 0 1 0-9.8" strokeWidth={1.5} />
  </Svg>
);

export const UiStatusStoppedIcon = (p) => (
  <Svg {...p}>
  <circle cx="12" cy="12" r="2.4" strokeWidth={1.5} />
  <circle cx="12" cy="12" r="7.4" strokeWidth={1.5} opacity={0.55} />
  </Svg>
);

export const UiLogIcon = (p) => (
  <Svg {...p}>
  <path d="M3.6 7.2h16.8" />
  <path d="M3.6 12h12.4" strokeWidth={1.5} />
  <path d="M3.6 16.8h7.6" strokeWidth={1.5} />
  <circle cx="15.6" cy="16.8" r="1.5" fill="currentColor" stroke="none" />
  </Svg>
);

export const UiClearIcon = (p) => (
  <Svg {...p}>
  <circle cx="12" cy="12" r="8.8" />
  <path d="M9.2 9.2 14.8 14.8M14.8 9.2 9.2 14.8" strokeWidth={1.5} />
  </Svg>
);

export const UiFlowNextIcon = (p) => (
  <Svg {...p}>
  <path d="M4 12h14.4" />
  <path d="m14.4 8 4.4 4-4.4 4" />
  </Svg>
);

export const UiModeStandardIcon = (p) => (
  <Svg {...p}>
  <path d="M6.8 12h3.6M13.6 12h3.6" />
  <circle cx="4.2" cy="12" r="2.2" fill="currentColor" stroke="none" />
  <circle cx="12" cy="12" r="2.2" fill="currentColor" stroke="none" />
  <circle cx="19.8" cy="12" r="2.2" fill="currentColor" stroke="none" />
  </Svg>
);

export const UiModeAutonomousIcon = (p) => (
  <Svg {...p}>
  <path d="M20.6 12a8.6 8.6 0 0 0-8.6-8.6 8.6 8.6 0 0 0-6.4 2.8L3.4 8.4" />
  <path d="M3.4 3.8v4.6H8" />
  <path d="M3.4 12a8.6 8.6 0 0 0 8.6 8.6 8.6 8.6 0 0 0 6.4-2.8l2.2-2.2" />
  <path d="M20.6 20.2v-4.6H16" />
  </Svg>
);

export const UiWarningIcon = (p) => (
  <Svg {...p}>
  <path d="M13.5 4.2a1.7 1.7 0 0 0-3 0L2.9 18.4A1.7 1.7 0 0 0 4.4 21h15.2a1.7 1.7 0 0 0 1.5-2.6z" />
  <path d="M12 9.6v4.2" strokeWidth={1.5} />
  <circle cx="12" cy="17.4" r="1.15" fill="currentColor" stroke="none" />
  </Svg>
);

export const UiPassIcon = (p) => (
  <Svg {...p}>
  <path d="M2.8 7.2L3.8 7.3L4.7 7.7L5.7 8.3L6.6 9.1L7.2 10.0L7.9 11.0L8.5 12.3L9.6 14.6L10.1 15.3L10.3 15.4L10.5 15.4L11.5 14.7L12.1 14.5L12.6 14.5L13.2 14.6" strokeWidth={1.5} />
  <path d="m14.6 12.6 2.6 2.6 4.2-6.6" />
  </Svg>
);

export const UiFlagIcon = (p) => (
  <Svg {...p}>
  <path d="M5.4 21V3.6" />
  <path d="M5.4 4.6h11.4l-2.2 4 2.2 4H5.4z" />
  </Svg>
);

export const UiCloseIcon = (p) => (
  <Svg {...p}>
  <path d="M6.2 6.2 17.8 17.8M17.8 6.2 6.2 17.8" />
  </Svg>
);

export const UiParentDirIcon = (p) => (
  <Svg {...p}>
  <path d="M5.6 3.8h12.8" strokeWidth={1.5} />
  <path d="M12 20.6V8.2" />
  <path d="m6.8 13.4 5.2-5.2 5.2 5.2" />
  </Svg>
);

export const UiDriveIcon = (p) => (
  <Svg {...p}>
  <rect x="2.8" y="5.2" width="18.4" height="13.6" rx="2.4" />
  <path d="M2.8 12.6h18.4" strokeWidth={1.5} />
  <circle cx="6.6" cy="15.8" r="1.15" fill="currentColor" stroke="none" />
  <circle cx="10.4" cy="15.8" r="1.15" fill="currentColor" stroke="none" />
  </Svg>
);

export const swaxsHubIcons = {
  'mark': UiMarkIcon,
  'mark-bare': UiMarkBareIcon,
  'bus': UiBusIcon,
  'folder': UiFolderIcon,
  'folder-change': UiFolderChangeIcon,
  'stop-all': UiStopAllIcon,
  'ports': UiPortsIcon,
  'start': UiStartIcon,
  'stop': UiStopIcon,
  'open': UiOpenIcon,
  'status-running': UiStatusRunningIcon,
  'status-stopped': UiStatusStoppedIcon,
  'log': UiLogIcon,
  'clear': UiClearIcon,
  'flow-next': UiFlowNextIcon,
  'mode-standard': UiModeStandardIcon,
  'mode-autonomous': UiModeAutonomousIcon,
  'warning': UiWarningIcon,
  'pass': UiPassIcon,
  'flag': UiFlagIcon,
  'close': UiCloseIcon,
  'parent-dir': UiParentDirIcon,
  'drive': UiDriveIcon,
};

export const swaxsIcons = {
  calibration: CalibrationIcon,
  reduction: ReductionIcon,
  visualisation: VisualisationIcon,
  subtraction: SubtractionIcon,
  quality: QualityGateIcon,
  analysis: AnalysisIcon,
  autofit: AutoFitIcon,
  synthesis: SynthesisIcon,
  guinier: GuinierIcon,
  watch: AutoWatchIcon,
};

export const swaxsAccents = {
  calibration: { balanced: "#ed1632", light: "#be0e26", dark: "#f4677a" },
  reduction: { balanced: "#2374ee", light: "#0f59c7", dark: "#5a97f3" },
  visualisation: { balanced: "#0b8c47", light: "#086e38", dark: "#0dad57" },
  subtraction: { balanced: "#ab43f1", light: "#8d11e0", dark: "#c175f5" },
  quality: { balanced: "#0a8783", light: "#086a67", dark: "#0da7a2" },
  analysis: { balanced: "#a56d0c", light: "#83570a", dark: "#cd870f" },
  autofit: { balanced: "#815ef3", light: "#6136f0", dark: "#9e84f6" },
  synthesis: { balanced: "#0d81b2", light: "#0b678e", dark: "#11a0de" },
  guinier: { balanced: "#e011a2", light: "#b20d81", dark: "#f35ac5" },
  watch: { balanced: "#dc4011", light: "#af330d", dark: "#f16f47" },
};
