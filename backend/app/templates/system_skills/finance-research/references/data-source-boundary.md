# Finance Data Source Boundary

Use public live providers first when configured and reachable. Use static fallback
only as a continuity layer, and label it as fallback data in user-facing reports.

Paid sources are tenant-scoped tool configuration:

- FMP, Polygon, EODHD for listed market data.
- Tushare, Wind, iFinD, Choice for China market data.
- Qichacha and Tianyancha for China registry/KYC enrichment.
- Crunchbase, PitchBook, Capital IQ for private-market data.

Do not inspect process environment variables for provider credentials.
