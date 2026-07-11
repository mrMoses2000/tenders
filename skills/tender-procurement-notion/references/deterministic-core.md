# Deterministic Core And Extractor

When `/Users/mosesvasilenko/tenders/src/tender_agent` is available, use it for arithmetic and repeatable transformations before asking the model to reason about ambiguous evidence.

Suitable tasks:

- normalize units and package-aware prices;
- group likely duplicate supplier identities;
- compare structured hard requirements;
- calculate forecast landed cost and margin;
- allocate a shared delivery cost exactly to tiyn;
- produce a stable transport-neutral offer payload;
- extract JSON-LD/OpenGraph signals from a public HTML page or fixture.
- route Kaspi, OLX, 2GIS, Instagram and generic sources through extractor/browser/contact steps;
- compare local and intercity scenarios by full and risk-adjusted cost;
- summarize like-for-like route history;
- calculate an offline market trip break-even point and route sheet.

Run all checks:

```bash
cd /Users/mosesvasilenko/tenders
./scripts/run_checks.sh
```

The extractor is not a proof of supplier confirmation. `availability_signal=in_stock` is only a public-page signal and `stock_confirmed` remains `false` until direct evidence is obtained.

Do not use the extractor to bypass login, CAPTCHA, anti-bot controls, robots restrictions, or access controls. Use Chrome only when visible dynamic state is genuinely necessary and Computer Use only when connector, HTTP extraction and browser-semantic tools are insufficient.

`pack_notion_offer` does not write to Notion. Before any real upsert, fetch the current schema, resolve relations, preserve source request fields, and apply the rules in `notion-upsert.md`.

Risk reserve percentages and market probabilities are explicit inputs, not hidden model facts. A market price range remains a hypothesis until tied to a real supplier confirmation or purchase.
