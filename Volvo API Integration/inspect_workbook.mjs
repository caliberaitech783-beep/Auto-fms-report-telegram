import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const inputPath = "C:/Users/LENOVO/Downloads/fleet_dashboard.xlsx";
const outputDir = "C:/Users/LENOVO/OneDrive/Documents/Volvo API Integration/supporting_workbook_review";
await fs.mkdir(outputDir, { recursive: true });

const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(inputPath));
const overview = await workbook.inspect({
  kind: "workbook,sheet,table",
  maxChars: 12000,
  tableMaxRows: 10,
  tableMaxCols: 16,
  tableMaxCellChars: 120,
});
await fs.writeFile(`${outputDir}/overview.ndjson`, overview.ndjson, "utf8");

const sheets = await workbook.inspect({ kind: "sheet", include: "id,name", maxChars: 12000 });
await fs.writeFile(`${outputDir}/sheets.ndjson`, sheets.ndjson, "utf8");

const errorScan = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "formula error scan",
  maxChars: 12000,
});
await fs.writeFile(`${outputDir}/errors.ndjson`, errorScan.ndjson, "utf8");

const sheetList = sheets.ndjson.split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line)).filter((item) => item.kind === "sheet");
for (const item of sheetList) {
  const safeName = item.name.replace(/[<>:"/\\|?*]/g, "_");
  const focused = await workbook.inspect({
    kind: "table,drawing,computedStyle",
    sheetId: item.name,
    range: item.range,
    include: "values,formulas",
    tableMaxRows: 40,
    tableMaxCols: 20,
    tableMaxCellChars: 100,
    maxChars: 30000,
  });
  await fs.writeFile(`${outputDir}/${safeName}.ndjson`, focused.ndjson, "utf8");
  const preview = await workbook.render({ sheetName: item.name, autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(`${outputDir}/${safeName}.png`, new Uint8Array(await preview.arrayBuffer()));
}

console.log(overview.ndjson);
console.log(sheets.ndjson);
console.log(errorScan.ndjson);
