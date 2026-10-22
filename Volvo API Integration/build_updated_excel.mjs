import fs from "node:fs/promises";
import { FileBlob, Workbook, SpreadsheetFile } from "@oai/artifact-tool";

const refinedHtmlPath = "C:/Users/LENOVO/OneDrive/Documents/Volvo API Integration/fleet_dashboard_refined.html";
const outputDir = "C:/Users/LENOVO/OneDrive/Documents/Fuel Dasboard/outputs/fresh_dhoptala_report";
const outputPath = `${outputDir}/Dhoptala_Fleet_Report_July_2026_FRESH.xlsx`;
await fs.mkdir(outputDir, { recursive: true });
const sourceWorkbookPath = "C:/Users/LENOVO/Downloads/07 Avg Report July 2026 (26).xlsx";

const sourceWorkbook = await SpreadsheetFile.importXlsx(await FileBlob.load(sourceWorkbookPath));
const clean = (value) => String(value ?? "").trim();
const canonicalDoor = (value) => clean(value).toUpperCase().replace(/\([^)]*\)/g, "").replace(/[^A-Z0-9]/g, "");
const sectionType = (title) => ({
  MACHINES: "Excavator", LODER: "Loader", DOZER: "Dozer",
  GRADER: "Grader", DRILL: "Drill", TIPPERS: "Tipper",
})[title];
const extractSections = (rows) => {
  const sections = [];
  for (let index = 0; index < rows.length; index += 1) {
    const heading = clean(rows[index][0]).match(/DAILY DIESEL AVG REPORT \( ([^)]+) \)/i);
    if (!heading) continue;
    const title = heading[1].trim().toUpperCase();
    if (!sectionType(title)) continue;
    const sectionRows = [];
    for (let rowIndex = index + 3; rowIndex < rows.length; rowIndex += 1) {
      if (clean(rows[rowIndex][0]).toUpperCase() === "TOTAL") break;
      if (Number(rows[rowIndex][0]) > 0 && clean(rows[rowIndex][1])) sectionRows.push(rows[rowIndex]);
    }
    sections.push({ title, type: sectionType(title), rows: sectionRows });
  }
  return sections;
};
const sheet26Rows = sourceWorkbook.worksheets.getItem("26").getRange("A1:U220").values;
const metadataByDoor = new Map();
for (const section of extractSections(sheet26Rows)) {
  for (const row of section.rows) {
    metadataByDoor.set(canonicalDoor(row[1]), {
      type: section.type === "Tipper" ? "Tipper" : (clean(row[2]) || section.type),
      make: clean(row[3]) || "Unknown",
      model: clean(row[4]),
    });
  }
}
const latestCounts = new Map();
for (const section of extractSections(sheet26Rows)) {
  for (const row of section.rows) {
    const meta = metadataByDoor.get(canonicalDoor(row[1]));
    const kind = section.type === "Tipper" ? "Tipper" : "Machine";
    const key = `${kind}|${meta.type}|${meta.make}`;
    latestCounts.set(key, (latestCounts.get(key) ?? 0) + 1);
  }
}

const dataRows = [];
for (let day = 1; day <= 26; day += 1) {
  const sheetName = String(day).padStart(2, "0");
  const rows = sourceWorkbook.worksheets.getItem(sheetName).getRange("A1:U220").values;
  for (const section of extractSections(rows)) {
    for (const row of section.rows) {
      const meta = day >= 20
        ? { type: clean(row[2]) || section.type, make: clean(row[3]) || "Unknown", model: clean(row[4]) }
        : (metadataByDoor.get(canonicalDoor(row[1])) ?? { type: section.type, make: "Unknown", model: "" });
      if (section.type === "Tipper") {
        const km = Number(day >= 20 ? row[17] : row[14]) || 0;
        const diesel = Number(day >= 20 ? row[18] : row[15]) || 0;
        const eff = Number(day >= 20 ? row[19] : row[16]) || 0;
        const trips = Number(day >= 20 ? row[20] : row[17]) || 0;
        if (!(eff > 0 && eff < 5 && diesel > 0 && km > 0)) continue;
        dataRows.push([day, "Tipper", "Tipper", meta.make, meta.model, clean(row[1]), eff, diesel, null, km, trips]);
      } else {
        const hmr = Number(day >= 20 ? row[14] : row[11]) || 0;
        const diesel = Number(day >= 20 ? row[15] : row[12]) || 0;
        const eff = Number(day >= 20 ? row[16] : row[13]) || 0;
        if (!(eff > 0 && eff < 200 && diesel > 0 && hmr > 0)) continue;
        dataRows.push([day, "Machine", meta.type, meta.make, meta.model, clean(row[1]), eff, diesel, hmr, null, 0]);
      }
    }
  }
}

