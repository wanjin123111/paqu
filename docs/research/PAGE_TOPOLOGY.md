# Page Topology

## Target
Use the supplied dashboard screenshot as the visual reference for the public report landing page. The app is a static HTML + Python backend project, so the clone is implemented inside `index.html` and `tikhub-report-frontend.html` instead of a Next.js component tree.

## Sections
1. Sticky top bar
2. Public data dashboard
3. Report table with account summary and account detail tabs
4. History popover

## Dashboard Interaction Model
Static, data-driven. The dashboard renders immediately after the latest report loads, then updates again when historical report metadata and JSON payloads are loaded.

## Reference Visual Notes
- White rounded board with a thin cyan outline and a strong blue bottom rule.
- Large blue title line.
- Four compact KPI tiles across the top.
- Large line chart in the middle left.
- Donut share chart on the right.
- Three smaller bottom panels: area chart, area chart, progress bars.
- Three stacked blue stat cards on the lower right.

