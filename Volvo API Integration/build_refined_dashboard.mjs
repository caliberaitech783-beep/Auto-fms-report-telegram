import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const sourceHtml = "C:/Users/LENOVO/OneDrive/Documents/Volvo API Integration/outputs/dhoptala_fleet_reports/dhoptala_fleet_dashboard.html";
const sourceXlsx = "C:/Users/LENOVO/Downloads/07 Avg Report July 2026 (26).xlsx";
const outputHtml = "C:/Users/LENOVO/OneDrive/Documents/Fuel Dasboard/outputs/fresh_dhoptala_report/Dhoptala_Fleet_Report_July_2026.html";

const html = await fs.readFile(sourceHtml, "utf8");
await fs.mkdir("C:/Users/LENOVO/OneDrive/Documents/Fuel Dasboard/outputs/fresh_dhoptala_report", { recursive: true });
const dataMatch = html.match(/<script>const DATA=(\{[\s\S]*?\});<\/script>/);
if (!dataMatch) throw new Error("Embedded dashboard data was not found.");
const data = JSON.parse(dataMatch[1]);

const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(sourceXlsx));
const num = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
const clean = (value) => String(value ?? "").trim();
const round = (value, digits = 3) => Number(num(value).toFixed(digits));

function normalizeTipperMake(door, make, model) {
  const token = `${door} ${model}`.toUpperCase();
  if (token.includes("LIUGONG")) return "Liugong";
  if (token.includes("SANY")) return "Sany";
  if (token.includes("FMX")) return "Volvo";
  if (token.includes("G440") || /^S[-\d]/.test(clean(door).toUpperCase())) return "Scania";
  return clean(make) || "Unknown";
}

function rowsBetween(values, titleText, nextTitleText) {
  const start = values.findIndex((row) => clean(row[0]).includes(titleText));
  if (start < 0) return [];
  const firstData = start + 3;
  let end = values.length;
  if (nextTitleText) {
    const found = values.findIndex((row, index) => index > start && clean(row[0]).includes(nextTitleText));
    if (found >= 0) end = found;
  }
  const total = values.findIndex((row, index) => index >= firstData && index < end && clean(row[0]).toUpperCase() === "TOTAL");
  return values.slice(firstData, total >= 0 ? total : end);
}

for (let day = 20; day <= 26; day += 1) {
  const sheet = workbook.worksheets.getItem(String(day).padStart(2, "0"));
  const values = sheet.getRange("A1:AA154").values;

  const machineRows = rowsBetween(values, "MACHINES", "LODER")
    .filter((row) => num(row[0]) > 0 && clean(row[1]))
    .map((row) => ({
      door: clean(row[1]),
      make: clean(row[3]) || "Unknown",
      model: clean(row[4]),
      type: clean(row[2]) || "Machine",
      eff: round(row[16]),
      diesel: round(row[15], 2),
      trips: Math.round(num(row[17])),
      hmr: round(row[14], 2),
    }))
    .filter((row) => row.diesel > 0 && row.hmr > 0 && row.eff > 0 && row.eff < 200);

  const tipperRows = rowsBetween(values, "TIPPERS", null)
    .filter((row) => num(row[0]) > 0 && clean(row[1]))
    .map((row) => ({
      door: clean(row[1]),
      make: normalizeTipperMake(row[1], row[3], row[4]),
      model: clean(row[4]),
      type: "Tipper",
      eff: round(row[19]),
      diesel: round(row[18], 2),
      trips: Math.round(num(row[20])),
      km: round(row[17], 2),
    }))
    .filter((row) => row.diesel > 0 && row.km > 0 && row.eff > 0 && row.eff < 5);

  data.detail[String(day)] = { MACHINE: machineRows, TIPPER: tipperRows };
}

