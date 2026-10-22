import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const sourcePath = "C:/Users/LENOVO/Downloads/07 Avg Report July 2026 (26).xlsx";
const outputPath = "C:/Users/LENOVO/OneDrive/Documents/Fuel Dasboard/outputs/fresh_dhoptala_report/source_asset_inspection.txt";
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(sourcePath));
const clean = (value) => String(value ?? "").trim();
const chunks = [];
for (const name of ["01", "19", "20", "26"]) {
  const values = workbook.worksheets.getItem(name).getRange("A1:U220").values;
  chunks.push(`===== SHEET ${name} =====`);
  for (let index = 0; index < values.length; index += 1) {
    const row = values[index];
    if (String(row[0] ?? "").includes("DAILY DIESEL AVG REPORT")) {
      chunks.push(`${index + 1}: ${JSON.stringify(row.slice(0, 21))}`);
      for (let offset = 1; offset <= 4; offset += 1) {
        chunks.push(`${index + offset + 1}: ${JSON.stringify(values[index + offset]?.slice(0, 21))}`);
      }
    }
  }
}
await fs.writeFile(outputPath, chunks.join("\n"), "utf8");
const outputWorkbook = await SpreadsheetFile.importXlsx(await FileBlob.load(
  "C:/Users/LENOVO/OneDrive/Documents/Fuel Dasboard/outputs/fresh_dhoptala_report/Dhoptala_Fleet_Report_July_2026.xlsx",
));
const outputRows = outputWorkbook.worksheets.getItem("Data").getUsedRange(true).values.slice(1);
const missing = outputRows.filter((row) => Number(row[0]) <= 19 && !clean(row[4]));
console.log(JSON.stringify({
  rowsDays1to19: outputRows.filter((row) => Number(row[0]) <= 19).length,
  missingModelDays1to19: missing.length,
  missingExamples: missing.slice(0, 30).map((row) => [row[0], row[2], row[5]]),
}, null, 2));
console.log(outputPath);
