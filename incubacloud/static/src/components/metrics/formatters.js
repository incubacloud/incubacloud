import {_t} from "@web/core/l10n/translation";
import {userLocale} from "../../utils/dates";

const KIB = 1024;
const MIB = KIB * 1024;
const GIB = MIB * 1024;
const TIB = GIB * 1024;

/** Between a number and its unit: a line never breaks there. */
const NBSP = "\u00a0";

/**
 * A number in the reader's language: its decimal separator and digit
 * grouping, not the browser's.
 *
 * @param {number} value
 * @param {number} digits fraction digits, at most
 * @returns {string}
 */
export function formatNumber(value, digits = 0) {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return new Intl.NumberFormat(userLocale(), {
    maximumFractionDigits: digits,
    minimumFractionDigits: 0,
  }).format(value);
}

/**
 * Digits that keep a value readable at its magnitude: none from 10 up,
 * one below, two below 1.
 *
 * @param {number} value
 * @returns {number}
 */
function digitsFor(value) {
  const abs = Math.abs(value);
  if (abs === 0 || abs >= 10) {
    return 0;
  }
  return abs >= 1 ? 1 : 2;
}

/**
 * A percentage, ``100`` meaning one core for CPU.
 *
 * @param {number} value
 * @returns {string}
 */
export function formatPercent(value) {
  return `${formatNumber(value, digitsFor(value))}${NBSP}%`;
}

/**
 * Bytes in binary units (MiB, GiB), as the containers' limits are set.
 *
 * @param {number} value
 * @returns {string}
 */
export function formatBytes(value) {
  if (value === null || value === undefined) {
    return "—";
  }
  const units = [[TIB, "TiB"], [GIB, "GiB"], [MIB, "MiB"], [KIB, "KiB"]];
  for (const [size, label] of units) {
    if (Math.abs(value) >= size) {
      const scaled = value / size;
      return `${formatNumber(scaled, scaled < 10 ? 2 : scaled < 100 ? 1 : 0)}${NBSP}${label}`;
    }
  }
  return `${formatNumber(value)}${NBSP}B`;
}

/**
 * Seconds, in milliseconds below one.
 *
 * @param {number} value
 * @returns {string}
 */
export function formatSeconds(value) {
  if (value === null || value === undefined) {
    return "—";
  }
  if (Math.abs(value) < 1) {
    return `${formatNumber(value * 1000, 0)}${NBSP}ms`;
  }
  return `${formatNumber(value, value < 10 ? 2 : 1)}${NBSP}s`;
}

/**
 * Requests per second.
 *
 * @param {number} value
 * @returns {string}
 */
export function formatRate(value) {
  return _t("%s req/s", formatNumber(value, digitsFor(value)));
}

/**
 * A duration, the way a person reads an age: "45 s", "6 min 12 s",
 * "2 h 5 min".
 *
 * @param {number} seconds
 * @returns {string}
 */
export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) {
    return "—";
  }
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) {
    return _t("%s s", s);
  }
  if (s < 3600) {
    return _t("%(m)s min %(s)s s", {m: Math.floor(s / 60), s: s % 60});
  }
  return _t("%(h)s h %(m)s min", {h: Math.floor(s / 3600), m: Math.floor((s % 3600) / 60)});
}

/**
 * A value in the unit a panel declares (``percent``, ``bytes``,
 * ``seconds``, ``rps``); a plain number otherwise.
 *
 * @param {string|null} unit
 * @param {number} value
 * @returns {string}
 */
export function formatValue(unit, value) {
  switch (unit) {
    case "percent":
      return formatPercent(value);
    case "bytes":
      return formatBytes(value);
    case "seconds":
      return formatSeconds(value);
    case "rps":
      return formatRate(value);
    default:
      return formatNumber(value, 2);
  }
}