data.days = Array.from({ length: 26 }, (_, index) => index + 1);
data.fleet = {};
for (const day of data.days) {
  const detail = data.detail[String(day)] ?? { MACHINE: [], TIPPER: [] };
  const machines = detail.MACHINE.filter((row) => row.diesel > 0 && row.hmr > 0 && row.eff > 0 && row.eff < 200);
  const tippers = detail.TIPPER.filter((row) => row.diesel > 0 && row.km > 0 && row.eff > 0 && row.eff < 5);
  const mFuel = machines.reduce((sum, row) => sum + num(row.diesel), 0);
  const mHmr = machines.reduce((sum, row) => sum + num(row.hmr), 0);
  const tFuel = tippers.reduce((sum, row) => sum + num(row.diesel), 0);
  const tKm = tippers.reduce((sum, row) => sum + num(row.km), 0);
  data.fleet[String(day)] = {
    m_fuel: round(mFuel, 2),
    m_hmr: round(mHmr, 2),
    m_eff: round(mHmr ? mFuel / mHmr : 0),
    m_units: new Set(machines.map((row) => row.door)).size,
    t_fuel: round(tFuel, 2),
    t_km: round(tKm, 2),
    t_eff: round(tFuel ? tKm / tFuel : 0),
    t_units: new Set(tippers.map((row) => row.door)).size,
    t_trips: tippers.reduce((sum, row) => sum + num(row.trips), 0),
  };
}

const hierarchy = { Tippers: {}, Machines: {} };
const filtered = {};
for (const day of data.days) {
  const detail = data.detail[String(day)] ?? { MACHINE: [], TIPPER: [] };
  for (const row of detail.MACHINE) {
    hierarchy.Machines[row.type] ??= {};
    hierarchy.Machines[row.type][row.make] ??= [];
    if (row.model && !hierarchy.Machines[row.type][row.make].includes(row.model)) hierarchy.Machines[row.type][row.make].push(row.model);
    const key = `Machines|${row.type}|${row.make}`;
    filtered[key] ??= [];
    if (!filtered[key].includes(row.door)) filtered[key].push(row.door);
  }
  for (const row of detail.TIPPER) {
    hierarchy.Tippers[row.make] ??= [];
    if (row.model && !hierarchy.Tippers[row.make].includes(row.model)) hierarchy.Tippers[row.make].push(row.model);
    const key = `Tippers|${row.make}`;
    filtered[key] ??= [];
    if (!filtered[key].includes(row.door)) filtered[key].push(row.door);
  }
}
data.hierarchy = hierarchy;
data.filtered = filtered;
data.quality = {
  sourceSheets: 26,
  correctedLayoutFromDay: 20,
  sourceFormulaErrors: 14,
  excludedRule: "Zero/invalid activity and implausible efficiency rows are excluded.",
};

let refined = html.replace(dataMatch[0], `<script>const DATA=${JSON.stringify(data)};</script>`);
refined = refined.replaceAll("CMLL SASTI OB", "CMLL DHOPTALA OB");
refined = refined
  .replace("Days 01–24", "Days 01–26")
  .replace(
    '<div class="kpis" id="kpis"></div>',
    `<div class="quality" role="status">
      <span class="quality-dot"></span>
      <strong>Source-aware validation enabled</strong>
      <span>26 daily sheets loaded · Day 20+ layout shift corrected · 14 source formula errors isolated</span>
    </div>
    <div class="kpis" id="kpis"></div>`,
  )
  .replace(
    "</style>",
    `.quality{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:-4px 0 14px;padding:9px 12px;
  border:1px solid rgba(72,181,160,.35);background:rgba(72,181,160,.08);border-radius:8px;
  color:var(--mut);font-family:var(--mono);font-size:9px}
.quality strong{color:var(--teal);font-size:10px}.quality-dot{width:7px;height:7px;border-radius:50%;
  background:var(--teal);box-shadow:0 0 0 4px rgba(72,181,160,.12)}
@media(max-width:480px){.quality{font-size:8px}.quality strong{width:calc(100% - 18px)}}
</style>`,
  );

