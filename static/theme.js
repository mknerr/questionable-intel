/* Questionable Intel: shared squad guidons and phonetic helpers (no build step). */

// Okabe-Ito colors stay colorblind-safe; each squad also gets its own charge.
const SQUAD_COLORS = ["#0072B2", "#E69F00", "#009E73", "#D55E00", "#CC79A7", "#56B4E9", "#F0E442", "#8B5A2B"];
const SQUAD_INKS = ["#FFFFFF", "#1B1E13", "#1B1E13", "#FFFFFF", "#1B1E13", "#1B1E13", "#1B1E13", "#FFFFFF"];
const SQUAD_CHARGES = [
  "M24 12L35 26L24 40L13 26Z",
  "M24 14a11 11 0 1 1 0 22a11 11 0 1 1 0-22Z",
  "M24 13L36 36H12Z",
  "M14 15H34V35H14Z",
  "M24 11l4 9.6 10.4.8-7.9 6.8 2.4 10.1L24 32.9l-8.9 5.4 2.4-10.1-7.9-6.8 10.4-.8Z",
  "M20 12h8v9h9v8h-9v10h-8V29h-9v-8h9Z",
  "M30 13a13 13 0 1 0 0 24a10.5 10.5 0 1 1 0-24Z",
  "M24 14L38 28V38L24 24L10 38V28Z",
];
const PHONETIC = {
  A: "ALPHA", B: "BRAVO", C: "CHARLIE", D: "DELTA", E: "ECHO", F: "FOXTROT", G: "GOLF", H: "HOTEL",
  I: "INDIA", J: "JULIET", K: "KILO", L: "LIMA", M: "MIKE", N: "NOVEMBER", O: "OSCAR", P: "PAPA",
  Q: "QUEBEC", R: "ROMEO", S: "SIERRA", T: "TANGO", U: "UNIFORM", V: "VICTOR", W: "WHISKEY",
  X: "X-RAY", Y: "YANKEE", Z: "ZULU", 0: "ZERO", 1: "ONE", 2: "TWO", 3: "THREE", 4: "FOUR",
  5: "FIVE", 6: "SIX", 7: "SEVEN", 8: "EIGHT", 9: "NINE",
};

function squadIndex(i){ return (i || 0) % SQUAD_COLORS.length; }
function squadColor(i){ return SQUAD_COLORS[squadIndex(i)]; }
function squadInk(i){ return SQUAD_INKS[squadIndex(i)]; }
function squadCharge(i){ return SQUAD_CHARGES[squadIndex(i)]; }

function guidon(i, width){
  const w = width || 64;
  return `<svg class="guidon" width="${w}" height="${Math.round(w * 56 / 64)}" viewBox="0 0 64 56" aria-hidden="true">
    <rect x="3" y="2" width="4" height="52" fill="#E6DEC2"></rect>
    <path d="M7 8H61L49 26L61 44H7Z" fill="${squadColor(i)}" stroke="#F4F0E0" stroke-width="2"></path>
    <path d="${squadCharge(i)}" fill="${squadInk(i)}"></path>
  </svg>`;
}

function optionLetter(i){ return "ABCDEFGH"[i] || String(i + 1); }
function optionWord(i){ return PHONETIC[optionLetter(i)] || ""; }
function phoneticCode(code){
  return String(code || "").toUpperCase().split("").map(c => PHONETIC[c] || c).join(" · ");
}

// The API reports time left but not the clock's length, so track the longest
// value seen for this question; +30s restarts the bar from full.
let clockQuestion = null, clockLongest = 0;
function clockPercent(s){
  const qid = s.question ? s.question.id : null;
  const left = s.clock_remaining || 0;
  if (qid !== clockQuestion || left > clockLongest) { clockQuestion = qid; clockLongest = left; }
  return clockLongest ? Math.round(left / clockLongest * 100) : 0;
}