const workbook = Workbook.create();
const dashboard = workbook.worksheets.add("Dashboard");
const daily = workbook.worksheets.add("Daily Summary");
const data = workbook.worksheets.add("Data");
const metadata = workbook.worksheets.add("Metadata 20-26");
dashboard.showGridLines = false;
daily.showGridLines = false;
data.showGridLines = false;
metadata.showGridLines = false;

const navy = "#214F86";
const blue = "#31699E";
const pale = "#EAF2F8";
const amber = "#D4A24E";
const teal = "#48B5A0";
const grid = "#D5DEE8";
const text = "#17212B";
const muted = "#5B6B7A";

data.getRange("A1:K1").values = [[
  "Day", "Kind", "Type", "Make", "Model", "Door No", "Efficiency",
  "Diesel (L)", "Run Hrs", "Distance (KM)", "Trips",
]];
data.getRange(`A2:K${dataRows.length + 1}`).values = dataRows;
data.getRange("A1:K1").format = {
  fill: navy,
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
data.getRange(`A2:K${dataRows.length + 1}`).format = {
  font: { color: text },
  borders: { insideHorizontal: { style: "thin", color: "#E6EBF0" } },
};
data.getRange(`A2:A${dataRows.length + 1}`).format.numberFormat = "00";
data.getRange(`G2:G${dataRows.length + 1}`).format.numberFormat = "0.000";
data.getRange(`H2:J${dataRows.length + 1}`).format.numberFormat = "#,##0.0";
data.getRange(`K2:K${dataRows.length + 1}`).format.numberFormat = "#,##0";
data.getRange("A:K").format.font = { name: "Aptos", size: 10 };
data.getRange("A:A").format.columnWidth = 7;
data.getRange("B:C").format.columnWidth = 12;
data.getRange("D:E").format.columnWidth = 15;
data.getRange("F:F").format.columnWidth = 25;
data.getRange("G:K").format.columnWidth = 14;
data.freezePanes.freezeRows(1);
data.tables.add(`A1:K${dataRows.length + 1}`, true, "FleetDataTable");
data.getRange(`A2:A${dataRows.length + 1}`).format.numberFormat = "00";
data.getRange(`G2:G${dataRows.length + 1}`).format.numberFormat = "0.000";
data.getRange(`H2:J${dataRows.length + 1}`).format.numberFormat = "#,##0.0";
data.getRange(`K2:K${dataRows.length + 1}`).format.numberFormat = "#,##0";

const metadataRows = dataRows
  .filter((row) => row[0] >= 20 && row[0] <= 26 && row[1] === "Machine")
  .map((row) => [row[0], row[5], row[2], row[3], row[4], row[6], row[7], row[8]]);
metadata.getRange("A1:H1").values = [[
  "Day", "Door No", "Machine Type", "Make", "Model",
  "Efficiency (L/hr)", "Diesel (L)", "Run Hrs",
]];
metadata.getRange(`A2:H${metadataRows.length + 1}`).values = metadataRows;
metadata.getRange("A1:H1").format = {
  fill: navy, font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center", verticalAlignment: "center", wrapText: true,
};
metadata.getRange(`A2:H${metadataRows.length + 1}`).format = {
  font: { name: "Aptos", size: 10, color: text },
  borders: { insideHorizontal: { style: "thin", color: "#E6EBF0" } },
};
metadata.getRange("A:A").format.columnWidth = 8;
metadata.getRange("B:B").format.columnWidth = 25;
metadata.getRange("C:E").format.columnWidth = 18;
metadata.getRange("F:H").format.columnWidth = 16;
metadata.getRange(`A2:A${metadataRows.length + 1}`).format.numberFormat = "00";
metadata.getRange(`F2:H${metadataRows.length + 1}`).format.numberFormat = "#,##0.00";
metadata.freezePanes.freezeRows(1);
metadata.tables.add(`A1:H${metadataRows.length + 1}`, true, "MachineMetadata2026Table");
metadata.getRange(`A2:A${metadataRows.length + 1}`).format.numberFormat = "00";
metadata.getRange(`F2:H${metadataRows.length + 1}`).format.numberFormat = "#,##0.00";

daily.getRange("A1:L1").values = [[
  "Day", "Excavator Units", "Loader Units", "Dozer Units", "Grader Units", "Drill Units",
  "All Machine Units", "Machine L/hr", "Machine Diesel (L)",
  "Tipper Units", "Tipper km/L", "Tipper Diesel (L)",
]];
daily.getRange("A2:A27").values = Array.from({ length: 26 }, (_, i) => [i + 1]);
const lastDataRow = dataRows.length + 1;
for (let row = 2; row <= 27; row += 1) {
  for (const [column, type] of [["B","Excavator"],["C","Loader"],["D","Dozer"],["E","Grader"],["F","Drill"]]) {
    daily.getRange(`${column}${row}`).formulas = [[`=COUNTIFS('Data'!$A$2:$A$${lastDataRow},A${row},'Data'!$C$2:$C$${lastDataRow},"${type}")`]];
  }
  daily.getRange(`G${row}`).formulas = [[`=SUM(B${row}:F${row})`]];
  daily.getRange(`H${row}`).formulas = [[`=IFERROR(SUMIFS('Data'!$H$2:$H$${lastDataRow},'Data'!$A$2:$A$${lastDataRow},A${row},'Data'!$B$2:$B$${lastDataRow},"Machine")/SUMIFS('Data'!$I$2:$I$${lastDataRow},'Data'!$A$2:$A$${lastDataRow},A${row},'Data'!$B$2:$B$${lastDataRow},"Machine"),0)`]];
  daily.getRange(`I${row}`).formulas = [[`=SUMIFS('Data'!$H$2:$H$${lastDataRow},'Data'!$A$2:$A$${lastDataRow},A${row},'Data'!$B$2:$B$${lastDataRow},"Machine")`]];
  daily.getRange(`J${row}`).formulas = [[`=COUNTIFS('Data'!$A$2:$A$${lastDataRow},A${row},'Data'!$B$2:$B$${lastDataRow},"Tipper")`]];
  daily.getRange(`K${row}`).formulas = [[`=IFERROR(SUMIFS('Data'!$J$2:$J$${lastDataRow},'Data'!$A$2:$A$${lastDataRow},A${row},'Data'!$B$2:$B$${lastDataRow},"Tipper")/SUMIFS('Data'!$H$2:$H$${lastDataRow},'Data'!$A$2:$A$${lastDataRow},A${row},'Data'!$B$2:$B$${lastDataRow},"Tipper"),0)`]];
  daily.getRange(`L${row}`).formulas = [[`=SUMIFS('Data'!$H$2:$H$${lastDataRow},'Data'!$A$2:$A$${lastDataRow},A${row},'Data'!$B$2:$B$${lastDataRow},"Tipper")`]];
}
const machineDoors = new Set(dataRows.filter((r) => r[1] === "Machine").map((r) => r[5])).size;
const tipperDoors = new Set(dataRows.filter((r) => r[1] === "Tipper").map((r) => r[5])).size;
const latestTypeCounts = Object.fromEntries(["Excavator", "Loader", "Dozer", "Grader", "Drill"].map((type) => [
  type, [...latestCounts].filter(([key]) => key.startsWith(`Machine|${type}|`)).reduce((sum, [, count]) => sum + count, 0),
]));
daily.getRange("A28:L28").values = [[
  "SHEET 26 FLEET", latestTypeCounts.Excavator, latestTypeCounts.Loader, latestTypeCounts.Dozer,
  latestTypeCounts.Grader, latestTypeCounts.Drill, null, null, null,
  [...latestCounts].filter(([key]) => key.startsWith("Tipper|")).reduce((sum, [, count]) => sum + count, 0), null, null,
]];
daily.getRange("G28").formulas = [["=SUM(B28:F28)"]];
daily.getRange("H28").formulas = [[`=IFERROR(SUMIFS('Data'!$H$2:$H$${lastDataRow},'Data'!$B$2:$B$${lastDataRow},"Machine")/SUMIFS('Data'!$I$2:$I$${lastDataRow},'Data'!$B$2:$B$${lastDataRow},"Machine"),0)`]];
daily.getRange("I28").formulas = [[`=SUMIFS('Data'!$H$2:$H$${lastDataRow},'Data'!$B$2:$B$${lastDataRow},"Machine")`]];
daily.getRange("K28").formulas = [[`=IFERROR(SUMIFS('Data'!$J$2:$J$${lastDataRow},'Data'!$B$2:$B$${lastDataRow},"Tipper")/SUMIFS('Data'!$H$2:$H$${lastDataRow},'Data'!$B$2:$B$${lastDataRow},"Tipper"),0)`]];
daily.getRange("L28").formulas = [[`=SUMIFS('Data'!$H$2:$H$${lastDataRow},'Data'!$B$2:$B$${lastDataRow},"Tipper")`]];
daily.getRange("A1:L1").format = {
  fill: navy, font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center", verticalAlignment: "center", wrapText: true,
};
daily.getRange("A2:L27").format = {
  borders: { insideHorizontal: { style: "thin", color: "#E6EBF0" } },
};
daily.getRange("A28:L28").format = {
  fill: pale, font: { bold: true, color: navy },
  borders: { top: { style: "double", color: navy } },
};
daily.getRange("A:A").format.numberFormat = "00";
daily.getRange("B:G").format.numberFormat = "#,##0";
daily.getRange("H:H").format.numberFormat = "0.00";
daily.getRange("I:J").format.numberFormat = "#,##0";
daily.getRange("K:K").format.numberFormat = "0.000";
daily.getRange("L:L").format.numberFormat = "#,##0";
daily.getRange("A:L").format.font = { name: "Aptos", size: 10 };
daily.getRange("A:A").format.columnWidth = 15;
daily.getRange("B:F").format.columnWidth = 14;
daily.getRange("G:L").format.columnWidth = 18;
daily.getRange("A1:L1").format.rowHeight = 36;
daily.freezePanes.freezeRows(1);
daily.tables.add("A1:L28", true, "DailySummaryTable");
daily.getRange("A2:A27").format.numberFormat = "00";
daily.getRange("B2:G28").format.numberFormat = "#,##0";
daily.getRange("H2:H28").format.numberFormat = "0.00";
daily.getRange("I2:J28").format.numberFormat = "#,##0.0";
daily.getRange("K2:K28").format.numberFormat = "0.000";
daily.getRange("L2:L28").format.numberFormat = "#,##0.0";

const groupMap = new Map();
for (const row of dataRows) {
  const [day, kind, type, make, model, door] = row;
  const key = `${kind}|${type}|${make}`;
  if (!groupMap.has(key)) groupMap.set(key, { kind, type, make, doors: new Set(), records: 0 });
  const group = groupMap.get(key);
  group.doors.add(door);
  group.records += 1;
}
for (const [key] of latestCounts) {
  const [kind, type, make] = key.split("|");
  if (!groupMap.has(key)) groupMap.set(key, { kind, type, make, doors: new Set(), records: 0 });
}
const groups = [...groupMap.values()]
  .filter((g) => (latestCounts.get(`${g.kind}|${g.type}|${g.make}`) ?? 0) > 0)
  .sort((a, b) => {
    const order = { Excavator: 1, Loader: 2, Dozer: 3, Grader: 4, Drill: 5, Tipper: 6 };
    return (order[a.type] ?? 99) - (order[b.type] ?? 99) || a.make.localeCompare(b.make);
  });

dashboard.getRange("A1:F1").merge();
dashboard.getRange("A1").values = [["CMLL DHOPTALA OB — FLEET DIESEL EFFICIENCY"]];
dashboard.getRange("A1:F1").format = {
  fill: navy, font: { name: "Aptos Display", size: 18, bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center", verticalAlignment: "center",
};
dashboard.getRange("A1:F1").format.rowHeight = 34;
dashboard.getRange("A2:F2").merge();
dashboard.getRange("A2").values = [["Updated through 26 July 2026 · Corrected for the source layout change from Day 20"]];
dashboard.getRange("A2:F2").format = {
  fill: "#DDEBF7", font: { name: "Aptos", size: 10, italic: true, color: navy },
  horizontalAlignment: "center", verticalAlignment: "center",
};
dashboard.getRange("A4:F4").values = [["Type", "Make / OEM", "Unit Count (Sheet 26)", "Weighted Efficiency", "Unit", "Active Records"]];
dashboard.getRange("A4:F4").format = {
  fill: blue, font: { name: "Aptos", bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center", verticalAlignment: "center",
};
dashboard.getRange(`A5:F${groups.length + 4}`).values = groups.map((g) => [
  g.type, g.make, latestCounts.get(`${g.kind}|${g.type}|${g.make}`) ?? 0,
  null, g.kind === "Machine" ? "L/hr" : "km/L", g.records,
]);
for (let index = 0; index < groups.length; index += 1) {
  const row = index + 5;
  const g = groups[index];
  if (g.kind === "Machine") {
    dashboard.getRange(`D${row}`).formulas = [[`=IFERROR(SUMIFS('Data'!$H$2:$H$${lastDataRow},'Data'!$C$2:$C$${lastDataRow},A${row},'Data'!$D$2:$D$${lastDataRow},B${row})/SUMIFS('Data'!$I$2:$I$${lastDataRow},'Data'!$C$2:$C$${lastDataRow},A${row},'Data'!$D$2:$D$${lastDataRow},B${row}),0)`]];
  } else {
    dashboard.getRange(`D${row}`).formulas = [[`=IFERROR(SUMIFS('Data'!$J$2:$J$${lastDataRow},'Data'!$D$2:$D$${lastDataRow},B${row},'Data'!$B$2:$B$${lastDataRow},"Tipper")/SUMIFS('Data'!$H$2:$H$${lastDataRow},'Data'!$D$2:$D$${lastDataRow},B${row},'Data'!$B$2:$B$${lastDataRow},"Tipper"),0)`]];
  }
}
const dashboardEnd = groups.length + 4;
dashboard.getRange(`A5:F${dashboardEnd}`).format = {
  font: { name: "Aptos", size: 10, color: text },
  borders: { insideHorizontal: { style: "thin", color: grid } },
};
dashboard.getRange(`C5:C${dashboardEnd}`).format.numberFormat = "#,##0";
dashboard.getRange(`D5:D${dashboardEnd}`).format.numberFormat = "0.000";
dashboard.getRange(`F5:F${dashboardEnd}`).format.numberFormat = "#,##0";
dashboard.getRange(`D5:D${dashboardEnd}`).conditionalFormats.add("dataBar", { color: "#7FA9D1", gradient: true });
dashboard.getRange(`A${dashboardEnd + 2}:F${dashboardEnd + 2}`).merge();
dashboard.getRange(`A${dashboardEnd + 2}`).values = [[
  "Data quality: zero-activity rows and implausible efficiencies are excluded. Source workbook formula errors are not propagated.",
]];
dashboard.getRange(`A${dashboardEnd + 2}:F${dashboardEnd + 2}`).format = {
  fill: "#FFF4D6", font: { name: "Aptos", size: 9, italic: true, color: "#7A5A16" },
  wrapText: true, verticalAlignment: "center",
};
dashboard.getRange(`A${dashboardEnd + 2}:F${dashboardEnd + 2}`).format.rowHeight = 30;
dashboard.getRange("A:A").format.columnWidth = 17;
dashboard.getRange("B:B").format.columnWidth = 18;
dashboard.getRange("C:C").format.columnWidth = 19;
dashboard.getRange("D:D").format.columnWidth = 20;
dashboard.getRange("E:E").format.columnWidth = 11;
dashboard.getRange("F:F").format.columnWidth = 16;
dashboard.freezePanes.freezeRows(4);
dashboard.tables.add(`A4:F${dashboardEnd}`, true, "FleetDashboardTable");
dashboard.getRange(`C5:C${dashboardEnd}`).format.numberFormat = "#,##0";
dashboard.getRange(`D5:D${dashboardEnd}`).format.numberFormat = "0.000";
dashboard.getRange(`F5:F${dashboardEnd}`).format.numberFormat = "#,##0";

const machineChart = dashboard.charts.add("line", { title: "Machine Fuel Rate by Day (L/hr)", hasLegend: false });
const machineSeries = machineChart.series.add("Machine L/hr");
machineSeries.categoryFormula = `'Daily Summary'!$A$2:$A$27`;
machineSeries.formula = `'Daily Summary'!$H$2:$H$27`;
machineSeries.fill = amber;
machineChart.xAxis = { axisType: "textAxis", title: { text: "July day" } };
machineChart.yAxis = { numberFormatCode: "0.0", title: { text: "L/hr" } };
machineChart.setPosition(`A${dashboardEnd + 4}`, `H${dashboardEnd + 19}`);

const tipperChart = dashboard.charts.add("line", { title: "Tipper Fuel Efficiency by Day (km/L)", hasLegend: false });
const tipperSeries = tipperChart.series.add("Tipper km/L");
tipperSeries.categoryFormula = `'Daily Summary'!$A$2:$A$27`;
tipperSeries.formula = `'Daily Summary'!$K$2:$K$27`;
tipperSeries.fill = teal;
tipperChart.xAxis = { axisType: "textAxis", title: { text: "July day" } };
tipperChart.yAxis = { numberFormatCode: "0.000", title: { text: "km/L" } };
tipperChart.setPosition(`I${dashboardEnd + 4}`, `P${dashboardEnd + 19}`);

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);

const inspectDashboard = await workbook.inspect({
  kind: "table,drawing",
  sheetId: "Dashboard",
  range: `A1:P${dashboardEnd + 20}`,
  include: "values,formulas",
  tableMaxRows: 40,
  tableMaxCols: 16,
  maxChars: 16000,
});
const inspectDaily = await workbook.inspect({
  kind: "table",
  sheetId: "Daily Summary",
  range: "A1:L28",
  include: "values,formulas",
  tableMaxRows: 30,
  tableMaxCols: 12,
  maxChars: 16000,
});
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
  maxChars: 5000,
});
await fs.writeFile(`${outputDir}/excel_verification.ndjson`,
  `${inspectDashboard.ndjson}\n${inspectDaily.ndjson}\n${errors.ndjson}`, "utf8");

for (const [name, range] of [
  ["Dashboard", `A1:P${dashboardEnd + 20}`],
  ["Daily Summary", "A1:L28"],
  ["Data", "A1:K40"],
  ["Metadata 20-26", `A1:H${Math.min(metadataRows.length + 1, 45)}`],
]) {
  const preview = await workbook.render({ sheetName: name, range, scale: 1, format: "png" });
  await fs.writeFile(`${outputDir}/${name.replaceAll(" ", "_")}.png`, new Uint8Array(await preview.arrayBuffer()));
}

console.log(JSON.stringify({
  outputPath,
  dataRows: dataRows.length,
  dashboardGroups: groups.length,
  machineDoors,
  tipperDoors,
  metadataRows: metadataRows.length,
  latestCounts: Object.fromEntries(latestCounts),
  dashboardRange: `A1:P${dashboardEnd + 20}`,
  errors: errors.ndjson,
}, null, 2));