refined = refined.replace(
  "let mf=0,mh=0,mct=0,tf=0,tk=0,tct=0,tr=0;",
  "let mf=0,mh=0,tf=0,tk=0,tr=0;const machineUnits=new Set(),tipperUnits=new Set();",
);
refined = refined
  .replace("mf+=r.diesel||0;mh+=r.hmr||0;mct++;", "mf+=r.diesel||0;mh+=r.hmr||0;machineUnits.add(r.door);")
  .replace("tf+=r.diesel||0;tk+=r.km||0;tr+=r.trips||0;tct++;", "tf+=r.diesel||0;tk+=r.km||0;tr+=r.trips||0;tipperUnits.add(r.door);")
  .replace(
    "return {mEff:mh?mf/mh:0,tEff:tf?tk/tf:0,mDsl:mf,tDsl:tf,trips:tr,mct,tct};",
    "return {mEff:mh?mf/mh:0,tEff:tf?tk/tf:0,mDsl:mf,tDsl:tf,trips:tr,mct:machineUnits.size,tct:tipperUnits.size};",
  );

refined = refined.replace(
  "function drawCharts(){",
  `function dailyFiltered(day,kind){
  const dd=DATA.detail[String(day)]||{MACHINE:[],TIPPER:[]};
  let rows=(dd[kind]||[]).filter(r=>r.eff>0);
  if(selectedFilter){
    const p=selectedFilter.split('|');
    if(kind==='MACHINE'){
      if(p[0]!=='Machines')return 0;
      rows=rows.filter(r=>r.type===p[1]&&r.make===p[2]);
    }else{
      if(p[0]!=='Tippers')return 0;
      rows=rows.filter(r=>r.make===p[1]);
    }
  }
  const fuel=rows.reduce((s,r)=>s+(r.diesel||0),0);
  const run=rows.reduce((s,r)=>s+(kind==='MACHINE'?(r.hmr||0):(r.km||0)),0);
  return kind==='MACHINE'?(run?fuel/run:0):(fuel?run/fuel:0);
}

function drawCharts(){`,
);
refined = refined
  .replace("const mData=days.map(d=>DATA.fleet[d].m_eff);", "const mData=days.map(d=>dailyFiltered(d,'MACHINE'));")
  .replace("const tData=days.map(d=>DATA.fleet[d].t_eff);", "const tData=days.map(d=>dailyFiltered(d,'TIPPER'));")
  .replace(
    "const arrow=st.k===c[0]?(st.dir<0?' ▾':' ▴'):'';\n    h+='<th class=\"'+(c[2]?'num':'')+'\" data-k=\"'+c[0]+'\">'+c[1]+'</th>';",
    "const arrow=st.k===c[0]?(st.dir<0?' ▾':' ▴'):'';\n    h+='<th scope=\"col\" tabindex=\"0\" class=\"'+(c[2]?'num':'')+'\" data-k=\"'+c[0]+'\">'+c[1]+arrow+'</th>';",
  )
  .replace(
    "tbl.querySelectorAll('th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;",
    "tbl.querySelectorAll('th').forEach(th=>{const sort=()=>{const k=th.dataset.k;",
  )
  .replace(
    "if(st.k===k)st.dir*=-1;else{st.k=k;st.dir=k==='door'||k==='make'||k==='model'?1:-1;}renderTable(kind);});",
    "if(st.k===k)st.dir*=-1;else{st.k=k;st.dir=k==='door'||k==='make'||k==='model'?1:-1;}renderTable(kind);};th.onclick=sort;th.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();sort();}};});",
  );

const inlineScripts = [...refined.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map((match) => match[1])
  .filter((script) => script.trim());
for (const script of inlineScripts) new Function(script);

await fs.writeFile(outputHtml, refined, "utf8");
console.log(JSON.stringify({
  outputHtml,
  days: data.days.length,
  day20: data.fleet["20"],
  day24: data.fleet["24"],
  day26: data.fleet["26"],
  machineFilters: Object.keys(data.hierarchy.Machines).length,
  tipperMakes: Object.keys(data.hierarchy.Tippers).length,
}, null, 2));
