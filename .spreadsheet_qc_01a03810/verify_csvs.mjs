import fs from "node:fs/promises";
import path from "node:path";
import { Workbook } from "@oai/artifact-tool";

const root = "/home/moses/tenders";
const files = ["karaganda_frezy_30.csv", "karaganda_marlya_30.csv"];
const results = [];

for (const file of files) {
  const csvText = await fs.readFile(path.join(root, file), "utf8");
  const workbook = await Workbook.fromCSV(csvText, { sheetName: "Contacts" });
  const inspection = await workbook.inspect({
    kind: "table",
    sheetId: "Contacts",
    range: "A1:P31",
    include: "values,formulas",
    tableMaxRows: 4,
    tableMaxCols: 16,
    maxChars: 6000,
  });
  const errors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 50 },
    summary: "CSV error scan",
  });
  const preview = await workbook.render({
    sheetName: "Contacts",
    range: "A1:P8",
    scale: 1,
    format: "png",
  });
  await fs.writeFile(
    path.join(root, ".spreadsheet_qc_01a03810", `${file}.png`),
    new Uint8Array(await preview.arrayBuffer()),
  );
  results.push({ file, inspection: inspection.ndjson, errors: errors.ndjson });
}

console.log(JSON.stringify(results, null, 2));
