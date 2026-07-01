# Dashboard Specification

## Overview
- Target files: `index.html`, `tikhub-report-frontend.html`
- Screenshot reference: `C:/Users/24541/AppData/Local/Temp/codex-clipboard-225b6aea-a18f-456f-82d6-4f4fa196156f.png`
- Interaction model: static data dashboard with asynchronous history refresh

## DOM Structure
- `#dashboardCard.dashboard-card`
  - `.dashboard-head`
    - `.dashboard-title`
    - `.dashboard-stamp`
  - `.dashboard-grid`
    - `.dashboard-kpis`
    - `.dashboard-main`
    - `.dashboard-donut`
    - `.dashboard-area`
    - `.dashboard-area`
    - `.dashboard-bars`
    - `.dashboard-side`

## Visual Tokens
- Board background: white
- Border: light cyan blue
- Primary blue: `#168bd1`
- Deep blue: `#085f9f`
- Cyan: `#10b7ca`
- Pale panel fill: `#f7fcff`
- Text: dark navy
- Grid lines: pale blue

## Data Mapping
- Current cumulative plays: sum of account summary cumulative views.
- Growth rate: compare current total plays with the nearest previous report snapshot.
- Project exposure: current drama count and growth.
- Average play: average view count per drama.
- Main trend chart: last ten report snapshots by total plays.
- Donut chart: current top account share by cumulative plays.
- Area chart 1: drama count trend.
- Area chart 2: average drama views trend.
- Progress bars: top three accounts as percentage of the top account.
- Side cards: account count, drama count, total episode count.

## Responsive Behavior
- Desktop: screenshot-inspired 4-column top, 3-column content grid.
- Tablet: two-column panels.
- Mobile: stacked cards, charts preserve fixed height.

